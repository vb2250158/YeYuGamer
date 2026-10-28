# Host 取消管道修复：仍 CHANGES_REQUIRED

日期：2026-10-28。基线 f08feda，分支 qa/queue-timeout-20261028。只在隔离工作树、临时 runtime 与自有无 GUI 夹具进程验证；没有操作真实 Manager、游戏或配置。

## 先核对续投与已完成项

stop-budget、committed handoff gate、failure persistence atomic rollback 已由 76d03a9 实现，不是尚未修改的三个项目。本轮先复跑 `test_host_stop_budget test_manager_host_handoff test_queue_timeout_budget test_adapter_protocol.AdapterHostSupervisorTests`：51 pass、0 skip，7.122 秒。没有为这三个名称再加一套重复门禁或预算。

实际未关闭的是 hung/killed-owner 两项独立红测，以及停止预算前仍可能阻塞的 I/O。本切片确认并修复后者的 cancel transport 子路径，不能据此声明两红已关。

## 根因、触发和证据

| 状态/严重度 | 基线位置 | 条件与后果 | 规则与最小处理 |
| --- | --- | --- | --- |
| 已确认、已修 P1 | services/adapter_host.py:1634、1658–1659 | durable 文件已成功持久化后，仍持 execution_lock 同步写/flush Host stdin，且 sent 尚未置位；满管道令 cancel 与 watcher 停止预算前一起等待 | 单环节不得堵整个队列；复用认证 durable 控制通路，取消不再写 stdin，持久化成功才设置请求标记 |
| 已确认、已修 P1 | services/adapter_host.py:313；adapter-host/YeYuGamerAdapterHost.cs:665 | Python 在 Manager runtime 写控制文件，但子环境未投影该 runtime；C# 回退 ProgramData，D 盘部署可能读错位置。单独删 pipe 会丢取消 | 唯一数据源；显式派生 self.runtime_dir.resolve() 为 YEYU_GAMER_RUNTIME_DIR，不继承父环境、不接受请求路径、不加入 ambient allowlist |
| 已确认、未修 P1 | adapter-host/YeYuGamerAdapterHost.cs:185–187、495 | Runner 先启动再入 inner Job，且兼容允许客户端 BREAKAWAY；Manager 缺完整静默证明 | 保留未知 owner，不凭 poll/PID 解锁；不能未经客户端资格验证强加禁止 breakaway 的 outer Job |

独立子 agent Russell 取证 Python→C#→恢复通路、历史与反证；Kuhn 独立复核 OS1 及最终取消 diff。相关代码在浅仓库边界 f84c22a 已存在，最初引入版本不可据此确定。76d03a9 的等待预算并未移除预算前同步写。

改前 7 项新测试实跑 3 fail、1 error；其中派生 runtime 缺失和缺 stdin 不能取消均直接复现。另用同一真实满管道回归动态加载 SHA 备份的原 Host 类：取消线程 2 秒仍未返回，测试失败；finally 仅结束本 fixture 子进程并回收线程，未残留后台写线程。改后同场景通过，不是仅靠 Mock 宣称修复死锁。

## 行为与保留边界

- cancel、cancel_run、watcher timeout/protocol failure 仍校验精确原 plan/attempt/fencing 和活跃 owner；重复请求沿用原 false 语义。
- 请求成功只表示原 HMAC/separate cancel authority、ACL、原子文件写入成功，**不表示 Host/Runner 收到、已取消或业务完成**；false/异常不置 sent，也不清 owner/lease/恢复材料。
- C# 在同一个显式 runtime 校验 HMAC/attempt/时间窗口并转发；delivered 仍由 Host 确认。没有更改生产 C#、JSONL v1.1、取消权限或任意进程终止范围。
- 既有 host_stop_grace_seconds 默认 5 秒/次，仍来自同一预算 JSON；cancel 不再被 stdin write 拖住后才有机会进入该升级链路。本切片没有添加另一份默认预算。
- 文件 fsync/ACL/I/O 仍未具备硬 deadline；初始 execute request 同步写、C# 向 Runner 写仍可阻塞；Job/reader/callback 静默与 formal owned recovery 仍缺。不能称为“所有环节已全有界”。

## 原生兼容性反证

历史 a0dda7f 在 C# Job 设置 BREAKAWAY_OK，注释说明官方游戏客户端可以显式脱离 automation Job。禁止 breakaway 的 outer Job 会截留这些客户端，随后关闭/终止 Job 可能误关它们。该推断同时符合 [Microsoft nested Job 规则](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs)。因此不把上轮 15 项资格原型直接接成产品默认，不放宽 preserve-client 约束。

后续须建立能保留合法游戏客户端、只收敛自动化输入执行器的可信所有权合同，并证明 writer/reader/event callback 都结束，才能沿 Store 原子收尾→一次性 handoff→通知队列顺序放行。无凭证的旧 owner 和原 fake 私有字段注入仍不能清锁。

## 本切片验证

- Python 定向联合 137 pass、0 skip，19.791 秒，覆盖新满管道、原 stop/handoff/rollback、未派发收尾、resume/review/parking/orphan/priority。实际最终干净检出结果见归档收据。
- 编译真实 C# 固定 Host + 惰性 Runner，非默认 runtime 与伪 ProgramData 分开：valid、bad-hmac、runner-not-reading 三场景均通过，最终工作树复跑分别 732/177/5256ms；坏 HMAC 退出 70，非读取 Runner 到达已有取消宽限，均不是成功业务。
- native 脚本登记 3 个 Host 和 3 个 Runner 的 PID/父 PID/用途/来源/日志；结束查询六个 PID 均不存在。Runner-not-reading 测试本身未填满 C#→Runner 管道，不把它扩大为该管道任意饱和时都可返回的证明。
- 原独立组不改断言：10 pass、2 fail，2.735 秒，hung/killed-owner 仍红。dead/missing-callback 的 prepare-only 路径保持通过。
- 工作树安全筛查 High/Critical/Medium/Low/Info 均 0；只是确定性预筛，不替代两名子 agent 的信任边界审查。

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH="$PWD\backend;$PWD\backend\tests"
& 'D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe' -B -m unittest -v test_host_durable_cancel_transport
& .\adapter-host\tests\Test-YeYuGamerDurableCancelRuntime.ps1 -SourceRoot "$PWD\adapter-host" -WorkRoot "$PWD\.cache\pipe1-new-fresh-run"
```

native WorkRoot 必须不存在；脚本复用并编译仓库已有惰性 Host protocol fixture，只读生产 C#。不运行全量构建、不安装、不调 Manager API、不启动/关闭游戏，不合 main、不 push。

## 结论

本切片修复两项已证实的取消链路缺陷，不是三红全关。原 hung/killed-owner 仍为 CHANGES_REQUIRED；旧两项夹具没有可信静默数据，不改成 fake 特判。没有新上游算法问题或需要重新请求的普通授权；剩余是所有权/输入生命周期实现与兼容性验证工作。提交号、最终命令与日志归档见 `D:\Game\Archives\QA\host-owned-containment-20261028\VALIDATION_RECEIPT.md`。
