# PGR guard 与预留补全（任务 2/3）

## 最终当前结论：严格自身 intent 例外与原子边界

以下覆盖本文后续两轮历史记录。Store fresh pending 过滤已完成，父方已提交 `709135b`（包含 Store 与 17 项 SQL 回归）；当前这两个文件与父方 HEAD 无差异。本 Agent 未执行任何提交。旧 mixed_fixture 的两项红灯是未知源归属反证，不再列作 Store 未修缺陷。

### 已补齐的第五份源补丁

`integration-patches/pgr-owned-resume-store.patch` 仅修改只读 reference Store 的 `serial_observation_execution_blockers`，17 insertions / 4 deletions。它不是整文件快照，也不包含无关 main 改动。原文件：`backend/yeyu_gamer_manager/store/sqlite_store.py`。

- baseSHA256：`DB85CD6497EAC66109528B64F848725A969B3B2B7BD71C56FCDAEFA6F2C56E45`
- baseGitBlob：`e279195a760a4fd64662f7c07e4eab0f04e522e5`
- 接受的唯一例外是 PGR admission 刚刚验证、且与当前 Store 完整 intent record 相等的特定 resumeIntentId；SQL 只排除该 ID，其余 pending/dispatched intent 继续全局阻塞。默认无 proof 不变，不按整个 owned Run 排除 resumes。
- QA 的对应方法已随父方 709135b 纳入 HEAD。此额外 patch 用于当前缺少 QA 模块的 reference main Store；不是要求在已含该方法的 QA 上重复应用。

四份业务文件的补丁仍在 `integration-patches/pgr-queue-followup.patch`，现在是 119 insertions / 11 deletions：serial 17/6，daily_admission 85/1，unused_reservation 16/3，daily_successor 1/1。其四个逐文件 baseSHA 沿用下文表格及 successor 补记，均未改变。当前父方 709135b Store 的 SHA256 为 `B4CBBFF74AD8E40D48C301B63E4B5603DD9A811F3B7D1C4C63142E24C6AFED9A`。

### 自身 pending 的精确资格

新增 `pgr_daily_admission.owned_resume` 只由 PGR 上下文 guard 调用，每次从 Store 重新读取而非缓存：

1. bounded 完整本 Run intent 历史；恰好一个 active intent，并且是按 created_at/intentId 排序的 exact latest。Run 必须 PGR/default/daily/execute，冻结七项 scope 和账号/generation/周期仍通过 ledger.guard。
2. batch、资源 proof、runId、reservationId 与 member.resume_intent_id 精确一致；batch running、未 seal、无活动取消。
3. ledger ready 的首次资格只接受 successor_pending，successorRunAttemptId=None、membership resume_pending、pending 首项 operation/phase、index=len(steps)、sourceAttempt 等完整 decision 字典全部一致。所有既有 Attempt 必须就是 ledger steps 且已 completed，没有任何新/未知 Attempt 或 active lease。
4. 前驱必须等于 ledger 上一 terminal step、lastClosedRunAttemptId 和 intent.predecessor/sourceAttempt；原生 permit 已消费，Attempt completed、exitCode=0、protocolValid、clean transport 与 cleanup receipt 全部通过。未消费当前待执行步骤的预算不会被补充，历史步骤不重写。
5. 已创建 preparation 的第二阶段不能继续拿 ready proof：必须唯一精确 current step/Attempt、member.latest_run_attempt_id 一致、至多一个且属于该 Attempt 的 lease，并校验已存储的非原生 pgrPreparation（Run/Attempt/Todo/operation/phase，nativeKind=daily，nativePermitIssued=False，resourceActionsAllowed=False，无 pgrDailyStep）。同步 Host preparation 回调期间 intent 仍 pending 时，只接受 ledger preparing、没有已绑定 successor、没有原生 permit 的该唯一 preparation；已 dispatched intent 则必须绑定 exact successor Attempt。真正 dispatched ledger 另验已持久化 permit 的 reservation/index/operation/phase/Run/Attempt。

没有通过上述事实就不产生 SQL 排除参数。unknown kind、foreign batch/reservation/Todo/source、错误 index/phase、非 latest/重复 active、错误 member 指针、额外 Attempt/lease、过期或漂移 record 均失败关闭。没有新增 request 字段或外部授权入口。

不存在 active intent 也不是自动放行：已有 intent 历史的 latest 必须为真实 Manager 使用的 `terminal` 或原 fixture 使用的 `successor_terminal`；已有原生步骤消费记录的 ready/preparing ledger 必须持有有效 active intent。新增回归证明 successor_failed 不能授权 ready ledger，也不能在 preparation 创建后撤销 intent、仍领取 native grant。另以真实临时 DB 的残留前驱 active lease 验证 pending 资格拒绝。

### TOCTOU 与最终提交边界

`serial_observation_recovery._current_ownership` 将 **owned_resume qualification 与 blockers SQL 放在同一个短 Store.atomic() 内**。只读 SQL、ledger/receipt 字典检查在该区间；Host active_execution、execution_occupancy、原生环境/文件检查都在其外，cleanup 检查函数在模块初始化时导入。不是先拿 proof 后仅重验 intent、忽略 member/ledger 的窗口。

新增双连接 SQLite 回归在 qualification 返回后尝试 BEGIN IMMEDIATE + member 变更：真实第二连接立即得到 locked，qualification 和 blockers 都观察到 in_transaction=True；事务结束后同一变更可写入，再次资格检查明确拒绝。另一个回归只变 ledger、不变 intent，仍重新判定为 resume_decision_changed。测试 wrapper 只观察原函数，未替换为通过结果；生产原子区没有事件等待或线程 join。

这仍不宣称“第二次 guard 重读等于最后提交”。原 `Manager._prepare_run_attempt` 在 `store.atomic()` 内再次调用 guard_pgr_admission 然后 reserve ledger/创建 Attempt；原 `finalize_preparation` 在 handoff lock + store.atomic() 内重新 guard/fresh 文档比较后提交 grant。新资格证明在这些既有原子边界内重新计算，不被带出当永久许可使用。

### 最终验证与遗留边界

- 最新复核 QA HEAD 为父方 **41c43e2**，PGR 新回归独立重跑 **26/26，exit 0，26.874s**，`.cache/pgr-followup/pgr-head-confirmed.log`；最终完整组合中也为 26/26。含合法混合批次实际离线 **15 steps / 6 one-use actions / 同一 reservation**；兄弟 pending Run 无 Attempt；businessAccepted=False。没有 mock intent/ledger/owner proof，只有原 fixture 的 native environment、native assessment/artifact 输入依赖使用 synthetic/mock；不是实机 PGR 验收。
- QA Store **17/17，exit 0，2.194s**，`.cache/pgr-followup/store-confirmed.log`。
- 原 orphan **14/14，exit 0，2.648s**，`.cache/pgr-followup/orphan-confirmed.log`。
- **最终完整组合 88 项：86 pass / 1 failure / 1 error，exit 1，90.081s**，日志 `.cache/pgr-followup/pgr-final.log`。本任务新增 **26/26 通过**，原 PGR/serial 测试 **60/62 通过**。两项红灯仅为下述旧 mixed fixture 归属冲突。
- 原先真实阻塞的两项原 15 步测试另行重跑 **2/2，exit 0，6.273s**，`.cache/pgr-followup/original-fifteen-final.log`；它们也在最终 88 项中通过。中途 `owned-resume-final.log` 的 88 项 2 failures / 1 error 是发现并修复 terminal 名称合同之前的结果，**已被 pgr-final.log 覆盖，不是当前状态**。
- 两项原 15 步测试现已可通过：`test_all_fifteen_steps_use_one_period_reservation_and_six_one_use_actions`、`test_real_batch_loop_consumes_fifteen_steps_with_final_native_plans`。不再将自身合法 intent 写作未修缺陷。
- 剩余旧 fixture 反证仍是 `test_mixed_batch_keeps_exact_pgr_reservation_owner` 和 `test_mixed_batch_does_not_ignore_other_unsealed_execution`。未更改原测试，未忽略旧 cancelled batch 上的非终态 member 来凑全绿。

执行命令仍为下节 PowerShell 示例：`PGR_FOLLOWUP_ORIGINAL_TESTS=0` 跑 26 项；`=1` 跑 88 项。现在测试固定校验 **五个** reference 文件 SHA，临时复制并实际应用 **两个**源补丁；serial blocker 方法从临时已打补丁的 Store 原样加载，摘要/孤儿 SQL 和 Manager occupancy 从 QA 原样绑定。没有缓存快照也能重建；transitive reference 依赖仍非全部冻结，不宣称干净 HEAD 自包含构建。两个 integration patch 当前由父方待纳入版本控制，Agent 未提交，不把文件存在说成已提交。

状态仍 **CHANGES_REQUIRED（候选集成与实际业务验收尚未完成，旧测试 fixture 需另行调整合同）**，不是 Store 根因未修。未改主源码/真实 DB/API，无游戏/OCR/GUI，无提交/暂存。本 Agent 最终未提交的交付仅本说明、`backend/tests/test_pgr_queue_followup.py`、上述两个 integration patch；Store 与 `test_pgr_store_followup.py` 已由父方提交。

本轮没有编辑父方人工 blocker helper/human_gates 候选、stop 策略、Host None keyword 修复或父方总报告；也未把它们的测试数字并入本 PGR 结果。当前补丁与这些候选的全栈集成由父方统一收尾。

最终核对：两个 source patch 对只读 main reference 执行 git apply --check 均 exit 0；五个 reference 文件 SHA 仍与固定基线相同。未暂存，无本 Agent 提交；所有 replay 临时目录已清理。最终待纳入的四个文件均在 `D:\Game\Automation\_qa\queue-timeout` 下：`backend/tests/test_pgr_queue_followup.py`、`docs/qa/pgr-queue-followup.md`、`integration-patches/pgr-queue-followup.patch`、`integration-patches/pgr-owned-resume-store.patch`。

---

## 第二轮历史：保守保留自身 intent 两红时的结论

仍为 **CHANGES_REQUIRED**。本轮仅直接修改 QA `backend/yeyu_gamer_manager/store/sqlite_store.py`；新增 `backend/tests/test_pgr_store_followup.py`，更新本任务已有 overlay 测试和集成补丁。未修改父方 Manager、autonomous_queue_followup 补丁/测试或 `QUEUE_TIMEOUT_FOLLOWUP_RESULT.md`，未修改任何原 PGR 测试。

### 已实现与可证明范围

- Store 上下文摘要同时过滤 fresh queued / pending_execution 兄弟；要求 queued membership、无任意历史 Attempt/resume intent、无 active controller lease、无 latest Attempt/resume/terminal outcome 残留、同 cadence 且 execute、无其它 nonterminal membership，当前批次未封口且无活动取消。默认无上下文摘要保持保守。
- 当前步骤收尾后 membership 已 terminal 时，只有精确 latest Attempt 同 Run、completed、completed_at 存在、exitCode=0、membership outcome=completed 才可作为上下文检查者；**这不是 successor intent 豁免**。PGR successor 原逻辑仍独立核验协议、Host 退出、cleanup、ledger、generation 和源配置。
- 不因另一个 batch 被简单改成 cancelled 就忽略它的 nonterminal membership。中途曾实验性按批次终态过滤，已完整撤回；最终 SQL 仍检查所有其它 nonterminal member。新增反证测试固定该边界；另用真实取消请求 sealed 记录、membership cancelled 和 Store seal 回执构造已退役旧 owner 的正向样本。
- serial 的 PGR 上下文分支改走 **Manager.execution_occupancy(owned_batch_id=..., current_run_id=...)**，不是直接 Store 摘要。overlay 从 QA 原样绑定 Manager 方法及 Store 的 active_execution_summary/orphan_pending_execution_candidates，实际执行 Host 查询和 SQLite，不用 mock 空闲结果。
- 本任务自己的 setUp 已恢复真实 AdapterHost.active_execution 实现；无 Host 的正向样本调用原方法读内部进程状态。unknown Host / 具名 orphan owner 的负向样本显式注入非空 Host 边界，确认不能当空闲排除。
- 第四份业务补丁覆盖 `pgr_daily_successor.prepare` 的 chain.guard，同样传 allow_queued_siblings=True。原函数在该调用之前的 PGR、原生 plan、前驱终态、cleanup、owner 等防护均保留；没有放行自身未证明 intent。
- 新的合法混合批次样本从正式 Start 的单一 PGR owner 出发添加 pending_execution 兄弟，完成第一步 synthetic preparation/finalization/真实 successor.prepare，成功提交 successor_pending；下一步仍真实拒绝 execution_owned。它证明首步后的 guard 通道修复，**不是混合 15 步通过**。

### 可复现性和第四个 baseSHA

overlay 已不读取 `.cache/pgr-followup/base` 或 `candidate`。显式 `PGR_FOLLOWUP_SOURCE` 指向只读 reference root；测试先校验四个原文件固定 SHA256，再复制到新的临时 replay 目录并实际 `git apply` 本次受版本控制交付范围内的 unified patch，结束清理临时目录。没有缓存快照也能重建候选；源码漂移则报 SHA mismatch，不静默改基线。

第四个文件：`backend/yeyu_gamer_manager/services/pgr_daily_successor.py`。

- baseSHA256：`8E0D2F7E200B83F6B37F00F755F6A860963C6AB5F3BDA1D4992644BA380C0C1E`
- baseGitBlob：`6e3964a3af21f98f46a0e9c373e2559d6b508f7a`
- 其它三个基线仍为下表所列值；补丁现在共四个业务文件、27 insertions / 10 deletions。Store 是 QA tracked 文件的直接 diff，不打入缺 HEAD 业务模块补丁。
- QA Store 修改前已备份到 `.cache/pgr-followup/qa-store-before.py`，SHA256 `512515FCBADDC859E913D4CE7C198DFBF4F55B410E2977B2B8277A0C96192D15`；最终 Store SHA256 `4E2245E9712BCA647BB235D607AE33329283BCD89DF30B1257DDE66DC2FC81A1`。
- 其余 main 依赖和原测试仍通过显式 reference root 只读导入，并非全部 transitive dependencies 都已冻结；因此这是可重建的限定离线 overlay，不是干净 HEAD 自包含构建或运行验收。

### 最新限定命令

```powershell
# QA 根 D:\Game\Automation\_qa\queue-timeout
$python='D:\Game\Automation\YeYuGamer\.cache\build-python\Scripts\python.exe'
$env:PYTHONPATH='D:\Game\Automation\_qa\queue-timeout\backend'
& $python -B -m unittest discover -s backend/tests -p test_pgr_store_followup.py -v
& $python -B -m unittest discover -s backend/tests -p test_orphan_pending_execution.py -v
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:PGR_FOLLOWUP_SOURCE='D:\Game\Automation\YeYuGamer'
$env:PGR_FOLLOWUP_VARIANT='candidate'
$env:PGR_FOLLOWUP_ORIGINAL_TESTS='1' # 0 只运行新 overlay 测试
& $python -B -m unittest discover -s backend/tests -p test_pgr_queue_followup.py -v
```

- 最新真实 SQL 新回归：**17/17，exit 0，1.956s**；`.cache/pgr-followup/store-final.log`。
- 原 orphan 回归：**14/14，exit 0，2.592s**；`.cache/pgr-followup/orphan-final.log`。
- 新 overlay 限定回归：**15/15，exit 0，13.840s**；`.cache/pgr-followup/followup-v2.log`。包含真实 Host/孤儿 SQL，以及合法混合首步 successor 和下一步保守拒绝。
- 最终严格组合：**77 项，73 pass / 2 failures / 2 errors；exit 1，74.141s**，`.cache/pgr-followup/strict-final.log`。其中本任务新增 overlay **15/15 通过**、原 PGR/serial 测试 **58/62 通过**。中途 `combined-final.log` 使用过已撤回的宽松归属实验，不作为最终验收。
- 真实 SQL 红绿反证：从 `git show HEAD:backend/yeyu_gamer_manager/store/sqlite_store.py` 原样抽取摘要方法，在独立进程运行 `test_fresh_pending_and_queued_siblings_are_excluded_read_only`，pending_execution 子例失败、queued 子例通过，**1 项 / 1 failure，exit 1，0.115s**；`.cache/pgr-followup/store-head-red.log`。没有改工作区回退验证，最终实现同例在 Store 17 项和完整组合均通过。
- 四文件业务补丁正向、反向 `git apply --check -p4` 均 exit 0；`git diff --check` exit 0。

### 剩余红灯分类

**真实未修缺陷（两项）**：`test_all_fifteen_steps_use_one_period_reservation_and_six_one_use_actions` 与 `test_real_batch_loop_consumes_fifteen_steps_with_final_native_plans`。当前 main Store 的 serial_observation_execution_blockers 对所有 successor_pending/successor_dispatched 全局排斥；只是同 Run、同 batch 或标记 PGR 都不足以证明安全。合法例外需把唯一 intentId、membership.resume_intent_id、前驱终态及 lastClosedRunAttemptId、decision 的 reservation/step/operation/phase/Todo 精确匹配、当前 ledger ready/preparing/dispatched 阶段、successor Attempt 身份及 generation 一起核验，并在两次 guard 重读及最终 handoff 原子边界重新确认。QA HEAD 不含该 serial blocker 方法及完整 PGR ledger/admission 模块，未生成或集成仅凭 runId 放行的补丁。保留 execution_owned 和 1/15 的实测失败；完整混合 15 步同样未验收。

**历史 fixture 与当前安全约束冲突（两项）**：`test_mixed_batch_keeps_exact_pgr_reservation_owner`、`test_mixed_batch_does_not_ignore_other_unsealed_execution` 的共同 mixed_fixture（原测试 :180）只是 update_batch(original, cancelled)，没有终态化原 membership、取消 sealed 记录或正式 seal。随后把同 Run 绑定到 mixed，形成第二个 nonterminal owner。严格 Store 在先拒绝该歧义，后一个测试因此也未到达其预期的 other_batch_active。它们不再单凭红灯判定 fresh pending 过滤失败；原测试未改，也不通过假过滤转绿。合法正向覆盖使用本任务新混合样本及 Store 正式退役回执样本。

---

## 第一轮历史记录（以下 SHA/次数/未改 Store 描述仅对当时成立）

结论：**CHANGES_REQUIRED**。已交付最小离线集成补丁和 11 项新回归；不是已合入、已发布或实际 PGR 验收。完整限定测试仍有 4 项既有失败，不能宣布每日链路恢复。

## 边界与实际写入

- QA 根：`D:\Game\Automation\_qa\queue-timeout`，HEAD `4361c935d8168625e04ca4067b1e8d0d498acb9e`。
- 只读来源：`D:\Game\Automation\YeYuGamer`，读取时 HEAD `f4ab6bf`。没有修改主源码、真实数据库或真实 API，没有启动游戏、OCR、GUI，也没有提交、暂存、切分支或发布。
- 交付只新增本文件、`integration-patches/pgr-queue-followup.patch`、`backend/tests/test_pgr_queue_followup.py`。所有 apply_patch 使用 QA 绝对路径。
- 获准离线快照仅位于 QA `.cache/pgr-followup/base/` 与 `candidate/`，各包含下表三个原文件；缓存被 Git 忽略，不得提交。日志也只在该缓存目录。真实原测试使用各自 TemporaryDirectory/Settings.for_test，测试结束清理；不加载用户运行目录。
- 已核对 QA 根和上述实际写入文件不是转向 main 的链接；main 下不存在三个交付路径。三份主源码当前 SHA256 与 base 快照一致。此比较只覆盖这三个源文件，不冒称整个主工作区完全无变化。
- QA 的 manager、adapter_protocol、queue_parking、已有测试及其他协作者文件均未改动。未处理 WW/ZZZ 或 queue_parking 的实现。

## 真实调用、根因与反证

### 2：queued 预留不等于执行占用

1. `services/manager.py:7897` `_prepare_run_attempt` → `_build_execution_plan` → `pgr_formal_admission.build_plan` → `pgr_daily_admission.build_plan/guard_dispatch` → `serial_observation_recovery.guard` → `_current_ownership`。
2. 原 `pgr_daily_admission.py:49` 只传 child Run；原 `serial_observation_recovery.py:61` 无参调用 `active_execution_summary()`。同批后续 fresh queued 成员因此留在 gameRuns，紧接着触发 `observation_chain_execution_active`。
3. QA HEAD 已有 Store 上下文参数 `owned_batch_id/current_run_id`（`store/sqlite_store.py:6829`），且对同批 queued 成员要求：未封口、同一有效归属、没有任何 Attempt、没有 active lease、没有待处理 resume intent、没有另一个非终态批次归属。不是简单按 batchId 放行。
4. 补丁只由经过 PGR daily proof/预留归属验证的 `guard_dispatch` 开启 `allow_queued_siblings`，两次 ownership 重读均传上下文。普通 observation guard 默认仍保留全局排斥，不改变其他游戏的默认语义，也不修改 Store 的 lifecycle/shutdown 无参摘要。
5. 新测试通过真实 Manager preparation 证明同批 fresh queued 兄弟可以保留且不创建兄弟 Attempt；反证包括外批归属、兄弟 Attempt、running 状态、第二次重读期间变 running、无 child guard、默认 observation guard仍拒绝。

### 3：operator-cancel 的零效果预留可复用

1. `manager_todos.py:1162` 调用 `pgr_daily_selection.select`，其内部 `prior`（:31）→ `pgr_unused_reservation.validate`（:16）。原 validate 只接受 sealed blocked，或指定 Manager 内存压力 cancelled 批次；还限定 PGR Run failed、membership terminal。
2. `Manager.cancel_batch`（`manager.py:11792`）→ `_seal_cancelled_batch`（:6206）会把尚未执行的 queued/pending Run 和 membership 改为 cancelled，并将取消请求最终记为 sealed。这个正式输出无法满足原 unused 判定；select 捕获 ManagerConflict 后返回 None，`manager_todos.py:1167` 将真实拒绝折叠为 `pgr_daily_scope_not_ready`。
3. 新逻辑仅增加正式 operator/webgui 取消的分支：从 Store 按 cancelRequestId 读取请求，要求同 batch、请求 sealed、无 active Attempt 列表、requested_by 与取消原因均匹配封口结果。没有真实取消记录或记录仍 acknowledged 都拒绝。既有 memory-pressure 分支不被扩大。
4. 仅该新分支允许 cancelled Run/membership，其他证据继续原样验证：当前游戏日和账号、精确七项 scope/proof、pristine ledger、零 Attempt（包含其他同 scope 历史 Run）、零许可、零消费、零 Todo effect、无未知字段/阻塞、有限历史完整性。
5. 不释放/删除资源，不制造第二个 admissionId，不自动清除 Stop。普通新 Start 在既有 CAS/幂等事务内执行 `pgr_daily_selection.register`（:100；调用点 `manager.py:7240`），复用同一个预留并保留旧 owner/unusedDailyLedger。新测试实际调用原 Manager 取消方法、再经 TestClient 发起临时应用 Start，确认旧批次、旧 Run、Todos 不变且 businessAccepted=False。
6. 反证：任意 Attempt、未封口取消、错误 batch/原因/发起者或未清空 active IDs 都拒绝；原测试继续覆盖 scope 漂移、预算/许可痕迹、账号/周期变化、未知字段、历史截断、CAS、幂等、人工门与 Stop。

## Git 与来源核对

- QA `git show --stat HEAD` 确认 4361c93 包含 Store 上下文摘要；`git diff -- store/sqlite_store.py` 为空。
- QA `git ls-files '*pgr*' '*serial_observation*'` 没有目标业务模块；main 对下表三个文件的 `git ls-files` 也为空，针对这些路径的 `git log --all` 无可追溯提交。它们是未提交内容，不能把整文件或其他 1113 项变更带入提交；引入缺陷的 revision 和最初设计动机无法从这些 Git 路径证明。
- 已查真实调用、原测试及当前未提交源文件；本轮用户说明是当前需求依据。未读真实 DB/API/现场日志，也没有发消息或访问外部评审记录，因此只确认离线复现，不推断某个真实 Run 的现场结果。
- 已读用户指定的 `D:\GameDevelopmentSkills` review 详细模式/style 与适用细则。当前工具无子 Agent 能力；已顺序核对关键反证，但未完成独立多 Agent 复核。

## 补丁基线（逐文件）

所有路径均相对于 `backend/yeyu_gamer_manager/services/`。baseSHA256 是原文件字节 SHA256；baseGitBlob 是 `git hash-object`（未写对象库）。

| 文件 | baseSHA256 | baseGitBlob |
| --- | --- | --- |
| serial_observation_recovery.py | DF40C76229405A024D1F75462F867FD1A59109CA0596A7B47027D8578DAF09DB | 17eb7a48d53304aec26afb54f713f2c373376db6 |
| pgr_daily_admission.py | 8149569A560F3512B21499B4448680F4471126C75A651C6B57DAF70D184DF909 | c29837fa3f489ec1fe0feb334c6682540515ffdf |
| pgr_unused_reservation.py | DE1A474B47CF45EF74297E067126D754009B525B1B2B0D778E7BA9492F0AF245 | d93103953bd7b6b5a352f85e3d672caea52134da |

补丁为三个既有文件的 unified diff：26 insertions / 9 deletions，无整文件新增，无 manager/store 修改。依赖 QA 的真实 Store 上下文实现，不可直接应用到当前无参数的 main Store。

离线组合用的 QA Store SHA256：`512515FCBADDC859E913D4CE7C198DFBF4F55B410E2977B2B8277A0C96192D15`；Git blob `d2ad6b12cecd5503062100bb846a078cc95c3180`。该文件未复制到快照或打入补丁。

## 测试方法与命令结果

新测试是显式离线 overlay harness：新进程从只读 main 导入真实业务依赖和原测试，三份目标模块分别加载 base 或 candidate 快照；仅用 AST 抽取 QA Store **原样的 active_execution_summary 方法**，临时挂入真实 SqliteStore。真实 SQLite 查询会执行；不是用 stub 返回“空闲”。不复制整套 main、不启动真实服务。未设置 PGR_FOLLOWUP_SOURCE 时明确 SkipTest，不应把跳过解释成成功。

原 fixture 的 native 环境、Host/desktop 探针、调度入口及判定素材包含 mock/synthetic 数据；新的竞态与篡改测试也明确使用 mock。它们只能验证编排、Store、事务和不变量，**不能证明真实 PGR/原生识别/游戏任务完成**。

在 QA 根 PowerShell 执行（独立新 Python 进程；`-B` 禁写依赖 pycache）：

```powershell
$env:PGR_FOLLOWUP_SOURCE='D:\Game\Automation\YeYuGamer'
$env:PGR_FOLLOWUP_VARIANT='candidate' # 改 base 对照原实现
$env:PGR_FOLLOWUP_ORIGINAL_TESTS='1' # 改 0 仅运行新 11 项
& D:\Game\Automation\YeYuGamer\.cache\build-python\Scripts\python.exe -B -m unittest discover -s backend/tests -p test_pgr_queue_followup.py -v
```

| 命令组合 | 实际结果 | 日志（QA 根相对路径） |
| --- | --- | --- |
| base，original=0 | 11 项，6 pass / 2 failures / 3 errors；exit 1，10.597s | .cache/pgr-followup/base-tests.log |
| base，original=1 | 73 项，64 pass / 4 failures / 5 errors；exit 1，71.521s | .cache/pgr-followup/base-originals.log |
| candidate，original=1 | 73 项，69 pass / 2 failures / 2 errors；exit 1，72.371s | .cache/pgr-followup/candidate-final.log |
| candidate，original=0 最终重跑 | 11 项全过；exit 0，10.191s | .cache/pgr-followup/followup-final.log |

原测试未经修改，来自 `test_pgr_daily_manager.py`、`test_pgr_unused_reservation.py`、`test_serial_observation_recovery.py`。候选的 11 项新增测试与 58/62 项原测试通过。

`git apply --numstat integration-patches/pgr-queue-followup.patch`：exit 0，只列三个目标文件。

```powershell
git apply --check -p4 --directory=.cache/pgr-followup/base integration-patches/pgr-queue-followup.patch
git apply --reverse --check -p4 --directory=.cache/pgr-followup/candidate integration-patches/pgr-queue-followup.patch
```

正向、反向 check 均 exit 0；只核验，没有应用到主目录或 QA 正式业务模块。初轮 harness 曾有方法签名、空 Todo fixture 与模块 cleanup 过早撤销组合 Store 的问题；已修正测试装配，最终结果采用上表 final 日志，不以初轮结果报成功。运行有既有 Starlette/httpx deprecation warning，未扩大修改依赖。

## CHANGES_REQUIRED：交给协调者的精确缺口

1. **Store 仍漏掉 fresh pending_execution 兄弟**：原 `test_pgr_daily_manager.mixed_fixture`（:180）真实构造的是 `pending_execution`，QA 摘要只剔除 `r.state='queued'`（QA `sqlite_store.py:6869` 附近）。`test_mixed_batch_keeps_exact_pgr_reservation_owner` 仍报 execution_active；`test_mixed_batch_does_not_ignore_other_unsealed_execution` 在到达应有 other_batch_active 前先报 execution_active。两项 base/candidate 均失败。请 Store 负责人按同等零效果/唯一归属约束审查 queued 与 pending_execution，不能宽泛忽略同批全部 Run。本任务没有修改该文件。
2. **自己的合法 PGR resume intent 仍被全局挡住**：main `SqliteStore.serial_observation_execution_blockers`（:7344）只对 attempts 排除 owned_run_id，resumes 没有归属例外。`pgr_daily_successor.prepare` 创建 successor_pending 后，下一步 PGR admission 会报 execution_owned。`test_all_fifteen_steps_use_one_period_reservation_and_six_one_use_actions` 和 `test_real_batch_loop_consumes_fifteen_steps_with_final_native_plans` 均在 base/candidate 失败（后者只跑 1/15）。需 Store/Manager 负责人按当前 Run、membership、intent 和 ledger step 的精确身份设计例外，不能直接去掉 resume 检查。本任务没有改 manager/store 或把这两项改成 mock 通过。
3. **缺 HEAD 依赖尚未集成**：本补丁只适用于表中确切未提交原文件；QA HEAD 本身缺少这些模块及其完整依赖。协调者须选择归属明确的依赖后再组合验证，不可提交 main 全部未提交内容，也不可提交 `.cache` 快照。当前 overlay 只是限定离线组合，不是干净 HEAD 可独立构建证明。
4. 完成独立审查和上述原测试后，仍需另行授权的真实同账号、同游戏日、同 RunAttempt 业务验收；本轮没有且不得用 synthetic fixture、permit 生成或 businessAccepted=False 代替。

撤回本交付只需不集成补丁并移除本次三个交付文件；没有真实状态、配置、历史 owner 或 Stop 意图需要回滚。未提交。
