# Android 模拟器运行时绑定

FGO、棕色尘埃2 与卡厄思梦境是 Android 游戏。`gamePath` 不应指向 `dnplayer.exe`：模拟器安装路径、实例身份、ADB 终端和 Android 包名是四个不同的事实。

`services/emulator_binding.py` 提供受限的 LDPlayer 生命周期边界：

- 配置只包含 `consolePath`、`adbPath`、`instanceIndex`、`instanceName` 和 `adbSerial`。
- Android 包名由 Manager 源码内的 `EMULATOR_GAME_PROFILES` 唯一绑定，调用方不能传入任意包名、ADB shell 或命令行。
- 启动顺序是 `ldconsole launch --index` → 等待指定 ADB serial → `ldconsole runapp --index --packagename`。
- 收尾顺序是 `killapp` → `quit`；只关闭本轮启动的应用和实例，预先存在的实例/应用保留。
- 此模块不点击 Android UI、不执行 Todo、不生成完成证据，因此不会把“模拟器启动成功”当成“每日完成”。

## 已核对的本机绑定

2026-08-30 只读巡检结果：

| 字段 | 值 |
| --- | --- |
| `provider` | `ldplayer` |
| `consolePath` | `C:\Game\LDPlayer9\ldconsole.exe` |
| `adbPath` | `C:\Game\LDPlayer9\adb.exe` |
| `instanceIndex` | `0` |
| `instanceName` | `雷电模拟器` |
| `adbSerial` | `emulator-5554` |

| GameId | 固定包名 | 本机状态 |
| --- | --- | --- |
| `FGO` | `com.bilibili.fatego` | 已安装 |
| `BD2` | `com.neowizgames.game.browndust2` | 已安装 |
| `CZN` | `com.tencent.czn` | 已安装 |

CZN 同一实例中仍存在国际服 `com.smilegate.chaoszero.stove.google`；它只作为排除项显示，`runapp` 永远只会使用国服 `com.tencent.czn`。

## 接入边界

这层已经解决“找不到游戏 EXE”的错误建模。FGO/BD2/CZN 的 Todo Adapter 仍需分别提供可验证的业务终态和同轮证据；在此之前，它们保持未晋级，不因 CLI 或 ADB 已打通而冒充完成。

LDPlayer 9.5.13.0 本机 `ldconsole help` 明确列出 `launch`、`quit`、`list2`、`isrunning`、`runapp`、`killapp` 和 `adb` 接口。雷电官方资料也说明多开实例可启动/关闭，官方论坛的 CLI 示例使用 `runapp --index <index> --packagename <package>`。

- [雷电官方：模拟器多开功能教程](https://help.ldmnq.com/docs/btJtNt)
- [雷电官方论坛：命令行与 ADB 接口](https://www.ldmnq.com/forum/30.html)
