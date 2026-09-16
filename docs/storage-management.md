# 本机目录管理规范

本规范管理 YeYu Gamer 的文件归属与维护产物。源码仍唯一位于 `C:\Projects\YeYuGamer`。目录清爽不能以丢失配置、历史证据或破坏现有路径为代价。

用户最终要求：**缓存全部收在项目 `.cache` 下，不另设根目录缓存。** 构建、下载、测试、发布快照、历史归档和维护清单都在这一个目录分子类；`.cache/` 已被 Git 忽略，源码快照在进入目录前排除它，禁止递归复制缓存。

旧 `%LOCALAPPDATA%\YeYuGamer` 仅保留指向本项目 `.cache` 的兼容目录链接，不存第二份数据，用于旧虚拟环境和已安装配置中的绝对路径。新脚本直接使用项目路径。正式安装目录、Manager 运行数据和游戏本体具有独立生命周期，不属于缓存。

## 固定落点

| 类别 | 位置 | 生命周期 |
| --- | --- | --- |
| 维护源码、测试、文档、项目 skill | `C:\Projects\YeYuGamer` | Git 管理；禁止在副本修补 |
| 正式安装 | `%LOCALAPPDATA%\Programs\YeYuGamer` | 统一发布脚本安装，保留事务回退版本 |
| 配置、数据库、账号、日志和游戏证据 | `C:\ProgramData\YeYuGamer\runtime` | Manager 拥有；不按缓存删除或手工迁移 |
| 正式构建及上一有效构建 | `C:\Projects\YeYuGamer\.cache\build`、`build.last-known-good` | 保留当前及最后有效版本 |
| 发布源码快照、证据 | `C:\Projects\YeYuGamer\.cache\release-source`、`release-evidence` | 按发布 ID 归档；保留安装引用和回滚所需版本 |
| 构建工具、依赖与下载缓存 | `C:\Projects\YeYuGamer\.cache` 现有 `build-tools`、`build-python`、`upstream-cache` 等专用子目录 | 可重建，但离线依赖与被引用载荷不能仅按日期删除 |
| 短路径打包虚拟环境 | `C:\Projects\YeYuGamer\.cache\yg\<随机 ID>` | 构建成功或失败后 finally 回收；进程强杀残留留待盘点 |
| 历史根目录归档 | `C:\Projects\YeYuGamer\.cache\archive\legacy-root\<原目录名>` | 冷归档，只用于追溯或恢复，不能从这里启动旧虚拟环境 |
| 维护盘点、迁移清单 | `C:\Projects\YeYuGamer\.cache\maintenance\storage-<时间>` | 私有审计，包含原路径、目标路径和状态 |
| 新的临时验证与调查 | `C:\Projects\YeYuGamer\.cache\verification\<日期-任务>` | 每个任务一个目录；结束时写结论与保留原因 |
| 游戏、正式上游助手、候选助手 | Manager 中已登记的本机路径；候选统一 `C:\Game\YeYuGamerCandidates` | 工具与配置可能含绝对路径；迁移需独立验证启动链 |

禁止新增 `C:\ygb-*`、`C:\YeYuGamer-<日期/实验>` 或新的根目录源码副本。确需短路径时使用项目 `.cache\yg`。其他项目新源码默认归 `C:\Projects\<项目>`；现有程序不能只因命名杂乱就直接搬动。

## 如何维护

在源码根运行：

```powershell
# 默认只盘点，结果写入 maintenance，不移动文件。
.\scripts\Manage-YeYuGamerStorage.ps1
# 将根目录中符合限定命名、至少两天未修改且无已发现引用的旧产物归档。
.\scripts\Manage-YeYuGamerStorage.ps1 -ArchiveLegacy
```

脚本读取进程、计划任务和安装/运行配置目录中的 JSON 引用；拒绝重解析路径、Git 工作副本、重复目标及近期目录。它只处理限定的历史 YeYuGamer 根目录，不删除文件，不停进程，不改运行配置。虚拟环境还需 `pyvenv.cfg` 证明。进程或配置快照不证明不存在所有外部引用，移动遇到占用或无法核对时保留并记原因；开始前不要并行发布或构建。

归档使用同盘目录重命名，先写 `moves.jsonl` 意图，后写结果，最终 `result.json` 是逐项清单。异常中断时按源/目标是否存在核对 JSONL，不能把 planned 当成功。恢复单项前读取该记录，确认原路径为空、目标仍在归档根下且无重解析点，再用 PowerShell `Move-Item -LiteralPath <归档路径> -Destination <原路径>`；不得覆盖新目录或跨 shell 拼删除命令。虚拟环境移动后绝对路径可能失效，需要使用时重建，不把冷归档当可运行安装。

## 保留与清理

- 每轮维护或发布结束检查短路径临时环境；正常退出应为空。强杀残留先核对进程与归属，再归档或清理。
- 测试/调查优先复用固定分类父目录，每个任务只建立一个带日期和目的的子目录，记录负责人、生成命令、结论、是否可重建及保留原因。
- 失败调查材料默认保留 14 天后列入复查；历史冷归档默认保留 30 天后列入复查。这是复查时间，不是自动删除授权。
- 当前安装、上一有效构建、尚未结束的运行、故障恢复材料、明确要求保留的历史记录，以及全部凭据和用户配置不参与通用清理。
- `release-source`、`release-evidence`、`build.incoming-*` 与现有 Adapter 测试目录本轮保持兼容路径；后续清理必须先核对安装清单和进行中的发布。不能为了表面整齐批量搬动被引用目录。
- 不建立定时清盘任务；需要定期自动维护时另行明确范围和保留策略。

## 其他 C 盘目录

系统目录、系统分页文件、`Program Files`、`ProgramData`、`Users`、Windows 恢复目录由系统管理。游戏安装、助手及历史运行数据按各自生命周期处理；其他项目、私有插件和配置不在本项目中重定位。

盘点中归属与恢复用途未验证的备份、安装介质、驱动或临时目录，不自动称作垃圾。后续由对应项目核对引用后迁移至其维护归档；不要用通配符清空 C 盘或系统临时目录。具体私有目录清单仅保留本机维护记录。

## 规范入口与技能

`AGENTS.md` 是维护入口；目录规则只在本文维护，本机私有的 `docs/local-ownership.md` 说明源码与运行数据边界（不随公开仓库发布），[编排合同](integrations/orchestration-contract.md)说明产品职责。项目 skill 位于 `.codex/skills/yeyugamer-maintenance/SKILL.md`，全局同名 skill 只负责引导到当前源码；`game-daily-automation` 的 YeYu 分支必须遵循当前合同，不能转到旧 NAS。

本机 WorkBuddy 的 `yeyugamer-run-triage`、`yeyugamer-weekly-todo-design`、`yeyugamer-upstream-pr`、`cef-game-launcher-update` 同样指向这些当前规范，保留历史案例但不把案例中的临时路径、版本和操作授权永久化。项目本机 AGENTS 与 `.codex` 当前由既有 `.gitignore` 排除；它们已在本机落地，不表示已进入公开仓库。

历史调查文档和 `.workbuddy` 记录是当时的快照，涉及版本、阻塞、目录、计划任务和成功结果时必须读当前状态复核。历史快照不得覆盖上述规范。

## 2026-09-16 整理结果

根目录共归档 214 项，其中 202 项为旧打包虚拟环境，12 项为旧构建、测试、通知开发和发布源码副本。全部采用同盘移动，未删除文件，也未释放磁盘空间。私有清单位于 `C:\Projects\YeYuGamer\.cache\maintenance\storage-20260916-161417-568`；迁移后根目录不再有 `ygb-*` 和 `YeYuGamer*` 散落目录。

随后按用户要求将缓存全部集中到项目 `.cache`，214 项归档的最终位置为 `.cache\archive\legacy-root`。旧路径下较新产物归入对应分类，同名较早产物保留在 `.cache\archive\cache-before-consolidation`。迁移补充清单为 `.cache\maintenance\project-cache-migration.json`；历史清单中的 `C:\Cache` 是当时位置，恢复前需套用补充清单。`C:\Cache` 本轮创建的目录已移空并移除。

项目缓存调整已通过发布源码隔离 18 项检查、PrepareOnly 离线测试、构建源码静态检查及缓存清理/源码排除测试。当前锁定 PySide6 wheel 在新打包路径下最长成员路径为 210 字符；本轮未执行完整打包与发布。

验证：`Test-YeYuGamerStorage.ps1` 覆盖正常清理、失败清理和越界拒绝；`Test-YeYuGamerBuild.ps1` 的源码静态合同通过，三个 Codex skill 使用 UTF-8 模式校验通过。尝试检查现有构建包时，隐私扫描报 `Runtime package leaks a private machine token: _asyncio.pyd`，未改该已有产物，也未执行完整构建、安装或真实游戏验收。该报告不能解释为当前安装包已通过发布验收。
