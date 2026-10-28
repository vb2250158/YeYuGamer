# 孤儿 pending_execution、每日优先级追加修复

## 结论

状态：QA 分支候选，仍为 **CHANGES_REQUIRED**，不是安装版修复或真实每日验收。没有修改真实数据库、配置、运行批次、计划任务或运行指向；没有调用真实运行 API、启动或关闭游戏。报送的现场记录没有在本轮重新查询数据库，不能据此宣称已复核安装版现场。

基线为 `f4ab6bf` 加前次候选 `34c4523`。主工作区的未提交 scheduler/serial 模块没有复制或提交到本分支。

## 已确认的源码原因

1. 原 `active_execution_summary()` 直接按 `game_runs.state='pending_execution'` 上报占用，不检查最后 Attempt、恢复意图和成员关系。
2. `cancel_game_run()` 无执行者分支只接收 queued/planned，不接收 pending_execution，导致合法待执行收尾走到“terminal cannot be cancelled”。
3. 正式 same-run resume 在提交意图后先把 Run 设为 pending_execution；后续 `_start_game_run` 抛异常时只把意图设为 successor_failed，没有撤销该 Run 的占用标签。
4. 每日默认顺序来自游戏列表的持久化 order_index。当前调度器还会在建批次之前按该顺序冻结周期目标、选择重置时间前缀，所以仅排序最终批次不充分。

## 已实现

### 孤儿识别与真实 Host 事实分离

Store 提供只读 `orphan_pending_execution_candidates()`，严格要求：

- Run 仍为 pending_execution；
- 最后 Attempt 为明确终态；
- 最新恢复意图是 successor_failed，且没有任何 successor_pending/successor_dispatched 意图；
- 无 starting/running/cancelling Attempt；
- 无 active controller lease，包括不能因到期就擅自忽略的租约；
- 所有批次成员均为 terminal/cancelled。

SQL 不能证明物理 Host 空闲。因此 Store 的无参 summary 保持保守；Manager 的 `execution_occupancy()` 结合真实 Host 读取后才省略已证实孤儿。Host 为当前 Run 或归属未知时仍报告占用；Host 明确属于另一 Run 时，仅该另一 Run 的候选得到排除。只读查询不产生事件或修改状态。

### 正式取消、失败回退与有界自动收尾

- 无 owned Attempt/lease/Host 的 pending_execution 可经现有幂等、CAS、事件和 receipt 路径取消；不改最后 Attempt 的历史结论。
- 有活跃 Attempt 的 pending_execution 仍走协作取消；有孤立 lease/Host 时拒绝假装无执行者。
- 正式 resume 启动失败经 `fail_resume_successor()` 处理：仅终态化该 intent 匹配、仍由最后 Attempt 拥有的未封账成员，然后在原子重查后恢复 Run 的最后终态。旧意图失败不能回写新意图的 reservation。
- 完成类 Attempt 只回退为 review_required，不从传输退出推导业务成功。
- 既有 orphan 使用统一预算字段 `orphan_pending_seconds`，默认120秒、上限7200秒。Manager 启动时及既有60秒 review watchdog tick 回收；名义最迟为预算到期后的下一次 tick，不宣称硬实时120秒。
- 收尾仅更新原 Run 并记录普通更新事件及 `game-run.orphan-pending-execution-reconciled`，包含原 Run/Attempt/intent、原因、预算和 `businessCompleted=false`；重复调用无新变更。

### 防止迟到启动和 ABA

正式 resume 传递 exact `resume_intent_id`。prepare 在原子提交前核对最新 intent、pending 状态和 predecessor，并绑定新 Attempt；Host handoff 再核对 exact successor。取消或超时后即便 Run 又被新意图设回同一个 pending_execution 字符串，旧启动也不能借此取得新 lease。

### 数据驱动每日优先级

默认源：`services/resources/daily_queue_priority.json`：

`WW → ZZZ → NTE → Endfield → StarRail → GF2 → PGR → FGO`

可通过 typed `daily_queue_priority` 覆盖，不改原 `order` 或任何真实配置。未知游戏稳定后置、缺席游戏不补造、同游戏账号保序。Manager 提供 `daily_execution_games()`；默认每日选取以及 manager-daily 的游戏/账号批次冻结入口已经使用该源。显式手动批次顺序保持原请求，不重排已冻结或恢复中的批次。

## PGR observation_chain_execution_active 调查

只读追到两个独立来源：

1. 异己 orphan pending_execution 被直接计为执行占用。
2. 正常多游戏运行批次中，PGR 的 queued 兄弟仍被列入 summary.gameRuns；serial guard 只豁免当前 Batch 摘要，却拒绝任何非 child Run。PGR 前置守卫通过且后面还有 queued 成员时，该冲突在源码上可达。

没有本次完整原始堆栈及同一时点 blocker 身份，不能确认 `1a181261` 具体命中了哪个来源。

Store 新增可选 owned_batch_id/current_run_id 上下文，只豁免同批、未封账、无歧义归属且无任何 Attempt/lease/待派发意图的纯 queued reservation。无参生命周期语义保持不变；Batch 摘要和异己执行所有者始终保留。活动 Attempt、外批归属或新意图会重新阻断。

## 必须在一致源码基线上接入的两处

这两处文件不在本分支的 HEAD 中，不能把 Store 层测试当成它们已接入。

1. 当前 `services/autonomous_daily.py::_scheduled_prefix()` 在首次冻结 `cycleTargets` **之前**，把游戏来源改为 `manager.daily_execution_games()`；不能重排今天已经冻结的周期/批次。其占用判断应复用 `manager.execution_occupancy()`，不能自行推断 Host 空闲。
2. 当前 `services/serial_observation_recovery.py::_current_ownership()` 的 summary 调用改为：

```python
active = manager.execution_occupancy(
    owned_batch_id=(owned or {}).get('batch_id'),
    current_run_id=child_run_id,
)
```

保留其 Host、人工门、Stop、generation、历史证明及二次回读检查，不用上下文过滤替换这些栅栏。然后在含该模块的提交上运行实际 serial guard/PGR admission 回归。

需要维护方先提供当前调度/serial/protocol 的一致已提交前置，再将这两处消费者接入。本分支没有替代它们创建第二套调度器，也没有将别人的未提交代码夹带进来。

## 验证

设置 `PYTHONDONTWRITEBYTECODE=1`，`PYTHONPATH=<QA worktree>/backend;<QA worktree>/backend/tests`：

```powershell
& D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe -m unittest -q test_orphan_pending_execution test_daily_queue_priority test_daily_queue_priority_manager test_queue_timeout_budget test_queue_parking_policy test_completion_contract_manager.CompletionContractManagerTests.test_failed_attempt_does_not_hold_the_review_barrier test_completion_contract_manager.CompletionContractManagerTests.test_review_barrier_expires_into_machine_seal
```

联合49项通过，最后记录8.069秒。新增 orphan 的14项使用真实临时 SQLite 和 Manager 方法，没有前次 parking 测试的协议兼容支架。正式 resume 失败测试只 mock 启动入口抛异常；ABA 测试进入真实 prepare，在 mock 计划构造处设置线程停顿并实际提交新旧意图变更，不冒称实际 package IO 或完整 Host 派发竞争。

测试涵盖幂等取消、失败立即回退、120秒边界、重复回收、完成不误报成功、真实 active lease、同 Run/未知 Host、迟到旧意图、fresh pending 保守处理、prepare ABA、exact successor、同批 queued reservation、外批/歧义/新增 owner、每日排序与账号稳定性。49项中的原 parking 支架限制仍见前次报告。

未执行全量测试、构建、安装、实际每日及原始 serial/PGR admission 调用链验收。前次基线的协议字段缺失、活跃 Host 静默交接、停车后的实际 same-Run 恢复等缺口仍未关闭；整体不得发布。
