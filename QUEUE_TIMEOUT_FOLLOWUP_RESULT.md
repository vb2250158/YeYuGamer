# 队列超时与六项追加任务：CHANGES_REQUIRED

本报告承接 `QUEUE_TIMEOUT_RESULT.md`、`ORPHAN_PENDING_EXECUTION_RESULT.md`，不是上线或游戏每日完成证明。分支 `qa/queue-timeout-20261028`，前序提交 `34c4523`、`4361c93`。本轮已提交 `fca21ec`（review lease与真实plan基线修复）、`5254256`（scheduler/ZZZ/WW固定来源补丁）、`709135b`（纯预留Store判定、精确intent SQL接口与17项回归）、`41c43e2`（不自动截队、真实Host签名）、`1ac63ac`（残留human blocker让队判定）；PGR最终补丁及报告提交号以最终答复和该分支 Git HEAD 为准。

## 边界与基线

- 唯一维护源码 `D:\Game\Automation\YeYuGamer` 的 HEAD 为 f4ab6bf，但当前调度、PGR、HoYoPlay、WW account scope 等模块尚未跟踪；QA 从 HEAD 建立，不能把另外1113项工作区内容夹入提交。
- 已跟踪逻辑在 QA 直接修复。缺 HEAD 的当前模块按文件固定 SHA，交付 unified patch 和明确标注的只读来源 overlay 回归；**没有声称这些补丁已安装或已集成进可运行 main**。
- 没有启动/关闭游戏、模拟器、启动器，没有读写 Manager 数据库或调用业务 API，没有修改运行配置、锁、计划任务、安装指向；没有 push、PR、main 合并。
- 边界事故：WW binding 子任务曾错误在 main 新增三个归属明确的交付文件及两个忽略缓存文件。发现后停止其写入，按记录 SHA 将这五个文件移至 QA，没有替换任何原文件。main 状态条目由1116恢复1113。条目数不是全仓字节不变证明，不能表述为“从未写 main”。

## 设计与新增实现

状态机：原 terminal human/review → 原 Attempt 的 awaiting → 到冻结预算，原子重查原 Run/Attempt/member、旧 lease ID+generation、批次未封口、无其他 owner、Host 已交接 → parked。Run 与成员保持 review_required，businessCompleted=false；后续成员继续同一批次。不是成功，也不重建队列。等待中正常解除走 released，避免把合法解除误判成整批恢复屏障。迟到事件保留原 Attempt 身份，不释放新 owner 的租约。

### 已确认发现与最小修法

| 严重度/状态 | 位置 | 触发与后果 | 规则/本轮处理 |
| --- | --- | --- | --- |
| P1 已确认、QA修复 | `backend/yeyu_gamer_manager/services/manager.py:9306`；`services/queue_parking.py:40` | terminal review 仍持有旧租约时，没有像 human 一样有界让队列继续 | 超时有界、逐 owner 释放：增加 review gate，冻结 lease ID/generation；正常释放、换 owner、取消均分别处理 |
| P1 已确认、QA修复 | `services/adapter_protocol.py:376`、`:521` | 真正 `_prepare_run_attempt` 传入不存在的字段，旧测试靠非WW兼容支架绕过，无法进入待测队列 | 可验证性：补空 scope 字段；空值保留v1.1 wire，非空拒绝，绝不假装已支持WW操作scope；移除测试支架 |
| P1 已确认、QA修复 | `services/manager.py:8359` | 非WW也总传Host签名不接受的extra_environment=None，真实启动及旧Mock均TypeError；部分“失败继续”测试因此掩盖真正callback路径 | 接入合同：None不传该扩展；真实Host方法在禁Popen的回归中到达预期Host不可用门，而非TypeError；不静默吞掉WW非空环境 |
| P1 已确认、QA修复 | `services/manager.py:9143`、原`:9386` | 配置execution_strategy=stop时，普通失败会将全部后续标not_started并截队，不是人工Stop | 按新合同删除自动失败截队分支，保留正式cancel/Stop与未知所有权屏障；旧值保留可读但记录忽略事件；迁移原stop回归为前项blocked、后项completed |
| P1 已确认、QA helper+当前模块补丁 | main `services/human_gates.py:15`、`serial_observation_recovery.py:53`；QA `services/queue_parking.py:38` | 虽已park，原human blocker仍被全局owner查询选中，后项真实PGR准入仍拒绝；generic第二项测试未经过此guard | 统一human-owner判定只认已提交expiry收据、同Attempt/member、无新lease/intent的有效让队；不清blocker、不解原Run恢复限制；reclaimed/伪marker/新owner拒绝 |
| P1 已确认、集成补丁 | 当前 main `services/autonomous_daily.py:585`、`:1250` | 首次冻结按注册序；调度读 Store 默认摘要，将可证明的孤儿 pending 当执行者 | 唯一顺序源、Host-aware occupancy：补丁只改首次冻结与占用查询，不改已冻结 cycle；3项限定回归 |
| P1 已确认、部分实现 | `services/adapter_host.py:1958`、`:2040`，`services/manager.py:9229` | kill 后 wait无预算，callback缺失/卡住会 fence整批；仅Host退出不能证明breakaway输入执行者已停 | 不得卡队列与不可重叠执行同时约束；本轮未以裸 continue、清指针或任意进程树kill绕过。独立3项仍红，详见Host专项报告 |
| P1 已确认、前序已修 | `services/orphan_pending_execution.py`、`store/sqlite_store.py` | successor_failed后run留pending，取消409，阻塞自主每日 | 原Run终态回退、正式幂等取消、120秒回收、exact-intent ABA栅栏；见前序报告和真实SQLite测试 |
| P1 已确认、当前模块补丁 | main `services/serial_observation_recovery.py:61`、`pgr_daily_admission.py:49` | 纯预留兄弟被当执行者；自身合法successor意图的例外亦需精确证明 | 同批/无effect/无歧义归属过滤，不能豁免真正活跃、外批、新意图；最终修订与精确测试数见PGR专项报告 |
| P1 已确认、当前模块补丁 | main `services/pgr_unused_reservation.py:16` | 正式operator取消产物不符合unused判定，预留不能再用 | 仅接受正式sealed取消收据，保留全部零效果、同账号/周期/scope检查；复用原admission，不清Stop |
| P2 已确认诊断缺陷，现场根因待确认 | main `services/hoyoplay_native_observation.py:346`、`hoyoplay_native_execution.py:426` | 身份不可读与错误路径、不同文档拒绝分支合并成unknown，不能定位今日启动问题 | 只拆原因码、保持拒绝和零额外重试。不是已修好ZZZ启动；详见ZZZ报告 |
| P2 已确认防御缺陷，登录现场根因待确认 | main `services/ww_multi_account_scope.py`；fork历史20d2a0的`MultiAccountDailyTask.py:226` | source plan非dict引发AttributeError；首账号未知画面已有100秒wait，visibleFalse不是无超时证明 | 最小类型保护维持明确dependency失败；15项离线回归，不放宽未知画面、不盲点；详见WW登录报告 |
| P1 现场绑定按投递事实，源码成因部分确认 | `scripts/Build-YeYuGamerWwAdapter.ps1`；WW binding报告 | upstream-candidates不是builder默认；裸fork不是合格运行包，直接改ToolRoot无效 | 交付绑定合同补丁、资格/布局/prepare门禁证据，不执行真实切换；当前QA publisher尚缺桥接 |

路径未加 main 前缀者指 QA `backend/yeyu_gamer_manager/`；完整逐文件SHA、调用链、反例、原始行号与补丁应用命令在 `docs/qa/*-followup.md`。不以关键词检查替代行为测试。

## 超时预算与剩余缺口

| 环节 | 唯一当前定义 | 默认 | 到期行为/缺口 |
| --- | --- | --- | --- |
| terminal人工接管 | `services/resources/queue_timeout_budgets.json:2` human_takeover_seconds | 300秒 | 只有原owner已静默才撤旧lease、保留未完成、后续继续；活跃Host未解决 |
| terminal待复核/残留lease | 同JSON completion_review_seconds | 900秒 | 原lease句柄匹配且Host已交接则park；正常解除则released；换owner不撤 |
| Manager批次callback | 同JSON callback_overhead_seconds | plan预算+40秒 | 协作取消，另等grace；缺回调且活跃则整批fence，仍是缺陷 |
| callback取消宽限 | 同JSON callback_cancel_grace_seconds | 15秒 | 缺结果不伪造成功；尚未实现quiet receipt后继续 |
| park轮询 | 同JSON poll_seconds | 1秒 | UTC冻结expiry与monotonic双界，取消可退出；不是总预算 |
| orphan pending | 同JSON orphan_pending_seconds | 120秒 | 启动+既有60秒watchdog原子收尾；最迟下一tick，不声称硬实时 |
| 执行Host总预算 | plan.timeout_seconds，来自当前工具binding | 各工具配置 | 协作cancel→固定Host terminate/kill；非统一queue默认；不能替代子执行者静默证据 |
| Host cancel/pipe join | `services/adapter_host.py:101` CANCEL_GRACE_SECONDS | 5秒/次 | 固定Host回收；最后kill后的wait无预算，callback也无独立执行隔离，未统一配置 |
| Host canary | `services/adapter_host.py:100` | 10秒 | subprocess有界失败；本轮未跑canary，非每日完成证据 |
| WW首账号识别 | 历史fork20d2a0 `MultiAccountDailyTask.py:226` | 100秒 | 明确login_screen_unavailable、不点未知画面；SDK capture本身可能阻塞，未证完整绝对deadline |
| 04:00/05:10、补跑/resume、心跳 | 当前main未跟踪scheduler与各fork任务循环 | 未完整证明统一总预算 | 提供当前scheduler补丁与九fork入口预算审计；根循环统一deadline与抢占未修全，不能宣称全链路每环节完成 |

新queue预算可通过typed queue_timeout_budget覆盖，默认/上下限分属各自资源唯一来源；未改真实config。九fork固定ref、给定基线、自改入口、逐循环默认/来源/取消/退出见 `docs/qa/fork-loop-budget-audit.md`。公共SDK局部timeout不是根循环上限；现存Host/fork预算尚未迁入统一源，这是明确未完成项，不是“已有预算所以全过”。

## 测试与证据

所有测试在临时SQLite、合成/native mock或只读源文件overlay运行，不访问真实游戏。Python为 `D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe`，`-B`，`PYTHONDONTWRITEBYTECODE=1`，`PYTHONPATH=<QA>/backend;<QA>/backend/tests`。

```powershell
python -B -m unittest -q test_orphan_pending_execution test_pgr_store_followup test_daily_queue_priority test_daily_queue_priority_manager test_queue_timeout_budget test_queue_parking_policy test_execution_plan_baseline_contract test_review_lease_queue test_completion_contract_manager.CompletionContractManagerTests.test_failed_attempt_does_not_hold_the_review_barrier test_completion_contract_manager.CompletionContractManagerTests.test_review_barrier_expires_into_machine_seal
$env:YEYU_QA_REFERENCE_SOURCE_ROOT='D:\Game\Automation\YeYuGamer'
python -B -m unittest -v test_autonomous_queue_followup test_ww_binding_followup
python -B -m unittest discover -s tests -p test_zzz_launcher_followup.py -v
$env:WW_FOLLOWUP_REFERENCE_ROOT='D:\Game\Automation\YeYuGamer'
$env:WW_FOLLOWUP_FORK_ROOT='D:\Game\Tools\forks\ok-ww'
python -B -m unittest -q test_ww_login_followup
```

- 本轮主联合测试79项通过（13.465秒，包含新增Store17项、Host签名4项与失败/取消3项）。review6项含真实batch loop+prepare、正常解除、新owner保护；独立review与Store只读复核未见新增P1/P2。
- scheduler3项：执行原方法AST+真实顺序函数，首次周期排序/双号保序、已冻结不重排；占用查询静态全覆盖。不是完整heartbeat/重启运行证明。
- scheduler+WW binding+WW登录联合24项通过（1.188秒），ZZZ单独4项通过（2.194秒），均0 skip。WW binding的6项中5项QA合同+1项currentMain桥接参考，**不代表QA已有该桥接**。测试均明确reference与固定SHA，不再依赖预存.cache快照；具体命令见专项报告。
- 独立测试按另一QA分支 `f253225` 原blob `0d9698449bd82700d135c9e113881b45a11ee0ff` 未改回放，12项中9过3失败：dead/missing-callback、hung、killed-owner。未skip/xfail、未修改断言；本轮从8/12改善至9/12但不能验收。
- 原 `test_adapter_protocol.AdapterProtocolTests` 有3项失败；在未修改4361c93对照重现同样3项（artifact重复序列、两个downstream blocker用例），不归咎本轮，也未扩大修复。没有报全套green。
- PGR实际Store/当前模块overlay与新增反例，base/candidate结果和最终重跑详见专项报告；不把source-overlay当干净HEAD集成。
- PGR最终新回归26项由父方独立重跑通过（26.482秒）；严格组合88项为86过、1 failure、1 error。两项原15步及合法混合15步均过，同一reservation、六个一次性action、兄弟零Attempt。两项遗留红来自原mixed fixture只改旧batch状态cancelled却留下非终态membership，当前严格归属拒绝是预期安全反证，不为过旧测试放宽。原测试文件未修改；具体测试名、生命周期终态合同与原子双连接测试见PGR报告。
- 没有全量构建、部署、真实同游戏日同账号同RunAttempt验收；没有九fork完整循环与重启恢复验收。静态检查/安全预筛不能替代这些。
- stop策略迁移、真实Host签名及既有显式cancel等7项通过（1.182秒）；最初迁移测试暴露extra_environment接口断裂，修复后重跑通过，未把TypeError当作预期游戏失败。设置页旧stop标签/说明需转交表现维度同步废弃，不能恢复自动截队逻辑。
- 带真实active human blocker的统一owner补充回归16项通过（4.782秒，显式`PARKED_HUMAN_REFERENCE_ROOT`固定SHA参考）。覆盖原blocker保留、reclaim、新Attempt/lease、伪marker、过期与外来owner，真实serial guard越过human阶段仍保留Host栅栏；不是完整PGR/native启动验收。
- integration-patches局部`.gitattributes`禁止补丁换行转换，以保留固定SHA；只对补丁禁用空白上下文行的尾空格提示，不改变业务源码的空白检查。常规unified diff空白上下文不是待修业务尾空格。

### 最终批量复跑

在上述环境增加 `PARKED_HUMAN_REFERENCE_ROOT=<reference main>`，将主联合79项、人工作用域16项、scheduler3项、WW binding6项及WW login15项合并执行：**119项通过，17.405秒，0 skip**（`.cache/continuation-final-tests.log`）。ZZZ另行最终 **4项通过，2.084秒，0 skip**。PGR必须单独新进程跑overlay，不能将其修改的Python模块绑定混入QA主测试；父方 **26项通过**。独立12项最新回放 **9过3失败，2.658秒**，失败仍为三种Host/回调所有权未知情况，未豁免。

每次实现提交前均执行 staged 安全预筛，最终未提交交付物执行 WorkingTree 预筛，均无High/Critical；`git diff 4361c93 --check`通过。预筛不代表完整语义安全或上线批准。

## 六项任务逐项状态

| 项目 | 可交付结果 | 是否仍CHANGES_REQUIRED |
| --- | --- | --- |
| 1 队列/孤儿/预算/排序 | QA直接修复+有界terminal让队+固定来源scheduler补丁；119项及独立反例 | 是：Host三红、恢复provider、统一根deadline与重启/截图缺口 |
| 2 PGR观察guard | 纯兄弟上下文、原子exact own-intent资格、真实15步/混合15步限定回归 | 是：补丁未与当前全部未提交依赖正式集成；旧fixture须按真实退役合同迁移 |
| 3 PGR取消预留 | sealed正式取消收据+零effect复用同reservation，保留Stop | 是：离线候选，不是实机或安装验收 |
| 4 ZZZ启动器 | 分离具体拒绝原因，不加重试/放宽身份 | 是：本次真实启动失败根因未证，未宣称启动已修好 |
| 5 WW登录与依赖 | 核实已有100秒局部等待；损坏source-plan明确依赖终态，15项回归 | 是：visibleFalse现场成因与SDK阻塞未证；当前QA非空WW scope/environment transport不支持 |
| 6 WW来源绑定 | 固定目标ref/布局/资格证据与合同文档补丁 | 是：不直接把裸fork当运行包，不进行禁止的真实安装/配置切换 |

## 转交、上游、决定项与结论

1. **不得上线：仍CHANGES_REQUIRED。** 真实执行者静默/回调隔离、park后same-Run恢复provider、完整截图证据与重启恢复未闭合；3项独立红不能用“保护设计”当产品验收通过。
2. 需维护整合者将归属明确的当前scheduler/PGR/launcher/account-scope依赖变成一致提交后，应用逐文件SHA补丁，并做完整调用链测试。选项为分模块先整理再cherry-pick（推荐），或提供经审定的新基线重放；禁止合main那1113项全部变更。本轮不等待该决定而已完成独立可测部分。
3. WW源码资格、正式运行包准备与最终安装切换由夜雨按原授权另行安排。本轮没有改变当前运行指向；裸fork路径不是直接可执行布局。
4. ZZZ与WW本次真实失败需同次launcher/进程身份、capture/SDK耗时证据，才能决定改识别还是等待；不从症状推断并盲增重试。投递给证据/工具接入维度，不重复启动每日或抢现场。
5. 没有已修复且现场验证成立的上游算法问题，没有上游PR/issue或远端推送。仅报告历史fork已存在100秒等待，不声称上游已修本次现场问题。

本次可交付的是可审查的QA修复、最小集成补丁与真实失败边界；不是所有六项修完、不是队列不堵塞全面达标、不是游戏每日验收。
