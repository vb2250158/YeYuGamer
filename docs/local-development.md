# 本机开发与验证

本文描述公开仓库的开发方式，不依赖维护者的个人目录或私有运行资料。项目仅供学习自动化编排，完整多游戏每日仍在实验验证中。

## 环境与目录

发布目标为 Windows x64。Python 源码声明支持 Python 3.11 及以上；当前发布依赖锁对应 CPython 3.12 / Windows amd64，正式打包应使用该锁定目标。WebGUI 使用 Node.js 和 `webgui/package.json` 声明的 pnpm 版本，依赖以 `webgui/pnpm-lock.yaml` 为准。Windows/.NET 编译工具与路径由构建脚本检查。

将仓库放在本机磁盘的维护目录，不从网络盘构建或运行。源码用于修改，发布快照用于验证和构建，安装目录用于运行；运行目录由 Manager 管理配置、状态与证据。不要将安装目录或运行数据复制回仓库。

游戏客户端与上游助手需独立获取，安装在本机后通过 WebGUI 配置。公开仓库不提供账号、登录状态或私人运行资料。

## 构建完整性限制

StarRail Adapter 依赖 `March7thAssistantBasePatch.b64` 兼容字节码载荷。该载荷基于 GPL 上游，尚未提供对应的完整可读源码与生成器，因此不纳入公开仓库。当前不能仅用公开仓库独立构建 StarRail Adapter，需要经审计、来源及许可条件明确的本机载荷；本说明不替代上游许可要求。

其余 Adapter 同样需要各自的本机依赖和独立获取的上游助手。不要将缺失依赖或未公开输入描述为可复现构建成功。上游接入条件应以各构建脚本的实际检查为准。

## 离线开发检查

在仓库根目录建立本机 Python 环境并安装源码包：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[test]" -e ".\platform"
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests
.\.venv\Scripts\python.exe -m unittest discover -s platform/tests
```

部分 Windows 或桌面依赖测试要求额外组件；以测试说明及构建脚本检查结果为准，缺失依赖造成的跳过不能作为通过。复现发布依赖时，使用 `packaging/requirements.cpython312-win_amd64.lock` 及其来源记录。

WebGUI 检查在对应目录运行：

```powershell
Set-Location webgui
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm test
pnpm run build
```

这些检查不代替正式安装后的游戏内验收。修改版本时同步 Python 包元数据与导出版本、Manager 健康版本、WebGUI 包版本和 Adapter Host 握手版本；不要改写依赖锁中的第三方版本或历史协议 fixture。

## 统一发布

从仓库根目录按本机依赖选择 Adapter 组。以下示例不包含暂时缺少公开构建输入的 StarRail：

```powershell
.\scripts\Publish-YeYuGamerLocalRelease.ps1 -AdapterGroups Classic,OpenKuro,NTE,FGO,CZN,BD2
```

省略 `AdapterGroups` 会请求默认全部组，仍需满足 StarRail 等组的输入条件。该入口在本机生成独立源码快照，检查依赖与构建工具，构建并测试主程序和所选 Adapter，再进行事务安装和 Manager 诊断晋级。它会更新已安装程序，已有运行配置应按本机流程保留，不能用样例覆盖；当前批次须已结束，或通过正式界面安全停止。仍未结束的人工暂停批次也会阻止安装。

发布时使用已测试快照中的客户端向现有 Manager 申请安全停机，以便升级旧客户端自身的生命周期修复。未显式指定状态版本的停止请求遇到 HTTP 412 时，最多重读并发送三次，保持同一幂等键与请求内容；HTTP 409 仍立即停止发布，不取消新出现的任务，也不强制结束宿主。

需要指定工具时，可使用脚本声明的 `PythonPath`、`NodePath`、`PnpmPath` 和 `CSharpCompilerPath` 参数，指向本机已验证工具。构建或验证失败时修复维护源码，不在快照或安装目录临时打补丁。

## 真实运行验收

安装完成后，通过 WebGUI 检查游戏与助手路径、启用范围和 Todo 选择，再点击“开始每日”。出现人工门时保留现场，从同一运行的释放接管/恢复流程继续；诊断时缩小的范围应记录并恢复。

逐项检查同次事件、前后画面、游戏语义和复核结果。启动器可用、助手退出成功或测试通过不能单独证明每日完成；只有通过复核的 `accepted_done` 项目可作为完成证据。

## 公开提交前

仅提交本次需要的源码、公开文档与依赖锁，不纳入本机虚拟环境、游戏或助手安装包、构建产物、个人配置、账号凭据、数据库、日志和截图。检查实际待提交内容，不仅依赖忽略规则。

第三方软件及素材遵守各自许可。“仅供学习”是用途说明，不是许可证。提交说明应区分离线验证、受限只读检查和真实游戏验收，明确仍未验证的环节。
