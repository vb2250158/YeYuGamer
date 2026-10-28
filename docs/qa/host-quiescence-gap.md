# Host quiescence 缺口独立取证

## 结论与范围

2026-10-28，只读检查源码与独立测试；仅新增本报告。基线 HEAD：`4361c935d8168625e04ca4067b1e8d0d498acb9e`，结论包含当前未提交 protocol/review lease 候选，而非声称这些改动已提交。未运行测试、启动进程、调用业务 API、读取数据库或修改实现。

**当前候选不能安全兑现三个红用例的“必然继续下一游戏”。** 可以在本 QA 分支继续实现有证据的恢复路径，但不能仅修改 Manager 超时分支、调用 `poll()` 或清空 Host owner 就完成安全验收。对于已经失去追踪的旧进程，后补实现不能凭空恢复历史 OS 所有权证据；证据不足必须保留 reconciliation。三个红项是既有未完成范围，不作为新增 P1 重复上报。

用户提供的独立回放结果是 12 项中 9 pass / 3 fail；本次未独立重跑，不把源码分析写成实测。测试文件 `.cache/independent/test_queue_timeout_independent.py` 本次 SHA256：`D978E495FCEBE18DB799BD1639C05C055BD6FB79F25B68266D778C01B29B64F6`。

以下行号均为本次读取时的工作树行号；父方并行修改后需重定位。

## 三个红测试实际证明什么

文件：`D:\Game\Automation\_qa\queue-timeout\.cache\independent\test_queue_timeout_independent.py`。

| 测试入口 | 实际注入 | 当前安全行为与缺口 |
| --- | --- | --- |
| 263：`test_dead_owner_without_callback_cannot_park_the_queue` | 264 调用 `missing_callback=True`；185 真实 prepare 后不调用 callback，也不记录 terminal（191 分支跳过）；没有创建真实 OS Host | 是缺失 callback/悬留 starting attempt 的模型，不是 OS 已死亡证明。Manager 应保留未知状态；若要恢复，必须取得“从未 dispatch”的可靠收据或真实静默证据。 |
| 267：`test_simulated_hung_owner_cannot_block_the_next_game` | 203 注入伪 PID 和 `poll() -> None`，206 mock cancel 成功，207 写 running | 无真实 watchdog、Job 或取消确认；cancel 返回 True 仅模拟请求接受，不证明停机。 |
| 271：`test_simulated_killed_owner_cannot_block_the_next_game` | 同上，但 203 的 `poll() -> 0` | 仅根进程退出模型，没有子输入执行器退出、Job 清空或 callback 交接证据。 |

223 mock 的是 `_start_game_run`，224 仍运行真实 `_run_batch`。229 要求两个成员均启动，245 要求后继仅有一个 completed Attempt；这些断言是期望，不是静默证明。不能为了变绿给上述 fake process 自动生成成功 quiescence receipt。应保留证据不足的负向测试，再另加携带有效收据的正向继续测试。

## 逐调用链证据与最小修法

### 1. Dispatch 与取消入口

文件：`D:\Game\Automation\_qa\queue-timeout\backend\yeyu_gamer_manager\services\adapter_host.py`。

- 2153 保存 `_active_process/_active_plan`；2159 向 Host 写 request；2172 创建 daemon watcher。准确名称是 `_watch_execution`（1757）→ `_watch_execution_bound`（1783），本文件没有 `_watch_process`。
- 1630 `_send_cancel_locked`：1637 核对当前 plan、process 存在且未退出；1645 写 durable cancel，1654 写 stdin；1661 `cancel` 再核对 attempt/token。**发送成功不等于执行停止**；且同步 write/flush 没有此处可见的超时，Manager 外层 callback wait 不能约束卡在 cancel 调用内的时间。
- 最小修法：为固定 Attempt 增加 dispatch 阶段收据（尚未启动 / 已启动及原生进程身份），并让取消请求非阻塞或有可核验上界。不得把没有内存 owner 当作“从未启动”；进程创建与收据持久化的崩溃窗口必须 fail-closed 或由 OS containment 接管。

### 2. Watcher 停机与 callback 交接

同文件：1917 起以 execution deadline 观察 Host；1935 起发送取消；1950 等 grace；1953 `terminate()`、1957 `kill()` 只针对固定 Host；1958 最后 `wait()` 没有显式 timeout。1966/1967 对 stdout/stderr reader 仅 bounded join，随后 2024 起关闭 pipe；reader 未退出时不能把 join 返回当作 drain 完成。

2038 调 `on_complete(result)`，2040 捕获异常且不二次调用；成功返回才在 2047 清 attempt controls。2050 的 finally 则按同一个 process 对象清内存 owner，即使 callback 抛异常也会清；这不是持久 callback commit 收据，更不是整个输入执行器集合的静默收据。

最小修法：把以下事实分开记录：Host 根退出、输入执行器 containment 已空、reader/event producer 已停止、结果已提交、coordinator 已收到交接。给 stop/drain 明确期限，未完成保留 owner/recovery 材料。callback 不可并发重放；应基于 Attempt + lease ID/generation 做幂等提交/接管，先封住迟到写入，再允许后继。不能强行杀 Python callback 线程；卡住的 callback 必须靠协作阶段检查或隔离边界保证不会在恢复后继续操作客户端。

### 3. Manager callback deadline

文件：`D:\Game\Automation\_qa\queue-timeout\backend\yeyu_gamer_manager\services\manager.py`。

- 9202 coordinator callback 只检查 Run ID，9205 保存结果并 set Event。
- 9211 等 `plan.timeout_seconds + callback_overhead_seconds`，9216 发 cancel，9221 写 cancelling，9227 再等 grace。
- 9229 无结果且 Attempt 仍 starting/running/cancelling 时，9240 fence，9262 返回；**不会落入 terminal review parking**。这是三红仍红的直接控制流，不是新增 review helper 的错误。
- 8146 finished 包装 `_finished`；8174 先执行 cleanup，8209 提交结果；8252 finally 才通知 coordinator。cleanup/持久化卡住或部分提交均不能靠 root exit 代替交接。

最小修法：deadline 后调用正式、按固定 Attempt 限定的 recovery 入口，返回 `quiescent + commit/handoff`、`still_owned`、`unknown` 等结构化结果。仅第一种可原子核对同 Run/Attempt/membership、无新 lease/owner、未 sealed/取消后终态化旧 Attempt 为未完成并继续；其余保留 fence。提交与迟到 callback 必须共享恢复代次/所有权检查，不能仅调用现有 terminal review helper 或裸 `continue`。OS 等待不持 Store 事务锁，最终 CAS 提交才进入短事务。

### 4. C# Job 为什么不构成当前充分证据

文件：`D:\Game\Automation\_qa\queue-timeout\adapter-host\YeYuGamerAdapterHost.cs`。

- 185 `runner.Start()` 后才在 186 建 Job、187 Assign：存在 runner 在分配前运行并派生子进程的窗口；190 才写 request 不等于启动代码绝不派生。
- 279/283 在强制终止路径 Kill runner 根；288 bounded WaitForExit。332 发 terminal，343 才 Close Job：terminal 本身早于最终 Job cleanup。
- 489 `CreateKillOnCloseJob`；495 同时设置 KILL_ON_JOB_CLOSE 与 BREAKAWAY_OK。固定 Host 被杀、其最后 Job handle 关闭，可以约束仍在该 Job 内的成员；**无法覆盖已 breakaway 或分配前逃出的进程，也没有在此处等待完整输入集合归零的收据**。不得为消除此缺口直接取消游戏合法脱离机制或杀任意后代树。
- 375 的 `JobOwnershipObservation.TryCreate` 是环境开关控制的可选记录；408 查询 PID 列表，419 起遇到无法 Open/核验的成员会跳过；434 快照只记录成功取得身份的成员，342 调用 Stop、456 定义清理删除记录。它是采样观察，不是完整、不可遗漏、持久的零存活证明，且 Host 死后无法再使用其私有 Job handle。

最小设计方向：在产生任何自动化子进程前完成 containment（例如受控 suspended create + Assign + resume，具体兼容需在本机验证）；由可信 supervisor/Manager 保有可查询的 Attempt Job 句柄，不能仅依赖可能挂死的 Host。把输入执行器与允许脱离的官方客户端明确分组；输入 helper 禁止逃逸，合法客户端按受控启动链单独放行。若必须维持可 breakaway 的同一 Job，则需完整、持续的进程创建/转移登记与独立 supervisor；启动后采样父 PID 树不足以补齐。不能简单复制保留 KILL_ON_JOB_CLOSE 的唯一清理语义而不设计谁负责显式停止与最终关句柄。

## 必需的 OS/交接证据

1. 固定 Run/Attempt、desktop、controllerLeaseId/generation，与 Host/runner 原生句柄和 PID + creation time 绑定；PID 单独不足以防复用。
2. 从进程创建前建立、覆盖全部输入执行器的 containment；记录合法脱离对象及理由。不能凭可执行文件名猜归属，不终止用户游戏/其它会话。
3. 独立可信拥有者能在 Host 挂死/被杀后查询该 containment；查询失败、成员遗漏、句柄丢失都返回 unknown，不能把空快照当零成员。
4. 停止输入执行器后观测 Job active-process 为零/对应进程句柄已 signaled，并保证之后不能再派生输入执行器；breakaway 范围另有完整覆盖。具体 Windows Job/进程句柄 API 方案须实现时验证，本文没有实测 OS 行为。
5. 所有事件生产者、reader 和 callback 的写权限/清理权限已交接或撤销；对原 Attempt 幂等持久化恢复收据，旧回调不能撤销后继 lease、改后继状态或操作其客户端。

## 可修下一步与验收门槛

1. 先补证据合同与负向测试：退出但遗留输入 helper、breakaway helper、Assign 前派生、PID 复用、Job 查询失败、callback 正在 cleanup、旧 lease 被新 generation 替换。都不得自动继续。
2. 实现有界 dispatch/cancel/watch/drain 和独立 containment supervisor，再接 Manager recovery CAS；不是只改 parking。当前源码具备可修改的 Python/C# 入口，没有发现必须修改线上数据库才能起步的障碍，但不能据此承诺本 HEAD 已有完整安全实现或安装兼容性已验证。
3. 新增无游戏的受控 OS fixture：一个合法保留的客户端替身、多个可输入 helper 替身；hung/killed Host 后独立验证 helper 全部退出、客户端替身保留、收据与原句柄绑定，再验证后继只启动一次。
4. 回放原三红时，区分原来“无证明”的模型与新增“有证明”的恢复模型；不得 mock quiescence=True 或把 poll==0 升格为证据使其变绿。最后做真实 watcher→Manager callback 的集成测试，覆盖 callback 卡住/抛错/迟到和重启恢复。

**交付边界：本报告给出静态调用链与实现前置，不是新实现、测试通过、发布批准或真实游戏验收。父方可继续独立实现与测试，无需等待本报告发起任何业务动作。**
