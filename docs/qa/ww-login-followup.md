# 任务5：WW 登录等待与账号回放依赖

状态：**CHANGES_REQUIRED**。离线回归与一个依赖校验补丁候选已交付；没有完成真实登录、整队列有界跳过、持久状态收尾或发布验收。

## 归属与写入路径

本任务仅新增以下三个交付文件，所有 apply_patch 使用 QA 绝对路径：

- `D:\Game\Automation\_qa\queue-timeout\backend\tests\test_ww_login_followup.py`
- `D:\Game\Automation\_qa\queue-timeout\docs\qa\ww-login-followup.md`
- `D:\Game\Automation\_qa\queue-timeout\integration-patches\ww-login-followup.patch`

隔离副本、修改前备份和测试日志仅在 `D:\Game\Automation\_qa\queue-timeout\.cache\ww-followup`。
2026-10-28 实际检查：main 的上述三个同名路径均不存在；QA 缓存与候选文件不是链接。
没有修改 main、fork、Manager/store/model/queue/protocol、账号配置、绑定、数据库或运行文件；没有调用真实 API、启动/关闭游戏、模拟器或 Manager，没有提交或 push。

QA HEAD 为 `4361c935d8168625e04ca4067b1e8d0d498acb9e`。main HEAD 为 `f4ab6bf76365cd62e5ccde118398b417ed086de7`。
main 的 `ww_multi_account_scope.py` 是 untracked，QA HEAD 无该模块；不能将其作为新增整文件带入 QA 或提交。
QA 中其他任务正在修改的文件不是本任务产物，未覆盖、还原或提交。

## 根因、反证与证据边界

### 1. 首账号登录不可用

用户提供的“检查应用更新… / visible:False”是症状，不足以证明客户端更新、隐藏窗口或捕获失败中哪一个是现场根因。
本次不访问运行数据或真实画面，不能把历史资料当作这次运行的证明。

本地 fork 当前 HEAD `d7fe3e05f9ae52322e952089836387f85713dcc7` 的普通 `MultiAccountDailyTask.run` 与派生登录保护版本不同，不能直接在当前分支套补丁。
只读 Git 对象 `20d2a0fbd5cbe257d80cc2de5f70d369ba25d07f:src/task/MultiAccountDailyTask.py` 中：

- `_prepare_first_account_login` 第174行开始，只接受已识别账号列表或世界；未知页面不盲点、不执行 DailyTask。
- 第226行已有 `wait_until(ready, time_out=100, raise_if_not_found=False)`；第231行明确抛出 `account_login_screen_unavailable`，并声明没有开始每日动作。
- 这反证“首次登录没有任何等待预算”。它不证明 SDK 回调阻塞、捕获等待及世界分支另一个100秒等待都受同一个绝对墙钟截止约束。
- 本次测试直接执行该真实方法 AST，以合成等待器模拟100次未识别观察；证明参数预算、异常传播和零输入，不证明 SDK 的实际耗时或真实 OCR。

已复核 main 内历史资料：

- `.cache/verification/autonomous-daily-20261023/review/ww-title-login-entry-20261026/RESULT.md`：此前确认登出后缺“登入”分支，已有专门候选；不是现在“应用更新/不可见”的已证实根因。
- 同目录 `ww108-finalize-20261026/RESULT.md`：固定派生提交20d2a0，报告已有标题页衔接及79项历史探测；本次没有运行其 upstream-candidates 或复用其验收结论。
- 同目录 `ww0400-readonly-admission-v1/RESULT.md`：历史首账号已 failed、原清理已结束；第二账号明确在 Host 前失败。不能把二者描述为一直活跃，也不能由 exitCode=0 推导每日完成。

### 2. 第二账号缺少回放来源

main 当前代码有两个不同门禁使用同一错误码：

- `backend/yeyu_gamer_manager/services/ww_multi_account_scope.py:254`：读取首账号最新 Attempt 的冻结 `wwAccountScope`；缺少、来源不匹配或范围改变，拒绝为 `ww_account_replay_source_missing`。
- `backend/yeyu_gamer_manager/services/manager.py:2335`：即使 plan 存在，批次的共享观察 JSONL 不是文件，也以同码拒绝。必须区分“缺冻结 scope”和“缺观察文件”；不能只凭错误码断言是哪一层。

这是真正的依赖关系，不是第二账号需要重新登录。缺源不能退回 execute，不能制造空 JSONL，不得用首账号终态替代第二账号。
反例：首账号整体 failed 不代表其中所有账号的共享观察都无效；完整、同范围的逐账号记录仍应交由既有 runner 校验，不能新增“父失败则全部丢弃”。
另一个反例：Manager 文件门禁只判断 is_file，垃圾内容也会返回 read；这不是有效观察或成功凭据，后续 runner 校验未在本次覆盖。

### 3. 失败终态与假活跃

main `manager.py:10042` 的既有启动异常分支，在清理门禁允许的条件下，于原有事务内将 Attempt 标为 failed/completed、GameRun 标为 failed、撤销控制租约并将 membership 终态化。
本次执行其真实事务 AST，以内存 Store/清理门禁替身覆盖 starting/running/cancelling 三种前态，验证上述写入参数与原缺源原因保留。
这是局部异常收尾证明，不是持久 Store、Host 锁释放、callback 交接、队列下一成员或重启恢复的集成验收。不能由此声称整队列已无假活跃。

## 实际最小改动

仅在 QA 缓存的 `candidate/ww_multi_account_scope.py` 将一行依赖 plan 取值改为两行类型校验：

```python
source_plan = attempts[0].get('plan') if attempts else None
scope = source_plan.get('wwAccountScope') if isinstance(source_plan, dict) else None
```

反例实际复现：源 Attempt 的 plan 为 null/list/string 时，旧实现先抛 AttributeError，失去明确依赖错误；候选沿既有 fail-closed 分支抛 `ww_account_replay_source_missing`。
这是额外的损坏依赖边界加固，**没有证据说明这些损坏输入是用户现场的实际原因**，不得称为登录等待修复。
不改错误码、状态集合、原账号顺序、冻结成员、来源校验或有效 replay；没有增加执行入口。

`integration-patches/ww-login-followup.patch` 是单文件、2增1删的最小 unified patch，不是新模块整文件补丁。仅供有对应基线模块的后续集成使用；禁止直接套到缺模块的 QA HEAD。

### base SHA256 与备份

| 输入 | SHA256 |
| --- | --- |
| main `ww_multi_account_scope.py` 原始字节，备份 `base/ww_multi_account_scope.py` | `8b2560faf338d41eb25555ed4c913e8dd7f8219c30e97b9fbd8b47ab50670e90` |
| QA `candidate/ww_multi_account_scope.py` | `adc98819bab5c78c5997f52ce0928635da5ae0ba0c76e61aaf6178ed767f5cf0` |
| main `manager.py` 只读输入，未保留整文件副本 | `279ccef22f0916d45821ef2d9693957e6a4a06f3ec8fff4536f71a7360202244` |
| fork 历史提交20d2a0任务 Git blob 原字节 | `890d2fe4eea471cc54ae651da6545d27e079db289875eee77833ae37061ed9a6` |
| `base/MultiAccountDailyTask.py` PowerShell 导出快照，换行规范化后 | `41e422c436727471e64028656458739630e74dad40251ae787fec68838b57dc9` |

测试在执行任何来源 AST 前硬校验输入 SHA，来源漂移就失败，不默默换基线。只执行选定函数 AST；去掉函数内相对 import 并注入 Profile/配置读取替身，完整 typed 模型/协议未参与测试。
测试首次 AST 装载夹具错误（Lambda.body 不是语句列表）已修正，初始失败日志保留在 `harness-initial-*.log`；不将它算作生产红测。
每次改测试前保留备份与 SHA：初稿 `5129ace26bfc7cffa161ae2f4bed250cbed61b5a7c9f6a3a848cf32a77737817`；补终态测试前版本 `bc7fc5151c077c27086c473fd422b1abb4184d116ac18343f99a0958b3284adb`。

## 真实离线测试

实际解释器：`D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe`，只用标准库，不导入 SDK、OCR、GUI 或完整 Manager。

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:WW_FOLLOWUP_REFERENCE_ROOT='D:\Game\Automation\YeYuGamer'
$env:WW_FOLLOWUP_FORK_ROOT='D:\Game\Tools\forks\ok-ww'
& D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe D:\Game\Automation\_qa\queue-timeout\backend\tests\test_ww_login_followup.py -v
```

- `red-final.log`：15项，退出1；仅损坏 plan 的3个子用例因 AttributeError 出错。其余既有边界通过。
- `green-final.log`：15项全部通过，跳过0，退出0。含100次未识别预算、列表/世界反例、取消传播、缺/错源、父失败有效 scope、文件/目录/只读分支、禁止退回 execute、3种前态的事务收尾及输入 SHA。
- 在 `.cache/ww-followup/apply` 的原始最窄模块副本执行 `git apply --check --directory=.cache/ww-followup/apply integration-patches/ww-login-followup.patch`，退出0；没有对 main 或 HEAD 应用补丁。
- `git apply --numstat` 确认只含专用模块的2增1删；没有 Manager/store/model/queue/protocol 内容。
- 未执行全套测试、构建、发布或真实业务。初始与最终输入核验中 main 两个源文件 SHA 一致，fork 跟踪文件状态为空。

## 可复现性修正（2026-10-28）

原版测试依赖未提交 `.cache` 的缺陷属实；以上历史日志保留，但不再作为重跑输入。此次仅修改本测试与本报告，未修改交付 patch、main 或父方实现。

新的运行合同：

1. 必须显式提供上面两个绝对 reference root，不再默认读取 main，也不使用旧 `WW_FOLLOWUP_SOURCE_ROOT` / `WW_FOLLOWUP_SCOPE_SOURCE`。两个 root 仅只读：主参考提供固定 SHA 的 scope/Manager，fork 仅运行有15秒上限的 `git show` 读取固定20d2a0对象；不 checkout、fetch、修改分支或运行 SDK。
2. 执行前校验 scope baseSHA、Manager SHA、原始 Git blob SHA，以及交付 patch SHA `0d96d5a943820bbfdb1f19a38ade42330e6e411882d6597a2274b03d39705d3b`。缺文件、Git对象缺失、缺环境变量或任一 SHA 漂移均明确失败，不 skip、不替代来源。主参考文件 SHA 仍须符合上表，不能用缺模块的 QA HEAD 冒充 reference。
3. 只在当前测试所在根目录下新建唯一临时目录。复制 scope 原字节，临时规范化 LF 后对交付文件 `integration-patches/ww-login-followup.patch` 执行 `git apply --check` 和 `git apply`，恢复原 CRLF并核对 candidateSHA。Git 搜索上限阻止向父仓库发现，引用仓库不作为补丁目标。登录任务由固定 Git 对象重建并校 SHA，完全不读取 `.cache`。
4. 文件门禁夹具也放在该临时目录；正常结束及初始化失败均清理。选定 Manager AST 每次读取前复核 SHA。默认测试 candidate；设置 `$env:WW_FOLLOWUP_VARIANT='base'` 可复现旧版的3个 AttributeError子用例，结束后移除该环境变量。
5. 重建需要 Python标准库、Git、交付测试文件、交付 patch，以及两个满足固定哈希/对象的只读参考根；不依赖本次 worktree 的缓存或额外脚本。当前三个交付文件仍是未提交文件，后续拥有者集成时必须一同纳入版本控制；本次未擅自 stage/commit，也不把未跟踪 patch 称为已提交。

真实复跑证据：新建干净临时目录，只复制测试与交付 patch，完全没有 `.cache`；用显式 reference root 执行：

- candidate：15项通过，退出0；结束后无临时目录残留。
- base：15项，损坏 plan 的3个子用例报错，退出1；无缓存或临时目录残留。
- 缺 reference root：0项执行，模块初始化明确拒绝；本机 unittest 返回5，不能误报通过。
- 临时交付 patch 增加一个换行：SHA 校验拒绝，退出5，未执行来源 AST。

重跑中首先发现仓库外 `git apply` 不自动处理原 CRLF，已将换行处理限定在临时副本，最终 candidateSHA与原候选完全相同；没有修改交付补丁或参考源码。该可复现性修正不改变 CHANGES_REQUIRED 与业务验收缺口。

## CHANGES_REQUIRED 与后续最小范围

1. 当前请求那一轮的固定源码/同次日志与实际捕获缺口未核对；历史104/107/108证据不能证明这次更新等待的根因。需要现成同次脱敏日志/捕获证据，不靠盲重跑取证。
2. 有界跳过、后续游戏继续、Host 输入静默/回调交接、持久 membership 与重启恢复未集成。本任务禁止改共享队列/Manager，不能通过私建状态机或测试替身宣称完成。
3. QA HEAD 缺专用模块与一致协议基线；需拥有者提供可评审的集成基线，按 baseSHA 应用最小补丁，不能整文件纳入 main 的 untracked 工作。
4. 若确需 fork 修改，候选位置已确认是派生版本 `src/task/MultiAccountDailyTask.py::_prepare_first_account_login`，而非当前 fork HEAD。建议先在用户创建并授权的独立 worktree 核对 SDK wait_until 与捕获是否守绝对截止，以及实际更新页识别；只增加诊断/有界拒绝，不扩大登录、账号选择或每日执行。现有100秒配置不应盲目加长。本次未写 fork 补丁或切分支。
5. 候选的损坏 plan 加固不是现场两项故障的完整修复；业务完成数未知，原失败/未完成结果不变。最终维持 CHANGES_REQUIRED。
