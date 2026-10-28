# 任务 6：WW ww124 绑定来源跟踪（QA 修订）

## 本轮有效结论：candidate test 不等于 reference evidence

本轮只允许修改并实际仅修改以下两个绝对路径；不写主源码或主缓存，不改迁回的 patch，不运行准备、构建、发布、API 或游戏：

- `D:\Game\Automation\_qa\queue-timeout\backend\tests\test_ww_binding_followup.py`
- `D:\Game\Automation\_qa\queue-timeout\docs\qa\ww-binding-followup.md`

改前 SHA256 分别为 `f808940f4f3c71855da53ce6a30667da9d429e9a8af390d5c78cebce424870db` 和 `fb916f00e7eeb5d2766bf1d74ad8029ce6f7099f585d763a4328cb50f4dc56bc`。这是本轮读取到的基线哈希，不冒称另存了新备份；为遵守只改两文件，没有新建备份文件。

本次读取 QA HEAD=`4361c935d8168625e04ca4067b1e8d0d498acb9e`；主仓库 HEAD=`f4ab6bf76365cd62e5ccde118398b417ed086de7`。用户的 `main=1113` 是交接标记，本报告不将它解释为 Git 提交号。QA 下三份脚本相对 QA HEAD 无 diff；**currentMain 工作文件与 QA HEAD 是不同证据集**。下方历史记录中的主源码行号、4 项通过及补丁检查只属于旧轮次，全部不能作为迁回后 QA HEAD 绿灯。

| 文件 | QA HEAD 文件字节 SHA256（candidate 静态基线） | currentMain 工作文件字节 SHA256（只读 reference） |
| --- | --- | --- |
| scripts/Build-YeYuGamerWwAdapter.ps1 | `947b1b9cd4bad244d8b7eddca14f36a8fe5a432ea9dba3c1db88aff40760c813` | `d323479b4358fd04da430eecdd72ff257ce0e679d1c591a886bd11336cec58ff` |
| scripts/Publish-YeYuGamerLocalRelease.ps1 | `91ad7702178fb96a239373ae2b463b89341531cf45efbcfd4469b9f3e925a365` | `213f885f7f9377f9ff88c316c437e45580bc4f71dc5c4fa9b4e08cd48a86ec23` |
| scripts/Prepare-WwOfficialCandidate.ps1 | `9eaa7594d02b568d20e4ad72bf00b0e50e1addf109b462a7bfb1adf4fc89928b` | `29c7dec685080dd9212f4b567727f5a7f1a80de4ae84a4c747af393c6d77f6df` |

QA publisher `:336` 直接调用 WW builder，**不存在** `$WwCandidateToolRoot` 或 `Sync-YeYuGamerCandidatePaths`。此前测试在 QA 发生 3pass/1fail 是真实缺口，不能通过把测试根静默改回主目录掩盖。currentMain 的 `:621` 转发参数、`:636` 生成 WW 映射及 `:770`/`:1915` 配置同步，只能证明参考实现中存在该静态链路。

### 测试边界与复现

- `WwBindingCandidateTests` 固定从测试自身所在 QA root 读 builder、preparer、publisher 和本任务 patch；均在语义断言前核对原始字节 SHA256。测试覆盖默认根、运行包布局、QA publisher 的已知缺口、目标提交未获准及文档 patch 范围；**缺口基线检查通过不等于 publisher 功能通过**。
- `WwBindingReferenceEvidenceTests` 单列参考证据。`YEYU_QA_REFERENCE_SOURCE_ROOT` 默认 QA root，此时先核对 QA publisher 哈希，再显式 skip 缺失的桥接功能；不会拿 currentMain 补位。显式设置绝对参考根后，必须匹配上表固定的参考 publisher SHA256，否则短消息失败，禁止静默 repin。
- 所有源码内容断言使用布尔结果及短原因；哈希漂移仅报告相对文件名，不输出整份源码。不导入 Manager，不访问 DB/token，不调用网络或子进程。

最终命令、结果及边界事故统一见文末“最终 QA 结果”；本节不另维护一套验收结论。

## 最小可操作 runtime-prep 路线（方案，未执行）

目标 ref 本次只读重新解析仍为 `1c3dc74ef985c09a9b26a4255a459d6df5d3333f`；`git ls-tree yeyu-local-20261028 ok_templates` 返回模板 gitlink=`438526eafcf4b1caf78c1dea68ef36b42e81e3c4`。这两项是当前本地来源证据，不代表官方版本或资格通过。

### 已有正式入口及不能直接调用的原因

1. 正式准备入口是 QA 的 `D:\Game\Automation\_qa\queue-timeout\scripts\Prepare-WwOfficialCandidate.ps1`：`:3-8` 提供 OfficialSource、CandidateRoot、SourceRevision、PythonPath、TemplateSource；`:113-121` 校验干净 HEAD、来源、官方基线和模板；随后 archive 到 `WW/source`、建立 `r`、安装依赖、pip check/import probe，`:172-173` 写 `WW/runtime-preparation.json`。它不是部署或注册入口，但会创建文件、运行解释器并可能联网，**本任务不得执行**。
2. QA 的 ValidateSet 和 currentMain 的 ValidateSet 均未接纳目标提交。不能以 ref 名代替固定 SHA，不能只加白、不核对基线和完整文件清单；fork 的真实来源也不能通过伪造 origin 成官方地址绕过。正式脚本还要求输入仓库 HEAD 等于固定提交，不能直接传当前不同 HEAD 的 fork 工作树。
3. currentMain preparer `:9-16`、`:159-196`、`:233-301` 有额外 cache/donor/derived 能力；它们**不在 QA HEAD**，derived 分支只允许固定 `20d2a0...`。其 `Reuse-WwPreparedDependencies.py` 又固定主缓存与 OLD/NEW 提交，不能把它当成任意 ref、任意 QA 路径的通用离线搬运器。不得直接复用主缓存 donor 或运行主脚本。

### 后续实施顺序与验收点

以下是下一独立维护范围的明确输入、产物与停止点，不是本轮已完成操作：

1. **固定来源并隔离 staging**：从 `D:\Game\Tools\forks\ok-ww` 的目标 ref 准备 QA 内新的干净 detached source checkout，例如 `D:\Game\Automation\_qa\queue-timeout\.cache\ww-runtime-prep\source`；先核对 ref SHA、父提交/官方基线、实际 fork provenance、完整 Git blob 清单和上述模板 gitlink。保留原 fork 工作树，不 checkout/reset 原目录。归档仅含固定 ref，不复制账号 configs、日志、mutable 或已有运行状态。
2. **先补齐资格合同再运行**：在独立授权中给 QA preparer/builder 增加这个**精确提交**的来源 profile（官方基线不能沿用猜测值），固定源码/模板清单及 SDK/资源哈希，声明实际 fork 来源并保留官方祖先证据；提供离线正例、提交/模板/SDK/源码篡改反例。若基线或依赖材料不足就停在资格缺口，不生成 ready manifest。当前两份脚本对该提交的拒绝保持原样。
3. **准备独立运行根**：使用新、未存在的 `D:\Game\Automation\_qa\queue-timeout\.cache\upstream-candidates\ww-yeyu-local-20261028`，不覆盖 ww124。准备入口显式传 `-OfficialSource <上述干净checkout>`、`-SourceRevision 1c3dc74ef985c09a9b26a4255a459d6df5d3333f`、`-CandidateRoot <新运行根>`、`-PythonPath <已核验Python3.12>`、`-TemplateSource <模板固定提交的干净本地checkout>`。**该参数组合只有步骤2完成后才可能通过，当前不可直接执行成功。**
4. **离线依赖与资格产物**：预先核验本地 wheelhouse 完整性，准备进程使用 `PIP_NO_INDEX=1` 和明确本地 `PIP_FIND_LINKS`，模板只从本地精确提交取；材料不齐就报缺失，不联网补齐。若目标要求 SDK 本地 wheel，须纳入正式 profile 和依赖安装合同，不能手改 manifest 声称存在。正式流程建立 `r/Scripts/python.exe` 与 pythonw、固定 `official-source.zip`、`WW/source/main.py`、模板和 SDK 收据；通过 pip check、import-only probe、源码/模板/入口/归档/SDK 字节核对后才由 producer 写 manifest。不能手工补 ready/status 或借旧 ww124 manifest。
5. **producer/build 边界**：独立准备成功后，才向 QA 正式 `Build-YeYuGamerWwAdapter.ps1` 显式传 `-SourceRoot D:\Game\Automation\_qa\queue-timeout -BuildRoot <QA内新adapter构建根> -ToolRoot <步骤3运行根>` 及它要求的其他游戏输入。它是共享 OpenKuro builder，仍会校验 Endfield/GF2；材料不齐不能借本机真实配置/工具补位。新绑定须核对 root、entry、python 和各自哈希及来源提交，离线检查拒绝裸 fork 与篡改包；这里的 ToolRoot 是运行根，**不是 fork 源码目录**。
6. **到此停止，不发布**：本任务不迁移 QA publisher 缺失的桥接实现，更不调用 Install、Publish 或配置同步。未来另行集成 publisher 时才将本轮合格运行根作为显式 WW 参数；保留 Manager 用户配置归属、CAS/幂等和回读。准备包通过仍不等于真实绑定切换或游戏验收。

最小缺口清单因此是：目标提交来源 profile/完整哈希资格、QA 内离线依赖与模板材料、新运行包 producer 结果、共享 builder 必要输入，以及 QA publisher 桥接实现。文档 patch 不能解决这些缺口。本轮交付是可按输入/产物/门禁执行的方案及现有入口证据，没有声称已经产出运行包。

---

## 以下为迁回前历史调查（旧轮次，不作本轮 QA 验收）

## 结论与范围

2026-10-28，只读追踪维护源码，交付一个**未应用的文档合同补丁**和纯文本离线回归；不修改可执行逻辑，不把路径替换当作运行包迁移。源码 HEAD 为 `f4ab6bf76365cd62e5ccde118398b417ed086de7`。工作树存在大量其他任务改动；没有暂存、提交、push、联网、API 请求、构建、安装、部署、游戏或业务程序启动。离线检查仅运行测试解释器与 Git 检查，不导入 Manager。

正确的源码目标是 `D:\Game\Tools\forks\ok-ww` + `yeyu-local-20261028`，本次本地 ref 解析为 `1c3dc74ef985c09a9b26a4255a459d6df5d3333f`。该源码仓库 HEAD 为 `d7fe3e05f9ae52322e952089836387f85713dcc7`，**HEAD 与目标 ref 不相同**，不可用当前工作树冒充目标版本。未切换该仓库分支或文件。

## 当前源码证据

以下路径均相对于 `D:\Game\Automation\YeYuGamer`，行号来自本次读取：

| 层次 | 证据 | 解释 |
| --- | --- | --- |
| 接入合同 | `adapter-host/operation-contracts.json:22` | WW 指向 OpenKuro runner、Ww builder 与 installer；没有 ww124、工具根或版本选择字段。 |
| 来源准备默认 | `scripts/Prepare-WwOfficialCandidate.ps1:3`、`:5`、`:6` | `OfficialSource` 默认 vendor-baselines，`SourceRevision` 为 ValidateSet 固定提交，默认 c3fef9a；既不默认 ww124，也不接受目标标签作为任意输入。 |
| 源码归档 | `scripts/Prepare-WwOfficialCandidate.ps1:225` | 用 `$ExpectedCommit` 做本地 git archive；不是自动采用源码仓库 HEAD。不能仅改 OfficialSource 就声称目标 ref 已纳入。 |
| 构建默认 | `scripts/Build-YeYuGamerWwAdapter.ps1:5`、`:248` | `ToolRoot` 默认 `D:\Game\Tools\Automation\ok-ww`，实参经 GetFullPath 成为 `$tool`；不是 ww124。 |
| 两种布局 | 同脚本 `:250`、`:258`、`:329`、`:333` | 有 preparation manifest 时要求 WW/source、r/Scripts/pythonw.exe、归档及既有来源/哈希门；没有时要求 ok-ww.exe 和 PyAppify 配置。裸 fork 源码树不能直接作为任一合格运行包。 |
| 生成绑定 | 同脚本 `:492`、`:493`、`:497` | `$tool` 写入候选 tool-binding.json 的 tools.WW.root，入口/解释器及哈希随之冻结；不能只替换 root 或绕过哈希。 |
| 发布实参 | `scripts/Publish-YeYuGamerLocalRelease.ps1:38`、`:621` | 显式 WwCandidateToolRoot 覆盖 builder ToolRoot。没有该实参才沿用 builder 默认；不是自动从当前 Manager 配置选 ww124。 |
| 用户配置同步 | 同脚本 `:636`、`:770`、`:805`、`:1915` | 仅选中 WW 且传入 WW 候选根时生成 toolPath 映射；安装发布阶段通过当前 stateVersion/幂等键 PATCH 配置并回读。**该流程会写真实配置，禁止在本任务运行，包括将发布脚本当作查询入口。** |

对 scripts、adapter-host、docs、platform 及后端/前端源码的限定文本检索未命中 `ww124` 或 `yeyu-local-20261028`（不含缓存及真实配置）。因此没有源码证据把 ww124 定性为硬编码默认；具体是谁在历史发布时传入该路径，本次证据不足，不能猜测调用者。

## 只读派生产物对照（不代表当前 Manager）

- `.cache/upstream-candidates/ww124/WW/runtime-preparation.json` 的白名单字段：schemaVersion=1、status=read-only-import-ready、candidateKind=isolated-local-ww-source-patch；source.commit=`20d2a0fbd5cbe257d80cc2de5f70d369ba25d07f`，officialBaseCommit=`6ffaa7f0b0b7f3140367de96c8eacba533c96894`。entry 为 `main.py --task 1 --exit`，工作目录 `WW/source`，解释器 `r\Scripts\pythonw.exe`。**它不是目标 ref 的提交。**未读该运行候选的账号配置、日志或 mutable 内容。
- `.cache/adapter-build/local-daily-candidate/tool-binding.json` 白名单字段仍指向历史 `C:\Projects\YeYuGamer\.cache\upstream-candidates\ww-v3.7.3-revive-escape-20261004`，并非 ww124。它只是某次构建的派生绑定，不据此推断安装包或 Manager 现值，不沿该旧路径继续读取。
- 未读取真实运行 DB、token、Manager 保存配置或安装目录。因此**真实绑定当前值未知；真实绑定未切换、未验收**。

## 最小交付与后续集成条件

`integration-patches/ww-binding-followup.patch` 只为已跟踪的 `docs/integrations/ok-ww.md` 插入来源/运行包边界说明，未应用；不夹带该文件已有 dirty diff，不夹带任何现存 untracked 文件，不改 manager/model/store/protocol/queue 或其他任务 WW 专用模块。没有足够证据支持可执行路径修复：把 ToolRoot 改成 fork 会触发错误布局；把 SourceRevision 直接加白会绕过未完成的来源资格审查。故本次不制造这种补丁。

补丁基于当前工作文件而不是 HEAD：baseSHA256=`62c1397bbc3ff25c18715dc22351885017360f25a0924b9ead5938451332152e`。应用前必须核对此哈希；漂移时重新检查最小 hunk，不强制覆盖。备份位于 `.cache/ww-binding-followup/before/ok-ww.md`，SHA256 相同。该备份保留原 dirty 内容，仅用于保护现场，不纳入交付补丁。

后续独立维护任务应从指定 ref 归档、核对来源及依赖/资源/入口/哈希资格，再准备满足现有合同的运行包，显式传入发布参数；不得全局替换 upstream-candidates（其他游戏和历史证据仍使用该目录）。上述构建、发布、配置切换与实机验证均不在本任务授权中，尚未执行。

## 历史初版离线验证（已被最终 QA 段替代）

下列记录对应迁回前、错误写入主工作区时的初版，不是 QA HEAD 的最终结果。初版测试没有区分 currentMain 参考实现和 QA candidate，后续已修正。

- `C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe -B backend/tests/test_ww_binding_followup.py`：4 项通过，退出码 0。
- `git apply --check -- integration-patches/ww-binding-followup.patch`：退出码 0，仅检查可应用性，没有应用补丁。
- 检查后原合同 SHA256 仍为 `62c1397bbc3ff25c18715dc22351885017360f25a0924b9ead5938451332152e`，与备份一致。
- 撤回初版“范围仅新增三份指定交付物”的不完整表述：实际还在主缓存新增两份备份，共五文件，属于写入边界事故，不可表述为从未写入主目录。

## 最终 QA 结果（唯一最终验证段）

**边界事故**：初版三份交付物及两份备份实际写入过 `D:\Game\Automation\YeYuGamer` 与其 `.cache`，共五文件。父方已记录五文件按 SHA 迁回 QA；本报告保留该事实，不冒称主目录从未写入。本轮报告修正只修改 `D:\Game\Automation\_qa\queue-timeout\docs\qa\ww-binding-followup.md`。

最终测试版本的已有执行结果：默认 QA root 为 **5 pass + 1 skip**，退出码 0；显式 currentMain reference 为 **6 pass**（5 个 QA candidate 基线检查 + 1 个 reference evidence），退出码 0。reference 的通过不能代替 QA publisher 桥接功能通过；QA 缺口仍保留。错误 SHA 负例已验证短消息拒绝。上述结果是前轮实际运行记录，本次纯报告修正没有重跑测试。

正确复现命令（明确清除继承环境，结束后恢复；仅运行测试解释器）：

```powershell
$previousReference = $env:YEYU_QA_REFERENCE_SOURCE_ROOT
try {
    Remove-Item Env:YEYU_QA_REFERENCE_SOURCE_ROOT -ErrorAction SilentlyContinue
    & C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe -B D:\Game\Automation\_qa\queue-timeout\backend\tests\test_ww_binding_followup.py -v
    # 预期：QA 5 pass + 1 skip
    $env:YEYU_QA_REFERENCE_SOURCE_ROOT = 'D:\Game\Automation\YeYuGamer'
    & C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe -B D:\Game\Automation\_qa\queue-timeout\backend\tests\test_ww_binding_followup.py -v
    # 预期：reference 6 pass，不是 QA 发布功能绿灯
} finally {
    if ($null -eq $previousReference) {
        Remove-Item Env:YEYU_QA_REFERENCE_SOURCE_ROOT -ErrorAction SilentlyContinue
    } else {
        $env:YEYU_QA_REFERENCE_SOURCE_ROOT = $previousReference
    }
}
```

参考源码读取前必须匹配测试中固定 SHA。此前结束前复核三份主源码参考脚本哈希未变，QA 对应脚本相对 HEAD 无 diff；这些只是当时的检查，不扩大为全主目录未写入证明。旧文档 patch 未应用，其旧 baseSHA 与可应用性不属本轮最终验收。真实绑定未切换、运行包未准备、未部署、未实机验收。
