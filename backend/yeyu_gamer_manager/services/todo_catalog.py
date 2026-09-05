from __future__ import annotations

import hashlib
import json
from typing import Any


DAILY_CAPABILITY = "game.daily.run@1.0"
WEEKLY_CAPABILITY = "game.weekly.run@1.0"
CATALOG_VERSION = "2026.09.01.1"
DEFINITION_VERSION = 2
CANONICAL_DEFINITION_SCHEMA = "todo-definition-source/v2"


def canonical_definition_document(record: dict[str, Any]) -> dict[str, Any]:
    """Project one Todo definition into its stable, semantic source document.

    Catalog release metadata is deliberately excluded: adding an unrelated Todo
    must not change the source digest of an unchanged definition.  The stable
    definition id and definition revision remain part of the digest, alongside
    every field that can change scheduling, execution, review, or presentation.
    """

    return {
        "schemaVersion": CANONICAL_DEFINITION_SCHEMA,
        "todoDefinitionId": record["todo_definition_id"],
        "definitionVersion": int(record["definition_version"]),
        "gameId": record["game_id"],
        "cadence": record["cadence"],
        "operation": record["operation"],
        "title": record["title"],
        "category": record["category"],
        "orderIndex": int(record["order_index"]),
        "required": bool(record["required"]),
        "risk": record["risk"],
        "automationDifficulty": record["automation_difficulty"],
        "adapterCapabilityRef": record["adapter_capability_ref"],
        "automationState": record["automation_state"],
        "initialStatus": record["initial_status"],
        "initialReason": record["initial_reason"],
        "resetRule": dict(record["reset_rule"]),
        "sourceRefs": list(record["source_refs"]),
    }


def definition_source_hash(record: dict[str, Any]) -> str:
    """Return the canonical SHA-256 for one semantic Todo definition."""

    canonical_json = json.dumps(
        canonical_definition_document(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _definition(
    game_id: str,
    cadence: str,
    operation: str,
    title: str,
    category: str,
    order_index: int,
    *,
    required: bool = True,
    risk: str = "routine_action",
    difficulty: str = "medium",
    capability: str | None = None,
    automation_state: str = "catalogued-unbound",
    initial_status: str = "pending",
    initial_reason: str = "",
    reset_time: str = "04:00",
    source_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    reset_rule: dict[str, Any] = {
        "timezone": "Asia/Shanghai",
        "time": reset_time,
        "cadence": cadence,
    }
    if cadence == "weekly":
        reset_rule["weekStartDay"] = "Monday"
    record = {
        # The v1 id is a durable identity, not the mutable definition revision.
        "todo_definition_id": f"todo.v1.{game_id.lower()}.{cadence}.{operation}",
        "definition_version": DEFINITION_VERSION,
        "catalog_version": CATALOG_VERSION,
        "game_id": game_id,
        "cadence": cadence,
        "operation": operation,
        "title": title,
        "category": category,
        "order_index": order_index,
        "required": required,
        "risk": risk,
        "automation_difficulty": difficulty,
        "adapter_capability_ref": capability,
        "automation_state": automation_state,
        "initial_status": initial_status,
        "initial_reason": initial_reason,
        "reset_rule": reset_rule,
        "source_refs": list(source_refs),
    }
    return {**record, "source_hash": definition_source_hash(record)}


def _daily(
    game_id: str,
    operation: str,
    title: str,
    category: str,
    order_index: int,
    **kwargs: Any,
) -> dict[str, Any]:
    return _definition(
        game_id,
        "daily",
        operation,
        title,
        category,
        order_index,
        capability=kwargs.pop("capability", DAILY_CAPABILITY),
        **kwargs,
    )


def _weekly(
    game_id: str,
    operation: str,
    title: str,
    category: str,
    order_index: int,
    **kwargs: Any,
) -> dict[str, Any]:
    return _definition(
        game_id,
        "weekly",
        operation,
        title,
        category,
        order_index,
        capability=kwargs.pop("capability", WEEKLY_CAPABILITY),
        **kwargs,
    )


TOOL = "source-tool-declared-unbound"
HISTORY = "historical-evidence-unbound"
UNIMPLEMENTED = "disabled-unimplemented"
FORBIDDEN = "forbidden-by-policy"


TODO_DEFINITIONS: tuple[dict[str, Any], ...] = (
    # Honkai: Star Rail / March7thAssistant.
    _daily("StarRail", "attach-home", "启动并确认已进入可操作主界面", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#星铁",)),
    _daily("StarRail", "spend-trailblaze-power", "使用普通开拓力完成拟造花萼（金）", "stamina", 20, difficulty="medium", automation_state=TOOL, source_refs=("March7thAssistant/config.yaml#instance_type", "game-automation-policy.json#dailyStaminaPriority")),
    _daily("StarRail", "daily-training-objectives", "补足每日实训目标至 500", "daily", 30, difficulty="high", automation_state=TOOL, source_refs=("March7thAssistant/config.yaml#daily_enable", "按游戏总结.md#星铁")),
    _daily("StarRail", "claim-daily-training-rewards", "领取每日实训各档奖励", "reward", 40, difficulty="medium", automation_state=TOOL, source_refs=("March7thAssistant/config.yaml#DailyPracticeCompleted",)),
    _daily("StarRail", "verify-daily-task-list", "复核任务行与奖励均已完成", "evidence", 50, risk="observe_only", difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#全游戏统一验收口径",)),
    _daily("StarRail", "gacha", "跃迁/抽卡（禁止自动执行）", "gacha", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),

    # Zenless Zone Zero / OneDragon. Names follow the installed group file.
    _daily("ZZZ", "attach-home", "启动并确认已进入大世界", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#绝区零",)),
    _daily("ZZZ", "coffee", "咖啡店点单", "daily", 20, difficulty="low", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#coffee",)),
    _daily("ZZZ", "scratch-card", "刮刮卡", "daily", 30, difficulty="low", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#scratch_card",)),
    _daily("ZZZ", "trigrams-collection", "卦象集录", "daily", 40, difficulty="medium", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#trigrams_collection",)),
    _daily("ZZZ", "suibian-temple", "随便观日常", "daily", 50, difficulty="medium", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#suibian_temple",)),
    _daily("ZZZ", "random-play", "录像店经营", "daily", 60, difficulty="medium", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#random_play",)),
    _daily("ZZZ", "charge-plan", "消耗电量完成当日战斗任务", "stamina", 70, difficulty="high", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#charge_plan", "game-automation-policy.json#dailyStaminaPriority")),
    _daily("ZZZ", "city-fund-free-claim", "领取丽都城募免费档", "reward", 80, difficulty="medium", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#city_fund", "ZZZTool/docs/game/screens/丽都城募.md#可交互元素", "ZZZTool/src/zzz_od/application/city_fund/city_fund_app.py")),
    _daily("ZZZ", "engagement-reward", "领取活跃度奖励并复核满档", "reward", 90, difficulty="high", automation_state=TOOL, source_refs=("ZZZTool/_group.yml#engagement_reward", "按游戏总结.md#绝区零")),
    _daily("ZZZ", "drive-disc-dismantle", "驱动盘拆解（禁止自动执行）", "inventory", 120, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="分解属于不可逆资产动作", source_refs=("game-automation-policy.json#forbiddenActions", "ZZZTool/_group.yml#drive_disc_dismantle")),
    _daily("ZZZ", "gacha", "调频/抽卡（禁止自动执行）", "gacha", 130, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),
    _weekly("ZZZ", "ridu-weekly-reward", "领取丽都周纪奖励", "weekly_reward", 10, difficulty="medium", automation_state=TOOL, source_refs=("daily-gui-config.json#weeklyCommands.ZZZ", "一条龙队列说明.md#ZZZ周常")),
    _weekly("ZZZ", "lost-void-bounty", "补足并领取迷失之地悬赏委托", "weekly", 20, difficulty="high", automation_state=TOOL, source_refs=("一条龙队列说明.md#ZZZ周常",)),
    _weekly("ZZZ", "withered-domain", "枯萎之都（永久排除）", "weekly", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="skipped", initial_reason="用户永久排除；不执行且不计缺项", source_refs=("game-automation-policy.json#games.ZZZ.excludedTasks",)),

    # Arknights: Endfield / ok-ef.
    _daily("Endfield", "attach-world", "启动并确认在世界与有效队伍中", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#明日方舟：终末地",)),
    _daily("Endfield", "mail", "领取邮件", "mail", 20, required=False, difficulty="low", automation_state=TOOL, source_refs=("ok-ef/DailyTask.json#⭐收邮件",)),
    _daily("Endfield", "spend-sanity", "刷普通理智关卡", "stamina", 30, difficulty="high", automation_state=TOOL, source_refs=("ok-ef/DailyTask.json#⭐刷体力", "game-automation-policy.json#dailyStaminaPriority")),
    _daily("Endfield", "outpost-exchange", "据点兑换（仅游戏内调度券/货物）", "base", 40, required=False, risk="approval_required", difficulty="medium", capability=None, automation_state=TOOL, source_refs=("game-automation-policy.json#games.Endfield.permittedEconomyActions.outpost-exchange",)),
    _daily("Endfield", "delivery-commission", "转交运送委托并领取奖励", "commission", 50, required=False, difficulty="high", automation_state=TOOL, source_refs=("ok-ef/DailyTask.json#⭐转交运送委托",)),
    _daily("Endfield", "craft-equipment", "制造装备", "craft", 60, required=False, risk="approval_required", difficulty="medium", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="消耗资源与制作配方尚未建模，需人工确认", source_refs=("ok-ef/DailyTask.json#⭐造装备",)),
    _daily("Endfield", "simple-craft", "简易制作", "craft", 70, required=False, risk="approval_required", difficulty="medium", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="消耗资源与制作配方尚未建模，需人工确认", source_refs=("ok-ef/DailyTask.json#⭐简易制作",)),
    _daily("Endfield", "collect-credit", "收取信用", "base", 80, required=False, difficulty="high", automation_state=TOOL, source_refs=("ok-ef/DailyTask.json#⭐收信用",)),
    _daily("Endfield", "dijiang-harvest", "帝江号收菜", "base", 90, required=False, difficulty="medium", automation_state=TOOL, source_refs=("ok-ef/DailyTask.json#⭐帝江号收菜",)),
    _daily("Endfield", "claim-daily-reward", "领取日常活跃奖励", "reward", 100, difficulty="medium", automation_state=TOOL, source_refs=("ok-ef/DailyTask.json#⭐日常奖励",)),
    _daily("Endfield", "dynamic-objective-gap", "处理工具未覆盖的随机每日任务", "daily", 110, risk="approval_required", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="干员/武器升级等随机任务尚无稳定 handler", source_refs=("按游戏总结.md#终末地能力缺口",)),
    _daily("Endfield", "verify-reward-panel", "复核任务行及所有可领取档位", "evidence", 120, risk="observe_only", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, source_refs=("按游戏总结.md#终末地完成规则",)),
    _weekly("Endfield", "weekly-affairs-reward", "领取活动中心每周事务奖励", "weekly_reward", 10, difficulty="high", automation_state=TOOL, source_refs=("daily-gui-config.json#weeklyCommands.Endfield", "一条龙队列说明.md#终末地周常")),

    # Girls' Frontline 2 / ok-gf2.
    _daily("GF2", "attach-home", "启动并确认主界面", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#少女前线2：追放",)),
    _daily("GF2", "mail", "领取邮件", "mail", 20, difficulty="low", automation_state=TOOL, source_refs=("ok-gf2/DailyTask.json#邮件",)),
    _daily("GF2", "public-area-dispatch", "公共区/调度室", "base", 30, difficulty="medium", automation_state=TOOL, source_refs=("ok-gf2/DailyTask.json#公共区/调度室",)),
    _daily("GF2", "spend-stamina", "自动刷取普通体力关卡", "stamina", 40, difficulty="medium", automation_state=TOOL, source_refs=("ok-gf2/DailyTask.json#自动刷体力",)),
    _daily("GF2", "squad-tasks", "班组要务", "daily", 50, difficulty="medium", automation_state=TOOL, source_refs=("ok-gf2/DailyTask.json#班组",)),
    _daily("GF2", "claim-daily-missions", "领取每日任务奖励", "reward", 60, difficulty="medium", automation_state=TOOL, source_refs=("ok-gf2/DailyTask.json#领任务",)),
    _daily("GF2", "battle-pass-free-track", "领取巡录免费档", "reward", 70, difficulty="high", automation_state=TOOL, source_refs=("ok-gf2/DailyTask.json#大月卡", "ok-gf2/src/tasks/DailyTask.py#xunlu")),
    _daily("GF2", "event-layer", "活动层（按用户要求排除）", "event", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="skipped", initial_reason="用户要求不自动执行 GF2 活动层", source_refs=("按游戏总结.md#少女前线2：追放",)),
    _daily("GF2", "arena", "竞技场/PVP（禁止自动执行）", "pvp", 100, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="PVP 属于全局禁止自动动作", source_refs=("ok-gf2/DailyTask.json#竞技场", "game-automation-policy.json#forbiddenActions",)),
    _weekly("GF2", "expanded-practice-reward", "扩编实练：优先检查并领取奖励", "weekly_reward", 10, risk="approval_required", difficulty="high", capability=None, automation_state=TOOL, source_refs=("ok-gf2/WeeklyTask.json#扩编实练", "一条龙队列说明.md#GF2周常")),

    # Neverness to Everness / ok-nte.
    _daily("NTE", "attach-home", "经官方启动器进入可操作主界面", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#异环",)),
    _daily("NTE", "mail", "领取邮件", "mail", 20, difficulty="low", automation_state=TOOL, source_refs=("ok-nte/src/tasks/DailyTask.py#claim-mail",)),
    _daily("NTE", "cafe", "运行一咖舍自动化", "base", 30, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前一咖舍复合路径含补货/费用语义，未进入安全 runner", source_refs=("ok-nte/DailyTask.json#一咖舍任务",)),
    _daily("NTE", "spend-city-vitality", "按配置消耗都市活力", "stamina", 40, difficulty="high", automation_state=TOOL, source_refs=("ok-nte/DailyTask.json#目标消耗体力",)),
    _daily("NTE", "daily-activity", "读取并完成工具已覆盖的每日活跃项", "daily", 50, difficulty="medium", automation_state=TOOL, source_refs=("ok-nte/src/tasks/DailyTask.py#inspect-daily-progress",)),
    _daily("NTE", "claim-activity-reward", "领取活跃度奖励", "reward", 60, difficulty="medium", automation_state=TOOL, source_refs=("ok-nte/src/tasks/DailyTask.py#claim-daily-reward",)),
    _daily("NTE", "claim-cycle-reward", "领取环期任务奖励", "reward", 70, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#异环",)),
    _daily("NTE", "uncovered-objectives", "方斯消费/赠礼/弧盘升级等缺失每日行", "daily", 80, risk="approval_required", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="现有工具尚未实现这些随机每日行", source_refs=("按游戏总结.md#异环",)),
    _daily("NTE", "gacha", "抽卡（禁止自动执行）", "gacha", 100, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),

    # Punishing: Gray Raven / MPA.
    _daily("PGR", "attach-home", "启动并进入主界面", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#进入游戏",)),
    _daily("PGR", "claim-serum", "领取体力", "stamina", 20, difficulty="low", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#领取体力",)),
    _daily("PGR", "dorm", "宿舍任务", "base", 30, difficulty="medium", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#宿舍任务",)),
    _daily("PGR", "simulation-field", "拟战场域消耗普通血清", "stamina", 40, difficulty="high", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#拟战场域", "game-automation-policy.json#dailyStaminaPriority")),
    _daily("PGR", "maintainer-action", "维系者行动", "daily", 50, difficulty="medium", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#维系者行动",)),
    _daily("PGR", "claim-daily-tasks", "领取任务与五档活跃奖励", "reward", 60, difficulty="high", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#领取任务", "按游戏总结.md#战双完成规则")),
    _daily("PGR", "battle-pass-free-track", "领取战令免费档", "reward", 70, difficulty="medium", automation_state=TOOL, source_refs=("MPA/config_tasks_dump.json#战令",)),
    _daily("PGR", "shop-action", "商店/购买类每日（待人工判断）", "shop", 80, required=False, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="目标与成本未分类，禁止自动购买", source_refs=("MPA/config_tasks_dump.json#购买物品", "game-automation-policy.json#forbiddenActions",)),
    _daily("PGR", "gacha", "研发/抽卡（禁止自动执行）", "gacha", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),
    _weekly("PGR", "phantom-pain-audit", "幻痛囚笼证据审计", "weekly", 10, risk="approval_required", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="排名/竞争玩法仅做证据审计", source_refs=("一条龙队列说明.md#战双周常",)),
    _weekly("PGR", "war-zone-audit", "纷争战区证据审计", "weekly", 20, risk="approval_required", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="排名/竞争玩法仅做证据审计", source_refs=("一条龙队列说明.md#战双周常",)),

    # Wuthering Waves / ok-ww.
    # These are the actual source-stage boundaries in OK-WW DailyTask.run().
    # A selected row means that exact stage is invoked; conditional stages are
    # reported as skipped when the current daily progress makes them unnecessary.
    _daily("WW", "attach-world", "进入游戏并到达大世界", "attach-world", 10, difficulty="high", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#ensure_main",)),
    _daily("WW", "inspect-daily-progress", "读取今日体力与活跃进度", "inspect-daily-progress", 20, difficulty="medium", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#open_daily",)),
    _daily("WW", "farm-nightmare-daily-echo", "通关1次梦魇聚落或残象聚落（+20活跃度）", "farm-nightmare-daily-echo", 30, difficulty="high", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#need_nightmare",)),
    _daily("WW", "spend-waveplates", "按配置路线消耗结晶波片", "spend-waveplates", 40, difficulty="high", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#need_stamina", "ok-ww/DailyTask.json#Which to Farm")),
    _daily("WW", "claim-daily-reward", "领取每日活跃奖励", "claim-daily-reward", 50, difficulty="medium", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#claim_daily",)),
    _daily("WW", "claim-mail", "领取邮件", "claim-mail", 60, difficulty="low", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#claim_mail",)),
    _daily("WW", "claim-battle-pass", "领取战令免费奖励", "claim-battle-pass", 70, difficulty="medium", automation_state=TOOL, source_refs=("ok-ww/src/task/DailyTask.py#claim_battle_pass",)),
    _daily("WW", "gacha", "唤取/抽卡（禁止自动执行）", "gacha", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),

    # NIKKE / ok-NIKKE and MNA. The strict daily completion gap is explicit.
    _daily("NIKKE", "attach-lobby", "经 WeGame 进入大厅", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#胜利女神新的希望",)),
    _daily("NIKKE", "outpost", "领取前哨基地挂机奖励", "base", 20, difficulty="medium", automation_state=TOOL, source_refs=("一条龙队列说明.md#nikke.outpost",)),
    _daily("NIKKE", "dispatch-friend", "派遣与好友互动", "social", 30, difficulty="medium", automation_state=TOOL, source_refs=("一条龙队列说明.md#nikke.dispatch_friend",)),
    _daily("NIKKE", "daily-shop", "每日商店（只允许已确认零成本项）", "shop", 40, risk="approval_required", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="现有复合节点混入商店与战斗补足，未进入受限 runner", source_refs=("一条龙队列说明.md#nikke.daily_shop",)),
    _daily("NIKKE", "campaign", "完成战役每日目标", "combat", 50, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前 ark 战斗分支未启用", source_refs=("按游戏总结.md#NIKKE严格复核",)),
    _daily("NIKKE", "tower", "完成爬塔每日目标", "combat", 60, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前行为树没有稳定导航", source_refs=("按游戏总结.md#NIKKE严格复核",)),
    _daily("NIKKE", "simulation-room", "完成模拟室每日目标", "combat", 70, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前行为树没有稳定导航", source_refs=("按游戏总结.md#NIKKE严格复核",)),
    _daily("NIKKE", "interception", "完成拦截战每日目标", "combat", 80, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前行为树没有稳定导航", source_refs=("按游戏总结.md#NIKKE严格复核",)),
    _daily("NIKKE", "claim-daily-reward", "领取每日任务奖励并复核 100 活跃", "reward", 90, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="三个旧 Mark 不能证明每日任务全部完成", source_refs=("按游戏总结.md#NIKKE严格复核",)),
    _daily("NIKKE", "recruit", "招募/抽卡（禁止自动执行）", "gacha", 120, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),
    _daily("NIKKE", "arena", "竞技场/PVP（禁止自动执行）", "pvp", 130, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="PVP 属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),

    # Fate/Grand Order CN. Its server daily reset differs from the Manager default.
    _daily("FGO", "attach-home", "进入 FGO 国服主界面", "session", 10, difficulty="high", automation_state=TOOL, reset_time="00:00", source_refs=("按游戏总结.md#FGO国服",)),
    _daily("FGO", "three-10ap-quests", "运行三次最低 10 AP 关卡", "stamina", 20, difficulty="high", automation_state=TOOL, reset_time="00:00", source_refs=("game-automation-policy.json#games.FGO.dailyBattle", "按游戏总结.md#FGO完成实绩")),
    _daily("FGO", "daily-missions-4of4", "确认御主任务每日 4/4", "daily", 30, difficulty="medium", automation_state=HISTORY, reset_time="00:00", source_refs=("按游戏总结.md#FGO完成实绩",)),
    _daily("FGO", "claim-daily-missions", "领取每日任务奖励", "reward", 40, difficulty="medium", automation_state=HISTORY, reset_time="00:00", source_refs=("按游戏总结.md#FGO完成实绩",)),
    _daily("FGO", "verify-reward-panel", "保存任务页与领取证据", "evidence", 50, risk="observe_only", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, reset_time="00:00", source_refs=("按游戏总结.md#FGO完成实绩",)),
    _daily("FGO", "mailbox-expiry-review", "扫描礼物箱期限与库存溢出风险", "mail", 60, required=False, risk="approval_required", difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", reset_time="00:00", initial_reason="领取前必须判断库存与期限", source_refs=("game-automation-policy.json#games.FGO.mailbox",)),
    _daily("FGO", "summon", "召唤/抽卡（禁止自动执行）", "gacha", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", reset_time="00:00", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),
    _daily("FGO", "servant-enhance-or-sell", "从者强化/出售（禁止自动执行）", "inventory", 100, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", reset_time="00:00", initial_reason="强化和出售属于不可逆资产动作", source_refs=("game-automation-policy.json#games.FGO.servantInventory",)),

    # Brown Dust 2 / MFABD2. Safe tasks use fixed MFA entries only.
    _daily("BD2", "attach-home", "启动并确认登录完成", "session", 10, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#棕色塵埃2",)),
    _daily("BD2", "daily-claim", "领取每日与通行证免费奖励", "reward", 20, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#MFABD2功能",)),
    _daily("BD2", "stamina-sweep", "扫荡消耗普通体力", "stamina", 30, difficulty="high", automation_state=TOOL, source_refs=("按游戏总结.md#MFABD2功能",)),
    _daily("BD2", "dispatch-collect", "采集/派遣领取", "base", 40, required=False, difficulty="high", capability=None, automation_state=UNIMPLEMENTED, initial_status="blocked", initial_reason="MFABD2 尚未接入 Manager Adapter", source_refs=("按游戏总结.md#MaaBD2功能",)),
    _daily("BD2", "event", "活动图（未建稳定策略）", "event", 50, required=False, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="活动内容和成本未建模", source_refs=("按游戏总结.md#MaaBD2功能",)),
    _daily("BD2", "free-gacha", "免费抽卡候选（仍禁止自动执行）", "gacha", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="即使工具标为免费，抽卡仍需独立许可与核账，当前全局禁止", source_refs=("game-automation-policy.json#forbiddenActions", "按游戏总结.md#MaaBD2功能")),
    _daily("BD2", "craft-enhance-dismantle", "装备制作/强化/分解（禁止自动执行）", "inventory", 100, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="不可逆资产动作", source_refs=("game-automation-policy.json#forbiddenActions",)),

    # Chaos Zero Nightmare CN. The safe subset is bound to fixed Maa_KES entries.
    _daily("CZN", "login-bonus", "登录奖励", "reward", 10, difficulty="high", automation_state=TOOL, source_refs=("Show-CZN-DailyChecklist.ps1#Login Bonus", "game-automation-policy.json#games.CZN",)),
    _daily("CZN", "achievement-schedule", "Achievement Schedule 每日奖励", "daily", 20, difficulty="high", automation_state=TOOL, source_refs=("Show-CZN-DailyChecklist.ps1#Achievement Schedule",)),
    _daily("CZN", "arkhianon-supply", "Arkhianon Supply 每日任务", "daily", 30, difficulty="high", automation_state=TOOL, source_refs=("Show-CZN-DailyChecklist.ps1#Arkhianon Supply Daily Missions",)),
    _daily("CZN", "policy-office", "Policy Office 派遣/领取", "base", 40, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="候选清单，成本与按钮尚未分类", source_refs=("Show-CZN-DailyChecklist.ps1#Policy Office",)),
    _daily("CZN", "garden-cafe", "Garden Cafe 收益/互动", "base", 50, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="候选清单，尚无实机 handler", source_refs=("Show-CZN-DailyChecklist.ps1#Garden Cafe",)),
    _daily("CZN", "simulation-stamina", "Chaos/Simulation 普通体力路线", "stamina", 60, difficulty="high", automation_state=TOOL, source_refs=("Show-CZN-DailyChecklist.ps1#Chaos / Simulation stamina",)),
    _daily("CZN", "verify-daily-reward", "复核每日奖励终态", "evidence", 70, risk="observe_only", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="尚无原生完成证据合同", source_refs=("按游戏总结.md#卡厄斯梦境",)),
    _daily("CZN", "gacha", "抽卡（禁止自动执行）", "gacha", 90, required=False, risk="forbidden", difficulty="unknown", capability=None, automation_state=FORBIDDEN, initial_status="review_required", initial_reason="抽卡属于全局禁止自动动作", source_refs=("game-automation-policy.json#forbiddenActions",)),

    # Explicit weekly scope gaps. These are review items backed by current
    # configuration notes; they do not claim an executable weekly Adapter.
    _weekly("StarRail", "weekly-scope-evidence", "复核星铁本周任务范围与独立领奖证据", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前配置明确没有独立权威周常证据入口", source_refs=("daily-gui-config.json#weeklyCommands.StarRail",)),
    _weekly("WW", "weekly-scope-evidence", "复核鸣潮本周任务范围与独立领奖证据", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前配置尚未登记鸣潮周常脚本", source_refs=("daily-gui-config.json#weeklyCommands.WW",)),
    _weekly("NIKKE", "interception-review", "复核拦截战周进度与奖励边界", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="需逐步验收，尚无 Manager Adapter", source_refs=("daily-gui-config.json#weeklyCommands.NIKKE",)),
    _weekly("NIKKE", "simulation-room-review", "复核模拟室周进度与奖励边界", "weekly_review", 20, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="需逐步验收，尚无 Manager Adapter", source_refs=("daily-gui-config.json#weeklyCommands.NIKKE",)),
    _weekly("NIKKE", "union-content-review", "复核联盟相关周任务并排除竞争/排行动作", "weekly_review", 30, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="联盟任务尚未完成安全分类", source_refs=("daily-gui-config.json#weeklyCommands.NIKKE", "game-automation-policy.json#forbiddenActions")),
    _weekly("NTE", "weekly-scope-evidence", "复核异环本周任务范围与独立领奖证据", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="当前配置尚未登记异环周常脚本", source_refs=("daily-gui-config.json#weeklyCommands.NTE",)),
    _weekly("FGO", "weekly-scope-evidence", "复核 FGO 本周任务范围与独立领奖证据", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="review_required", initial_reason="现有 FGO 合同仅覆盖每日 4/4 与领取证据", reset_time="00:00", source_refs=("一条龙队列说明.md#FGO国服",)),
    _weekly("BD2", "weekly-scope-evidence", "复核棕色尘埃2本周任务范围与独立领奖证据", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="blocked", initial_reason="当前配置尚未登记 BD2 周常脚本且完成链未验证", source_refs=("daily-gui-config.json#weeklyCommands.BD2", "一条龙队列说明.md#棕色尘埃2")),
    _weekly("CZN", "weekly-scope-evidence", "复核卡厄思梦境国服本周任务范围与独立领奖证据", "weekly_review", 10, risk="approval_required", difficulty="unknown", capability=None, automation_state=UNIMPLEMENTED, initial_status="blocked", initial_reason="当前配置尚未登记 CZN 周常脚本且国服完成链未验证", source_refs=("daily-gui-config.json#weeklyCommands.CZN", "一条龙队列说明.md#卡厄斯梦境")),
)


def catalog() -> list[dict[str, Any]]:
    """Return detached catalog documents so callers cannot mutate the seed."""

    return [
        {
            **item,
            "reset_rule": dict(item["reset_rule"]),
            "source_refs": list(item["source_refs"]),
        }
        for item in TODO_DEFINITIONS
    ]
