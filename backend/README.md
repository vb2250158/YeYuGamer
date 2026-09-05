# YeYu Gamer Manager

Manager 是编排器的本机状态与队列服务。它作为 `YeYuGamer.exe` 的内部组件，通过 `127.0.0.1:8877` 向该 EXE 提供的 WebGUI 与诊断 CLI 提供 API。

## Manager 负责的事实

- 每个游戏的游戏路径、工具路径和工具参数。
- 用户勾选的今日步骤。
- 队列顺序、当前游戏、当前步骤和每项运行状态。
- Adapter 交回的步骤事件、错误摘要、日志和截图引用。

Manager 不实现游戏画面识别、坐标点击或游戏规则。这些属于各自的上游自动化工具。

## 关键数据

| 数据 | 说明 |
| --- | --- |
| `gamePaths` | 每个游戏的游戏/工具路径 |
| `dailyToolProfiles` | 工具参数，例如 OK-WW 的体力路线 |
| `dailyTodoSelection` | 用户今天勾选的步骤 |
| Run / Todo 状态 | 本次队列和每一步的 `pending`、`running`、`completed`、`failed`、`skipped` |

WebGUI 通过 API 编辑前三项；Adapter 通过结构化事件更新最后一项。任何页面或外部调用者都不应直接写 SQLite 或伪造完成状态。

## 开发与验证

后端测试不会启动游戏。修改 Manager 或 Adapter 协议后，至少运行对应 Python 测试，并验证一次配置保存、队列创建和步骤事件投影。

完整边界和事件流见根目录 [ARCHITECTURE.md](../ARCHITECTURE.md)。
