# 独立 owned input 契约修订（测试先行提交）

依据：2026-10-28 14:14 夜雨决定，保留原业务断言，正向改走真实 owned producer；裸私有字段/假 PID 保留为未知 owner 负向。基线 07886e5；原独立文件 f253225 blob 0d9698449bd82700d135c9e113881b45a11ee0ff，归档未修改。

## 差异

- 原 `test_simulated_hung_owner_cannot_block_the_next_game` / `test_simulated_killed_owner_cannot_block_the_next_game` 改成明确命名的 unknown-owner 负向：不凭假 PID 放行；原 Attempt/Run review、原 lease revoke、durable 桌面隔离；后项有界扫描且明确未启动/review，零客户端启动/置前/清理/输入，不当成功。
- 新增 `test_owned_hung_root_recovers_and_next_game_finishes` / `test_owned_killed_root_recovers_and_next_game_finishes`：保留原公共业务断言，走真实 prepare→claim→Host.execute→原生 Job 绑定/dispatch。证明 killed root 后 helpers 仍活，再由正式 provider 停止；外部 client 惰性替身始终存活。后项一次启动、原项非成功、无重新建队列。
- 其余原十个测试方法与 `assert_next_game_finished_without_false_success` 的 AST 均保持一致；没有 skip/xfail 或按 PID/对象类型求绿。
- 追加真实 Manager `_start_game_run` 完成回调桥接，不只测试丢弃 completion 的缺 callback 路径。释放 owner 前，透传观察点用另一只读 SQLite 连接验证原 Attempt/member/lease/quarantine 已提交，并检查 registered reader 都退出；不以同连接可见性冒充 commit。
- 原 137 组中仅 `test_first_timeout_claimed_still_cancels_waits_grace_and_fences` 按新决定修订：保持一次 cancel、既有 grace 和两次等待；到期不再要求无限保留 active lease，而要求 review/revoked/持久隔离。其余 136 项原断言不改。

## 有界负向预算

不添加 165 秒一类脱离数据源的常数。负向虚拟时钟上限为冻结 dispatch.deadlineAt 相对测试起点的时差，加配置 callback_cancel_grace_seconds；默认本夹具为 176 秒（含开始偏移 1 秒），精确对应 120+40+15+1，无额外裕量。真实 provider 另受 host_stop_grace_seconds 的统一单次 deadline 限制；root 死亡不是 helpers 静默证据。

## 独立审查

另一只读 agent Poincare 与测试作者 Fermat 分离。首轮发现两项 P2（生产 Manager callback 覆盖不足、缺独立 commit 顺序观察点），补齐后独立重跑 15/15，无 skip/xfail，确认原十项不削弱。预算硬编码问题再按唯一数据源校正；其复核证据连同最终干净检出结果归档。

本提交只修订测试契约/夹具，不宣称基线已有 provider。按 TDD 先单独提交契约并在该提交检出记录预期红，再提交产品实现，使最终检出通过；不能单独把测试提交安装或合入运行 main。实际提交号、红绿命令和数字见 `D:\Game\Archives\QA\owned-input-recovery-20261028\VALIDATION_RECEIPT.md`。
