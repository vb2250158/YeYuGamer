# Host 未派发超时收尾：仍 CHANGES_REQUIRED

## 范围与结论

2026-10-28，基于 qa/queue-timeout-20261028 的 76d03a9。只操作隔离工作树与临时离线测试数据，不连接生产 Manager、不启动游戏、不安装、不合 main、不 push。

原独立测试取自 f253225 的 `backend/tests/test_queue_timeout_independent.py`，blob `0d9698449bd82700d135c9e113881b45a11ee0ff`。保留原断言，导出后核对除换行外内容一致。该组原为 9/12，本切片首次复跑为 10/12：dead/missing-callback 通过，simulated hung 与 killed-owner 仍失败。不能报告三项全部关闭。

## 改前根因与独立取证

1. 已确认：`ManagerService._prepare_run_attempt` 原子建立 starting Attempt、controller lease、active membership 后，没有持久化事实证明启动副作用是否开始。`_run_batch` 缺 callback 即把整个批次封为 reconciliation，即使根本没派发。原 dead 测试替换 `_start_game_run`，只调用真实 prepare；这条路径可通过可信的未派发登记安全收尾，而不是猜测进程是否死亡。
2. 已确认：另外两项独立测试直接赋 `_active_process=SimpleNamespace(pid=87654321, poll=...)` 与 `_active_plan`，绕过真实 Host 创建，没有所属 Job、进程出生时间、reader/callback 停止证据。不能因 poll()==0、PID 不存在或 cancel 返回 True，就推断所有输入执行器静默；也不能在实现里识别测试 fake 来放行。
3. 已确认的后续实际缺口：`adapter-host/YeYuGamerAdapterHost.cs` 185 行先 `runner.Start()`，186–187 行才建立和分配内层 Job；Python Manager 未持有这层 Job。`adapter_host.py` 的初始请求与 cancel 仍在同步管道 write/flush 中。仅将等待改为有限时长，不能产生丢失的进程树静默证明，也不能保证阻塞管道及时返回。

Goodall 独立审查 prepare/claim/recovery 的数据来源、调用路径和事务边界；Ohm 独立审查 native Host 所有权及原三红夹具边界。方案先于实现：只有可信 reserved、精确原 scope/lease generation/member、无人启动且超时，才能恢复；claimed、未知旧记录、遗留 owner、已有副作用一律不以本路径放行。

## 状态机与证据

`prepare -> reserved -> claimed`，claimed 不可回退成 reserved。claim 必须在日志、关闭其它游戏、客户端/模拟器启动、Host 派发之前提交。Host 在 probe、材料写入与 Popen 前再检查原 claim；检查异常直接拒绝，不伪造 callback。

`reserved + deadline elapsed + exact untouched scope + no owner/effect -> expired`。单个 Store.atomic 事务更新原 Attempt 和 Run 为 review_required、completed_at、原 lease 为 revoked、唯一原 member 为 terminal，并追加事件。Todo 不改为成功、不重建队列、不创建 successor。不动其它 Run 或后继 lease。任何一步写入失败整笔回滚。

独立 `attempt-dispatch` 资源记录 version、manager/game/account/run/attempt/game-day/cadence、计划和账号快照摘要、controller lease id/generation、desktop、原 member 快照、deadline、claim/expiry 时刻、reasonCode 和 businessCompleted=false。持久化摘要，不持久化 raw fencing token 或 cancel authority。该事实不复用人工接管 parking 标志。过期后的启动、结果及事件由原 claim/终态/lease 栅栏拒绝。

## 预算

| 环节 | 唯一来源 | 默认 | 到期行为 |
| --- | --- | --- | --- |
| 准备后未派发 | 原执行 plan.timeout_seconds + queue_timeout_budgets.json callback_overhead_seconds | 原计划预算 + 40 秒 | 只对可证明未派发的 reserved 收尾为 review_required，撤销原 lease，继续下一游戏 |
| 已派发 callback 取消宽限 | 同一 JSON callback_cancel_grace_seconds | 15 秒 | 保留原有保守 reconciliation；本切片不能证明未知输入执行器已停止 |
| Host stop wait | 同一 JSON host_stop_grace_seconds | 5 秒/次 | 沿用 76d03a9，不构成整个管道/reader/callback 的总 deadline |

## 剩余缺口与转交

- hung/killed-owner 的实际安全自动恢复仍需 Manager 持有、在任何不可信 Runner 副作用前建立的 native containment，以及 root/children/reader/callback 的一致静默凭证；丢失或旧 owner 不能靠任意 PID 枚举清锁。native Job 单独通过也不是完整桌面输入安全或游戏资格验证。
- 必须继续消除同步 request/cancel pipe 的无界等待，协调 watcher 和恢复方的互斥收尾、迟到 callback 栅栏，再接入相同原 Run 的事务收尾和队列通知。
- 本切片不修改独立审查会话拥有的原三红文件。后续正向用例应走真实登记的 owned execution；未知旧 owner 的负向用例仍须保留。
- PGR/ZZZ/WW 跟进与生产绑定仍按前轮缺口清单，不在本切片宣称完成。没有新的外发或生产操作要求。

实际提交号、最终测试命令与计数、原始日志和 native 资格验证状态以 Archives/QA 本切片收据为准。

## 本工作树实际验证

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH="$PWD\backend;$PWD\backend\tests"
$python='D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe'
& $python -B -m unittest -q test_undispatched_recovery test_host_dispatch_guard test_host_stop_budget test_manager_host_handoff test_queue_timeout_budget test_adapter_protocol.AdapterHostSupervisorTests test_manager_adapter_execution.ManagerAdapterExecutionTests.test_resume_creates_one_successor_for_only_eligible_subset test_manager_adapter_execution.ManagerAdapterExecutionTests.test_sealed_blocked_resume_creates_one_continuation_and_review_barrier
& $python -B -m unittest -q test_resume_reconcile test_review_lease_queue test_queue_parking_policy test_orphan_pending_execution test_daily_queue_priority
```

第一组 80 pass、0 skip，12.742 秒；第二组 49 pass、0 skip，6.820 秒。覆盖冻结期限、超时前/后的竞争、原子回滚、租约 ABA、拒绝旧记录/owner/已有事件、迟到结果不改后继、Host guard 前置、同 Run successor 与历史 terminal member、原 Host 交接及预算。期限在 prepare 冻结，等待用该 deadline 剩余值；成功恢复 reserved 不调用 cancel、不等 grace。

原独立组最终 10 pass / 2 fail，2.688 秒；dead/missing-callback 已关闭，不把 prepare-only 回归扩大成真实已启动 Host 的证明。日志 `independent-final.log`。

原生 Job 独立资格原型真实执行 15 pass、0 skip，0.877 秒，覆盖 hidden self-join/helpers、hung、killed root、nested breakaway、独立替身 client 保留与无效句柄拒绝。但 Host/C#/Manager 未集成，命名 Job ACL、句柄生命周期、真实独立 client 及输入 SDK 链路仍需审计。该原型和测试只保存在归档 `native-qualification`，没有把未接线模块放入交付代码，不抵扣两项剩余红测。

扩大到整个 test_manager_adapter_execution 的联合命令在 180 秒预算后中止：只终止核对过命令行和父子 PID 的本任务测试进程。该次无完整测试计数，不算通过，也未采到具体阻塞栈，不能断言是哪项测试所致；相关日志和中止记录保留为验证缺口。没有为拿绿数修改旧整队等待测试，也没有运行全量构建或游戏实测。
