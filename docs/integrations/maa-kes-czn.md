# CZN 国服 Maa_KES selected-Todo 接入审计

## 已审计上游与本机材料

- 主上游：`https://github.com/miaojiuqing/Maa_KES`，release `v1.2.3`，commit `438368b5392188410728000ccccdde13a0241ee6`。
- 官方 release 资产：`MaaKes-win-x86_64-v1.2.3.zip`，SHA-256 `ac16a0b86f209147ccc8557292ed452c0972abd7a13fffa66caa6375b3a20e6e`。
- 干净源码副本：`C:\Game\Maa_KES-upstream`；独立运行时：`C:\Game\MaaKes-runtime-v1.2.3`。两者均未覆盖其他本地补丁。
- 对照上游：`https://github.com/baoxin1100/ok-kes`，release `v1.4.2`。其源码没有等价的登录/日常领奖 selected task，ADB 包名还是占位注释，因此不作为每日主执行器。
- MaaFramework 一手接口依据：`MaaTaskerPostTask`、`MaaTaskerWait`、`MaaTaskerAddSink`，以及 `Tasker.Task.*`、`Node.Recognition.*`、`Node.PipelineNode.*` 回调。

## Android 身份与启动边界

Maa_KES 的 `tasks/进入游戏.json` 把国服固定为 `com.tencent.czn`。Manager 现有 LDPlayer 绑定同样固定：实例 `0`、实例名 `雷电模拟器`、ADB `emulator-5554`；国际服 `com.smilegate.chaoszero.stove.google` 只作排除项。

Manager 应先执行模拟器生命周期：`launch --index 0` → 等待 `emulator-5554` → `runapp --index 0 --packagename com.tencent.czn`，再调用 Adapter。Adapter 只连接 Manager 下发的精确 ADB 路径与 serial，不接受包名、命令、坐标或任意参数。

## 最小 selected-Todo 映射

| Manager operation | Maa entry | 接受的业务终态 |
| --- | --- | --- |
| `login-bonus` | `进游戏-任务开始层` | 命中 `进游戏-点击领取签到奖励` 为 completed；仅到 `已进入首页-结束任务` 为 skipped |
| `achievement-schedule` | `领奖-日常活跃-任务开始层` | 必须命中 `领奖-日常活跃-奖励领取完毕`；`不可领取奖励` 为 review_required |
| `arkhianon-supply` | `领奖-补给-任务开始层` | 必须命中 `领奖-补给-奖励获取界面-奖励领完了` |
| `simulation-stamina` | `清体力-任务开始层` | 必须命中 `清体力-当前体力不足`、`清体力-体力小于20已耗尽` 或 `清体力-当前体力不足以进行挑战` |

Maa task `Succeeded` 或进程退出本身不构成 Todo 完成。Runner 把命中节点、Maa status 和本轮体力 profile 写成 `tool-log-outcome` JSON artifact。

本轮不绑定 `policy-office`、`garden-cafe`、`verify-daily-reward`、邮箱、推图、出击、肉鸽、周本、商店、抽卡或删档。邮箱虽有独立入口，但当前 CZN Todo 目录没有对应定义，不能借其他 Todo 偷跑。

## Manager-owned CZN profile

`adapter_host.py` 需要把 CZN 加入 `dailyTaskProfile` 的精确字段集合：

```json
{
  "staminaCategory": "成长",
  "staminaTarget": "单元币",
  "battleEfficiency": 4,
  "untilExhausted": true
}
```

字段边界：

- `staminaCategory`: `成长`、`主战员`、`辅战员`、`潜能`。
- `staminaTarget`:
  - 成长：`单元币`、`主战员升级材料`、`辅战员升级材料`；
  - 主战员/辅战员：`前锋`、`守卫`、`游侠`、`猎人`、`奥义师`、`操控师`；
  - 潜能：`热情`、`秩序`、`本能`、`虚无`、`正义`。
- `battleEfficiency`: 整数 `1..5`。
- `untilExhausted`: 当前只能为 `true`。

`记忆碎片` 被排除，因为上游明确标注挑战次数尚未重构；`挑战(周本)` 不属于每日。Runner 通过 run-scoped `pipeline_override` 覆盖类型、目标模板与战斗效率，不修改 Maa_KES 的永久配置。

同时存在 profile 与 emulator binding 时，Runner 要求 Manager 暂存 schema 3 的 `installation-binding.json`，字段为：`schemaVersion`、`gameId`、`gamePath`、`toolPath`、`dailyTaskProfile`、`emulatorBinding`。`toolPath` 必须等于已固定的 Maa_KES runtime；`emulatorBinding` 必须恰好包含 `provider`、`consolePath`、`adbPath`、`instanceIndex`、`instanceName`、`adbSerial`。

## 当前晋级状态

`0.1.0-czn.1` 已构建为 candidate，静态 profile replay 接受 100 个安全组合、拒绝 4 个危险/无效组合；binding shadow probe 验证 resource tree，且未启动游戏、模拟器或 Maa GUI。没有真实 Manager canary 和同轮视觉终态，因此 `promotion.status=candidate`、`executionReady=false`、canary digest 全零，不能宣称 promoted。

候选安装脚本只允许安装 unpromoted 包。本轮安装因调用环境的 `YEYU_GAMER_LEGACY_EXECUTION_ENABLED` 已开启而被安全拒绝；需要在 Manager 执行闸门关闭、无活跃 runner 时重试，之后仍需真实受控 canary 才能单独走 promotion。
