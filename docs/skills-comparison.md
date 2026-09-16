# Codex 与 WorkBuddy 技能统一说明

2026-09-16 已按用户补充要求统一本机技能文件。共同正文是 [每日工作流](daily-workflow.md)，项目技能入口是 `.codex/skills/yeyugamer-daily/SKILL.md`。

## 两边一致的入口

以下六个入口同时安装在用户目录的 `.codex/skills` 与 `.workbuddy/skills`，每对 `SKILL.md` 内容一致，均读取本项目正文。

| 技能 | 共同来源 |
| --- | --- |
| `yeyugamer-daily` | 项目每日技能 → [完整闭环](daily-workflow.md) |
| `yeyugamer-run-triage` | [日志与归因](daily-workflow/triage.md) |
| `yeyugamer-upstream-pr` | [上游修复与 PR](daily-workflow/upstream-pr.md) |
| `yeyugamer-weekly-todo-design` | [周常](daily-workflow/weekly.md) |
| `cef-game-launcher-update` | [启动器更新](daily-workflow/launcher-update.md) |
| `yeyugamer-maintenance` | 项目维护技能 |

Codex 原有 `game-daily-automation` 保留非 YeYu 用途，YeYu 分支转入共同正文。两边其他无关技能、脚本和元数据未改动；遗留脚本不作为新流程依赖，需另行核验才可使用。

## 统一后的主要规则

1. 接续当前范围与轮次，核实官方最新版本、更新日志和现有 PR，验证兼容后更新。
2. 收集完整官方日志与编排日志，先检查 YeYu 注入、映射、观察和生命周期；版本最新不代表工具没有 bug。
3. YeYu 缺陷在本项目修复、验证并按授权提交 GitHub；只有证据确认属于工具且上游尚无修复，才修改隔离上游源码并提交 PR。
4. PR 附复现条件、脱敏日志、根因、排除接入的依据及修复前后验证，提交后核对 CI/反馈。
5. 回到已安装 WebGUI 继续未完成每日，以同账号、RunAttempt 和游戏日的官方终态确认结果；更新或 PR 提交不是每日完成。

原 WorkBuddy 正文中的 CLI 实跑验收、全面禁止 HTTP API、永久阻塞结论和直接照搬 CDP/强杀命令等已从入口移除。历史版本保存在本机 `.cache/verification/20260916-skill-unification/before`，只作追溯，不作为执行规则。

本次交付是技能与文档统一；文件检查不代表 WorkBuddy 应用已经热加载新技能，也不代表今日所有游戏已经完成。后续会话按平台技能发现机制加载入口。
