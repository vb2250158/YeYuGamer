# OpenKuro 每日接入规范

此文定义鸣潮（WW）、明日方舟：终末地（Endfield）和少女前线 2（GF2）的 Manager 接入边界。自动化策略仍留在各自上游工具源码中。

## 启动与路径

Manager 先以游戏卡片保存的 `gamePath` 启动或复用已运行客户端，并依据注册的游戏进程名确认客户端进程已经出现；随后才启动 Adapter 和工具。`gamePath` 必须是本地游戏 EXE，`toolPath` 必须是下列工具根目录：

| 游戏 | 工具入口 | 必需文件 |
| --- | --- | --- |
| WW | `ok-ww.exe -t 1 -e` | `ok-ww.exe` 与 `data/apps/ok-ww/working/configs/DailyTask.json` |
| Endfield | `python/pythonw.exe working/main.py --task 1 --exit` | 内置 GUI Python、`working/main.py`、`working/configs/DailyTask.json` |
| GF2 | `working/ok-gf2.exe --task 1 --exit` | GUI 正式入口、`working/main.py`、`working/configs/DailyTask.json` |

工具不自行启动或强杀游戏。三个工具均经过带 GUI 的正式入口，使上游自带的更新检查和启动流程先运行，再由固定 `--task` 参数执行 DailyTask；不再调用 `ok.cli` 无界面入口。Adapter Host 使用本轮 Windows Job 回收 runner 创建的工具进程树；工具终态返回后，Manager 先尝试正常关闭、再限时回收本轮由 Manager 启动的游戏进程。启动前已经存在的客户端以及 `human_required` 现场都保留。

## 选择与阶段回执

每次运行，Adapter 都写入：

- `YEYU_GAMER_SELECTED_OPERATIONS`：本次用户勾选的操作 ID；
- `YEYU_GAMER_STAGE_FILE`：仅本次运行可写的 JSONL 阶段文件。

WW、Endfield、GF2 上游每日任务在该上下文存在时，仅构造被选择 Todo 对应的真实回调。每项依次写出 `started`，并根据回调结果写出 `completed` 或 `failed`。WW 的已核对条件分支还会写 `skipped`；Endfield 与 GF2 当前上游没有可证明的“无需执行”返回语义，因此不会伪造 `skipped`。Adapter 不会以工具退出码推断任一 Todo 完成；缺少阶段事件只会进入 `review_required`。

| 游戏 | 受控 Todo |
| --- | --- |
| WW | 进入大世界、读取进度、梦魇巢穴、体力、日常奖励、邮件、战令免费奖励 |
| Endfield | 进入世界、邮件、体力、运送委托、信用、帝江号收菜、日常奖励 |
| GF2 | 进入主页、邮件、公共区/调度、普通体力、班组、委托每日奖励、巡录免费档 |

GF2 的 Manager 路径不会自动确认游戏内“全局自动”设置；该设置未由用户预先确认时会报错并保留客户端。终末地的 Manager 路径不执行外部命令，也不打开本地汇总文件。WW 强制清空上游附加任务，避免周常、合成或其他非勾选动作串入每日。

GF2 上游的“巡录/大月卡”实现只在“沿途行动”页识别领取按钮，并排除按钮文字含“航线解锁、解锁、升级、购买、充值、补差价、￥、¥”的目标；它是免费或已拥有奖励的领取玩法，不是购买逻辑。Manager 已把它映射为可独立勾选的 `battle-pass-free-track`，不会再用“安全锁定”阻止整批每日。

## 正式 GUI 更新等待

WW、Endfield 和 GF2 都必须先走 PyAppify 正式 GUI 更新检查。Adapter 默认给基础检查 `120` 秒；只要 `app.json` 显示 `update_state` 非 `idle`、`update_target_version` 非空，或文件时间/内容继续变化，就按最后一次进展滚动延长空闲窗口，单次等待硬上限默认 `1200` 秒。Manager 后续可通过环境变量调整：

- `YEYU_GAMER_FORMAL_UPDATE_CHECK_SECONDS`
- `YEYU_GAMER_FORMAL_UPDATE_IDLE_SECONDS`
- `YEYU_GAMER_FORMAL_UPDATE_HARD_CAP_SECONDS`

超时时仍使用兼容原因码 `formal_gui_update_check_timeout`；失败和重试失败仍使用 `formal_gui_update_failed` / `formal_gui_update_retry_failed`，但原因文本会附带最后观察到的 `update_state`、`update_target_version`、`update_error` 与已等待秒数。超时停止正式 GUI 前，Adapter 会尽量截取正式 GUI 或游戏窗口，作为 `game-ui-launch-timeout` / `image/png` 证据随 `tool-log-outcome` 一起上报。

## 已知停止条件

- 客户端 EXE、GUI 正式入口、`main.py` 或每日配置缺失：启动前报配置错误，不运行工具。
- 登录、验证码、权限、付费/购买、PVP 或不清楚的消耗：按 Manager 的人工接管边界停止，不伪造完成。
- 上游返回 `False`、抛异常或未写阶段事件：对应 Todo 失败或待复核；不会因进程退出码为 0 变为完成。
