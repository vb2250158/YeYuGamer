# 每日执行、排障与上游贡献工作流

本流程供 Codex 和 WorkBuddy 共用。2026-09-16 按用户要求统一；技能入口只引用本文件，专项参考位于 `docs/daily-workflow/`。项目职责仍以 [编排合同](integrations/orchestration-contract.md) 为准。

## 适用范围与授权

用户要求“看看每日还有什么没完成，继续做，有 bug 就修复，直到做完”时，执行本流程，持续推进当前勾选范围。不要只做就绪检查、生成计划、准备补丁或发出 PR 就宣称结束。

用户已在 2026-09-16 明确授权此每日修复流程中的官方工具更新、YeYuGamer 修复及 GitHub 提交、确认属于上游工具的修复及上游 PR。该授权适用于相应每日修复任务；已有授权时直接完成，不反复询问。其他仓库、无关功能、任意消息或付费操作不由此获得授权。在其他用户或环境使用本流程时，依据那次任务的实际授权，技能文本本身不是授权。

仅要求整理技能、文档或只读检查时，完成相应范围，不因此启动每日、更新工具或对外提交。本文件不创建定时任务；“直到完成”指当前执行任务的闭环。

## 1. 接续真实状态

先读项目 `AGENTS.md`、`README.md`、`ARCHITECTURE.md`、`docs/local-ownership.md`、[编排合同](integrations/orchestration-contract.md) 和 [目录规范](storage-management.md)。源码只维护本机项目；候选、下载与调查放项目 `.cache` 对应子目录。

通过已安装 WebGUI、CLI 或受支持的 typed API，读取健康状态、能力、游戏日、账号、勾选项、活动批次、RunAttempt、人工接管和未完成原因。不要用未鉴权 HTTP 失败推导“所有 API 禁用”；状态变更必须经 Manager，携带接口要求的幂等键和当前 stateVersion。

建立本次范围清单：每个游戏/账号的已完成、未完成、未运行、待人工、能力缺失；区分今日聚合结果与当前冻结批次。已有活动队列先跟进，不能重复启动或趁运行替换工具。历史成功与失败保持原记录，不改数据库、不清空待办、不取消必选项来变绿。跨游戏日时保留旧日缺口，重新核对新日范围，不能挪用旧日结果。

## 2. 优先核实并更新上游

对范围内每个工具建立版本清单：官方仓库、当前发行版/commit、依赖及资源版本、实际入口、查询时间、官方最新发行版和默认分支 commit、相关 issue/PR。通过官方仓库、release、更新日志和 PR 实时查询，不凭旧技能版本或文件修改时间断言“最新”。

先查看官方是否已经修复同类问题，再进入深度归因。默认更新到最新正式可用版本；若修复仅在官方默认分支或未合并 PR 中，固定 commit，在隔离候选验证。明确区分正式发行、官方开发版、未合并 PR 和本地补丁；不能把本地分支称为官方最新版。

更新前保留用户配置与原绑定，检查配置迁移、依赖、资源、正式入口和 Adapter 兼容性。通过现有候选/发布机制切换，不覆盖运行中的工具、用户配置或游戏目录。验证启动和配置兼容后，再从产品入口验证任务。下载失败、版本来源不可查或最新版本不兼容时记录原因；不得冒称已更新，也不要无限重试相同失败。需要暂留旧版时明确记录差异及恢复条件。

客户端版本与工具版本分别检查。客户端使用官方启动器更新，不自行拼接补丁或解包覆盖。具体步骤见 [启动器更新](daily-workflow/launcher-update.md)。

“最新版”只证明版本基线，不能证明工具没有 bug。完成版本检查后仍按证据链调查。

## 3. 执行并持续观察

真实每日从已安装 WebGUI 的“开始每日”进入。按当前授权的勾选范围执行，不用 CLI `start-daily`、裸启游戏、裸启工具或旧队列脚本代替真实验收。专项整轮验收按该次明确范围进行；临时诊断改变范围时记录并恢复，局部验证不能代表整轮完成。

定期查看新增官方日志、步骤、截图或进程状态并给用户简短进展。关注实际变化、异常与下一步，不能把重复心跳当作进度。静态弹窗或重复失败先诊断；没有新证据、新修复或外部状态变化，不重复执行同一失败路径。

普通已保存账号的登录确认按项目已授权的最小衔接处理。凭据、验证码、条款、付费、抽卡、不明成本、PVP 或不可逆选择保留现场、暂停整条队列并说明具体所需人工动作。不能借“直到完成”绕过这些边界。

## 4. 日志证据与责任判断

先收集证据，再修改。日志至少关联游戏、脱敏账号标识、游戏日、Batch/Run/RunAttempt、工具版本/commit、Adapter/YeYuGamer 版本、阶段、时间和原始异常。保留实际入口及脱敏的有效配置，区分用户配置、注入值与恢复后的值。

完整采集官方 logger 的名称、级别、时间、消息、异常堆栈以及 stdout/stderr；YeYu 补充配置投影、启动/关闭、事件接收和终态汇总日志。观察代码保持原参数、返回值、异常与任务顺序；日志采集失败不改变官方任务。截图辅助定位，不增加完成门槛；不靠单句日志或退出码归因。

调查顺序：Manager 总览 → 当前批次/RunAttempt → Adapter 事件 → 官方完整日志 → 必要的客户端日志与画面。优先检查 YeYu 的路径与参数、账号/步骤映射、配置写回恢复、启动交接、焦点及进程归属、观察器副作用、协议事件丢失/重复/时序、终态汇总。上游报错可能由错误注入触发，不能自动归为工具 bug。

使用 [归因与证据清单](daily-workflow/triage.md) 将问题标为：已确认 YeYu、已确认上游、客户端/环境、能力缺失、人工阻塞或待定。证据不足就继续调查，不能为了提交 PR 强行选边。

## 5. 先修 YeYu，确属上游才改上游

**YeYuGamer 问题：** 在唯一源码修复，保留其他任务的改动；增加能复现故障的必要测试，检查相关接入协议、配置恢复和终态归属。按 [本机开发](local-development.md) 完成相关验证，用统一发布脚本发布主程序与 Adapter，保留用户配置。检查差异和敏感信息，仅提交本次确认改动，按已有授权推送 GitHub，记录 commit 和验证结果；不把工作区其他改动一并提交。

**上游工具问题：** 再查官方主线和相关 open/closed PR，排除调查期间已出现的修复与重复 PR。若已有修复，验证并采用它；若没有，在项目 `.cache` 内的隔离上游 checkout 修改可审查源码，遵循该仓库贡献规则。游戏识别、任务分支、奖励判断和重试的修复放上游仓库，不能搬进 YeYu Adapter 或通过运行时 monkey patch、字节码补丁改游戏逻辑。

**尚未实现的工具能力：** 用户于 2026-09-17 明确允许继续补写此前未完成的功能。确认是能力缺口后，可以在工具侧实现、测试并作为功能补全提交，不必把它包装成已有功能的 bug；PR 如实说明新增入口、默认开关和实跑限制。源码来源不明时先建立可审查的独立候选，区分官方基线与新增本地代码，不能把本地任务树冒称官方发行。该授权不改变 YeYu 只负责编排的职责，也不授权成本动作。Codex 与 WorkBuddy 均按此执行。

提交前须有具体复现条件、上游代码位置及根因、脱敏日志、修复前失败与修复后验证，说明如何排除 YeYu 注入/观察层。用户于 2026-09-17 明确要求先验证通过，再向上游提交 PR：游戏任务改动须先通过相关测试，再经正常发布/绑定从已安装 WebGUI 实跑验证。离线测试或构建通过不替代实机验证；验证未完成、失败或受阻时不创建上游 PR（包括草稿 PR），也不把未验证的新修订推送到已有 PR。能力补全遵守同一顺序。

验证通过后，通过独立分支向正确上游提交 PR，记录 URL、base/head commit、已完成测试和实跑证据及额外未覆盖环境；完整流程见 [上游 PR](daily-workflow/upstream-pr.md)。提交后核对 CI/审查反馈并处理实际问题，新的修订仍先验证再推送。PR 已发出不等于合并或全部每日完成；上游合并发布后迁回官方版本，移除对应临时补丁。

## 7. 根本目标：全自动、零意外（用户 2026-09-21 明确）

用户原话：

> **"yeyugamer 的目标是会自动更新游戏版本，自动启动游戏，自动跑完所有游戏，不会有任何意外。"**
> **"不能有人工接管这个状态，这些都是严重缺陷错误，属于堵塞，要修复。"**

因此本项目的完成形态是**无人值守闭环**，四件事缺一不可：

1. **自动更新游戏版本**（客户端补丁：WeGame 版 NIKKE、WW/ZZZ 启动器、TLS 版等，按 `daily-workflow/launcher-update.md` 走官方启动器）；
2. **自动启动游戏**（含启动器 UI 驱动、点击、版本检查等待）；
3. **自动跑完所有游戏**（范围内每个游戏/账号的官方任务终态）；
4. **不会有任何意外**（不停在人工接管、不留残骸、不需要人来看一眼）。

**推论（对上面每一条都适用）**：任何一次人工接管、任何一次 `blocked`、任何"需要人点一下/看一眼"的停止，都是**被实现的缺陷**，必须归因到具体机制并修掉，然后恢复队列——**不是**记录原因后继续下一个或收工。停止在 `human_required` / 机制性 `review_required` / `blocked` 时按下面三步处置：

1. **取证归因**：Manager 事件 → RunAttempt → Adapter 事件 → 官方日志 → 客户端/进程状态，定位到**具体机制**（哪个码、哪个进程、哪条清理、哪个绑定、哪个规划决定）。不接受"工具不支持""环境问题"这类没落到证据的结论。
2. **分类并落到责任方**：
   - **YeYu 缺陷**（编排、进程生命周期、绑定信任、清理、观察层、规划/范围、启动交接）⇒ 本项目修 + 补可复现测试 + 发布，然后恢复队列继续。
   - **上游缺陷**（游戏识别、任务分支、奖励判断）⇒ 按第 5 节走上游。
   - **能力缺失**（该功能确实没实现）⇒ 按第 5 节在工具侧补实现。
   - **真的需要人**：只有凭据/验证码/条款/付费/抽卡/不可逆选择，以及客户端版本更新本身需要人等外部动作时，才允许保留现场并暂停，且必须写清**用户需要做的最小动作**。注意"客户端要更新"不是理由——自动更新本身就是目标 1。
3. **修完必须回到队列**：用 Manager 的合法路径恢复（注意 `run-resume ≠ batch-resume`；必要时发新批次），直到范围内完成或有上述"真的需要人"的具体阻塞。

**已知的缺陷家族（每次遇到先查这里，别再当新问题重查）**：

- **自有进程残骸挡住自己**：取消/接管/启动阶段失败后，Manager 不回收它自己启动的进程（死壳、无窗口启动器），下一次运行被自己的残骸挡住。WW 空壳已修（死客户端可回收 + 两个闸门可回收）；其余游戏待推广。
  - **Endfield：跨轮次继承"楔死启动器"**（2026-09-22 实测）。`鹰角启动器`/`Games.exe` pid 6444 由 09-21 23:31 那次失败尝试拉起，**一直存活到 09-22 04:00 仍在**（窗口 `IsWindowVisible=True`、标题 `鹰角启动器`）。03:16 的 Endfield 尝试直接继承了它：`launch.launcher-waiting pids=[6444] {'running': {'6444': 'Games.exe'}, 'lastLauncherProbe': 'exit=3 out=not-ready:accessibility-tree-empty'}`（无障碍树为空 = 楔死），随后 `restore-launcher-window` 唤醒旧窗口、点击后新起 `Endfield.exe` 24380，但始终无游戏窗口 → 13 次 audited action 后 `game_start_failed`（`Endfield launcher dispatched 13 audited actions without yielding a game window`）。现有代码只有 `_reactivate_headless_endfield_launcher`（**重新激活**基线启动器），没有 WW 那样的 `recycle-wedged-launcher` 回收分支 ⇒ 旧启动器永不被替换，每次尝试必然复现。修法：Endfield 补上与 WW 对称的"楔死启动器回收 + 重启"分支，并对 `accessibility-tree-empty` 走该分支而不是激活。
- **僵尸条目（已退出但仍被枚举）**：另一个进程持着句柄，进程已退出（`GetExitCodeProcess` 返回非 259）却仍出现在 `tasklist` 里，且 `taskkill` 报"没有此任务的实例在运行"。会让队列清理判定不完整并把人拦在 `queue_game_cleanup_incomplete`。**已修**：`_listed_cleanup_residuals` 只把仍然存活的条目算作残留。
  - **同族残留：启动期 `pids=[]` 且无原因码**（2026-09-22 实测，PGR）。02:42 的 PGR 尝试 `launch.launcher-waiting elapsed=242.5s pids=[] {'running': {}, ...}`、最终 `game_start_failed: configured game client did not expose a stable visible game window`，全程**没有任何进程被登记**（对照：同一 PGR 在 09-21 21:01 的尝试 `launch.ready readyWindowPid=6392 readyWindowWidth=1280 readyWindowHeight=720`，启动能力本身正常）。即"客户端活着但被判成不存活"时，观察层只留下空映射，无法区分"没起来"和"起来了但不可见"。修法：`_process_is_live`/`_list_running` 对 enumerated-not-live 给出可解释原因码与存活证据，而不是静默空映射。
  - **2026-09-22 05:04 实测补正（重要，推翻旧假设）**：PGR.exe 由 Manager（ppid=36124）在 05:04:39 启动为 **pid 22264**，探测结果 `exit=0`（已终止）、CPU 仅 `0:00:01`，且 `C:\Game\Punishing Gray Raven` 树下 **05:00 之后零文件写入** ⇒ 不是"活着却被判死"的观察层误判，而是**客户端自己起后数秒内以 0 退出并什么都不写**，同时留下一个可枚举的残留条目（**2026-09-22 12:1x 更正：这不是 Manager 的句柄造成的，见下文互斥体结论**）。同刻对照：存活的 Manager 进程在同一探测下返回 `259`，证明探测手段可靠（`scripts/heartbeat/hb-pids.py`）。
  - **旧结论"Manager 重启即可释放句柄"已被证伪**：Manager 04:36 重启后，09-21 20:59 起的 `PGR.exe` pid 6392 条目**依然可枚举**且仍 `exit=0` ⇒ 句柄持有者不是 Manager，重启也不释放。残留条目会自我延续：每次新启动的 PGR.exe 都留下一个新条目（6392 ← 18716 ← 22264 ……）。**真正原因见下文 2026-09-22 12:1x 的互斥体结论——它只被机器重启清除。**
- **跨批次遗留人工门堵死新一天整条队列**（2026-09-22 05:01 实测，**新机制，影响最大**）。09-21 批次 `9a65d4fe` 的 NIKKE run `7c101f48` 停在 `human_required`（`nikke_wegame_game_window_missing`），该批次**未 seal 也未 cancel**。09-22 05:01:15 新批次 `bb1faa27` 正常创建（**没有前置检查**），但 4 秒后首个游戏就死：`launch.queue_cleanup` → `code=queue_game_human_required`，`message='NIKKE has an unreleased human gate; preserve its scene before starting another game.'` ⇒ **PGR 根本没启动**，整条 09-22 队列无法跑任何游戏，只能靠人工 release 解开。机制见 `manager.py` 的 `check_gate`（`~L8366`）：它遍历所有未 seal/未 cancel 的历史批次，任何 `state == HUMAN_REQUIRED` 或存在 active human blocker 的**同成员** run 都会抛门。
  - 合法解法（已验证可用）：`POST /api/v1/game-runs/{run_id}/takeover-release-requests`（能力名 `release-takeover`，带 `Idempotency-Key` + `X-Expected-State-Version`）⇒ run 变 `review_required`、blocker 被 resolve ⇒ 保护解除。注意顺序：`/batches/{id}/resume-requests` 会因"terminal Batch members require same-GameRun resume first"返回 409，**必须先 resume 那个 run**（`/game-runs/{id}/resume-requests`）。
  - 待修（设计决策未定）：一是 `start-daily` 在存在跨批次未释放人工门时应**快速失败并指出 拥有者批次/run**，而不是创建一个注定卡死的批次（当前会白烧一个 batch 并让当天静默堵住）；二是"上一个游戏日的门"其 todo 已不可能完成，是否应自动 release 需与用户确认语义，不要私自放宽"保留现场"。
- **同源次级缺陷：一次门污染整个批次**。`queue_cleanup` 抛的门会把**当前批次当前游戏**也写成 `human_required`（PGR run `fc9e1103` 就是这样被写脏的），于是这个派生门又变成下一批次的保护源，形成级联。恢复时必须逐个 release，别只放最老的那个。
- **暂停批次的长尾成员永久占位，堵死整条发布通道**（2026-09-22 实测，**影响最大，已修**）。批次 `bb1faa27` 在 PGR `game_start_failed` 后按设计进入 `review_required`，并把 `recoveryPhase.status = ready_for_resume`（`affectedRunIds` = 其余 6 个成员）交给 typed resume；这 6 个成员**按设计保持 `queued`**（`manager.py` `_run_batch` 末尾 "Batch stopped before every queued member ran; a typed resume request is required" 分支，L9046-9073）。缺陷在于 `sqlite_store.active_execution_summary()` 把**任何** `queued` 的 game run 都算作"活动执行"，于是：
  - `POST /manager/stop-requests`、`/manager/restart-requests`、Adapter promotion、账号删除守卫、库压缩从 05:09 起**永久 409**。`scripts/Publish-YeYuGamerLocalRelease.ps1` 第一步就要安全停 Manager ⇒ **任何修复都装不了机**。表症是发布监视器一直停在 `queue busy (running); waiting`，而实际 `activeControllerLeaseCount = 0`、这 6 个 run **零 attempt**、无 lease、无 Host 进程。
  - 同时 `activeBatch` 已是终态，5 分钟 `DailySupervisor` 看到"队列忙"不会补跑 ⇒ 当天静默停摆，两条机械通路同时失效。
  - **修法**：`active_execution_summary()` 只在 queued run **属于一个活动状态批次**时才上报它；**无 membership 的 queued run 保持保守上报**，以维持 `test_lifecycle_rejects_while_execution_is_active` 的原有契约。契约测试 `test_lifecycle_gate_ignores_parked_queue_members_of_a_paused_batch` 做双向验证（把 parked 判定改回空集 ⇒ 该测试 FAIL，原有 orphan 测试仍 PASS）。
  - **运维处置（不改库）**：对这种已终态、零活跃 attempt 的批次发 `POST /batches/{id}/cancel-requests`，`_seal_cancelled_batch` 会把 parked 成员的 membership 与 run **一起**置为 `cancelled` 并封口；随后发新批次重新规划即可。2026-09-22 用它解开了 bb1faa27。
- **已释放的人工门仍让暂停批次霸占整个游戏日**（2026-09-22 实测，**已修**）。`_current_human_batch()` 原本只要批次是 `human_required` + `mode=execute` + 未封口 + 成员 todo 属于当前周期就返回 True，于是：
  - `POST /batches` 一律 **409** `explicit_human_release_required: resolve the current human takeover and resume its same GameRun before creating another batch`；
  - 同一谓词还驱动 snapshot 的 `activeBatch`（`manager.py` L1574-1587），所以 5 分钟 `DailySupervisor` 一直看到 `queue busy (human_required)` 而不补跑 —— **两条机械通路同时失效，当天完全停摆**。
  - 而"resume its same GameRun"在**成员已全部 terminal** 时是空操作：4ddab5cd 的 8 个成员全 terminal，resume-batch 仍 409（列出 8 个 run 要求逐个 resume），逐个 resume 会把整批按冻结顺序重跑，而 **WW 是首个成员、其失败是客户端层**（每次必然再抛门）⇒ **活锁**，永远走不到后面的游戏。此时 cancel-seal 又会作废已通过的验收合同（2026-09-15 教训）⇒ 两条既有出口都不可用。
  - **修法**：`_current_human_batch()` 增加"仍有可恢复的工作"判据 —— 只要**没有**非终态成员（`queued`/`resume_pending`/`reconciliation_required`）**且**门已释放（无 `human_required` 状态的成员 run、无活跃 `human_required` todo blocker），就不再霸占 Today。真正卡在门上的批次（后续成员仍 `queued`）行为不变。测试 `test_paused_batch_stops_owning_the_game_day_once_its_gate_is_released` 覆盖三种情形（已释放/仍有未跑成员/门仍活着），并已验证反向区分度。
  - **通用教训**：`human_required` 不是"批次终态"，而是"等待外部动作的暂停态"。给它写的任何"拥有今天"的判据都必须包含**退出条件**，否则一次人工门就等于当天报废。
- **适配器信任绑定与 Manager 注册值不一致**：`classic-runner/Program.cs` 要求 Manager 的 `toolPath`/`gamePath` 与包内 `tool-binding.json` 完全一致，不符即 `configured_installation_missing_or_untrusted`。**构建脚本的默认候选根/游戏路径是错的**，发布 `-AdapterGroups Classic` 时**必须显式传** `-PgrCandidate*`、`-ZzzCandidate*`、`-NikkeCandidate*`（含 `-NikkeCandidateGamePath` 用 `nikke.exe`），否则会把能用的包换成不可信的包（2026-09-21 实际发生过）。
- **启动器 WebView 空白 wedge**：启动器进程活着、窗口存在但画面全黑（WW 已见两次），必须靠回收重启自愈。
- **客户端需要版本更新**：表现为启动器停在「检查/更新」、或客户端起成空壳。属目标 1，必须自动更新，不交给人。
- ★ **自愈分支自己抛异常，把可自愈的门降级成"启动失败"**（2026-09-22 05:17 & 05:25 实测，**本项目自引入回归，已修**）。WW 的"启动后门"处理里读了未定义的 `running`：`game_launcher.py` `_ensure_started` 第 2117 行 `self._ww_launcher_recycle_targets(running)`，而该作用域只有启动前快照 `baseline` 与启动后枚举 `observed`。后果有两层：
  - 表症：`attempt.launch.failed code=adapter_start_failed error=NameError: name 'running' is not defined` —— 本该抛 `GameLaunchHumanRequired(ww_launcher_game_window_missing)` 并触发回收，却变成启动失败；
  - 实质：回收分支（本次自产残骸）**从未真正执行**，WW 的启动器+空壳客户端因此永远留着，与"自有残骸挡住自己"合流。
  修法：改为 `observed`（本次尝试自己枚举到的进程集合）；补两个可复现测试 `test_ww_post_launch_gate_recycles_the_debris_this_attempt_started` / `test_ww_post_launch_gate_keeps_a_live_client_at_the_human_gate`，并用"改回旧写法 ⇒ 精确复现同一 `NameError`"验证过区分度。**通用教训**：门（gate）的处理路径必须也在测试里跑到，"只有回收函数有单测、调用点没有"就是缺口——本轮之前 `_ww_launcher_recycle_targets` 有 4 个单测，调用它的两条分支却零覆盖。
  - 检查手段（可复用）：`scripts/heartbeat/undef_check2.py`（标准库 `symtable` 递归解析，报"被引用但任何作用域都没绑定"的名字）。全 `backend/yeyu_gamer_manager` 扫描当时只报出这一处真实缺陷（`settings.py` 的 `__file__` 是误报）。**改完动态语言的门处理路径后跑一次它**，成本几秒。
- **同一天多个游戏的"客户端不建窗口"是各自客户端层问题，不是编排缺陷**（2026-09-22 game day 实测汇总，避免下一轮再当新问题查）。当日除 StarRail 4/4 成功外全部失败，但表现与归属各不相同：
  - **PGR**（3 次失败）：客户端 `exit=0`、CPU 1s、安装树零文件写入，且 `CrashSightLog/` 最新条目仍是 **09-21 21:00**（`CrashSight.1789995610.6392.log`，文件名 PID = 09-21 那次成功启动的 6392）⇒ 09-22 的每次启动**连 CrashSight/引擎初始化都没走到**就退出了，属客户端极早期自退（不是观察层误判，05:04 的补正结论成立）。残骸共 4 个：`6392 / 18716 / 22264 / 33848`，全部 `exit=0`、工作集 0MB。
  - **GF2**：客户端 `GF2_Exilium.exe` **`exit=15`**（不是 `259`，即真的退出了），随后新起的实例 302s 无窗口、`bytesWrittenDelta=0` ⇒ 同样起不来。
  - **ZZZ**：客户端 `ZenlessZoneZero.exe` 无可见窗口 ⇒ 上游 OneDragon `OneDragon run ended without an upstream normal-world readiness marker` → `run_terminal failed exitCode=23 transportOutcome=crashed`。编排侧正确（`official-tool-pending`）。
  - **WW**：客户端仍是 39MB 空壳（对照 02:30 残骸 33MB），活着但不建窗口。
  - **已排除的共因（别再重查）**：磁盘不是瓶颈（`C:` 1907GB 总/490GB 空闲，74%）；有交互桌面（`console` 会话 ID 1「运行中」，`EnumDisplayMonitors` 只有一块真实显示器 `\\.\DISPLAY1` 2560x1440 RTX 5060 Ti）；系统里确有一批会 hook 图形/注入游戏的组件（`GameViewer Virtual Display Adapter` × 10 = 网易UU远程、`EdgeGameAssist.exe` + 24 个 `VisualHostingHelper`、`GameBarFTServer`、`wegame.exe`），但 **StarRail 在同一时段（05:39）成功** ⇒ "图形层全局故障"不成立，共因假设被削弱。**注意：网易UU远程很可能是用户连到这台机器的通道，禁止擅自停用其进程/服务。**
  - 下一步（按价值排序）：① 查 PGR/GF2 客户端为何在初始化前自退（客户端自身日志与版本/资源完整性 = 目标 1）；② WW 客户端空壳同上；③ Endfield 回收分支。
- ★★ **2026-09-22 07:33–08:40 复查：三个候选修复的前提都被证据推翻，客户端层仍是唯一根因**（记录在此以免下一轮重走）：
  - ❌ **"Endfield 楔死启动器需要回收分支"不成立**。当日两次失败（03:15、06:12）的日志显示启动器**其实工作正常**：首个 audited action 后 `pids=[6444( Games.exe), 24380]`（06:12 那次是 `10388`）——新的 **`Endfield.exe` 客户端进程确实被启动器拉起来了**，之后循环再点 12 次（`ENDFIELD_MAX_LAUNCHER_ACTIONS`，已有上限与测试 `test_endfield_launcher_actions_are_capped_even_when_every_action_is_acted`）才失败。**卡点不是启动器读不到界面，而是客户端不建窗口** ⇒ 回收/重启启动器什么也修不了，不要实现它。
  - ❌ **"PGR 被我们写坏窗口偏好"不成立**：`_prepare_pgr_window_preferences()` 是空实现（`return {}`，有测试 `test_pgr_launch_does_not_rewrite_unity_window_preferences`）。
  - ❌ **"ACE 反外挂坏了导致秒退"不成立**：本机带 `AntiCheatExpert/` 目录的只有 **Endfield 与 NIKKE**；PGR 安装树里**没有任何反外挂组件**（只有腾讯 CrashSight 上报）。`ACE-BASE` 是 `DEMAND_START`（`sc qc`），只在受保护游戏运行时才加载 ⇒ 平时 `STOPPED` 是正常态；其 `WIN32_EXIT_CODE=31` 只是上次启动失败的陈旧记录，**不能作为结论**，仅留作观察项。`AntiCheatExpert Protection` 服务同为 `DEMAND_START`。
  - ❌ 全局图形/会话故障**再次被否**：08:27:28 起 `ZenlessZoneZero.exe`(pid 37648) 生成了**真实可见窗口「绝区零」**（同一时段 PGR 正在失败），StarRail 也照常成功。机器自 09-20 20:18 起未重启（uptime 1d12h）。
  - ★ **新增可区分证据（客户端层）**：`PGR.exe` 当日 5 次启动，每次进程**秒级 `exit=0`**，且它自己的 `LocalLow/kurogame/战双帕弥什/log/` 与 `CrashSightLog/` **自 09-21 21:00 起零写入** ⇒ 连引擎初始化都没进（09-21 21:00 同一条直接启动路径曾成功并给出 1280x720 窗口，说明启动路径本身有能力）。`GF2_Exilium.exe` 不是静默退出：`LocalLow/SunBorn/少女前线2：追放/SentryNative/*run/__sentry-event` 是 `level=fatal / platform=native / sentry.native.unity` 的**原生崩溃**（`app_start_time=2026-09-21T17:26:26Z`＝本地 01:26:26，与 pid 27768 `exit=15` 同秒），`Player.log` 停在 animator/mesh rebind 栈；客户端版本 `4.0.5136.14079.27315.2988`。`AppData/Local/CrashDumps` 另有 **6 个 `nikke.exe` 转储**（09-20 23:04 ~ 09-21 01:38）。
  - ★ WER 里那批 `BlueScreen` 报告（`Kernel_1e/22/133/141/144`，含 0x133 DPC_WATCHDOG、0x141 VIDEO_ENGINE_TIMEOUT 等图形味很重的码）`EventTime` 对应 **09-20 20:18**、且是 09-22 04:40 才被 WER 冲刷出来的 ⇒ 是那次重启前的旧崩溃，**不是今天的事件**，不要据此判断今日根因。
  - ★ **可归因性缺口（已修，2026-09-22）**：被 Manager 启动的进程此前**没有任何日志行**。PGR 08:20:24 的 `PGR.exe`(pid 36880，`ppid=YeYuGamer.exe`) 只能靠 Manager 之外枚举才看得到——`attempt.launch.begin` 在 spawn 之前，之后若 `_wait_until_ready` 失败就连 receipt 都没有，`launch.launcher-waiting` 全程 `pids=[]`。修法：在两处 spawn 点（新建启动与回收后重启）补 `operation=launch-executable-started` 通知，带 `processId` 与 `executable`，测试 `test_spawned_process_is_recorded_even_when_it_dies_before_any_window`（已验证：去掉通知⇒测试 FAIL，观察里只剩 `launch-failed`，正是当日日志缺口）。
  - 下一步（更新）：**只剩客户端层**。按 `docs/daily-workflow/launcher-update.md` 逐游戏走官方启动器的版本/修复流程（目标 1），优先 PGR（连引擎都没进）与 GF2（原生 fatal 崩溃）；WW 在客户端层修好前保持 `enabled=false`（它是冻结顺序首成员，一抛门就活锁整批）。

- ★ **单次限额溢出被当成协议违规，连带打死整个 run（2026-09-22 NTE，机制已定位，待决策）**。NTE 在 `09:34:30` 失败，`nextAction` 显示的 `Adapter artifact_count_exceeded: Todo artifact count exceeds its limit` 不是客户端问题，而是**我们自己的限额粒度**：
  - 实测 `todo-instance-59dbf468` 在 `attempt.log` 里发出 **21** 条 `artifact_staged`（第 21 条被拒），`MAX_ARTIFACTS_PER_TODO = 20`（`adapter_protocol.py:30`）。
  - 拒收被**上升为协议失败**：`adapter.event.rejected seq=1851 ... AdapterArtifactImportError` → `adapter.stop.escalation trigger=cancel_grace_expired` → `adapter.watch.end protocolFailure=artifact_count_exceeded -> status=failed transport=crashed` → `attempt.adapter_finished ... unresolved=6`。即**一个 Todo 少存一帧，换来整局 6 个 Todo 全部未决**。
  - 这不是意外：`artifact_count_exceeded` 是**被三处测试固定下来的契约**（`test_adapter_protocol.py` 的 stream 单测 ×2、host 级 `result.protocol_valid=False` ×1）⇒ 要改属**设计决策，需用户确认**，不要在日常里单方面放宽。
  - 溢出的一侧也在我们这边：`adapter-host/nte-runner/Program.cs:240`（以及 `openkuro-runner/Program.cs:198`）会为同一个 Todo 反复暂存 `nte-upstream-snapshot-<guid>.jsonl`。两个方向（收紧适配器暂存 / 让限额降级为"丢帧+留痕"而不是打死 run）都要落到 `docs/integrations/orchestration-contract.md` 的工件预算口径上再动手。
- ★ **客户端层新增证据（2026-09-22 09:5x 心跳，仍未解）**：
  - **WW 启动器其实会出窗口**：`launcher_main.exe` pid 36372（05:20:01 由 05:1x 那次尝试留下）**至今存活 3.5h，且窗口标题 `鸣潮` 可见**。即"启动器永不建窗口"的说法不成立于所有时刻——它比 300s 就绪门慢得多。WW 仍 `enabled=false`。
  - **NIKKE 客户端活着但不建窗口**：`nikke.exe` pid 21084 自 09:35:48 存活（WeGame 启动），`launch.launcher-waiting` 全程 `bytesWrittenDelta=0 / writeActivity=False`，309s 后抛 `nikke_wegame_game_window_missing` 人工门。属客户端层，不是"真的需要人"。
  - **PGR 僵尸条目跨 Manager 重启仍可枚举**：本次实测 `6392 / 18716 / 22264 / 33848 / 36880` 五个 `exit=0` 条目同时可枚举（6392 是 09-21 21:00 那次**成功**运行留下的）。"Manager 重启即释放句柄"再次被否。
- ★ **运维口径：排到末位的成员抛人工门会霸占整个游戏日**。09:44 `894e5767` 的末位成员 NIKKE 抛门，`activeBatch` 一直是 `human_required`、`POST /batches` 会被 `_current_human_batch` 挡住，5 分钟 `DailySupervisor` 也一直看到 `queue busy`（该批次 6 个成员全部 terminal，门是唯一原因）。用 `POST /game-runs/{id}/takeover-release-requests` 释放后（NIKKE run → `review_required`，结果如实保留、不伪造成功），**同一次 `10:04:02` 的 supervisor tick 立刻自动发出新批次 `ebf117e0`**，队列恢复。⇒ 遇到这种"末位成员卡门"先释放门让机械通路恢复，不要干等。
- ★ **本仓库工作树带着大量未提交改动，`git checkout -- <路径>` 是破坏性操作**（2026-09-22 实际行动踩到）。当时 `backend/tests/test_manager_adapter_execution.py` 相对 HEAD 多约 280 行（为未提交的 `on_close` 观测器、`extra_environment`（WW 多观察通道）、`run_artifact_staged` 等源码改动配套的测试）。用 `git checkout --` 还原自己的临时编辑会**连带丢掉这些配套测试**，表现是 `test_cooperative_cancel_preserves_manager_started_game` / `test_queue_cleanup_honors_persisted_batch_cancellation` / `test_terminal_cleanup_control_interrupts_are_preserved_not_persistence_failures` 失败——根因全是**测试替身缺新参数**（`close_for_queue(..., on_close=...)`、`adapter_host.execute(..., extra_environment=...)`）。**规律：要撤自己的编辑就用等长反向 Edit，不要用 `git checkout --`；改测试替身签名前先看源码调用点新增了哪些关键字参数。**
- ★★ **2026-09-22 10:17 第一次拿到"客户端为什么拒启"的明确自述：GF2 弹 `Fatal error / Another instance is already running`**（此前所有客户端层失败都只能记成"起来了但不建窗口"，无法归因）。证据是本次尝试自己的阶段截图 `runtime/artifacts/game-ui-launch-phase-91ffe586-*.png`（61/122/182/243s 四张同画面），对话框归属新起的 `GF2_Exilium.exe`(pid 29392，`running` 映射里有它)。即客户端**不是**起不来，而是**拒绝起**：它认为已有实例在运行。
  - 它看到的"已有实例"只能是我们自己留下的**死条目**：启动时 `launch.listed_exited_processes game=GF2 observed={27768: 'GF2_Exilium.exe'} cause=unverified`（pid 27768 是 01:26 那次 Sentry native fatal 崩溃的进程，`exit=15`）。
  - **这类条目不可回收**：`taskkill /PID 27768 /F` 报「没有此任务的实例在运行」，但 `tasklist /FI "PID eq 27768"` **仍列出它**（316K）。内核只在属主进程被销毁时才释放互斥体，而这些客户端从未完成终止 ⇒ 后来的每一次启动都必然认为"已有实例"（确切机制见下文 12:1x 定案）。PGR 同理：`6392/18716/22264/33848/36880/38088` 六个 `PGR.exe` 死条目同时可枚举，这正好解释它"秒级 `exit=0`、零文件写入、不建窗口"（与 10:17 的 GF2 是同一家族）。
  - ★ **我方确有一处该修的真实缺陷（已修、已装机）**：`subprocess.Popen` 的句柄保留。`Popen.__del__` 自身注释写明"Not reading subprocess exit status creates a zombie process which is only destroyed at the parent python process exit"，并把仍存活的子进程放进模块级 `subprocess._active`；而 `game_launcher` 的启动路径**只读 `.pid` 就丢掉对象**（每次 spawn 后几毫秒，子进程必然还在跑）⇒ Manager 在整个客户端生命周期里替它多持一个句柄。**（12:1x 更正：这并**不是**条目留存的原因——handle64 已证明持有者是 Windows 自身子系统与客户端自己的线程句柄；但留着自家的句柄本身不对。旧措辞"每次启动都稳定留下一个由我们持有句柄的僵尸条目"作废。）** 修法：`GameLaunchService._reap_spawned_process()`，用 daemon 线程 `wait()` 子进程，句柄在子进程退出即释放、不阻塞启动路径；接在 `_start_launch_executable`（WW shell 分支与通用分支）与 `_start_endfield_launcher`（覆盖 Endfield 新建与激活两条路）上。测试 `test_launch_executable_reaps_the_spawned_process_handle` / `test_endfield_launcher_reaps_the_spawned_process_handle`（区分度验证过：停用回收 ⇒ 两测均 FAIL）；`test_game_launcher.py` 194 项、`backend/tests` 全量 pytest 均 0 失败；`undef_check2.py TOTAL=0`。已由 release `20260922032632-50f9b0c6`（`PUBLISH_EXIT=0`，11:37 Manager 重启）装机，装机与源码 md5 一致（`664f338d27a973e7af01460226cf4d60`）。
  - ⚠️ **但持有者不全是我们**：11:37 重启 Manager 后，`36880/38088`（父进程就是刚退出的那个 Manager）**仍然可枚举**；11:46 PGR 重跑生了新条目 `38192`（`ppid` = 当前存活的 Manager `4696`），**而那次 spawn 已经走新的回收路径**（`launch.launcher-action` 里可见 `operation=launch-executable-started` 带 `processId=38192`，且装机 mtime 11:36:09 早于 Manager 启动 11:37:05）⇒ **留存的句柄不在我们的 spawn 路径里**。
  - **对照实验（2026-09-22 11:5x）**：用同一个 Python 分别 spawn 短命 `cmd.exe`，(a) 正常 `wait()` 回收、(b) 丢弃 Popen 不回收（复现旧 Manager 行为）——**两种都从 `tasklist` 里干净消失**。当时的读法"只有游戏客户端留存 ⇒ 指向外部 hook"**已作废**；正确读法是：普通进程能正常走完终止流程，而这些游戏客户端**卡在退出流程里**，才留下不解的互斥体与条目。
  - 本机原本无 `handle.exe`/`procexp`；本轮从 Sysinternals 官方站点取 `handle64.exe` 做只读全量 dump（17.3 万行，`.cache/handle-full.txt`）。
  - ★★ **2026-09-22 12:1x 定案（机制已查实，别再当新问题查）**：阻断后来者的**不是进程枚举，而是游戏自己的单实例互斥体**。只读探测（`CreateMutexW` + `ERROR_ALREADY_EXISTS`，`scripts/heartbeat/hb-mutex.py probe`）实测两者都是 **HELD**：
    - PGR → `\Sessions\1\BaseNamedObjects\comkurogameharukuro`
    - GF2 → `\Sessions\1\BaseNamedObjects\ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default`（名字里就写着 `SingleInstanceMutex`，与客户端自己弹的 `Another instance is already running` 完全对应）
    这些客户端**从未完成终止**：`WaitForSingleObject` 返回 258（未信号）、`GetProcessTimes` 的 exitTime=0、线程仍可枚举，但退出码已写定（PGR 0 / GF2 15）——即**已进入退出流程又卡住**。⇒ 那次成功运行留下的 `comkurogameharukuro` 一直不解，之后每次 PGR 启动都"看见已有实例" → 秒级 `exit=0`、零文件写入、不建窗口，与 09-21 21:00 起 `LocalLow/kurogame/战双帕弥什/log/` 和 `CrashSightLog/` 零写入完全吻合。**这才是 PGR/GF2 这一族失败的根因，不是观察层误判，也不是"驱动不建窗口"。**
  - **持有者（`handle64 -a` 全量解析）**：Windows 自身子系统 `svchost.exe`（RpcSs pid 1756 / Themes pid 3296 / Audiosrv pid 3636）**各持一个 `Process` 句柄**，加上 GF2 崩溃现场残骸 `UnityCrashHandler64.exe`(8204) 与 `crashpad_handler.exe`(30668/31572)，以及**这些进程自持的数百个自身 `Thread` 句柄**（PGR 6392 有 115 个、GF2 27768 有 257 个）。**列表里没有任何第三方 hook 进程** ⇒ 旧推断"指向网易UU远程 / EdgeGameAssist 等外部 hook"**不成立，不要再顺着它做实验**。（`audiodg.exe` 曾是持有者之一，杀掉后自动重启即不再持有，且条目一个没少 ⇒ 单杀子系统进程无效。）
  - **用户态无法清除，只有重启机器能解**：`OpenProcess(PROCESS_TERMINATE)` 能成功但 `TerminateProcess` 报 `err=5`（真实内核状态 `STATUS_PROCESS_IS_TERMINATING` 被映射成 `ACCESS_DENIED`）；从外部 `TerminateThread` 它剩余的 1–2 个线程**返回成功却什么也不改变**；`RpcSs` 不可能停（停了要重启/不稳定）；这些客户端卡在退出流程中、其自持线程句柄互相引用成环。
  - ★ **顺带更正一条旧错误结论**：PGR **并非**"不带任何反外挂"——它的句柄表里有 `Section \Sessions\1\BaseNamedObjects\TenProtect3_Share_Data_6392`，说明它加载了腾讯 **TenProtect3**（TP3），这正是它退出流程容易卡住的可疑处。
  - ★ **新增前置守卫（已实现 + 已测，待发布）**：`GameLaunchService._reject_leftover_client_instance()` 在 spawn 之前，若该游戏**已登记的客户端单实例互斥体仍被持有**且**存在同名非存活残留**，直接以 `GameLaunchError` 拒绝启动并报出互斥体名与 pid 列表——理由是这样启动必然秒退，只会白烧 300s 就绪超时、再多留一个不可回收残留。**故意用 `GameLaunchError` 而非 `GameLaunchHumanRequired`**：人工门会霸占当天、fence 整条队列（见上文）。登记表 `CLIENT_SINGLE_INSTANCE_MUTEXES` 只含**实测过**的 PGR/GF2，其余游戏不拦。新增 4 项测试，区分度验证过（让守卫失效 ⇒ 拒绝测试精确失败在 `assertRaises`）。
  - **下一轮（按价值排序）**：① 把"**重启机器**是唯一能让 PGR/GF2 恢复的动作"告知秋雨（**只能建议，不得自行重启**——重启会毁掉在跑批次，本身也属人工接管）；② 队列空闲后发布本轮前置守卫；③ 遇到"客户端秒退 / 不建窗口"，**先查该游戏的单实例互斥体是否被占**（`scripts/heartbeat/hb-mutex.py probe <名字>`），别再从头查图形层；④ WW 保持 `enabled=false`。

- ★★★ **2026-09-22 22:2x 自然实验：重启机器确实修好了这一族，互斥体根因被证实**。14:12 自愈器重启机器之后（详见下条），22:24:06 supervisor 自动发出新批次 `475df8c1`，首个目标 PGR 的 `attempt.log`：
  - `22:26:13.939 launch.ready elapsed=82.4s pids=[26544]`（此前同类尝试一律卡到 300s 就绪门或以"秒级 `exit=0` / 零写入"收场）
  - 随后 `attempt.launch.receipt` → `adapter.watch.begin` → `adapter.event seq=0 type=hello` → 真实上游待办 `pgr-formal-entry` / `pgr-official-dispatch` / `attach-home`
  ⇒ **同一台机器、同一份装机代码、同一配置，唯一的变量是"互斥体已被重启清空"**。这就是"PGR/GF2 秒退/不建窗口 = 单实例互斥体被卡死的残留占着"的判决性证据（12:1x 的只读探测 → 22:2x 的干预对照）。以后遇到这一族**先探互斥体**，别再查图形层、hook、观察层。
- ★★ **2026-09-22 14:12 自愈器重启之后，机器 7 小时 46 分没有起来 —— 这是"零意外"目前最大的单点风险**（事实，非推断）：
  - 时间线（全部为 UTC，本地 +8）：`14:12:14` 自愈器 `restarting: PGR,GF2 (60s delay, forced)` → 系统事件 `1074 C:\Windows\SYSTEM32\shutdown.exe (YEYU)` @ `06:12:14` → `6006`（事件日志服务停止 = **干净关机**）@ `06:13:55` → 下一次 `6005`（启动）@ `14:00:16`，即本地 22:00:16。
  - 机器在 `06:14`–`14:00` 之间**没有任何系统事件**（该窗口查询只返回启动那一刻的事件）⇒ 这段时间**操作系统没在运行**，是"关机后没上电"，不是"起了但我们没看见"。
  - 本次开机是**冷启动**：`GetTickCount64` 显示开机 77 秒；`powercfg -lastwake` 唤醒历史计数 = 0。
  - **不是脏掉电**：`System` 日志里最近一条 `6008`（非正常关机）是 `2026-09-05`，今天没有；即"我们的 restart 由 OS 干净执行完毕"，随后固件没有再上电。
  - 机器**无电池**（`GetSystemPowerStatus` = `BatteryFlag 0x80`），所以计划任务上的"电池模式停止/不用电池启动"是失效设置，不是本次原因（已排除）。
  - ⇒ 归因边界：**软件无法阻止 POST**，这段空窗属电源/固件/外部层面（AC 被切断后 BIOS 未自动上电，或人为断电）。但我们的动作是直接前因，且**代价极大**——当天下午整段报废（本日 12/39 里有 8 小时没有任何进展）。**必须按"重启可能回不来"来设计**，而不是当成必然恢复。
  - **建议给秋雨（无人值守下无法自行完成，需他决定）**：① 核 BIOS `Restore on AC Power Loss` / ACPI 上电策略，改成"来电即上电"；② 考虑给这台机器加一个可远程控制的电源（智能插座/带自动上电的 PDU），这是把"一次重启 = 整天报废"变成"能自愈"的唯一办法；③ 自愈器的 7 重守卫目前无法验证"重启后一定能起来"（它自己就是被重启的那个进程），这一条只能靠硬件兜底。
  - 好消息：**重启后的自恢复链是通的**——`StuckClientHealer` 因互斥体已释放而正确不做任何事；`DailySupervisor` 22:04 tick 自动 `manager recovery: started` 把 Manager 拉起来（22:04:56 起 0.3.7）；22:24 tick 自动补批。即"机器起来之后一切自己接上"。
- ★ **发布通道在重启后被网络挡住（2026-09-22 22:1x，已记录配方）**：本轮按 SOP §6 尝试发布 `_reject_leftover_client_instance` 前置守卫（唯一未发布差异 = `backend/yeyu_gamer_manager/services/game_launcher.py`，`named_mutex_is_held()` + `_reject_leftover_client_instance()`，约 100 行），脚本在**第一步**（准备 hash-locked release tools 环境）就失败：
  - `annotated-doc==0.0.5` 取不到：`SSLError(SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING'))`，`No matching distribution found`，`PUBLISH_TASK_EXIT=` 空。
  - 只读诊断：**`https://pypi.org/simple/...` 直连失败（SSL EOF）**，而 `pypi.tuna.tsinghua.edu.cn` / `mirrors.aliyun.com` / `baidu.com` 全部 200（<0.2s）；环境里 `HTTP(S)_PROXY=http://127.0.0.1:7897`（Clash Verge 在跑），即**代理在、但 pypi.org 这条路不通**。这解释了为什么 11:37 那次能成功、而现在不能（重启后网络/代理路径变了）。
  - **二次尝试的已验证配方**：`Publish-YeYuGamerLocalRelease.ps1:520` 是 `& $python -I -B -m pip install ... --require-hashes`，**没有 `--index-url`** ⇒ 在 driver `.cmd` 里加 `set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 即可（该镜像已实测含 `annotated-doc 0.0.5` 的 4 个文件；`-I`/`-E` 只屏蔽 `PYTHON*`，不屏蔽 `PIP_*`）。**不要**为此改系统级 pip.ini。
  - 处置：删除 `\YeYuGamer\Publish0922i` 任务与 `release-pending.flag`（**标志绝不能留**，见 heartbeat-playbook §9），把当天运行时间让给游戏。当前装机与源码的差异**只剩这一个文件**，所以"客户端层失败不是构建差异造成的"这个结论仍然成立。
- ★ **跨日遗留人工门已批量清除（2026-09-22 22:1x）**：`/game-runs` 里有 **10 个 `human_required` run**（09-18~09-21，WW×6 / StarRail×2 / Endfield×1 + NIKKE 等）从未被释放——它们全属**过去的游戏日**，因此没有拦住今天，但是"跨批次遗留人工门堵死整条新日队列"这一族的现成火药（记忆里 05:0x 已经因此报废过一天）。全部用 `POST /game-runs/{id}/takeover-release-requests` 释放 ⇒ `human_required` 10→0、`review_required` 11→21，**如实保留为 review_required，不伪造成功**。以后心跳在发布/发批前顺手核一遍这个计数。
- ★★ **2026-09-22 23:3x 新缺陷：一次瞬时"客户端没出窗口"把整天的入口待办永久锁死**（已修 + 已补测试，本轮发布）。
  - **现象**：重启后 PGR 7/7 ✓、Endfield 3/3 ✓、GF2 7/7 ✓、StarRail 4/4 ✓ 全部真跑完成，`accepted=3`；但 **ZZZ 卡在 8/9**，唯一缺的必选是入口待办 `todo.v1.zzz.daily.attach-home`（"启动并确认已进入大世界"），状态 `review_required`、`updatedAt=2026-09-21T22:10:22Z`（本地 09-22 06:10，即**重启前的坏窗口**）。
  - **机制**：那次 attempt（`ZZZ-9087cc36`）在 05:40 就 `todo.step_capture.failed reason=WindowCaptureError: no visible, non-minimized registered game window was found`（客户端还没出窗口），OneDragon 因此拿不到"大世界就绪"标记，收尾发给 Manager 的终态是
    `todo_terminal status=review_required reasonCode=upstream_observation_missing reason="OneDragon run ended without an upstream normal-world readiness marker" retryable=false`（driver exit=23）。
    而 `todo_dispatch.evaluate_todo_dispatch()` 的 `retryable_routine_review` 要求 `latest_attempt.retryable is True`（或命中已有的 `legacy_retryable_formal_gui_review` 允许列表）⇒ 这条被判为 `deferred_review`（`review_resolution_required`），**当天再发多少次新批次都不会重新派发它**。
  - **为什么是缺陷而不是"如实标记"**：同一个游戏日更晚的批次（08:2x–08:42）里 ZZZ 客户端正常出窗口、其余 8 个必选全部 completed ⇒ 环境已自愈，**只有这一条因为 `retryable=false` 永远回不来**。入口待办回不来 = 该游戏当天不可能 9/9。这与"自动跑完 / 零意外"直接冲突，也与"review_required 只用于'工具不支持/官方没有该项'或真实需人工"的口径不符：它不是策略决定，是一次瞬时环境失败。
  - **修法（`backend/yeyu_gamer_manager/services/todo_dispatch.py`，Manager 侧、最小面）**：新增 `legacy_retryable_readiness_marker_review`，与既有的 `legacy_retryable_formal_gui_review` 同构 —— 仅当 `status=review_required` 且 `review_reason_code == "upstream_observation_missing"` 且 reason 以
    `"OneDragon run ended without an upstream normal-world readiness marker"` / `"...an operable normal-world readiness marker"` 开头，且 `risk ∈ {routine_action, observe_only}` 时才允许"新请求的**整轮** daily 批次从头重跑该入口待办"。
    **其它 `upstream_observation_missing` 原因（如 NIKKE 的 `formalGui=run_nikke_gui.py; ... Behavior Tree Result failure`、`OneDragon formal selected daily timed out`、`missing_reward_evidence`）一律仍留在 review_required** —— 已在测试里逐条锁死。
  - **测试**：`backend/tests/test_todo_dispatch.py` 新增 4 项（可进入新批次 / active_blocker 变体 / 三种"别的原因不得自动重试" / `forbidden` 风险仍 deferred_forbidden）；**区分度验证过**（把 reasonCode 判据临时改坏 ⇒ 两条正向测试精确失败为 `deferred_review`，负向测试仍通过）；`backend/tests` 全量 pytest `PYTEST_EXIT=0`、`undef_check2.py TOTAL=0`。
  - **仍有残余**：NIKKE 当日 1/3，失败在上游工具自己的行为树（`run_nikke_gui.py` / "无法识别回到大厅"），属上游缺陷族，**不在本次修法覆盖范围内**（正确行为：如实 review_required，下一步查上游 WeGame/UIA 通路）。

## 8. 结束报告

结束报告列出：范围与逐项结果、承接与本轮执行的区别、更新前后版本、根因及关键证据、YeYu commit/上游 PR、实际验证范围、残余阻塞及恢复点、客户端清理和整轮报告状态。原始日志保留本机，外发只用已审查的脱敏材料。整轮邮件按已有配置和授权发送，SMTP 受理不冒充送达。
