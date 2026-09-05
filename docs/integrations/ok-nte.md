# OK-NTE 每日接入

## 已核对的上游与本机来源

- 官方仓库：`https://github.com/BnanZ0/ok-nte`
- 2026-08-30 核对的最新发布：`v1.3.11`，tag commit `44de38e583e72f1ba921f2df2eb0e9cec6d4ab81`
- 官方源码要求 Python 3.12；GUI 入口为 `python main.py`，调试入口为 `python main_debug.py`。
- Manager 使用 ok-script 的固定 GUI 入口：`.venv\\Scripts\\pythonw.exe main.py --task 2 --exit`。`DailyTask` 是当前 NTE GUI 的第二个一次性任务；因此启动时会经过上游 GUI 及其 `update_pyappify` 检查，不再绕过界面调用 `ok.cli`。命令、任务索引和路径都由 Adapter 固定，页面和 API 不能传入任意参数。
- 官方 `v1.3.11` 已独立保存在 `C:\Game\ok-nte-upstream-v1.3.11`。当前实际工具仍是 `C:\Game\ok-nte-src`：该目录没有 `.git`，`src.config.version` 为 `dev`，因此不能把历史推测写成可证明的版本号。

现有本地目录包含 YeYu Gamer 的 selected-stage 补丁与私有配置。不得对它执行原地 `pull`、`reset` 或覆盖复制；升级时应在新的本机目录移植阶段钩子，只复制经确认的最小配置，再重新构建、回放、shadow、canary 和晋级。

## Todo 与上游阶段

| Manager Todo | OK-NTE 阶段 | 行为 |
| --- | --- | --- |
| `attach-home` | `attach-world` | 连接已由 Manager 启动的游戏并确认可操作主界面 |
| `mail` | `claim-mail` | 领取邮件 |
| `daily-activity` | `inspect-daily-progress` | 读取每日活跃度与都市活力 |
| `spend-city-vitality` | `spend-urban-vitality` | 按 Manager 本轮 profile 指定的异象界域类型、子目标与自然体力目标执行 |
| `claim-activity-reward` | `claim-daily-reward` | 领取每日活跃奖励 |
| `claim-cycle-reward` | `claim-period-reward` | 领取环期免费任务奖励 |

Runner 把 Manager 的选择转换为固定上游阶段列表。`DailyTask.py` 只执行这些阶段，并逐项写出 `started`、`completed`、`failed` 或 `skipped` JSONL；工具退出码为 0 但缺少阶段终态时，Todo 和整轮都保持 `review_required`。

## Manager 本轮参数桥

Manager 必须在运行 staging 目录的 `installation-binding.json` 中下发完整 profile；不允许 runner 从请求参数、上游 JSON 或历史默认值自行拼凑：

```json
{
  "schemaVersion": 2,
  "gameId": "NTE",
  "gamePath": "C:\\\\...\\\\NTEGame.exe",
  "toolPath": "C:\\\\Game\\\\ok-nte-src",
  "dailyTaskProfile": {
    "anomalyTaskType": "异能升级材料",
    "expRewardTarget": "甲硬币",
    "materialIndex": 5,
    "staminaTarget": 200,
    "autoCycleSubTask": false,
    "coffeeMode": "不执行"
  }
}
```

- `anomalyTaskType`：`经验与甲硬币 | 异能升级材料 | 弧盘突破材料 | 空幕`。
- `expRewardTarget`：`角色经验 | 弧盘经验 | 甲硬币`；只在经验/甲硬币类型生效，但仍必须完整下发。
- `materialIndex`：异能和弧盘为 `1..5`，空幕为 `1..6`；经验/甲硬币类型中仅作已校验占位。
- `staminaTarget`：`40..360`，且为 40 的倍数；Manager 默认值应为 `200`，不是与当前约束冲突的 `180`。
- `autoCycleSubTask`：本轮会映射给 DailyTask，但 Manager 运行中不调用 `sync_config`。若要实现“下次自动轮换”，应由 Manager 仅在 `spend-city-vitality` 有可信 `completed` 终态后更新自己的 NTE profile；失败或 `review_required` 时不轮换。经验目标按 3 项循环，异能/弧盘按 `1..5`，空幕按 `1..6`。
- `coffeeMode`：完整传入 `不执行 | 领取/补货 | 完整自动化`；当前只有 `不执行` 可启动工具，另两个值在进程启动前返回 `review_required`。

Runner 把该对象原样放入 `YEYU_GAMER_NTE_PROFILE`。本地 `DailyTask.py` 只在 Manager stage hook 同时启用时读取，先复制 `self.config`，再映射到上游中文键；运行结束后恢复原配置对象，不改 `configs/DailyTask.json`。

## 启动与收尾

Manager 先启动配置中的 NTE 游戏可执行文件，再启动 NTE Adapter。Adapter 固定调用工具目录自己的 `.venv\Scripts\pythonw.exe` 和 `main.py`，工作目录固定为已打包校验过的本机 `toolPath`，不要求用户按快捷键，也不启动第二份游戏。

工具 stdout/stderr 会被 runner 收拢，不能混入 Adapter JSONL。工具完成后 runner 退出，Host 的 Windows Job Object 回收遗留工具子进程；随后 Manager 关闭本轮自己启动的游戏并继续队列。登录、验证码或其他 `human_required` 现场仍由 Manager 保留客户端。

## 明确不在当前 binding 内的玩法

- `cafe` / 一咖舍不绑定。玩法选择会保存在 Manager profile 中，但当前旧源码的“领取/补货”和“完整自动化”是复合路径，可能补货或发生费用语义；本轮会在工具启动前进入 `review_required`，不是丢弃该玩法参数。
- `uncovered-objectives` 不绑定。方斯消费、赠礼、弧盘升级等随机每日行尚无稳定处理器。
- 抽卡、购买、充值、账号设置、PVP、交易、分解、强化和不可逆选择全部不进入 manifest。

## 构建和测试

```powershell
.\scripts\Build-YeYuGamerNteAdapter.ps1
.\scripts\Test-YeYuGamerNteAdapter.ps1
.\scripts\Install-YeYuGamerNteAdapter.ps1
```

前两步只生成候选包并使用假工具做 selected-Todo replay，再对真实绑定执行不启动进程的 shadow probe。无参数安装只把包登记为 `installed-unpromoted`；正式晋级还需要同一候选的测试证据和 Manager diagnostic canary，两份都匹配后才可传给安装脚本。
