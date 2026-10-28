# 九 fork：YeYu 调用主循环超时预算只读审计

日期：2026-10-28。唯一新增文件为本报告 `D:\Game\Automation\_qa\queue-timeout\docs\qa\fork-loop-budget-audit.md`。先前 WW 报告歧义已修正：旧 4 项结果明确标历史初版，最终统一为 QA 5 pass + 1 skip / reference 6 pass，并保留五文件越界写入后由父方按 SHA 迁回的事故记录。

## 范围、版本与证据口径

- 仅读取 `D:\Game\Tools\forks\` 下九 repo 的指定 ref 和有限入口/自改调用链；使用 `git show`、定向 `git grep`、`rg` 和限定文件 diff，不 checkout、不创建 worktree、不运行 fork、SDK、测试、游戏或准备/构建程序。未写 main、fork 或主缓存，未读运行 DB、凭据、账号配置、日志。
- 已读 `run-stable-game-automation` 及其 `references/workflow-contract.md`、YeYu `docs/integrations/orchestration-contract.md`。旧技能里的启动流程不适用于本次只读任务；本次没有执行它。按各 repo 适用 AGENTS 核对入口，读取了 Endfield/GF2 的 ok-script task 技能及 OneDragon 设计约定。规范不扩大授权。
- **给定基线已核实**：用户补回首投的八项基线：ok-ww `6ffaa7f0`、OneDragon `2ada106b`、ok-nte `3958f471`、ok-end-field `889b89ea`、ok-gf2 `14c907e0`、MAA_Punish `8dfbec69`、BGI `0af68c28`、ok-script `752e01d`。本次仅用各仓 `git rev-parse --verify <短SHA>^{commit}` 解析，八项完整 SHA 与下表此前记录的 merge-base 全部一致；因此既有八仓对照即用户给定基线，无需重扫代码。撤回此前“未提供/一致性未确认”的缺口表述；这些是用户指定的比较基线，不额外认定为官方发行版本。zzz-od-test仍为不适用，其 merge-base仅作版本登记。
- 所有 fork 代码行号均指下面固定的 `yeyu-local-20261028` 提交，**不是当前工作树 HEAD 的行号**。根目录 `D:\Game\Tools\forks\<repo>` 加表内相对路径即完整路径。YeYu 旁证另标为 `D:\Game\Automation\YeYuGamer` 当前源码工作文件，不代表安装绑定或 QA HEAD。
- “总预算未建立”指本次追踪的入口/主循环没有可证明覆盖整个调用的总期限，**不是泛称整个仓库没有任何 timeout**。有限任务清单、账号数、体力上限、重试次数、截图数量限制，都不自动形成墙钟时间上限。
- “absolute deadline”分为固定到期时刻与硬抢占：只在调用前后检查的 deadline 是协作式门禁，不能中断卡死的内部调用；`active_time` 或扣除暂停的计时也不等于连续墙钟期限。

| repo | 固定 yeyu-local SHA | 用户给定基线（八仓已解析核实；zzz-od-test仅登记merge-base，不适用） |
| --- | --- | --- |
| BGI | `c1cc1a618cbcd184e8099afaa3000fd8c799911d` | `0af68c28f6b0e714d5304625ce644736d508022e` |
| MAA_Punish | `a53794e6749473b4248db4153420a5d8f8b28b5c` | `8dfbec696362972385163976173e34b8c48361af` |
| ok-end-field | `1e69b7950cf2a48bd77d3a93316f0e4ea37d3da6` | `889b89ea57e4850d42c2016139cac2e6fd15ba44` |
| ok-gf2 | `888915e3c18589b21f80e746dabaa558c0d1f37a` | `14c907e0370ca015e4389afdb5a3863fa24ccb1c` |
| ok-nte | `212c90b8bff1ac91f2d99049ac0cafb6cec02c1b` | `3958f471702f504fe36cfb4f8b9e564bdf734bf0` |
| ok-script | `202f851fd3bd79f9f3d31d1e0dade3c7d938b712` | `752e01d02c9e298a3128f11d9ec0ed9c654dd0e6` |
| ok-ww | `1c3dc74ef985c09a9b26a4255a459d6df5d3333f` | `6ffaa7f0b0b7f3140367de96c8eacba533c96894` |
| OneDragon | `0df795e3c77c15979e12d49c6542a7bade929497` | `2ada106befbf175d5494392cbc48bec6630a3d15` |
| zzz-od-test | `77d101bb96024db66745ff335aa0b7b03cdae871` | `5218f06c0f576ba32907f9586a139200550a74ee` |

## 预算总表

| repo / YeYu 入口 | 主循环预算默认、来源 | absolute deadline / 取消 / 退出 | 判定 |
| --- | --- | --- | --- |
| BGI：`BetterGI.exe startOneDragon <配置名>` → OneDragonFlowViewModel → TaskRunner/ScriptService | 所查每日 root 未设总时间默认；配置任务数和 RunNum 是次数，不是时间。自改观察图预算是 2/6 次，不是秒。 | 官方 CancellationContext token + 自改 cancel 文件安全点；await 子任务返回后继续/结束，没有在每日 root 发现 CancelAfter 或同一固定总截止。 | 主循环总墙钟预算未建立；取消可协作传播，不证明任意 JS/内部调用可抢占。 |
| MAA_Punish：AgentServer / IPC 任务入口 → 自改 DailyStep → native pipeline | AgentServer.join 无本地 timeout；自改单步 `native_seconds` 为执行 claim-serum/dorm/simulation-field/maintainer-action 1800s，其余 120s。 | typed 单步同时检查 monotonic 已用时间、请求 expiresAt、tasker.stopping；但 context.run_task 是同步调用，guard 在前后，不是硬中断。 | **单步**有到期门禁；不能据此证明整条每日/Agent 主循环有绝对期限。 |
| ok-end-field：main.py → OK.start → DailyTask → DailyTaskRunner | 正常 repeat_times=1；debug 才读“重复测试的次数”。按账号/任务迭代，root 未设总秒数。start_timeout=120 仅启动。 | SDK 协作取消；runner 对 TaskDisabledException/BattleOutcomeError 向外抛。自改自然理智分支额外有 request expiry、取消文件、60s permission wait、15s 局部页面等待。 | 自然续行的门禁不能外推为整个日常总期限；固定到期默认由请求来源决定，本次未读取真实请求。 |
| ok-gf2：main.py → OK.start → DailyTask → DailyTaskRunner | repeat_times 默认1；root 未设总秒数。ensure_main 默认30s；资源更新策略额外900s、stall120s，构成局部 total_deadline。多账号登录调用600s。 | SDK 取消；资源重启检查 executor.check_enabled；runner 异常记录后向外传播。 | 930s（30+900）/1500s（600+900）只是对应 HomeWaitBudget 局部上界，不是每日总预算。 |
| ok-nte：main.py → startup patches → OK.start → DailyTask.do_run | 顺序 tasks 清单；root 未设总秒数。start_timeout=120 是启动，不是 root 日常时限。 | execute_task 对 TaskDisabledException 重抛，外层 run 捕获后返回；其他失败经原有异常处理。依赖 SDK 协作停止。 | 日常主循环总预算未建立；没有把外层捕获取消返回解释为每日成功。 |
| ok-script：TaskExecutor.execute → task.run | wait_condition 默认10s、next_frame默认6s、执行前next_frame显式4s，均局部；execute 服务循环不设总截止。 | exit_event、disable；stop 设置事件；wait_until_done 使用不带 timeout 的 join。daemon 线程不是每日预算。 | **共同 SDK timeout 不足以证明任何调用方 task.run 总预算**。 |
| ok-ww：main.py → OK.start → DailyTask / MultiAccountDailyTask | root 未设总秒数；账号循环至没有 next_account。局部登录100s、账号下拉60s、重选最多5次；Domain 恢复最多3次；体力预算是资源额度。 | 通过 SDK sleep/frame/wait 的协作取消；根任务返回后才有 disable/Exit After Task 收尾。 | 单账号、多账号和刷本循环未见同一总 deadline；局部超时/重试上限不能相加冒充覆盖所有路径的总时限。 |
| OneDragon：launcher --onedragon → application launcher → Application/Operation | Operation timeout_seconds默认-1（无超时），node retry默认3；初始化60s；自改 RewardStatusInspectionOperation显式240s。 | execute 每轮检查 context stop/pause；有效 operation_usage_time扣暂停。240s也在 round边界检查，内部 execute 阻塞不能抢占。 | 普通一条龙未建立有限总预算；240s只覆盖该只读 Operation 的计时语义，不是所有日常或硬墙钟截止。 |
| zzz-od-test | 非每日执行 repo；只读 README 与 tree 确认为测试目录，此任务未涉及其测试执行。 | 不适用；不把测试 timeout 映射到生产。 | N/A。 |

## 逐 repo 调用链和准确代码证据

### 1. BGI

- 入口旁证：YeYu `adapter-host/bettergi-runner/bettergi_runtime.py:170`、`bettergi_lifecycle.py:146` 构造 `BetterGI.exe startOneDragon`；这只是源码调用方式，不证明实际安装当前指向此 fork。
- fork `BetterGenshinImpact/ViewModel/Pages/OneDragonFlowViewModel.cs:533` 匹配 CLI action，`:556` 调用 OnOneKeyExecute，`:561` 定义入口、`:566` 设置 CancellationContext；`:614` 等待 StartGameTask，`:635` foreach任务，`:643-646` TaskRunner 包装并 await task.Action，`:666-668` 附近转 ScriptService.RunMulti，`:680` 检查取消，`:698` 末尾 CheckRewardsTask，`:706` 自改 flow_terminal。root 通过返回/异常收尾，未配置整个序列 deadline。
- `BetterGenshinImpact/Service/ScriptService.cs:120` RunMulti，`:182` 项目循环、`:334` RunNum重复、`:346` await ExecuteProject；`:516` ExecuteProject，`:531`/`:542`/`:548`/`:554` await project.Run。`Core/Script/Group/ScriptGroupProject.cs:195` Run，`:223` JS ExecuteAsync，`:229` 宏传官方 token，`:239` 路径传 token。这个检查只到正式分发，不扩扫每个用户脚本或路径点。
- `Core/Script/CancellationContext.cs:10` 默认 `new()` token source、`:26-30` Set，`:60-75` Cancel。`GameTask/Common/TaskControl.cs:132`、`:163` 调自改 RequestCancellationAtSafePoint；`Core/Observation/YeYuObservation.cs:112-123` 只在安全点读取绑定取消文件并调用官方 cancel，不是单独 watchdog。
- `Core/Observation/YeYuObservation.cs:24-25` 的 NormalImageAttemptLimit=2、DedicatedImageAttemptLimit=6 是本地采样次数。不能因最新提交“bound sampling attempts”认定每日执行时间有界。
- 技术对照：基线 `OneDragonFlowViewModel.cs:573`、`:643`、`:686` 也为顺序执行/取消检查；当前该文件限域 diff 为12增13删，未发现新增 root 总时间约束。

### 2. MAA_Punish

- `agent/main.py:16` main，`:23-25` AgentServer.start_up → join → shut_down；join 无 timeout 参数，Agent进程常驻不等于每日图有限。基线同文件对应`:23-25`相同。
- 自改 `agent/action/basics/DailyStep.py:46-61` 校验固定 native 入口、写单次调用收据后调用 DailyStepRuntime.execute。`agent/PgrDailyStepContract.py:34-36` 明确定义1800/120s，`:65-67` 验证issuedAt/expiry跨度不超过该阶段预算。
- `agent/action/basics/DailyStepRuntime.py:24-31` 开始时固定 monotonic started，guard 同时检查 tasker.stopping、已用时间和 UTC expiresAt；`:55-70` ordinary 在 context.run_task 前后检查，**`:69` 的同步 run_task 没有传 timeout 或独立中断线程**。若内部不返回，后置 guard不能按时打断。这是明确的预算覆盖缺口，非运行故障复现。
- 战斗子链 `agent/action/combat/entry/runner.py:37-43` → CombatTask；`agent/action/combat/core/session.py:62-69` 的等待poll0.2s、UI丢失8s、零血5s、切人12s均是局部策略；`:145` combat_once，`:162-185` while _in_combat，`:837-838` _should_stop读取context.tasker.stopping。所查循环未见整场战斗绝对总秒数，不泛扫全部战斗角色。
- 对照中 DailyStepRuntime 为新增139行；不能把其新增单步guard追认为基线已有或全每日主图已受预算控制。Maa框架对完整pipeline总预算/取消抢占的实现不在本次fork入口证据里，**未确认**。

### 3. ok-end-field

- `main.py:1-10` 安装pre-config/startup补丁，`:14` OK实例，`:29` start；`src/config.py:86` start_timeout=120，`:131` 注册DailyTask，`:168-169` 注册只读奖励/自然续行。
- `src/tasks/onetime/DailyTask.py:413-431` root：非debug固定repeat_times=1，构造plan与DailyTaskRunner，再run；`:433` finally。`src/tasks/daily/daily_task_runner.py:167` execute_task、`:184` 同步func，`:214-240` 账号/任务双循环；`:246-250` fatal分支向外抛，`:359-361` 取消/战斗终态错误重抛。本次所查root/runner没有总deadline字段。
- 自改分支 `src/tasks/onetime/NaturalSanityContinuationTask.py:23-24` _guard转transport，`:98-101` claim页面以active_time+15为局部截止，`:181` run，`:198-207`附近自然战斗/回世界继续链。`src/core/natural_continuation_transport.py:73-76` 检查endfield-natural.cancel文件及UTC request expiry；`:127-129` permission等待采用monotonic+60，`:133-142` permission自身到期且不超过request。请求期限定值来源是typed输入，不是代码里的固定日常默认，本次未读真实请求。
- `src/tasks/mixin/battle_mixin.py:793` 的 deadline参数默认None，`:818` 只有传入时才检查；`src/tasks/onetime/BattleTask.py:745-756` 的15s是重启局部等待，不证明整个battle_space或日常根任务有总期限。
- 基线DailyTask `:413-431` 同样单轮驱动runner；runner限域4增1删。自然续行的自改安全门不扩大为基线或普通每日时间上界。

### 4. ok-gf2

- `main.py:3-8` config → OK.start；`src/config.py:33` 启动60s，`:66` 注册DailyTask、`:72` 注册RewardStatusInspectionTask。
- `src/tasks/DailyTask.py:198-206` DailyTaskRunner.run并finally；`src/tasks/DailyTaskRunner.py:257` repeat_times默认1，`:262` 轮次、`:271` 多账号登录ensure_main(time_out=600)，`:279-282` 逐项同步execute_task；`:230` 同步func调用。`:326` 起handle_exception记录后重抛。该runner相对技术对照无diff，不能称这次自改已补日常root预算。
- `src/core/BaseGfTask.py:103-105` ensure_main默认30s并建立HomeWaitBudget，`:138` wait_until使用total_timeout；`assets/resource_update_policy.json:3-4` update900/stall120。`src/core/resource_update.py:123-134` 用调用方home_timeout固定home_deadline/total_deadline；`:140-142` 重启不能超过原total；`:145`起observe返回home/update/stall错误。`:37-60` 资源重启15s循环且check_enabled。默认30+900与多账号600+900只适用于这个主页/更新等待实例。
- 自改 `src/tasks/RewardStatusInspectionTask.py:173-181` 选择并迭代检查方法；`:163` 的range(8)、`:222`/`:225`/`:230` 的4s是局部扫描/动作。未找到覆盖整个只读选项集合的统一deadline；不能把可数页面当时间保证。

### 5. ok-nte

- `main.py:12-20` 配置、startup patches、OK.start；`src/config.py:122` start_timeout=120。YeYu `adapter-host/nte-runner/Program.cs:958` 旁证用 `main.py --task 2 --exit`，不是从任务名称猜入口。
- `src/tasks/DailyTask.py:113-120` root调用do_run；`:122-179` 创建清单并逐个execute_task；`:193-217` 单项func同步执行且TaskDisabledException向外抛，`:117-118` 外层run捕获取消后返回；`:181-184` ensure_main/打印/结束日志是自然收尾，不是总时间判定。
- 自改routine也仍由`:293-309`/`:443-459`转task.do_run；本次只沿调用入口核对，没有遍历咖啡、影院等整个玩法树。技术对照相同DailyTask根文件无diff，故其顺序循环不能被归因为本次新增预算机制。
- 取消是协作链，不能从`--exit`推导“到120s强制退出”；也不能把取消被catch后返回推断为每日成功。具体成功仍需上游正式任务终态。

### 6. ok-script（公共SDK，不是第六个游戏日常）

- `ok/task/TaskExecutor.py:51-53` 构造默认wait_until_timeout=10，`:72`赋wait_scene_timeout。`:409-442` wait_condition零参数回退该默认，再按本次调用start检查超时。每次调用都会重新起算，绝非每日累计预算。
- `:250-272` next_frame默认6s，`:547-549` execute while not exit_event，`:570`执行前next_frame显式4s，`:577`/`:584`直接task.run；root没有围绕该同步调用的剩余时间传递/总到期判断。`:589-595` Exit After Task在任务返回后才做关闭/quit，不是超时器。
- `:293-299` check_enabled判断disable；`:301-339` sleep观察暂停/退出；`:390-393` stop_current_task disable/unpause；`:395-402`启动daemon线程；`:638-641`stop设exit_event；`:673-674`thread.join无timeout。任意任务若不经过可检查点，取消不保证及时到达。
- `ok/task/task.py:451-460` sleep转executor，`:508` wait_until；`:1343-1347` disable与`:1356-1357`空BaseTask.run均未引入总预算。
- 技术对照TaskExecutor.py无diff，基线同`:52`/`:547`/`:584`/`:674`；不能把游戏fork自改或新局部timeout误记为SDK已新增root watchdog。
- **依赖闭包未确认**：目标ref的requirements分别为WW `requirements.txt:26` ok-script[ocr,qt]==2.0.8；GF2 `:41`==2.0.5；NTE `:65` Windows本地2.0.8+local.yeyu.bitblt.20261025；Endfield `:39`==2.0.7b1。因此本SDK fork的代码事实不能无条件等同四个实际运行包的SDK文件；本次不读取/比对安装运行包。

### 7. ok-ww

- `main.py:1-7` OK.start；`config.py:180` start_timeout=120，`:263`/`:269`注册DailyTask/MultiAccountDailyTask。YeYu当前OpenKuro源码中官方参数为`--task 1 --exit`及多账号`--task 7 --exit`；只证明调用意图。
- `src/task/DailyTask.py:100` root，`:144-186`依官方每日进度和体力条件分别调用刷本/领奖；本次所查root没有一份总deadline。`src/task/MultiAccountDailyTask.py:132` run、`:156-162` while选下个账号并同步run DailyTask，没有跨账号统一计时。
- 同文件`:226` wait_until登录100s、`:406-407` max_retries=5（combo存在时0）、`:423`附近单次等待、`:487`下拉60s；基线`:99-116`同样账号循环/同步日常、`:191-192`同样最多5次选择。当前增加只读/账号适配，不等于增加全账号wall-clock预算。
- `src/task/DomainTask.py:56` max_recovery_retries默认3，`:60`外层while、`:76-80`失败次数退出；`:85` farm_in_domain、`:93`内层while、`:97` combat_once、`:105` use_stamina。资源额度来自StaminaBudget，并非时间，任意一次内部战斗返回前不能靠“最多3次”证明耗时有界。
- 本次不扩扫各角色、全部副本及截图算法；这些内部调用对整轮最坏时间的影响保持未确认。

### 8. OneDragon（ZZZ）

- YeYu旁证：`adapter-host/classic-runner/classic_tool_driver.py:1804-1805`启动launcher --onedragon，`:1815`建monotonic+1800外层deadline。这是**YeYu外层监控**而非fork主循环；本次没有审完整外层所有continue/异常分支，不能宣称严格1800s墙钟结束。
- fork `src/zzz_od/win_exe/launcher.py:37-38` onedragon模式 → zzz_application_launcher；`src/one_dragon/launcher/application_launcher.py:72-98` run_application/main；`src/one_dragon/base/operation/application/application_run_context.py:443-467` init_timeout默认60并等待ready，`:504`同步app.execute。初始化预算与运行预算分离。
- `src/zzz_od/application/one_dragon_app/zzz_one_dragon_app.py:13`构造；`src/one_dragon/base/operation/one_dragon_app.py:23-34`调用Application未提供timeout，`:82`同步子应用组.execute；`src/one_dragon/base/operation/application_base.py:29-43` timeout默认-1并转Operation。
- `src/one_dragon/base/operation/operation.py:94-110` node_max_retry_times默认3、timeout_seconds默认-1（无超时）；`:425-438` while每轮检查operation超时/context stop/pause；`:523-525` node超时是节点范围；`:596`换节点重置起点；`:648-654` operation_usage_time扣pause_total_time。子round若阻塞，直到返回后才有下一次根检查，不能称硬抢占。
- 自改 `src/zzz_od/application/reward_status_inspection/reward_status_inspection_operation.py:42-50`明确timeout_seconds=240。它限制该只读Operation的有效执行时间，不修改普通一条龙默认-1，更不代表所有子Operation共用一个绝对到期时刻。
- 基线Operation同`:99`默认-1、`:425-430`轮边界检查、`:654`扣暂停；当前29增10删不构成已添加所有日常root预算的证据。

### 9. zzz-od-test

- `README.md:1-3`说明zzz测试项目/设置成test；ref根tree为README、docs、screens、test等。当前ref相对技术对照新增导航回归，但不是YeYu每日生产入口。
- 按本次明确范围记**不适用**。没有执行测试，没有拿测试超时去证明OneDragon每日预算，也没有扩大审计到独立测试框架。

## 验证范围和未完成确认

1. 本报告是固定Git对象的静态入口/自改调用链审计，不是运行测试或真实占用/取消验证；没有运行fork或安装程序。
2. 已在各项明确列出默认值、来源、局部期限、根循环未建立总期限的位置及取消出口。公共SDK的10s、启动120s、重试3/5次、截图2/6次均未被冒充为每日主循环预算。
3. 八仓给定基线已通过只读git rev-parse核实，与既有比较对象一致；zzz-od-test仍不适用。实际Manager绑定、安装包SDK版本/哈希、注入配置、请求expiresAt/timeoutSeconds当前值均未读取/未确认。
4. PGR同步native调用能否被框架及时中断、BGI任意用户脚本能否响应取消、各游戏深层单步能否在局部上界内返回，均未实测，不能由本报告推断。
5. 未实施root watchdog/取消协议修复，原任务的这部分仍未完成；未修改源码、预算、配置、部署或真实绑定。补齐总预算应在原任务授权和各源码拥有者边界内继续设计验证，不能把本报告当作完成证明，也不需要重复请求已有修复授权。
