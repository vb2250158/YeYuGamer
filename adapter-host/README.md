# YeYu Gamer Adapter Host

Adapter 是 YeYu Gamer 与一个具体游戏自动化工具之间的转接层。一个游戏对应一个 `GameIntegration`，而不是一份跨游戏通用脚本。

## 每个接入必须提供

1. **步骤目录**：从工具源码或工具配置得出的真实步骤、顺序和可选性。
2. **参数映射**：将 Manager 保存的工具参数写入该工具需要的配置。
3. **固定启动器**：使用已配置的游戏路径和工具路径启动该工具。
4. **进度桥**：把工具源码的 `started`、`completed`、`failed`、`skipped` 阶段事件转发给 Manager，并附上日志或截图引用。

## 约束

- 用户未勾选的步骤不能被工具顺手执行。
- 工具退出码不能替代逐步骤事件。
- Adapter 只映射和转发；不复制工具的点击、识别或游戏策略。
- 工具升级后，只更新该游戏的 Integration 与契约测试。
- Host 将每轮 runner 及其创建的工具子进程放进独立 Windows Job；本轮结束或 Host 异常退出时自动回收，不把工具残留带到下一游戏。游戏客户端由 Manager 按启动基线单独管理。

## Host 与执行包的完整性边界

- 当前 Host 每次探测或使用执行包前，都按自己的 `install-manifest.json` 校验固定目录、`host.exe` 大小和 SHA-256；Host 换版不会放宽这项自校验。
- 每个 promoted 执行包独立校验 manifest、声明文件、payload digest、promotion receipt 文件摘要，以及 Manager ledger 中的完整 receipt 文档。
- receipt 的 `hostEntryPointSha256` 记录晋级时无进程 Canary 使用的 Host 构建，是不可变的来源证据，不是跨 Host 版本的兼容键。运行时兼容继续由执行包已有的 `hostPackageId` 与 `protocolVersions` 合同决定；兼容 Host 换版不要求重新晋级未变化的游戏执行包。

`openkuro-runner/Program.cs` 是 OK-WW、终末地与少前 2 的固定转接实现。它只把已选择的操作与埋点上下文交给上游 `DailyTask`，轮询上游写入的 `started`、`completed`、`failed`、`skipped` 阶段事件，并逐项回传 Manager。三个游戏的路径、选择与收尾边界见 [OpenKuro 每日接入规范](../docs/integrations/openkuro-dailies.md)；鸣潮参数的具体含义见 [OK-WW 接入规范](../docs/integrations/ok-ww.md)。

### OpenKuro 正式 GUI 更新等待

`openkuro-runner` 等待 PyAppify `app.json` 进入可启动状态时，会在检测到更新状态、目标版本或文件内容继续变化时延长空闲超时，并在硬上限内重试一次正式 GUI。Manager 可用这些环境变量收紧或放宽窗口：

- `YEYU_GAMER_FORMAL_UPDATE_CHECK_SECONDS`：基础等待窗口，默认 `120`。
- `YEYU_GAMER_FORMAL_UPDATE_IDLE_SECONDS`：最后一次更新进展后的滚动空闲窗口，默认跟随基础窗口。
- `YEYU_GAMER_FORMAL_UPDATE_HARD_CAP_SECONDS`：单次正式更新等待硬上限，默认 `1200`。

完整模型见根目录 [ARCHITECTURE.md](../ARCHITECTURE.md)。
