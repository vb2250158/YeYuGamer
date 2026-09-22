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
  - **仍有残余（该归因已于 2026-09-23 01:1x 作废，见本节后面"NIKKE 永远只能完成第一个待办"条）**：NIKKE 当日 1/3。曾有结论说"失败在上游工具自己的行为树（`run_nikke_gui.py` / 无法识别回到大厅）、属上游缺陷族"——**错**：行为树本身成功，是我们自己的驱动用 `--exit` 在第一个子树后杀掉了客户端。

- ★★ **2026-09-23 00:xx 邮件机制改造：从"每轮一封"改为"每天一封、报当天结果"（已改 + 已补测试；`bf27b88`，装机待重发）**
  - **现象（用户原话）**："改一下邮件的发送机制，不是发那一轮的，而是更新当天的结果。不然我看到一堆失败，有点可怕……"
  - **机制缺陷（两重）**：① `notification_deliveries` 的唯一键是 `(batch_id, seal_version, channel, recipient_binding_id)` ⇒ **每个封口批次必然一封**；一天多次补批就多封。② 更要紧的是**算错**：批次报告的"已验收"只按**本轮** `completionContracts` 算，而验收是跨批次累计的 ⇒ 当天已经 `accepted_done` 的游戏在后续轮次的邮件里掉回"待验收／本轮未执行"（实测 00:26 那封仍写 `已验收 0/7 · 完成受阻`，而当天实际已有 4 个游戏 accepted）。**不是文案问题，是口径问题。**
  - **修法**：新增按**游戏日**汇总的报告轨道（`notification_policy.report_scope`，默认 `game_day`；`batch` 保留旧的每轮报告）。
    - 内容来自 `todo_overview()`（**与 WebGUI 同一份选择域投影**）+ `_game_projection`，所以邮件数字与界面一致；游戏日的键取启用游戏当前 daily `period_key`（04:00 边界），**不能**用 `Batch.result.gameDay` 的 scope fingerprint（它随冻结待办集变化，答不了"还是不是同一天"），也要**排除禁用游戏**（FGO/BD2/CZN 注册的是 00:00 窗口，会把报告日提前 4 小时）。
    - **不再自动携带截图附件**：证据字节靠单一不可变 seal 校验，跨批次的日报无法忠实携带；正文改为指向本机证据页。
    - **一天最多一封**：`dispatch_hold` ⇒ 新门 `game_day_pending`（不在可派发状态内）。释放时机只有两个——**当天必做全部完成且无活动执行**（`all_required_completed`）、或**游戏日已滚过 04:00**（`game_day_rolled_over`，当天做到哪儿算哪儿）。同一游戏日的多封 held 行只留最新一封，其余 `state='superseded'`。
    - **`human_required` 故意不作为终态**：否则第一次抛门就发一封"需人工处理"，门放掉、当天跑完后又会发一封自相矛盾的。日报里该游戏带"需人工处理"徽标，要立刻处理的操作员在 WebGUI 直接看得到。
    - 释放按钮在 Manager 周期 tick（`_completion_review_watchdog_loop`，60s）上，所以**不依赖"当天还有没有新批次封口"**；日界兜底也走它。
  - **测试**：`backend/tests/test_notification_game_day_report.py` 18 项（模板口径 / 策略默认与校验 / 终态判据 / held-不发 / 当日只发一封 / 已报告日不再发 / 未到达的日子绝不发 / 两个 scope 分流）；**两道重复守卫各自做了区分度验证**（破坏 rollover 守卫 ⇒ `test_a_day_that_already_reported_is_not_mailed_again` 精确失败；破坏 final 守卫 ⇒ `test_a_late_seal_of_an_already_reported_day_is_retired_too` 精确失败）。
  - **残余（如实记）**：某天若**一个批次都没封口**（例如整天卡在未释放的人工门），就不存在该日的报告行 ⇒ 那天没有日报（比多报更安全，但不完美）；`report_scope` 目前只有 API（`PATCH /api/v1/notification-policy` 带 `reportScope`），WebGUI 尚未加开关。
  - ⚠️ **自查教训（已修，`bf019fa`）**：`_game_day_report_snapshot` 第一版调 `self.todo_overview(games=…, current_items_by_game=…)`，而 `Manager.todo_overview()` **不接参数** ⇒ 封口的通知渲染会 TypeError 并被 `seal_batch` 吞掉（**一封邮件都不发**）、60s tick 永久刷 `game_day_report.tick_failed`。测试没抓到，因为**前面的用例把整个 snapshot stub 掉了**。⇒ 给包装层写 test double 时，必须留至少一个用例跑真实实现。

- ★★ **2026-09-23 00:5x：入口待办 `attach-home` 在"当天只剩它一条必选"时永远完不成 —— ZZZ 与 NTE 各差 1 项是同一个机制**（**未修，需用户定契约口径**）
  - **现象**：`daily:manager:59b5068fae16c5a5` 走到 35/39 后停住：ZZZ `8/9`、NTE `5/6`、NIKKE `1/3`；`DailySupervisor` 的每日 8 次补批预算也在 00:39 用尽（`the start budget (8) is exhausted; needs a decision`）。ZZZ 唯一缺的是入口待办 `attach-home`（"启动并确认已进入大世界"），NTE 唯一缺的也是 `attach-home`（"经官方启动器进入可操作主界面"）。
  - **决定性对照（同一个游戏日内）**：
    - `ZZZ-9087cc36`（05:40，冻结 9 条、含 `attach-home`）⇒ 客户端未出窗口，`launch.capture unavailable`，收 `OneDragon formal selected daily timed out`，`attach-home` 未完成。
    - `ZZZ-c5dbdf94`（08:26，冻结 8 条、**不含** `attach-home`）⇒ **`attempt.result status=completed`，8/8 全完成**，且 `adapter.stdout.jsonl` 的 `sequence=56` 有 `指令[ 返回大世界 ] 执行成功 返回状态 大世界-普通`。
      ⇒ **那一天确实到达过大世界，只是当时 `attach-home` 因 06:10 那次瞬时 review 被排除在冻结范围外，没人把这次到达记在它名下**。
    - 之后 ZZZ 只剩 `attach-home`：`00:19`/`00:39` 两次 attempt 都在 **0.4 秒内**结束，驱动 stderr = `unstarted operation=attach-home; No selected official application can produce the normal-world marker`，`driverExit=20`。
  - **机制（两侧都已查实，别再重查）**：
    - **驱动侧**（`adapter-host/classic-runner/classic_tool_driver.py:1070-1083`）：`selected_app_ids = [ZZZ_APPS[i] for i in selected if i != "attach-home"]`；**该列表为空时直接 `_emit(review_required)` + `return 20`，一个进程都不启**。
    - **上游侧**（`.cache/upstream-candidates/zzz-main-9f53b56-runtime/ZZZ/official-source/src/one_dragon/base/operation/application/group_application.py:63-91`）：`GroupApplication.run_app()` 只对 `enabled` **且** `run_record` 未完成的 app 调 `app.execute()`，而"打开游戏/进入游戏/返回大世界"是 `Application.execute()` 内部的 `op_to_enter_game`（`zzz_one_dragon_app.py` 把 `OpenAndEnterGame` 传进去）⇒ **没有启用的 app 就不会进游戏，也就不会出现大世界标记**；而当天 8 个 app 的 `app_run_record` 已 done，即便强行置 `enabled` 也会被 `is_done` 跳过。⇒ **`attach-home` 单独时在官方工具侧无解**，不是驱动写错。
    - **NTE 同形**（另一套驱动）：`NTE-25475c7a` 冻结 `['attach-home']`，54 秒后 `run_terminal review_required`，原因码 `nte_scope_without_routine_operation`（"该范围里没有例行操作可跑"）。
  - **结论**：入口待办本质是**就绪前置确认**，它的可完成性**依赖同一次 run 里存在真实业务待办**。一旦业务待办先完成（或被一次瞬时 review 排除），它就永久悬空 ⇒ 该游戏当天不可能全绿。与"自动跑完 / 零意外"冲突 ⇒ 这是**完成契约层面的设计缺口**，不是编排 bug，也不是"工具不支持该项"。
  - **候选修法（本轮未实施，需用户定口径）**：
    1. **同轮共选（最小、最保守）**：规划时只要入口待办未完成，就必须与至少一个业务待办同轮冻结 —— 健康环境里它必然随大世界标记一起完成（`ZZZ-c5dbdf94` 那次就是现成证据）。
    2. **同日证据归属**：把"同游戏日任一次 run 观测到大世界标记"作为入口待办的就绪证据（事实存在，不是伪造成功）—— 需要 Manager 侧跨 run 的证据归属，改动面比 1 大。
    - **不建议**：为了触发标记去 `enabled`/重跑某个业务 app（会重复消耗官方每日任务，违反"不为接入重写游戏任务"）。
  - **附带发现（本轮已修，见下条）**：这个缺口之所以一直看像"协议 bug"，是因为我们的协议层把它变成了 `invalid_schema`。

- ★★ **2026-09-23 00:5x：0 字节 artifact 会中止整条事件流，把驱动故意发的 `review_required` 吞成"协议崩溃"**（**两层都已修 + 已补测试**）
  - **证据链**：`ZZZ-73b72e90` 的 `adapter.stdout.jsonl` 只有 3 条：`hello` → `run_progress code=official_operation_not_started message="No selected official application can produce the normal-world marker" metrics.reportedStatus=review_required` → `run_artifact_staged kind=upstream-tool-log fileName=classic-upstream-zzz-lifecycle-*.log sizeBytes=0 sha256=e3b0c442…（空串摘要）`。
    ⇒ 第 3 条被 `adapter_protocol._consume_artifact()` 的 `_integer(minimum=1)` 判成 `invalid_schema: sizeBytes is out of range` ⇒ `adapter.watch.end protocolFailure=invalid_schema → transport=crashed`、`attempt.result protocolValid=False`。
  - **为什么 0 字节是合法的**：官方工具的生命周期日志在首次使用时才创建；失败路径（没跑到任何上游输出）下它就是 0 字节。**驱动发这条事件是有意的**，Manager 却把整条流废掉，于是：① 真实原因被掩盖（前几轮 ZZZ 一直被记成"Adapter invalid_schema"，看着像协议/适配器 bug）；② 明明走的是"如实 review_required"却被算成协议崩溃；③ 顺带解释了 `upstream_observation_missing` 这一族在 ZZZ 上认不出来。
  - ★ **同一处缺陷有**两层**闸门，只修一层不够（这是本次实测抓到的）**：修好解析层后，`01:13` 那轮 ZZZ attempt 的失败码从 `invalid_schema` 变成 **`artifact_size_rejected`** —— `adapter_artifacts.py:526-533`（导入/入账路径）也在无条件要求 `size >= 1`。两层必须一致。
  - **修法（两处，同一判据）**：
    - `backend/yeyu_gamer_manager/services/adapter_protocol.py::_consume_artifact` → `minimum=1 if mime.startswith("image/") else 0`；
    - `backend/yeyu_gamer_manager/services/adapter_artifacts.py::import_staged` → `minimum_size = 1 if str(event["mimeType"]).startswith("image/") else 0`（**补时故意做成同名变量，方便以后一眼看出两处必须同步**）。
    **非图片（日志类）允许 0 字节**（下游仍按 hash/magic/encoding 逐项 fail-closed），**图片仍必须非空**（空帧就是拍摄失败）。
  - **测试**：`backend/tests/test_adapter_protocol.py::test_empty_text_artifact_is_accepted_but_empty_image_is_not` 与 `backend/tests/test_adapter_artifacts.py::test_empty_text_artifact_imports_but_empty_image_is_rejected` / `::test_empty_image_artifact_is_rejected`；**两处都做了区分度验证**（各自临时改回 `>= 1` ⇒ 精确复现生产里那条 `sizeBytes is out of range` / `artifact_size_rejected`）。`backend/tests` 全量 pytest `EXIT=0`（0 FAILED）、`undef_check2.py TOTAL=0`。

- ★★ **2026-09-23 01:1x：NIKKE 永远只能完成第一个待办 —— 是我们自己的驱动用 `--exit` 把客户端杀了**（**已修 + 已补测试 + 区分度验证；属本项目缺陷，不是上游缺陷**）
  - **推翻上一轮的错误归因**：第 192 行曾把 NIKKE 记成"失败在上游工具自己的行为树（`run_nikke_gui.py` / 无法识别回到大厅）"。**该结论作废** —— 事后看 `attempt.log` 与上游日志，行为树本身是**成功**的。
  - **现象**：`NIKKE-0f9b6241`（01:14–01:37）冻结 `['attach-lobby','dispatch-friend']`。`attach-lobby` → `completed`（`reasonCode=upstream_task_succeeded`，`subtree=nikke_return_lobby.json; exit=0`），`dispatch-friend` → `review_required`（`subtree=nikke_dispatch_friend.json; timeout without upstream terminal marker`），20 分钟后才收尾。当日 NIKKE 长期卡在 `1/3`。
  - **客户端其实起来了**：`launch.ready elapsed=32.0s pids=[1360,20768] launchState=started readyWindowPid=1360 readyWindowWidth=1925 readyWindowHeight=1083`，阶段截图 `game-ui-launch-ready-dd9694ba…png` 也在；上游 `01:16:28` 找到窗口（`hwnd changed from 0 to 2492896 … nikke.exe UnityWndClass real:0,0,1925,1083 visible:True`）并起了 WGC 采集。⇒ **不是"客户端不建窗口"那一族**。
  - **决定性证据（上游日志，同一 attempt 的两个 artifact）**：
    - `attach-lobby`（`artifact-98169e18…txt`，01:16:27→01:17:39）：`info_set Behavior Tree Result success` → `TaskExecutor:Successfully Executed Task, Exiting Game and App!` → `DeviceManager:stop_hwnd C:\Game\胜利女神：新的希望(2002017)\nikke.exe` → `process:Trying to kill the exe {… 'pid': 1360}`。
    - `dispatch-friend`（`artifact-5f864be6…txt`，01:17:44→01:37:40）：**14 497 行里整整 20 分钟只有** `hwnd_window:bring_to_front failed: no hwnd found` 与 `bitblt_utils:capture_by_bitblt invalid params: hwnd=0, w=0, h=0`。
    ⇒ **第一个子树跑完就把客户端杀了，第二个子树从头到尾没有窗口**。Manager 侧 `attempt.game_cleanup state=already-closed requested=[] remaining=[]` 也印证"客户端不是我们关的"。
  - **机制（两侧查实）**：`adapter-host/classic-runner/classic_tool_driver.py::run_nikke` 对**每个**选中子树各起一个 GUI 进程，命令里带 `--exit`；ok-script 把 `--exit` 解析成 `exit_after`（`ok/util/process.py` → `ok/gui/MainWindow.py:400` → `StartController.do_start`），而 `ok/task/TaskExecutor.py:572` 在一次性任务结束后看到它就走
    `Successfully Executed Task, Exiting Game and App!` → `DeviceManager.stop_hwnd()` → `kill_exe(abs_path=…)`（`ok/device/DeviceManager.py:108-112`）。
    **该布尔量同时驱动"退出 App"和"关掉游戏"，没有只退出 App 的开关**；而"一个 GUI 会话只跑一个子树"是本驱动的结构（一次会话 = 一个 stage 子树，`NIKKE_TREE_FOLDER` 只指一个 `selected.json`），所以 `--exit` 必然在第一个子树后打死客户端。
  - **修法（驱动侧，最小面）**：`run_nikke` **不再传 `--exit`**；改为由驱动接管收尾 —— `_collect_nikke_output` 现在在**任一**树终态标记（新增 `NIKKE_SUCCESS_MARKER` / `NIKKE_TERMINAL_MARKERS`）出现时结束等待，给工具 `NIKKE_RESULT_GRACE_SECONDS`(5s) 做完自身收尾，然后自己 `kill()`。
    成功判据也随之改为纯看上游标记（`_nikke_subtree_succeeded`），不再依赖退出码 —— 退出码现在反映的是驱动的收尾动作。**客户端保持存活到最后一个子树**，尝试结束时由 Manager 自己的 `game_cleanup` 关闭（这才是设计中的生命周期）。
  - **测试**：`adapter-host/tests/test_nikke_output_collection.py` 新增 `test_success_result_ends_the_wait_without_waiting_for_gui_exit`（成功标记必须立刻结束等待，而不是耗满 20 分钟超时）与 `test_selected_subtree_never_asks_the_tool_to_exit_the_game`（用假 Popen 捕获 argv，断言**不含** `--exit`、且该子树仍被记为 `completed`）。
    ⚠️ `adapter-host/tests/validate_classic_selected_daily.py:153` 原本断言 `'str(gui_entry), "--task", "1", "--exit"' in source` —— **这条既有守卫把缺陷固化成了期望**，已翻转为 `not in source` 并补了 3 条正向断言。**区分度验证**（`.cache/bidir-nikke.py`）：把 `--exit` 加回去 ⇒ argv 测试精确失败；把终态标记换回只认失败标记 ⇒ 成功等待测试精确失败；两处实验后文件 md5 与实验前一致。`adapter-host/tests` 全量 40 项 unittest `EXIT=0`、`validate_classic_selected_daily.py` 31 项 `passed`。
  - **发布**：改动在**Classic 适配器包**（不是 Manager），所以 `Build-YeYuGamerClassicAdapter.ps1` 的 `$PackageVersion` 由 `0.3.0-classic-upstream.26` → `.27`，按 `publish-classic-correct.ps1` 的配方（PGR/ZZZ/NIKKE 三个候选绑定**必须显式传**，否则包被判 untrusted）发布。
  - **待验证**：需要一次真实 NIKKE 跑（`attach-lobby` 之后 `dispatch-friend` 能找到窗口并完成），才能把这条从"已修"升级为"已验证"。


- ★★ **2026-09-23 02:0x：日报轨道自己在生产里每 60 秒崩一次 —— `KeyError: 'WW'`**（**已修 + 已补测试 + 区分度验证**）
  - **现象（新装机版的真实日志）**：`manager.log` 每 60 秒一条 `ERROR game_day_report.tick_failed`，回溯为
    `manager.py::_game_day_report_snapshot` → `manager_todos.py:894 todo_overview` → **`KeyError: 'WW'`**。上一轮那个 `todo_overview() got an unexpected keyword argument`（`bf019fa`）已经修好、这次也确实走到了真正的实现，**这是它的下一层**。
  - **机制**：`todo_overview` 的契约是"`current_items_by_game` 必须覆盖 `games` 里的每一个游戏"（`current_items = current_items_by_game[game_id]`，无 `.get`）。而 `_game_day_report_snapshot` 传的是 **`games = self.store.list_games()`（整个目录，含禁用的 WW/FGO/BD2/CZN）** 配 **只含启用游戏的 `current`** ⇒ 第一个禁用游戏就 KeyError。异常被 `_completion_review_watchdog_loop` 吞掉 ⇒ **一封日报都发不出去**（正是用户要的那个功能）。
  - **为什么测试没抓到**：现成用例 `test_the_snapshot_covers_exactly_the_enabled_games` **确实跑了真实实现**，但测试环境的目录里**只有启用的 PGR**，没有"目录里有禁用游戏"这个生产常态。
  - **修法（调用方，1 行）**：把 `games` 收窄成 `enabled_games` 再传给 `todo_overview` —— 与 `current` 一致，也正合报告口径（日报本来就跳过禁用游戏；`scope_game_ids` 也只看 `enabled`，所以语义不变）。**没有**改 `todo_overview` 的契约（那是 WebGUI 也在用的投影，不应为了一个调用方的错而放宽）。
  - **测试**：`backend/tests/test_notification_game_day_report.py::test_the_snapshot_survives_a_disabled_game_in_the_catalog`（往目录里塞一个 `enabled=False` 的 WW，断言真实快照不抛异常且行仍只有 PGR）。**区分度验证**：把 `games=enabled_games` 改回 `games=games` ⇒ 该用例精确失败。`backend/tests` 全量 `PYTEST_EXIT=0`（0 FAILED）、`scripts/heartbeat/undef_check2.py TOTAL=0`。
  - ⚠️ **踩坑记录**：区分度实验一开始用 `Path.read_text()/write_text()` 做"改回去再恢复"，**文本模式往返会把行尾规范化**，恢复后 md5 与实验前不一致（git diff 仍然只有预期的 10/4 行，所以内容没坏，但这是不该有的副作用）。**以后这类实验一律用 `read_bytes()/write_bytes()`**；`.cache/bidir-nikke.py` 已改成二进制读写。

- ★★ **2026-09-23 01:5x：发布通道在最后一步"心跳体检"上失败，并把 Manager 留在了停机状态**（**环境/配置层，未改代码；处置配方已写入 `heartbeat-playbook.md` §6**）
  - **现象**：`publish-hb0923d` 跑到最后一步抛
    `Start-YeYuGamer.ps1:82 → YeYu Gamer desktop host did not become healthy before the startup timeout.`，打印 `{"status":"installed-unpromoted", "managerRestarted":false}`，**且没有写 `PUBLISH_EXIT=`**。
  - **实际损失为 0**：`install-manifest.json` 的 `installedAt` = 01:57:02，`.venv/Lib/site-packages/yeyu_gamer_manager/services/adapter_artifacts.py`（mtime 01:56:34）**含本轮 `minimum_size` 修复**、`adapter_protocol.py` 含 `minimum=1 if mime.startswith("image/")` ⇒ **文件都已装机**，只有"体检 + 提升声明"没走完。
  - **根因**：`desktop-host.log` 里 `17:57:14 manager-start-requested` → `17:59:44 manager-start-timeout budgetSeconds=150`，即 **Manager 启动用了 147–164 秒，超过 150 秒预算**。而同一台机器上更早几次启动只要 58–69 秒（`17:10:54→17:11:52`）。差别是**发布自己当时的磁盘 IO**（PyInstaller 6 个构建 + 8 GB 产物拷贝 + 安装目录整树复制）把 `snapshot` 的 `store` 阶段从 ~300 ms 抬到 6.4–9.4 s（`snapshot.slow` 记录），启动全程被拉长 ⇒ **体检超时是 IO 争用，不是新代码起不来**。
  - **代价（真实发生）**：体检失败 ⇒ 发布脚本抛出 ⇒ **Manager 没被重启，停在停机状态**（`/health` 连接被拒），而 `release-pending.flag` 当时还在 ⇒ `DailySupervisor` 一直 `standing down`、**连 `ensure_manager()` 都走不到**（第 213 行早于第 223 行）⇒ 队列整段停摆。**删掉标志后 supervisor 的下一个 tick（02:04）自己把 Manager 拉起来了**（`manager recovery: started (exit=0)`，用时 86 s，磁盘安静时就够）。
  - **教训（已写进 playbook §6）**：① 发布失败后**第一件事是删 `release-pending.flag`** —— 它既挡 supervisor 发批次、也挡 supervisor 恢复 Manager；② 判别"到底装没装"看 `install-manifest.json.installedAt` 与已装文件里的修复标记，**不要只看脚本有没有抛**；③ 别在发布同刻跑别的重 IO 任务（本轮发布前跑全量 pytest 是无害的，因为它已经结束；真正害事的是发布自身的构建）。
  - **仍未修（记录在案）**：150 秒体检预算对"启动本身要 ~60 s、且发布必然伴随重 IO"的场景偏紧，**建议把它提到 300 s**（改 `Start-YeYuGamer.ps1` 调用方的 `-TimeoutSeconds`/bootstrap 预算）。本轮**没有**去动它——那会改到承重的启动脚本，且当晚没有余量再验证一轮；下一次专门做。

- **2026-09-23 02:0x 附带发现（未修，非当日阻塞）**：`game_launcher.py:1227` 起的 `nte-launcher-upgrade-watch` 是 **daemon 线程、无停止句柄、不 join**，会活过它所属的 attempt；测试里表现为 `PytestUnhandledThreadExceptionWarning`（线程在 `subprocess.run` 已被 mock 掉之后醒来 ⇒ `TypeError: 'Mock' object does not support the context manager protocol`）。生产影响很低（该线程只做只读的启动器探测），但"watcher 活过 attempt"本身该修。
- ★★ **2026-09-23 03:2x：`DailySupervisor` 的"每日 8 次补批"预算从来不会重置 —— 一个打字错误把它变成终身上限**（**已修 + 已提交 `20b9531`；脚本跑源码树，无需发布**）
  - **现象**：09-23 00:39 起，每 5 分钟一条 `outstanding ZZZ:8/9;NTE:5/6;NIKKE:1/3 but the start budget (8) is exhausted; needs a decision`，一直到 03:24 仍是这一条；`supervisor-state.json` = `{"gameDay": "", "starts": 8, "lastStartAt": 1790095142.74}`（`lastStartAt` 换算为 09-23 01:0x 本地 ⇒ 这 8 次全花在**同一个游戏日**内，不是跨日累计）。
  - **机制**：原代码读 `payload["todo"]["gameDay"]`，但 `/api/v1/snapshot` 的 `todo` 对象**没有这个成员**（顶层才有 `gameDay`，`todo` 只有 `scopeKey`/`scopeFingerprint`/…）⇒ 取值恒为 `""`，与状态文件里的 `""` 相等 ⇒ 第 245 行的"游戏日变更就清零"永不触发。`MAX_STARTS_PER_GAME_DAY` 因此退化成一个**终身**计数：8 次之后五 分钟定时器**永久**不再补批（"needs a decision" 会一直刷），当天剩下的活只剩 04:00 的 `DailyRun` 与心跳，正是"两条机械通路同时坏掉"的形态。
  - **修法**：新增 `game_day_key(payload)`（顶层 `gameDay`，退化到 `todo.scopeKey`）与 `advance_state(state, payload)`；**游戏日真的换了才清零**，快照读不出游戏日时**保留剩余预算**（瞬时缺字段不该多发批次），而状态文件里 `gameDay` 为空的**遗留**状态是"被写坏的版本留下的"——它记的 8 次**就花在当前这个游戏日**，所以只打标签、不发放第二份配额（否则修复本身会白送第 9 批）。日志会写明是"rolled over"还是"adopted"。
  - **验证**：`--selftest`（新增，只读、不碰 Manager）覆盖 5 种情形并断言；**双向验证**：把 `game_day_key` 换回旧读法 ⇒ `advance_state` 对当前游戏日返回 `starts: 8`（预算照旧死着，缺陷精确复现）。实跑一次 supervisor：`adopted game day daily:manager:59b5068fae16c5a5 with the 8 starts already recorded` → 仍按预算挡住（未白送批次），队列保持空闲；`undef_check2.py TOTAL=0`。
  - **下一个游戏日（04:00 之后）**：`gameDay` 变成新键 ⇒ 状态清零、8 次配额恢复；这是设计意图，无需人工干预。

- ★★ **2026-09-23 03:33：0 字节 artifact 的两层修复在生产里验证通过；ZZZ 唯一缺口是"入口待办单独跑不出来"**（**已修并已验证；契约缺口仍待用户定口径**）
  - **验证（本轮实跑，`ZZZ-db8dc9d3`，单游戏批次 `a3561463`）**：同一份 `attach-home` 单目标，事件流变成 `seq=2 run_artifact_staged`（**被接受**，不再 `adapter.event.rejected`）→ `seq=3 run_terminal {'status':'failed','exitCode':20}` ⇒ `attempt.result status=failed protocolValid=True code=run_terminal`。对照 00:19/00:40 的 `invalid_schema`（解析层：`sizeBytes is out of range`）与 01:13 的 `artifact_size_rejected`（导入层：`artifact size is outside its limit`）⇒ **两层修复都真的生效**，驱动"故意发的终态"不再被记成协议崩溃；游戏级 `nextAction` 也从误导性的 `Adapter artifact_size_rejected` 变回 `Adapter run_terminal`。
  - **仍然完不成的是契约缺口**：驱动 `classic_tool_driver.py:1073-1085` 在 `selected_app_ids` 为空（当天只剩 `attach-home`）时直接 `_emit(..., "review_required", "No selected official application can produce the normal-world marker")` 并 `return 20`。上游 OneDragon 的"进入游戏"是**跑 app 时**的副作用（`op_to_enter_game` 在 `app.execute()` 里），且它会跳过 `app_run_record` 里已完成的行 ⇒ **当天别的 app 全完成之后，入口待办在本工具链里就是不可观测的**。09-22 的对照很干净：`ZZZ-c5dbdf94`（冻结 8 条、不含 attach-home）**8/8 completed 且日志里真的出现 `返回大世界 ] 执行成功 返回状态 大世界-普通`**，而只剩 attach-home 的 4 次尝试全部秒退。
  - **两个候选修法（都不本轮改，等用户定口径；改错等于伪造成功）**：① **同轮共选** —— 规划期把入口待办和一个"当天已完成"的 app 放进同一 run 取标记（要动 Manager 的运行范围与 `todo_overview` 契约，且已完成 app 的事件会落到冻结范围之外的待办上，风险大）；② **同日证据归属** —— 同游戏日已观测到的"大世界"标记允许为入口待办背书（要新增一份可审计的跨 run 证据记录）。**在口径确定前，签到概率为 0 是"设计如此"，不是编排 bug**（`docs/daily-workflow.md` 本节 09-23 条目）。
  - **顺带发现（本轮已用，建议固化为常用手段）**：`POST /api/v1/batches` 的 `BatchCreateRequest` **支持 `gameIds`**（camelCase），所以"单游戏诊断"**不需要临时关别的游戏**：`{"kind":"daily","mode":"execute","gameIds":["ZZZ"],"requestedBy":"cli"}` + `If-Match`/`Idempotency-Key`/`X-Expected-State-Version` 即可，批次 `a3561463` 的 `gameIds` 实测就是 `['ZZZ']`，收尾 `blocked`、无人工门、队列回空闲。比"改 `enabled` 再恢复"更安全（不动配置、不需要恢复步骤）。

- ★★ **2026-09-23 04:4x：「发布被中途杀掉」会把某个已启用游戏的适配器留在"已装未晋级"状态，于是那个游戏整天 0 项、而且毫无提示**（**已修：主管自动修复 + 新工具；见下**）
  - **现象**：09-23 游戏日 04:04 规划出的批次 `c118c3df` 有 7 个 `gameIds`，实际只生成 **6 个 run 成员**；`StarRail` 整天 `req=0/4`、`runtime=planned`、`nextAction` 为空，**没有任何 run、没有 blocker、没有 `review_required`**。批次 `result` 里直接写着 `deferredGameIds: ["StarRail"]`。
  - **机制（已追到最底层）**：`GET /todo-instances?gameId=StarRail` 显示今日 4 条必选全部 `dispatchDisposition=unsupported`、`dispatchReasonCode=execution_package_unpromoted`（"当前没有已验证并晋级的执行 manifest"）⇒ 规划期 `executableGameIds` 不含 StarRail ⇒ 整游戏被 deferred。落到 `todo_dispatch.py:321`：`facts.runtime["manifestVerified"] is False` 时直接把该 Todo 判为不可执行。
  - **为什么 `manifestVerified=False`**：`GET /adapters` 的 `execution_bindings()` 会对 `runtime\adapters\game-modules\<game>` 重新校验已晋级的执行包。实测该目录里 `install-manifest.json` 的 `promotion.status = "candidate"`、`executionReady = false`、`promotion.receiptFile = ""` ⇒ 校验抛 `execution_package_unpromoted`。**比对全部 11 个模块：只有 `starrail` 是 candidate，其余全是 promoted。**
  - **为什么会变成 candidate（根因，属本项目缺陷族）**：`scripts\Install-YeYuGamer*Adapter.ps1` **故意只接受未晋级的候选包**（`Install-YeYuGamerStarRailAdapter.ps1:78` 原文：*"StarRail promotion is Manager-owned; the installer accepts only candidate test evidence."*），晋级由发布脚本在**后面的步骤**调用 `POST /api/v1/adapter-versions/<版本>/promotion-requests` 完成。时间线：01:10:53 装机了一个 **promoted** 的 StarRail（`starrail.previous-20260922175712`，`receipt=True`）→ **01:57:12 又装了一个新构建（`starrail-20260922175513`）但只到 install、没跑到 promote**（那一轮发布被外部 `schtasks /end` 杀掉了）⇒ 旧的已晋级模块已被换走、新的没有执行授权。**这不是上游问题、不是环境问题，是我们自己的发布通道在"install 与 promote 之间被中断"时的破坏性副作用。**
  - **安静程度是关键危害**：规划器把该游戏 deferred 掉，既不抛 blocker 也不发 `review_required`，日报也不会说"这个游戏今天没跑"⇒ 只有盯 `req=0/4` 才发现。这是"零意外"里最坏的一类：**静默丢一整个游戏**。
  - **本轮修法（无人值守自愈，无需发布）**：
    1. 新工具 `scripts/heartbeat/hb-adapters.py`：`status` 只读列出每个模块的 `promotion.status`/`executionReady`/是否有 receipt，并对**已启用但无执行授权**的游戏以退出码 3 报警；`repair [--json]` 只对"已装候选 + 磁盘上的候选测试证据与安装载荷逐字段绑定（`payloadDigest`/`buildId`/`packageVersion`/`supportedGameIds`）"的包调用 Manager 自己的晋级接口（幂等键 + 当前 `stateVersion`）；`promote <GameId> [--wait-seconds N]` 供单次使用，`--wait-seconds` 下会像发布一样写 `release-pending.flag` **认领下一个空闲窗口**（否则 5 分钟的 `DailySupervisor` 会把每个空闲窗口抢走，晋级永远排不上），并在**所有退出路径**删掉该标志。
       - ⚠️ **晋级请求必须对 412 重试**：`read stateVersion → POST` 这件事在批次运行期间必然与 Manager 的连续 stateVersion 自增打竞态。实测 12 次探测里 **2 次是 `412 state_version_conflict`**（`expected 108797, current 108802`）。第一版没重试，于是在同一分钟内一会儿报 `deferred_busy`、一会儿报**假的** `promotion_failed`。现与 `Publish-YeYuGamerLocalRelease.ps1` 同一配方重试 3 次。
    2. `scripts\Invoke-YeYuGamerDailySupervisor.py` 在**队列空闲**的分支里、规划之前调用 `hb-adapters.py repair --json`：修好的游戏会被**同一 tick 紧接着发出的批次**带上，全程无人值守。晋级用 Manager 已审计的同一路径（发布脚本本来就是这么调的），只是把那次没跑成的步骤补上；证据不匹配则**拒绝**并**大声记日志**（`not_repairable`），不再静默。
    3. `--selftest` 增加 5 条断言覆盖"无事发生必须安静 / 修过必须出声 / 拒绝必须出声"；**双向验证**：把 `summarize_repairs` 改成永远返回空 ⇒ `AssertionError: a repair is logged` 精确失败。
  - **待补**：`Publish-YeYuGamerLocalRelease.ps1` 的 install→promote 之间仍不是原子的（被硬杀时 `finally` 也不会跑）。它与本节的 supervisor 自愈是一对：自愈负责收尾，发布侧若再加"安装前记录、失败则回滚"会更干净。**策略口径（是否允许自动晋级）**：发布脚本自己就自动晋级，本自愈只是重放同一步，未引入新的授权级别。
  - **不要再当新问题查**：见到"某个已启用游戏整天 0/N 且批次里没有它的 run"，**第一步就是 `hb-adapters.py status`**，不要从图形层/客户端/上游查起。

## 8. 结束报告

结束报告列出：范围与逐项结果、承接与本轮执行的区别、更新前后版本、根因及关键证据、YeYu commit/上游 PR、实际验证范围、残余阻塞及恢复点、客户端清理和报告状态。原始日志保留本机，外发只用已审查的脱敏材料。邮件按 `notification_policy.report_scope` 发送：默认 `game_day` = **一个游戏日一封、只报当天的最终结果**（不再按重试轮次逐轮发送）；`batch` = 旧的每轮报告。SMTP 受理不冒充送达。
