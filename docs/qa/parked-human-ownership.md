# Parked human owner 最小修复与独立验收

## 结果

2026-10-28：完成 QA helper、固定 SHA 的 human_gates 集成补丁与独立测试。最终联合回归 **37 tests / OK，9.370s，exit 0**；其中本次独立文件 **16 项**。未修改 main、forks、Manager、PGR sidecar 或 PGR 测试，未发布或启动业务。

仅解除旧人工 gate 对其他队列成员的全局占用；不解决原人工问题，不清 blocker，不标完成，不授权原 Run resume，不替代全局 Host/lease guard 或 OS 静默证明。

## 归属与备份

所有写入均在 `D:\Game\Automation\_qa\queue-timeout`：

- 修改 `backend/yeyu_gamer_manager/services/queue_parking.py`，只增加纯查询 helper `parked_human_gate_yielded` 及其 imports，原 parking 状态转换不变。
- 新增 `integration-patches/parked-human-ownership.patch`：给缺于 QA HEAD 的 `human_gates.py` 增加 helper import 和 owner 候选过滤。patch 含 reference SHA；测试必须先核对 SHA，再在临时副本实际 `git apply`，不修改维护源码。
- 新增 `backend/tests/test_parked_human_ownership.py` 与本报告。
- 写入前备份：`.cache/parked-human-ownership-before-20261028/queue_parking.py`、`human_gates.py`，附 `SHA256.md`。

写入前 queue_parking SHA256：`BED8D9EE8675782598C0F22F8EC47E251054957834345FEDF2D6648BFFCC79DB`。

固定 reference：`D:\Game\Automation\YeYuGamer`，仅用于读源码；`human_gates.py` SHA256：`A2EA026DD0834F7C89500C8143DB6AA8D91EBA701CFE705E0C2F210ACFC4E2F0`，测试后仍相同。另在测试内固定 Store、serial guard 和 launcher-observation 源 SHA，任何漂移均拒绝回放。

最终产物 SHA256：

| 文件 | SHA256 |
| --- | --- |
| queue_parking.py | `2E139AA86DB2832E127EB9FA8196AC6D19A9DC70B8813F0C59CF8E4824DF5852` |
| test_parked_human_ownership.py | `983B6E6241EC758479E30F4EC685FC970B203604D17E0E168283B93767657FB7` |
| parked-human-ownership.patch | `20C2FAA0313A33937EFA426506642E7DFAFB5E500F193D88ED1743787B006258` |

## 安全条件

helper 只读 Store，在其现有 RLock 下核对关联事实，不访问 Host、OS、API 或文件，也不等待、写入或清 blocker：

1. 最新 Attempt 是原 human_required 终态且有 completed_at；Run 为 execute/review_required。
2. resource 必须是原 Attempt 的 parked human_takeover，而非 awaiting、released、reclaimed 或 review parking；Run/Attempt/Batch/Game 精确匹配，ownershipReleased=true、businessCompleted=false。
3. 同 Batch/Run membership 必须 terminal/review_required，latest Attempt 精确一致；Batch 同 cadence、execute、未 sealed/取消、无 active cancel。其他 Batch 的非终态 membership 拒绝。
4. 冻结 budget 必须是有限非负数，expiresAt 与 startedAt+budget 完全相等；必须 expiresAt <= parkedAt <= 当前时间；拒绝未到期、未来、无时区、畸形和溢出值。配置后续变更不重置既有 deadline。
5. 使用 events 既有 entity 范围查询核对正式 `queue.parking.expired` 事件；resource 单独标 parked 不足以授权。事件记录的每个字段均必须仍与 resource 一致。允许正式 human-release 后追加记录，不把附加字段当新授权。
6. 同 Run 任何 active lease 都拒绝；冻结 lease 的 ID/generation、revoked 状态和 queue-expiry 原因必须匹配。无 active lease 的原 launcher 路径允许先前已结束的旧 lease，但拒绝 startedAt 之后创建或结束的新 lease。
7. 活跃 successor intent、新 Attempt、foreign/迟到 human blocker、迟到创建的 blocker 都不被旧收据覆盖。集合达上限 fail-closed；损坏或缺少记录同样拒绝豁免。

正式事件与关联 Store 都是可信 Manager 写入的内部事实；本改动防止裸 resource/错身份/陈旧记录被当作授权，不声称能抵抗拥有完整数据库任意写入权限者同时伪造全部 ledger。没有引入秘密、外部 token 或新业务入口。

## 独立测试方法与覆盖

独立文件显式要求 `PARKED_HUMAN_REFERENCE_ROOT`，不自动导入 main runtime。测试读取固定 SHA 的 human_gates 原文，在 QA `.cache` 临时副本应用本补丁并 import；QA 模块实际使用本次 helper。QA HEAD 缺少的 `list_human_gate_candidates` 和 `daily_stop_owned_run_ids` 从固定 Store reference 提取原始方法体，在临时 SQLite 上执行真实 SQL，**没有 mock owner 过滤结果或 SQL 返回行**。

fixture 使用真实 `_prepare_run_attempt`、TodoAttempt、绑定到当前 Attempt 的 artifact 与 `create_or_reuse_todo_blocker`，再由真实 `park_human_gate` 完成停车。查询前后 event sequence、SQLite total_changes 不变；blocker 全记录保持原样。

覆盖 16 项：

- 原版统一 owners 非空；真实带 active blocker parking 后补丁版 owners 为空，read-only availability 允许。
- 正式 `create_run_control_request(..., takeover)` 回收后，resource 为 reclaimed，统一 owners 重新阻断，blocker 不变。
- 新 Attempt、新 generation lease、membership 状态/Attempt 指针不匹配均阻断，不撤销新 lease。
- 缺少正式 expiry 事件的伪 record、错 Run/Batch/Attempt/Game/lease/gate、错误 ownership/business 标志、畸形日期均阻断。
- 未到期/未来时间、awaiting 即使超时仍阻断；真实非零 0.01 秒预算到期后可让队，修改当前配置不会重置冻结预算。
- launcher 提前结束 lease 的合法路径可让队，不要求二次 revoke。
- foreign live membership、foreign human owner、迟到 human blocker 保持阻断。
- 独立加载固定 serial_observation_recovery 的真实 `_current_ownership`、`require` 和真实 launcher-observation idle 查询函数，接到补丁版统一 owners；同 Batch 第二 child 能通过当前 ownership 检查。随后仅给原 Run 注入仍活跃的 Host fixture，真实 guard 仍以 `host_owned` 拒绝。

最后一项验证的是 **PGR admission 使用的共享 serial guard 源路径**，第二 child 使用现有 StarRail 测试 fixture；不是完整 PGR 七义务、native permit、OS Job 或游戏运行验收。全局 Host guard 未在实现中修改，测试没有把退出进程当作 quiescence。

## 最终回归

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='D:\Game\Automation\_qa\queue-timeout\backend;D:\Game\Automation\_qa\queue-timeout\backend\tests'
$env:PARKED_HUMAN_REFERENCE_ROOT='D:\Game\Automation\YeYuGamer'
& D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe -m unittest -q test_parked_human_ownership test_queue_parking_policy test_review_lease_queue test_execution_plan_baseline_contract
```

最终结果：37 tests，9.370s，OK，exit 0。`git diff --check -- backend/yeyu_gamer_manager/services/queue_parking.py` 通过。

初次独立回放有两项 fixture 错误：试图 terminal→active membership，以及复用唯一 fencing hash 创建新 lease；已分别改成合法 resume_pending 转换与临时库新 fencing identity 模型，未放宽生产校验。后续最终回归包含全部修正。

没有运行父方/PGR agent 正在修改的 suite，没有启动 Manager 服务、游戏、后台业务或查询真实数据库。主方仍需按集成流程应用此 human_gates 补丁并合并 QA helper；本交付不是已安装修复。
