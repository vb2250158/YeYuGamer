# scripts/heartbeat

无人值守心跳（见 `docs/heartbeat-playbook.md`）用的只读/typed-API 小工具。
**这里是唯一权威副本**（受版本管理）；历史上它们散在 `.cache/`，会被存储维护清掉，2026-09-22 移到此处。

用受管解释器跑：

```bash
PY=C:/Users/Admin/.workbuddy/binaries/python/versions/3.13.12/python.exe
```

| 脚本 | 用途 |
|---|---|
| `hb-status.py` | 一屏状态：`activeBatch`、`activeControllerLeaseCount`、各游戏 `req=完成/总`、`runtimeState`、`nextAction`。 |
| `hb-mutate.py` | typed API 变更（**都带幂等键 + 当前 stateVersion**）：`version` / `release-takeover <runId> <msg>` / `resume-run` / `resume-batch` / `cancel-batch <batchId> <msg>` / `start-daily-api` / `patch-enabled <GameId> <true\|false>`。直接打 API 能看到 409 的 `detail`。 |
| `hb-pids.py "<正则>"` | 按映像名报 pid / ppid / 启动时间 / 存活：`LIVE` 或 `exit=<退出码>`（`<非259>` = 已退出但仍被枚举的残留条目）。 |
| `hb-probe.py <pid...>` | **判定"残留条目 vs 真的卡住"**：分别用 `QUERY_LIMITED_INFORMATION` / `SYNCHRONIZE` / `TERMINATE` 三种权限 `OpenProcess`，报 `GetExitCodeProcess`、`WaitForSingleObject`、`GetProcessTimes`（含 exitTime）、真实线程数。 |
| `hb-mutex.py <probe\|threads\|reap>` | `probe <名字...>`：只读判断某个命名互斥体是否仍被持有（`CreateMutexW` + `ERROR_ALREADY_EXISTS`）——**遇到"客户端秒退/不建窗口"先跑这个**；`threads <pid...>` 列线程；`reap <pid...>` 从外部 `TerminateThread`（**实测对卡住的游戏客户端无效**，保留作诊断）。 |
| `hb-kill.py <pid...>` | 用 `PROCESS_TERMINATE` 句柄调 `TerminateProcess` 并报真实 Win32 错误码。**残留游戏进程实测恒为 `err=5`**（= `STATUS_PROCESS_IS_TERMINATING`），即用户态杀不掉。 |
| `hb-parent.py <pid...>` | 报这些 pid 是否还活着、是什么进程。 |
| `hb-procs.py` | 进程清单（带窗口/内存的粗略视图）。 |
| `hb-windows.py` | 枚举顶层窗口：pid / 可见性 / 最小化 / 标题——**判断"窗口真的存在吗"的可靠手段，别把"我们没找到窗口"当成"客户端没建窗口"**。 |
| `hb-foreground.py [--all]` | 报**前台窗口**的持有者（pid/映像名/线程）与它是否无响应（`SendMessageTimeout`+`SMTO_ABORTIFHUNG`、`IsHungAppWindow`），`--all` 再按 z 序看前 15 个。启动器报 `foreground-not-acquired` 时用它分清"有人正在用机器"与"前台线程挂死"。 |
| `heal-stuck-clients.py [--dry-run\|--force\|--selftest\|--no-observe]` | **无人值守自愈器**（计划任务 `\YeYuGamer\StuckClientHealer`，每 10 分钟）：7 重守卫通过才 `shutdown /r` 清除残留客户端互斥体。含防竞态：提交前观察队列 120s、提交后盯 50s，队列忙起来就 `shutdown /a` 撤销并回滚状态。★ **先认领再等待**：其余守卫全过但队列忙时，写下 `runtime\state\client-heal-pending.flag`（自过期，≤1500s），`DailySupervisor` 见新鲜标志即 stand down ⇒ 当前批次排空后下一 tick 就能重启（此前 `deferred: queue busy` 会把重启永久饿死）。用 `--dry-run` 时**不写标志**。退出码 0 健康 / 3 已认领或队列忙 / 4 拒绝 / 5 已重启（`--dry-run` 亦为 5）。`--selftest` = 只读打印"每个守卫看到的真实状态"（**改过这个文件就跑一次**）。日志 `runtime\logs\stuck-client-heal.log`。**心跳不要自己重启，交给它。** |
| `hb-uptime.py` | 开机时长（判断"是不是刚重启过"）。 |
| `undef_check.py` / `undef_check2.py` | `symtable` 扫"被引用但任何作用域都没绑定"的名字。**改完动态语言的门/异常处理路径后跑一次**（约 7 秒）。`undef_check2.py` 是当前用的那版。 |

约定：读操作可以随便跑；写操作只走 `hb-mutate.py`（Manager 是唯一热状态写入者，禁止改库或状态 JSON 冒充写入）。
