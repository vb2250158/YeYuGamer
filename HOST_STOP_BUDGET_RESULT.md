# Host 停止预算与完成交接切片：整体仍 CHANGES_REQUIRED

日期：2026-10-28。承接 QA 分支 `qa/queue-timeout-20261028` 的 `e9a016b9d9a7fb29da21d81c2856a17394a4e665`。本切片只修固定 Host 的停止等待、reader/完成交接门禁和 Manager 异常持久化的 ACK，不能把它说成整个队列超时问题已解决。

## 边界

- 源仓库 `D:\Game\Automation\YeYuGamer`；只在 `D:\Game\Automation\_qa\queue-timeout` 修改。main 的1113项原有改动不 checkout、不 stash、不 reset、不合入本分支。
- 本轮不修改任何 fork，不启动/关闭游戏、模拟器或启动器，不访问真实 Manager API/数据库/锁/配置，不切运行绑定，不安装生产、不重启服务、不 push/PR/issue。
- 新用例只用 fake process/reader、实际临时 SQLite；既有 Host supervisor 回归会启动自有短命 Python fake-host（不是游戏/实际 Host），沿原封装隐藏窗口并退出。没有 GUI、OCR 或模型加载。
- 使用共享技能 `D:\GameDevelopmentSkills`，本地提交45447f4；根因先静态追踪，再独立子 Agent 复核。QTS1与持久化 ACK 分别独立取证，修改前保存SHA和原文件于 `.cache/host-stop-budget-before`。没有读取、输出或提交凭据/账号数据。

## 发现、证据与修复

下表代码路径均相对于 QA 的 `backend/yeyu_gamer_manager/`；基线行号指e9a016b，当前行号指本切片工作树。

| 项目/严重度 | 位置与触发 | 证据、最小修法与状态 |
| --- | --- | --- |
| QTS1，P1，已确认 | 基线 `services/adapter_host.py:1958`，kill后无参wait；`:1960`正常出口也无参wait | kill请求不是退出确认。移入统一预算；watcher四个wait及startup失败wait都显式timeout。不是实机挂死的现场结论。 |
| QTS1，P1，已确认 | 基线 `adapter_host.py:2017`之后异常仍回调/删binding/control，finally只比较process即清owner | 未证Host退出或reader结束时保留全部恢复材料，不finish、不回调；回调正常返回也不自动释放，须Manager显式handoff。 |
| QTS1，P1，已确认 | 基线 `adapter_host.py:2115`只拒未退出的旧process | 当前`:2138`任何非空owner均拒绝覆盖；退出不等于交接完成。新测试拒绝exited-but-unacknowledged owner。 |
| QTS2，P1，已确认 | `services/manager.py:8239` fallback原来逐条独立提交、吞后续异常；Attempt terminal并不证明Run/lease/member都提交 | 首版ACK反证实际出现Attempt failed、Run running且错误handoff。现`:8243`整个fallback用短Store.atomic；异常立即阻ACK，完整提交后才解除；成功降级通知明确failed，不沿用原completed。 |
| 实施中P2，交付前修复 | handoff闭包在callback返回/抛错后曾仍可释放原owner | 独立复核指出。当前 `adapter_host.py:2040`附近的handoff窗口与封口/GC决策共用execution lock，封口后即使原owner未换也拒绝；有两种并发次序及异常/正常迟到回归。 |

Git blame追到shallow边界f84c22a，说明原等待和清owner代码在可见边界已经存在，不能冒称真正引入提交或完整还原原设计动机。未查线上失败日志，以上确认范围是代码路径和离线复现。

### 红绿证据

1. 从 `git show e9a016b:backend/yeyu_gamer_manager/services/adapter_host.py` 原样抽取旧watcher方法，在新隔离fixture里运行三个断言：**3项全部失败**。实际捕获最后一次wait为`call()`而非`timeout=G`，以及未ACK/回调抛错仍清owner。fake wait会立即抛TimeoutExpired，不会故意执行真实无限等待。日志 `.cache/host-stop-budget-baseline-red.log`。
2. 首版ACK的真实临时Store反证：primary finish抛错，fallback写Attempt成功，写Run失败；**1项失败**，确实错误调用handoff。日志 `.cache/host-stop-budget-partial-ack-red.log`。修复后同例要求更强的整体回滚，另覆lease、member阶段失败和先写入实际membership再抛错；queued peer完全不变。
3. 两个独立Agent只读复核均未发现剩余新增P1/P2；其中持久化审查实际跑14项及3个临时SQLite故障注入通过。该Agent当时的member样本没有实际成员行；父方随后补实际partial member写入回滚用例，不以空样本替代该证明。

## 预算唯一源与交接顺序

新增 `services/resources/queue_timeout_budgets.json:8`：`host_stop_grace_seconds=5`。dataclass在 `services/queue_timeout_budget.py:21`；复用原有有限正数、拒bool/NaN/未知字段及最大7200秒校验，可经现有typed `queue_timeout_budget` 部分覆盖，未写真实config。

旧 `CANCEL_GRACE_SECONDS` 类常量退役，两个旧测试迁移到预算resolver；没有保留第二套默认。resource原已列在 `backend/pyproject.toml` package-data，没新增包路径或依赖。

| 消费点 | 单次默认 | 到期/未知行为 |
| --- | ---: | --- |
| cancel后wait、terminate后wait、kill后wait | 各5秒 | 依序停止固定Host；最后仍未确认退出则留owner与binding/control，不造终态成功 |
| 正常退出分支wait | 5秒 | 显式有界；通常已有poll退出确认，不能与kill后风险等同 |
| stdout、stderr join | 各5秒 | 检查is_alive；任一未drain就不finish、不回调，不close忙reader的pipe |
| startup写入失败后的wait | 5秒 | 未确认退出时保留owner与实际staged binding/authority；单独有回归 |

Manager注入只读resolver，Host在execute启动前取一次不可变快照，watcher复用。不会在持execution lock停止阶段重新读Store；新日志记录冻结的hostStopGraceSeconds，不打印配置全文。

**最坏不是“整个Host总共5秒”**：三次stop wait与两次串行join可合计5G；另有原plan预算、callback、控制管道与文件I/O。callback overhead40和cancel grace15仍是其它预算，不能推导任意自定义G都能在40秒内交接。

正常顺序：root退出+readers结束 → Manager完成cleanup/结果持久化并关闭attempt log → 回读原Attempt身份、terminal与completed_at，且异常fallback已完整提交 → 一次性handoff清exact process/plan owner → 队列通知。Host与Manager的内部callback ABI必须一起集成；外部JSONL v1.1、游戏参数和退出码约定未改。

terminal-human自身人工lease不是Host内存owner；ACK不额外撤该lease，防止反过来卡住parking。仍由既有预算/lease ID+generation规则撤销。

无ACK、回读失败、closeLog失败、仍active、handoff False、reader未结束均不通知。handoff成功后通知抛错不恢复旧owner，也不重复回调。handoff之后的旧control/binding GC只处理原Attempt路径；失败记录cleanup_pending和原scope债务，不把垃圾文件清理当桌面授权。通知内已经建立的新owner/新control及其cancel标志均有隔离测试。

fallback事务只回滚该事务内Run/Attempt/lease/member/事件写入，**不回滚此前已提交状态或实际游戏清理副作用**。保留这项边界，不能把事务测试说成外部副作用原子性。

## 测试与复跑

解释器 `D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe`，`-B`，`PYTHONDONTWRITEBYTECODE=1`；没有安装依赖。所有结果均为离线测试，不是每日或领取验收。

```powershell
Set-Location D:\Game\Automation\_qa\queue-timeout
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH="$PWD\backend;$PWD\backend\tests"
$python='D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe'
& $python -B -m unittest -q test_host_stop_budget test_manager_host_handoff test_queue_timeout_budget test_adapter_protocol.AdapterHostSupervisorTests
```

新文件 `backend/tests/test_host_stop_budget.py` 17项；`test_manager_host_handoff.py` 15项，包含真正watcher→真正Manager callback→真实临时Store的失败交接和无法提交保留两条桥接。process是fixture，不是假装做过OS Job/桌面静默验证。旧supervisor12项已迁移显式handoff，budget7项继续验证默认/覆盖/零值/异常/缓存。

最终本切片联合 **167项通过，25.123秒，0 skip**，日志 `.cache/host-stop-budget-final-joint.log`：上轮119项、本轮32项、旧Host supervisor12项、相邻4项。早期150/164项的通过不是交付前复核的替代；实际partial member写入回归也已纳入最终167项。

完整联合还包括上轮orphan、priority、pure sibling、parking/review、人工作用域、失败继续及显式cancel、missing-callback fence、WW/launcher固定来源测试。重跑时固定参考环境变量与上轮 `VALIDATION_RECEIPT.md` 相同。PGR源overlay必须另起Python进程，不能混入主QA模块绑定。

另外，ZZZ固定来源4项通过（2.672秒）；PGR新回归26项通过（26.949秒，`.cache/host-stop-budget-pgr.log`）。PGR仍是参考主源码模块的限定overlay，不代表已经将本切片Host ABI与全部未跟踪PGR模块完成原生全栈集成。本轮未重跑完整88项或全量构建，前轮两项旧mixed fixture冲突没有据此消失。

独立原blob `0d9698449bd82700d135c9e113881b45a11ee0ff` 未改回放：**9/12，3失败，2.569秒**，仍是dead/missing callback、hung、killed owner。未skip/xfail、未制造quiescence=True。日志 `.cache/host-stop-budget-independent.log`。

## 其它未完成项与结论

- pending_execution孤儿回退/正式取消、优先级、parked-human原实现不重复修改；本轮联合回归覆盖。当前main的未跟踪调度/PGR模块仍需逐SHA集成，不夹带1113项改动。
- PGR严格后继/取消预留、ZZZ分支诊断、WW依赖/运行包资格的上轮补丁保留；没有切fork、切生产绑定或声称现场原因已证。其线上结果仍未验收。
- **整体仍CHANGES_REQUIRED。** 本切片不提供OS输入执行器containment/Job归零证明，不解决callback本身挂死、同步cancel write/flush、全部文件I/O的硬deadline，不补same-Run恢复provider，也不让未知owner自动跳过。保留owner不是“永久占锁已修好”，仍须后续正式恢复责任接管。
- 没有改C# BREAKAWAY行为、杀任意后代树、凭poll清owner或裸continue。上轮完整预算/九fork循环和截图/重启验收缺口继续有效。
- 没有已证实并修复验证的上游算法问题，无push/PR。按“做完本切片就停”，交付QA提交、证据及剩余边界，不启动真实业务。
