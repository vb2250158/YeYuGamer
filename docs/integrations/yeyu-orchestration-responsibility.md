# YeYu Gamer 职责与接入规范

本文说明接入职责；现行边界与明确例外以 [编排合同](orchestration-contract.md) 为准。旧补丁、自编切号、HUD 保险和已删除的游戏判断不能再作为实现依据。

## 一句话

YeYu Gamer 是本机游戏自动化工具的编排器。它不代替任何官方工具识别画面、判断战斗、消耗体力、切换账号或决定任务成败。

## 只负责四件事

1. **步骤埋点与必要截图**
   沿官方实际执行流程包装已有方法：原参数调用原方法，返回原返回值，原异常继续抛出。写入 `started` / `completed` / `failed` / `skipped`。步骤该有的截图必须在该步骤实际进入和结束时取得，对应本轮工具、任务和步骤。截图失败如实记录，不得用旧图或占位图冒充完成。埋点失败必须 fail-open。

2. **官方配置参数注入**
   把用户在 YeYu Gamer 保存的官方字段临时写入该工具自己的配置文件或官方 CLI。工具进程确认结束后恢复本次注入字段，保留官方对进度等其他字段的合法写回；具体恢复边界遵循编排合同。没有映射的键保持官方已保存值。未选步骤通过官方开关关闭，不改写 `run()`。

3. **每日前后的启动与关闭**
   按工具正式入口的生命周期要求启动客户端和工具。正常结束或普通失败后关闭本轮工具与本轮启动的游戏。取消、凭据、验证码、条款、付费或人工门保留现场；普通已保存账号登录确认按编排合同中已授权的最小衔接执行。不得扩大为自动输入凭据、自编登录或任意坐标回退。

4. **整轮邮件报告**
   一轮 = 一个 Batch 的全部 GameRun 都到达终态之后。按游戏和账号汇总官方任务终态，附步骤截图和本机完整日志入口。截图缺失只影响附件，不改写上游成败。不得把单个步骤或单个工具结束当成整轮结束。

日志通过官方 logger 追加 handler 全量采集。不靠自编 OCR、分数、窗口标题或日志里的某一句话截断任务。

## 明确不负责

- 不重写上游 `DailyTask`、`DailyRoutineTask`、行为树、战斗循环、副本进出或死亡恢复。
- 不为“更稳”增加保险：自编体力额度、假死亡退本、额外 `ensure_in_front`、HUD/输入兼容、截图确认门、登录页 OCR 过滤器、自编切号。
- 不把页面勾选、进程退出码 0 或“日志出现某句话”写成步骤完成。
- 不上游社区问题：先查官方 issue / PR；需要时在隔离候选核对官方修复，不在 Adapter 里补游戏逻辑。
- 不向工具源码写入 `--task` / `--exit`。官方 CLI 只作为 Adapter 启动参数。

官方怎么跑，YeYu 就怎么编排。

## DailyTask 只是入口

不能只看 `--task 1` 或类名就说“没有开关”。必须读完该版本的 `__init__` 配置项、`run()` 内部调用、基类和 `config_type`。

官方“执行每日”是入口。入口内部会按官方开关、阶段和条件跑完整内容。YeYu 页面上的步骤和开关必须对应其中一层。

| 层次 | 含义 | YeYu 怎么接 |
| --- | --- | --- |
| CLI 入口 | `main.py --task 1 --exit`、`main.py --task 7 --exit`、`March7th Assistant.exe daily` | Adapter 固定启动参数 |
| 配置开关 | `DailyTask.json` / Routine 里的官方布尔、下拉、多选项 | 参数注入；退出后恢复 |
| `run()` 阶段 | 入口内部固定顺序调用的方法 | 被动埋点 |
| 独立 one-time task | 已注册但不是这条每日入口 | 不拿独立 task 冒充每日内部阶段 |

页面步骤和开关都保留。能映射到官方配置的，用配置注入让官方入口自己跳过。官方入口没有独立开关、只是固定阶段的，整轮带着这些阶段一起跑，不能改 `run()` 去跳过。

## 允许的代码注入

只允许这三种，且都在 YeYu 侧：

1. **sitecustomize 观察器**
   Adapter 把观察器目录加入 `PYTHONPATH`。Python 加载 `sitecustomize.py`，包装官方已有方法。工具 `main.py`、`DailyTask.py` 保持官方原样。

2. **官方配置文件的写后恢复**
   启动前保存原值与恢复依据，写入本轮官方字段，子进程确认退出后恢复本次注入，保留官方合法写回。

3. **官方 logger handler**
   在工具自己初始化 logger 之后追加 handler，采集该工具本轮全部日志。

禁止：改工具 `main.py`、改 `DailyTask`、字节码补丁、HUD/输入保险、自编切号、选步过滤器、截图完成门。

跟随官方升级时：先对照新版本调用链，再决定观察器包装点是否仍存在。包装方法改名或删除时，观察器必须 fail-open 并记录缺口，不能自行补一段官方业务。

## 标准接入流程

1. 沿官方入口 → 配置加载 → 开关判断 → 任务分发 → 子步骤 → 日志/截图 → 结束处理，读完当前接入版本。
2. 固定上游仓库、提交/发行、许可证和正式 GUI/CLI。
3. 每个页面步骤指到官方配置字段或官方方法；指不到就写缺口。
4. 参数快照、临时写入、进程结束后按编排合同恢复。
5. 观察器回放：原返回值、原异常、日志写失败不影响流程。
6. 步骤截图：优先复用官方截图或设备窗口；在官方方法进入/返回时采集。失败如实记录。
7. 官方 logger 全量日志作为本轮 artifact。
8. 工具源码无 YeYu 业务补丁；CLI 参数只出现在 Adapter 启动命令里。
9. 真实验收只从已安装 WebGUI 的“开始每日”进入。

## 每日启动与关闭

1. Manager 保存开关、路径、官方参数和今日步骤。
2. 按正式入口的生命周期要求启动，确认进程归属。凭据、验证码、条款、付费或不明弹窗进入 `human_required` 并暂停整条队列；普通已保存账号确认按编排合同的已授权最小衔接处理。
3. Adapter 启动该工具带 GUI 的正式入口，并传入官方 CLI。
4. 工具自己的更新检查和 DailyTask/Routine 入口先跑。
5. 观察器转发真实阶段事件；官方 logger 写入本轮日志。
6. 工具退出后恢复配置。正常结束或普通失败时关闭本轮工具和本轮启动的游戏。
7. 整轮结束后发邮件。

`human_required` 现场保留并暂停后续队列。其他已配置游戏客户端的清理按编排合同与用户授权执行；进程归属不明时不强制关闭。

## 接入必须对齐的四层

1. Manager Todo 目录与 integration registration
2. 打包 manifest `operationBindings`
3. Adapter 运行器内部 allowlist
4. 目标工具实际方法或配置键

只改其中一层会出现“页面能勾、启动前被拒绝”或“页面没勾、工具仍执行”。

## 历史接入审计快照

以下入口、版本与文末未验证项保留先前审计的结果，不定义今日范围或当前安装状态。当时 FGO、棕色尘埃 2、卡厄思、胜利女神未纳入专项检查；不能将该历史排除规则带入新的每日任务。今日范围、绑定版本和未完成项必须按 [每日工作流](../daily-workflow.md) 从 Manager 与当前官方源码重新核实。共享代码改动不得破坏现有接口。

| 游戏 | 官方每日入口 | 官方开关所在 | 埋点与截图 |
| --- | --- | --- | --- |
| 鸣潮 当前账号或单个指定账号 | `main.py --task 1 --exit` → `DailyTask.run` | `Which to Farm`、梦魇布尔、`Additional Tasks`、`Exit After Task` | `WwYeYuObserver` 观察 `ensure_main` / `open_daily` / 梦魇 / 体力 / 领奖 / 邮件 / 战令。源码已迁到官方任务线程的步骤边界截图，复用官方捕获与写入器；`step_artifact` 保留原时间，奖励前后及水印分类保留。正式验证状态见 `ww-official-candidate.md`。 |
| 鸣潮 多个指定账号 | 第一个指定账号 GameRun：`main.py --task 7 --exit` → `MultiAccountDailyTask.run`。后续指定账号 GameRun 回放该次官方过程的观察与截图，不再启动官方工具。 | 同上；切号、OCR、重试全在官方 task 内 | 观察每次内部 `DailyTask.run` 及官方 `_detect_current_account_from_login` / `_select_and_login_account` 返回的账号标签。不组合 `_switch_to_login`，不改 `done_set`。 |
| 终末地 | `main.py --task 1 --exit` → `DailyTask.run` | 各子任务 ⭐ 开关及体力参数 | `EndfieldYeYuBridge` 观察官方方法 |
| 少前 2 | 官方 GUI `--task 1 --exit` → `DailyTask.run` | 邮件、公共区、体力、班组等配置布尔 | `Gf2YeYuObserver` |
| 异环 | `main.py --task 2 --exit` → `DailyRoutineTask` | Routine 队列项与配置 JSON | `NteYeYuBridge` |
| 星铁 | `March7th Assistant.exe <task>` | 官方 `config.yaml` | 追加官方日志。任务终态映射官方日志里成对的「开始」与单独一行「完成」，不是退出码。 |
| 绝区零 | 官方 GUI `OneDragon-Launcher.exe --onedragon` | `_group.yml` `app_list[*].enabled` | 应用终态读官方 `app_run_record`。`attach-home` 仅在官方日志出现「返回大世界 ] 执行成功 返回状态 大世界-普通」时完成。 |
| 战双 | `FOS.exe --direct-run` | 临时 profile `is_checked` | 官方执行日志中的任务开始/成功。隐私协议文案出现时映射为 `human_required`，不替点。 |

鸣潮领奖、邮件、战令在该版 `DailyTask.run` 里是顺序调用，没有各自的 DailyTask 布尔。页面仍保留这几行作为官方阶段进度。局部选择不能改写 `run()`；非整套七步以 `ww_selection_unsupported` 结束。

鸣潮多账号必须走官方 `MultiAccountDailyTask`。YeYu 不得再拼 `done_set` 过滤器、强制登出、或给 `ensure_main`/`wait_login` 再套 `ensure_in_front`。Manager 仍为每个启用账号保留独立 GameRun、步骤、配置快照和邮件行。官方多账号入口会在第一轮遍历登录页全部已记住账号；YeYu 只按官方 OCR 到的标签把观察和截图归到对应 GameRun。官方 `DailyTask.json` 在一次 MultiAccount 进程里只有一份，不能在账号之间中途换配置。

少前 2 和终末地每一项都有官方配置开关。YeYu 把已选 Todo 写成这些官方键，然后让原 `run()` 自己决定。未映射键保持用户已有官方值。按 catalog 明确排除的少前 2 项（活动层、竞技场、购买免费礼包、商店心愿单购买）注入为官方 `false`，避免未选操作被执行。

绝区零 `_group.yml` 与战双临时 `is_checked` 属于配置注入，不是自编任务选择器。未选官方任务必须临时关闭，否则官方会执行未勾选内容。

## 本机工具源码

运行时源码必须是官方树或官方候选的隔离副本。工具工作目录的 `main.py`、`DailyTask.py` / `DailyRoutineTask.py` 中不得出现 `yeyu`、`YeYuGamerRun`、`YEYU_GAMER_`。观察器只存在于 `adapter-host/*/sitecustomize.py` 及其观察模块。

## 上游版本

| 工具 | 隔离源码 / 发行 | 提交或版本 |
| --- | --- | --- |
| 鸣潮 | `vendor-baselines/ok-wuthering-waves` 与候选 `C:\Game\YeYuGamerCandidates\ww-official-c3fef9a` | `c3fef9a06f61f96abeb53571fb47dd578b6f7063` |
| 终末地 | `vendor-baselines/ok-end-field` | `b13f1197a503e6473fbf63c1f927a33838739d1f` |
| 少前 2 | 候选 `gf2-official-main-4122e01` | `4122e01e8a69063ae37807f316f2ae8f80eeedb1` |
| 异环 | 见 `docs/integrations/ok-nte.md` | 以当前候选 runtime-preparation 为准 |
| 星铁 | March7th Assistant 发行 | 以当前绑定发行目录为准 |
| 绝区零 / 战双 | 见 `docs/integrations/classic-upstream-boundaries.md` | 以当前候选为准 |

## 检查清单

1. 固定上游仓库、提交/发行、许可证和正式 GUI/CLI 入口。
2. 每个页面步骤都能指到官方配置字段或官方方法；指不到就写缺口，不编开关。
3. 参数快照、临时写入、进程结束后字节恢复。
4. 观察器回放：原返回值、原异常、日志写失败不影响流程。
5. 需要截图的步骤产生本轮对应图片；失败如实记录。
6. 官方 logger 全量日志作为本轮 artifact。
7. 工具源码无 YeYu 业务补丁；CLI 参数只出现在 Adapter 启动命令里。
8. 真实验收只从已安装 WebGUI 的“开始每日”进入。

## 验证与未验证

本轮源码已按四项职责收拢，并跑离线回放/后端账号编排测试。下列事项必须按实际证据阅读，不能把“代码已修改”写成“运行已验证”：

- **已做离线验证**：WW 被动观察器、官方 DailyTask 条件跳过、MultiAccount 身份观察不改 `done_set`、OpenKuro 静态入口合同、账号编排中“多个指定账号共享一份官方 MultiAccount 观察文件”。
- **本轮未做已安装 Host 的真实每日**：未构建/安装/晋级新 Adapter，未从 WebGUI 再跑一轮。已安装 Host 仍可能是旧包 `0.2.0-local-daily.54`（含已删除的 compose 切号桥）。
- **本轮跳过**：FGO、棕色尘埃 2、卡厄思、胜利女神的专项检查与修改。
- **已知官方/环境限制，不是 YeYu 私有补丁**：星铁、绝区零、战双、终末地、少前 2、异环的近期真实批次失败（退出码、启动器、schema、超时等）仍以当时批次为准，本轮没有重新实机验证。
- **鸣潮单指定账号**：官方入口是 `DailyTask --task 1`，不替用户切到该标签；当前客户端是谁就跑谁。多个指定账号才启动官方 `MultiAccountDailyTask --task 7`。
- **官方 MultiAccount 会遍历登录页全部已记住账号**，包括 YeYu 里未启用的标签。YeYu 不为“只跑这一个标签”改官方 `done_set`。
