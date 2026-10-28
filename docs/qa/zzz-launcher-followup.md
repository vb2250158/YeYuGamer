# ZZZ HoYoPlay 离线跟进（任务 4）

## 结果与范围

2026-10-28，QA 分支 `qa/queue-timeout-20261028`，HEAD
`4361c935d8168625e04ca4067b1e8d0d498acb9e`。
HEAD 没有 HoYoPlay 专用模块；当前主源码中的相关模块为 untracked。
因此不向 tracked 源码整文件复制，不修改 Manager/store/model/queue，也不提交或 push。
本交付只有独占测试、本文和最小上下文补丁。主源码只读。

已确认的是错误聚合造成的诊断信息损失，不是今日真实启动失败的唯一根因。
转述的 `0f1f0d61`、`84b8fb87` 未作为已核实的运行证据；本轮没有读取其会话正文、真实 DB、token 或运行配置。
没有启动游戏、Manager、API、OCR、GUI，没有操作绑定或计划任务。

## 位置、触发与独立反证

以下行号来自主源码只读快照，目录均为
`D:\Game\Automation\YeYuGamer\backend\yeyu_gamer_manager\services`。

| 位置 | 实际触发 | 反证与判断 |
| --- | --- | --- |
| `hoyoplay_native_execution.py:426-429` | 可见 Document 数量不是 1；origin/path/route/status 不合格；或 documentText 为 unknown，均抛 selection-unknown | 零/两个文档、错误 origin、其他游戏路由、Name/Value 冲突、TextPattern 不可用，可以各自独立触发相同码。不能据此断言只是加载慢或 ZZZ 未选中。 |
| `hoyoplay_native_execution.py:214-228` | worker 的受限 reason 经连字符转下划线，形成 zzz_launcher_selection_unknown | 原因传播没有证明界面具体分支；不是重试许可。 |
| `hoyoplay_native_observation.py:346-372` | Popen 已返回并登记 PID，之后单次身份查询为 None，或可执行路径不同，均报 launch_identity_unavailable | mock 身份不可读和明确错误路径均复现；匹配路径正常通过。不能从该码区分已退出引导进程、权限不足、身份查询失败或路径不符。 |
| `hoyoplay_native_observation.py:300-317` | admission 发现无启动器且允许启动，才调用 _start_launcher；后续 observe 在身份检查之后 | 已启动请求失败不能通过再 Popen 来证明恢复；不能直接认领其他 HYP 进程。 |
| `hoyoplay_native_observation.py:683-730` | Document-Location 严格匹配官方 origin、页面路径和 nap_cn fragment；Project-Document 对 Name/Value 冲突保留 unknown，对不可用文本保留 unknown | config 中 primaryGame 或 URL 单独正确，不等于当前文档/控件/身份全部合格；不应把 unknown 改成 true。 |

两码来自不同阶段，不应合并成一个“启动器慢”的已证实根因。
启动器可能是短命引导程序属于待证假设：本轮没有同次 PID/创建时间/退出/父子关系证据，不能据此允许路径继承或认领 HYP。

## 最小可验证修法

`integration-patches/zzz-launcher-followup.patch` 只修诊断聚合，不改变准入条件：

1. 身份缺失继续 `zzz_observation_launch_identity_unavailable`；身份可读但路径不符改为 `zzz_observation_launch_identity_mismatch`。
2. selection 拆成 `selection-document-count`、`selection-location-unqualified`、`selection-text-unavailable`；现有 Python reason 转换自然得到下划线错误码。
3. 所有原拒绝样例继续拒绝，原正常样例继续通过。不新增等待、重试、点击、进程认领、原始 URL/文本日志。

这不是“已经能启动”的业务修复。缺少现场事实时直接放宽身份或文档条件会扩大权限；本补丁先消除阻止根因判断的信息损失。
使用新错误码前，集成方须核对展示、失败分类及外部消费者是否依赖旧码；本轮未改它们。

## 基线与补丁应用边界

文件级 base SHA-256（不是 HEAD blob；HEAD 中不存在这些文件）：

| 文件 | base SHA-256 |
| --- | --- |
| hoyoplay_native_observation.py | E4042737D52AD2BE4FF2E0D52701CD8153320DF7DCF4776E8F156164F24E6162 |
| hoyoplay_native_execution.py | 574769488BE1DEE0E19AD7D69987170A54388AB2CB88F316A3A5CBA29C8AE79D |

原 QA `.cache/zzz-followup/base/` 和 SHA 收据仅为历史调查材料，测试不再依赖它们。
当前测试自足：必须显式设置 `YEYU_QA_REFERENCE_SOURCE_ROOT` 为包含 backend/ 的参考仓库绝对路径。
测试只读该根目录下 `backend/yeyu_gamer_manager/services/` 的上述两个白名单文件，
使用测试内固定 SHA 校验同一份读取字节，然后在系统 `TemporaryDirectory` 中组装基线与仓库布局副本。
只对临时副本执行 `git apply --check` 和 `git apply`，退出自动清理；reference 不被导入或写入，
不创建其缓存、不扫描其他源码、不读取配置、DB 或凭据。相对根路径、越界解析路径、文件缺失或 SHA 不匹配均报错，
不刷新基线、不降级到旧缓存、不冒充通过。集成时先核对两个 base SHA，备份目标，
再从仓库根目录应用此补丁；a/、b/ 路径均为
`backend/yeyu_gamer_manager/services/...`。本轮仅在 QA 缓存模拟该仓库布局，未对主源码应用。

注意 Git 在仓库忽略子目录可能跳过补丁却返回 0；测试设置 GIT_CEILING_DIRECTORIES，
使临时回放目录不发现外层仓库，并通过真实分支输出证明补丁生效，而非只相信退出码。

## 离线验证

测试实际绝对路径为
`D:\Game\Automation\_qa\queue-timeout\tests\test_zzz_launcher_followup.py`，
不是 `backend/tests/`，也未安装为顶层 `test_zzz_launcher_followup` 模块。
可从任意工作目录直接复跑 4 项：

```powershell
$env:YEYU_QA_REFERENCE_SOURCE_ROOT = 'D:\Game\Automation\YeYuGamer'
python -B D:\Game\Automation\_qa\queue-timeout\tests\test_zzz_launcher_followup.py -v
```

也可明确指定 discovery 目录，不依赖顶层模块导入：

```powershell
$env:YEYU_QA_REFERENCE_SOURCE_ROOT = 'D:\Game\Automation\YeYuGamer'
python -B -m unittest discover -s D:\Game\Automation\_qa\queue-timeout\tests -p test_zzz_launcher_followup.py -v
```

换 worktree 后只需保留测试、补丁，并提供具有上述固定 SHA 的参考源码；无需任何未提交 `.cache` 文件。
补丁位于测试所在仓库的 `integration-patches/zzz-launcher-followup.patch`。
没有设置环境变量时四项明确 skip，消息含 `fixture validation NOT PASSED`；
unittest 的 `OK (skipped=4)` 只是 runner 正常结束，不是 4 项验证通过，CI/人工报告须读取跳过计数。
显式指定但源文件缺失或 SHA 改变时失败，不跳过。
本机 Python 3.10.11，4 个 unittest 方法通过，包含 14 次纯 PowerShell 谓词回放及
6 次 Python 方法回放。项目要求 Python >=3.11；本次只执行标准库 AST 提取的目标方法，
不代表项目整包或生产解释器验证。

- Python 从快照 AST 只编译 _start_launcher；Popen、身份查询、文件 pin、guard、登记均为 mock。
  验证缺失、路径不符、匹配三种身份，基线和补丁各执行一次；每次仅 1 次 mock Popen/身份查询，登记与启动收据保留。
- PowerShell 只执行从 worker 源码截取的 if 谓词和合成字典：不加载 UIA、不访问真实窗口、进程、账号或配置。
  零文档、双文档、错误 origin、错误游戏、冲突来源、未知文本均拒绝；正常文档通过。
  基线得到原聚合原因，补丁得到对应细分原因。
- `git apply --check` 和实际应用仅针对隔离快照；真实输出证明候选与基线不同。

本轮未运行完整模块导入、原生 worker、现有全部 HoYoPlay 测试、安装或发布。
不能据此宣布 selection/identity 现场已恢复，更不能宣布每日完成。

验证层次明确分开：

| 层次 | 本轮状态 |
| --- | --- |
| 显式 reference 的 patch fixture 验证 | 本轮通过 4 项、0 skip：只读当前主源码白名单两文件，固定 base SHA 匹配，在系统临时仓库布局应用最小补丁后执行提取方法/谓词。仅证明这两份字节的 fixture 行为，不证明整库一致或生产执行。 |
| 默认缺 reference 的运行 | 本轮实测 4 项 skip、0 项执行通过；不计为 fixture 验证通过。 |
| QA 原生 integration | 未执行：QA HEAD 缺少相关专用模块；测试未导入 QA 的完整 HoYoPlay 服务、Manager 或原生执行链。4 项通过不能记为 integration 通过。 |
| 现场启动与游戏业务 | 未执行、未确认修复。 |

## 剩余缺口与下一步

1. 由持有今日证据的主任务关联同次任务 ID、阶段和实际失败码；优先复用已存的脱敏文本材料，不为调查重启游戏。
2. selection 需要文档数量、脱敏 origin/path/route、冲突状态和文本可用性，不需要原始 query、文档正文或账号信息。
3. identity 需要同次 Popen PID/请求时间、精确映像匹配结果及现有退出/创建时间证据。
   若证明为短命引导进程，再设计签名、创建时间、会话、父子与窗口归属都可验证的交接合同；不能 PID-only 或同目录放行。
4. 将相关模块正式纳入主分支后才有可集成编译基线；按主任务授权应用最小补丁、复核错误码消费者并跑专用回归。
   本任务不执行生产修改或真实启动。
