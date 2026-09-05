# 星穹铁道（March7th Assistant）每日接入规范

> 本文只定义 YeYu Gamer 对本机 March7th Assistant 每日 runner 的编排契约；星铁的画面识别、战斗和导航仍归上游工具。

## 已登记步骤

`starrail.march7th-daily` 只接收下列五个、由 runner 白名单固定的操作。`attach-home` 是入口前置；选择后续操作时，Manager 会一并要求选择它。

| 操作 ID | 作用 | 验收边界 |
| --- | --- | --- |
| `attach-home` | 启动并确认可操作主界面 | 当轮主界面标记和窗口帧 |
| `spend-trailblaze-power` | 普通开拓力拟造花萼（金） | 当轮关卡完成，或确认普通开拓力不足 10 |
| `daily-training-objectives` | 补足每日实训 500（与领奖组成一个上游命令组） | 当轮任务页视觉证据 |
| `claim-daily-training-rewards` | 领取每日实训各档奖励（与实训组成一个上游命令组） | 当轮任务页视觉证据 |
| `verify-daily-task-list` | 复核任务行与奖励终态 | 当轮任务页 PNG/OCR 复核 |

跃迁/抽卡不属于此接入，仍由全局禁止策略处理。

## 选择、事件与验收

runner 从 Manager 获得本次已选 Todo，仅允许白名单操作进入 March7th。`daily-training-objectives` 与 `claim-daily-training-rewards` 对应上游同一个 `daily` 命令，必须同时选中；只选一项时 runner 会为已选 Todo 写入 `daily_training_pair_required` 的 `review_required` 终态，并且不会启动 March7th。

游戏只能由 Manager 的当前 GameRun 启动。runner 会同时校验进程名、固定 `StarRail.exe` 完整路径和启动时间，找不到属于当轮 Manager 的游戏进程时返回 `game_not_started_by_manager`，不会自行启动游戏。确认游戏已就绪后，runner 调用同一安装目录下、已绑定哈希的 `March7th Assistant.exe` 执行白名单命令。每项都写入同一运行的启动、日志/窗口产物和终态事件；任务页相关步骤还要求当前 attempt 的视觉证据。退出码只是一条运输结果，不能单独完成任何 Todo。

无人值守的每日不做 March7th 的原地更新检查：runner 在本次运行期间把 `check_update` / `auto_update` 临时写为 `false`（运行结束恢复原配置），并跳过可见的 `March7th Launcher.exe` 更新入口。原因是该工具带有已校验的本地兼容补丁，原地更新会让补丁哈希失效；而且通过系统代理访问 GitHub 的更新检查曾在整个 15 分钟窗口内挂起或被限流（2026-09-02 的 `formal_gui_update_timeout`）。需要更新工具时设置环境变量 `YEYU_STARRAIL_FORMAL_UPDATE_ENTRY=1` 走维护流程，并按隔离副本规则重新构建 Adapter。

登录、验证码、协议或人工确认会投影为 `human_required`；失败、超时或缺少视觉证据则保持 `blocked` / `review_required`，并保留游戏客户端。

## 运行前提

登记映射不等于获准执行。实际运行仍要求本机 installation binding 已晋级，`March7th Launcher.exe`、`March7th Assistant.exe`、游戏和配置路径的哈希或固定路径通过校验，以及 Manager 在同一运行中收齐所需证据。真实验证只能从“执行今日批次”进入，不能直接启动游戏、Launcher 或 Assistant 代替 Manager 流程。
