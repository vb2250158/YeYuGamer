from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DailyParameterSpec:
    """A reviewed parameter, separate from a tool's raw configuration file."""

    key: str
    label: str
    value_type: str
    dispatch_status: str
    options: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class GameIntegrationRegistration:
    """One tool's real selectable stages, independent of execution promotion."""

    game_id: str
    integration_id: str
    tool_name: str
    source: str
    entry_operation: str | None
    daily_operations: tuple[str, ...]
    parameters: tuple[DailyParameterSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class GameIntegrationCandidate:
    """A discovered upstream tool that is not yet a safe selectable integration.

    Candidates deliberately carry no operation mapping.  A tool is promoted to
    ``GameIntegrationRegistration`` only after it accepts selected operations
    and produces per-operation structured stage events.
    """

    game_id: str
    tool_name: str
    source: str
    mapping_note: str
    parameters: tuple[DailyParameterSpec, ...] = ()


_REGISTRATIONS: tuple[GameIntegrationRegistration, ...] = (
    GameIntegrationRegistration(
        game_id="StarRail",
        integration_id="starrail.march7th-daily",
        tool_name="March7th Assistant daily runner",
        source="StarRail Manager runner operation whitelist and evidence bridge",
        entry_operation="attach-home",
        daily_operations=(
            "attach-home",
            "spend-trailblaze-power",
            "daily-training-objectives",
            "claim-daily-training-rewards",
            "verify-daily-task-list",
        ),
        parameters=(
            DailyParameterSpec("golden_calyx_region", "金蕾地区", "enum", "requires_adapter", ("雅利洛-VI", "仙舟罗浮", "匹诺康尼"), note="当前绑定会校验上游配置哈希；保存后必须重新晋级，不能直接改工具配置。"),
            DailyParameterSpec("normal_power_runs", "普通开拓力次数", "integer", "requires_adapter", minimum=1, note="固定金蕾路线；后备开拓力、燃料、遗器分解与沉浸器均不开放。"),
            DailyParameterSpec("existing_team_preset", "已存在的队伍预设", "enum", "requires_adapter", ("不切换队伍", "预设 1", "预设 2", "预设 3", "预设 4"), note="仅引用工具内已存在队伍，不创建或编辑配队。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="WW",
        integration_id="ok-ww.daily-task",
        tool_name="OK-WW DailyTask",
        source="OK-WW DailyTask structured-stage hook",
        entry_operation="attach-world",
        daily_operations=(
            "attach-world",
            "inspect-daily-progress",
            "farm-nightmare-daily-echo",
            "spend-waveplates",
            "claim-daily-reward",
            "claim-mail",
            "claim-battle-pass",
        ),
        parameters=(
            DailyParameterSpec("which_to_farm", "体力路线", "enum", "active", ("无音区", "锻造挑战", "模拟领域"), note="已随 Manager profile 下发到已晋级的 OK-WW 阶段钩子。"),
            DailyParameterSpec("material_selection", "模拟领域材料", "enum", "active", ("共鸣者经验", "武器经验", "贝币"), note="仅在体力路线为模拟领域时使用。"),
            DailyParameterSpec("farm_nightmare_nest_for_daily_echo", "梦魇巢穴补足活跃", "boolean", "active", note="仅当当天活跃不足时由每日流程自动处理。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="Endfield",
        integration_id="ok-ef.daily-task",
        tool_name="OK-EF DailyTask",
        source="OK-EF selected-operation structured-stage hook",
        entry_operation="attach-world",
        daily_operations=(
            "attach-world",
            "mail",
            "spend-sanity",
            "delivery-commission",
            "collect-credit",
            "dijiang-harvest",
            "claim-daily-reward",
        ),
        parameters=(
            DailyParameterSpec("stamina_stage", "普通理智本", "enum", "requires_adapter", ("干员养成", "武器养成", "危境再现", "危境预演", "能量淤积点"), note="玩法选择已保存；参数桥完成前沿用工具当前配置。"),
            DailyParameterSpec("reward_tier", "奖励档位", "enum", "requires_adapter", ("保持当前", "低阶", "高阶"), note="仅对支持档位的理智本生效。"),
            DailyParameterSpec("stamina_rotation", "理智本轮换", "notice", "requires_adapter", note="药剂、商店、交易、制造与演算不进入普通每日。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="GF2",
        integration_id="ok-gf2.daily-task",
        tool_name="OK-GF2 DailyTask",
        source="OK-GF2 selected-operation structured-stage hook",
        entry_operation="attach-home",
        daily_operations=(
            "attach-home",
            "mail",
            "public-area-dispatch",
            "spend-stamina",
            "squad-tasks",
            "claim-daily-missions",
            "battle-pass-free-track",
        ),
        parameters=(
            DailyParameterSpec("supply_stage", "补给本", "enum", "requires_adapter", ("军备解析", "深度搜索", "决策构象"), note="玩法选择已保存；参数桥完成前沿用工具当前配置。"),
            DailyParameterSpec("daily_stamina_cap", "每日自然体力上限", "integer", "requires_adapter", minimum=10, maximum=60, note="按 10 点步进；不购买体力。"),
            DailyParameterSpec("auto_battle_confirmed", "已确认游戏内自动战斗", "boolean", "requires_adapter", note="运行前置确认，不会替你修改游戏设置。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="NTE",
        integration_id="ok-nte.selected-daily",
        tool_name="OK-NTE selected DailyTask runner",
        source="OK-NTE selected-operation structured-stage hook",
        entry_operation="attach-home",
        daily_operations=(
            "attach-home",
            "mail",
            "daily-activity",
            "spend-city-vitality",
            "claim-activity-reward",
            "claim-cycle-reward",
        ),
        parameters=(
            DailyParameterSpec("anomaly_task_type", "异象界域资源类型", "enum", "active", ("经验与甲硬币", "异能升级材料", "弧盘突破材料", "空幕"), note="由 Manager 按本轮 profile 下发，不改工具永久配置。"),
            DailyParameterSpec("exp_reward_target", "经验与甲硬币子目标", "enum", "active", ("角色经验", "弧盘经验", "甲硬币"), note="仅在经验与甲硬币路线使用。"),
            DailyParameterSpec("material_index", "材料目标序号", "integer", "active", minimum=1, maximum=6, note="异能/弧盘路线为 1..5，空幕为 1..6。"),
            DailyParameterSpec("stamina_target", "自然体力目标", "integer", "active", minimum=40, maximum=360, note="每局固定 40，不使用付费补充。"),
            DailyParameterSpec("auto_cycle_sub_task", "自动轮换子目标", "boolean", "active", note="只在所选资源路线内部轮换。"),
            DailyParameterSpec("coffee_mode", "一咖舍", "enum", "active", ("不执行", "领取/补货", "完整自动化"), note="当前只有不执行可启动工具；另两个值会在启动前进入 review_required。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="PGR",
        integration_id="pgr.mpa-selected-daily",
        tool_name="MPA selected-task daily runner",
        source="MPA temporary selected profile and task-success event bridge",
        entry_operation="attach-home",
        daily_operations=(
            "attach-home",
            "claim-serum",
            "dorm",
            "simulation-field",
            "maintainer-action",
            "claim-daily-tasks",
            "battle-pass-free-track",
        ),
        parameters=(
            DailyParameterSpec("simulation_field", "拟战场域", "notice", "requires_adapter", note="runner 只启用所选任务；临期药剂与商店仍保持关闭。"),
            DailyParameterSpec("expiring_serum", "消耗临期体力药", "boolean", "requires_adapter", note="当前安全 runner 不启用此项。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="ZZZ",
        integration_id="zzz.one-dragon-selected-apps",
        tool_name="Zenless Zone Zero OneDragon selected-app runner",
        source="OneDragon run_application(app_id) fixed application allowlist",
        entry_operation="attach-home",
        daily_operations=(
            "attach-home",
            "coffee",
            "scratch-card",
            "trigrams-collection",
            "suibian-temple",
            "random-play",
            "charge-plan",
            "city-fund-free-claim",
            "engagement-reward",
        ),
        parameters=(
            DailyParameterSpec("charge_plan", "体力计划（关卡、次数、队伍）", "notice", "requires_adapter", note="按现有 OneDragon 体力计划运行；驱动盘拆解与其它组应用不在 allowlist。"),
            DailyParameterSpec("coffee_mode", "咖啡策略", "enum", "requires_adapter", ("不挑战", "只跑已选体力计划"), note="只运行已勾选的 coffee 与 charge-plan 应用。"),
            DailyParameterSpec("city_fund", "丽都城募免费领取", "notice", "active", note="只运行成长任务与等级回馈的全部领取；购买等级、计划升级、兑换商店和城募赠礼均不在交互白名单。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="NIKKE",
        integration_id="nikke.selected-behavior-tree",
        tool_name="NIKKE selected-subtree daily runner",
        source="NIKKE fixed behavior subtrees with authoritative success marker",
        entry_operation="attach-lobby",
        daily_operations=(
            "attach-lobby",
            "outpost",
            "dispatch-friend",
        ),
        parameters=(
            DailyParameterSpec("daily_completion_mode", "每日进度模式", "enum", "requires_adapter", ("仅检查", "尝试已选补足项"), note="当前只晋级三个独立低风险子树。"),
            DailyParameterSpec("recovery_tasks", "补足项目", "enum", "requires_adapter", ("模拟室", "拦截战", "爬塔"), note="这些战斗分支尚未进入安全 runner。"),
        ),
    ),
    GameIntegrationRegistration(
        game_id="FGO",
        integration_id="fgo.maafgo-formal-daily",
        tool_name="MaaFgo formal GUI daily runner",
        source="MaaFgo WebGUI selected task API and strict task log",
        entry_operation="attach-home",
        daily_operations=("attach-home", "three-10ap-quests"),
    ),
    GameIntegrationRegistration(
        game_id="CZN",
        integration_id="czn.maa-kes-selected-daily",
        tool_name="Maa_KES formal GUI selected daily runner",
        source="MFAAvalonia formal GUI plus fixed MaaFramework entries",
        entry_operation="login-bonus",
        daily_operations=(
            "login-bonus",
            "achievement-schedule",
            "arkhianon-supply",
            "simulation-stamina",
        ),
    ),
    GameIntegrationRegistration(
        game_id="BD2",
        integration_id="bd2.mfabd2-selected-daily",
        tool_name="MFABD2 formal GUI selected daily runner",
        source="MFAAvalonia formal GUI plus fixed MaaFramework entries",
        entry_operation="attach-home",
        daily_operations=("attach-home", "daily-claim", "stamina-sweep"),
    ),
)


_CANDIDATES: tuple[GameIntegrationCandidate, ...] = (
    GameIntegrationCandidate(
        game_id="ZZZ",
        tool_name="Zenless Zone Zero OneDragon",
        source="installed OneDragon group configuration",
        mapping_note="上游有按 app_id 运行独立应用的本地后端，但 Manager 尚未拥有受限服务生命周期、fencing、阶段事件或证据桥；整组一条龙会串跑所有已启用应用，不能作为 Todo 入口。",
        parameters=(
            DailyParameterSpec("charge_plan", "体力计划（关卡、次数、队伍）", "notice", "requires_adapter", note="必须先拆成单计划阶段；储备电量、以太电池、双倍与循环都会作为独立玩法参数接入。"),
            DailyParameterSpec("coffee_mode", "咖啡策略", "enum", "requires_adapter", ("不挑战", "只跑已选体力计划"), note="不能触发整组一条龙。"),
            DailyParameterSpec("city_fund", "丽都城募免费领取", "notice", "adapter_bound", note="只运行成长任务与等级回馈的全部领取；购买等级、计划升级、兑换商店和城募赠礼均不在交互白名单。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="Endfield",
        tool_name="OK-EF DailyTask",
        source="installed OK-EF DailyTask configuration",
        mapping_note="已发现配置驱动的整条 DailyTask；尚未接收已选步骤或回传逐阶段结构化事件。",
        parameters=(
            DailyParameterSpec("stamina_stage", "普通理智本", "enum", "requires_adapter", ("干员养成", "武器养成", "危境再现", "危境预演", "能量淤积点"), note="需先完成只跑理智与日常领奖的阶段隔离。"),
            DailyParameterSpec("reward_tier", "奖励档位", "enum", "requires_adapter", ("保持当前", "低阶", "高阶"), note="仅对支持档位的理智本生效。"),
            DailyParameterSpec("stamina_rotation", "理智本轮换", "notice", "requires_adapter", note="以起始日期和已选队列决定；药剂、商店、交易、制造与演算会拆成独立玩法参数。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="GF2",
        tool_name="OK-GF2 DailyTask",
        source="installed OK-GF2 DailyTask configuration",
        mapping_note="已发现配置驱动的整条 DailyTask；活动、商店和竞技场边界尚未拆分，不能按 Todo 安全映射。",
        parameters=(
            DailyParameterSpec("supply_stage", "补给本", "enum", "requires_adapter", ("军备解析", "深度搜索", "决策构象"), note="定向精研需要独立处理自律次数，接入时会作为单独玩法选项。"),
            DailyParameterSpec("daily_stamina_cap", "每日自然体力上限", "integer", "requires_adapter", minimum=10, maximum=60, note="按 10 点步进；不清空体力，不购买。"),
            DailyParameterSpec("auto_battle_confirmed", "已确认游戏内自动战斗", "boolean", "requires_adapter", note="这是运行前置确认，不会替你改游戏设置。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="NTE",
        tool_name="OK-NTE DailyTask",
        source="installed OK-NTE DailyTask source",
        mapping_note="已发现 DailyTask；当前会默认执行多个步骤，且体力与活跃任务共用阶段，尚未满足选择隔离。",
        parameters=(
            DailyParameterSpec("anomaly_task_type", "异象界域资源类型", "enum", "requires_adapter", ("经验与甲硬币", "异能升级材料", "弧盘突破材料", "空幕"), note="需先把体力与活跃任务拆为可选阶段。"),
            DailyParameterSpec("sub_target", "资源子目标", "enum", "requires_adapter", ("角色经验", "弧盘经验", "甲硬币", "材料序号"), note="随资源类型联动。"),
            DailyParameterSpec("stamina_target", "自然体力目标", "integer", "requires_adapter", minimum=40, note="每局固定 40；面板会按缺口向上取整显示预计实际消耗。"),
            DailyParameterSpec("coffee_shop", "一咖舍", "enum", "requires_adapter", ("不执行", "领取/补货", "完整自动化"), note="这是可配置玩法；若出现真实购买，执行中的确认门会停在该按钮前。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="PGR",
        tool_name="Maa PGR Assistant",
        source="installed MPA task definitions",
        mapping_note="已发现独立任务定义和本机保存配置，但没有可受 Manager 限制的非 GUI 启动入口、逐任务事件或证据桥。上游任务定义默认包含结束游戏，不能直接复用为保留客户端的每日 binding。",
        parameters=(
            DailyParameterSpec("simulation_field", "拟战场域", "notice", "requires_adapter", note="需先实现专属阶段与普通血清次数；临期药剂使用固定关闭。"),
            DailyParameterSpec("expiring_serum", "消耗临期体力药", "boolean", "requires_adapter", note="可按玩法配置；实际使用前仍须在同轮界面确认。"),
            DailyParameterSpec("shop_purchase", "商店购买", "notice", "requires_adapter", note="可作为玩法策略配置；需要先拆分商品、价格与确认事件。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="NIKKE",
        tool_name="NIKKE behavior tree",
        source="installed NIKKE daily behavior tree",
        mapping_note="已发现每日行为树；多个战斗与领奖动作仍揉在复合节点中，尚不能伪装为独立 Todo。",
        parameters=(
            DailyParameterSpec("daily_completion_mode", "每日进度模式", "enum", "requires_adapter", ("仅检查", "尝试已选补足项"), note="必须回传 100/100 OCR 和新截图后才可领奖。"),
            DailyParameterSpec("recovery_tasks", "补足项目", "enum", "requires_adapter", ("模拟室", "拦截战", "爬塔"), note="仅选择的项目可执行；不可用树的退出或标记作为完成证据。"),
            DailyParameterSpec("quick_battle", "快速歼灭", "notice", "requires_adapter", note="可作为玩法策略接入；需要先回传次数、消耗和同轮确认事件。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="FGO",
        tool_name="FGO BBchannel/FGA daily workflow",
        source="legacy FGO daily evidence workflow",
        mapping_note="已发现整体每日链；现有工具只声明整体验收，未提供可选择的 typed Todo 事件。",
        parameters=(
            DailyParameterSpec("daily_quest", "固定 10 AP 日常", "notice", "requires_adapter", note="未来仅允许 10 AP × 3、无苹果、既有队伍；必须先接入整体证据链。"),
            DailyParameterSpec("apple_refill", "补充行动力", "notice", "requires_adapter", note="可作为玩法策略接入；须区分苹果与圣晶石，并在消耗前确认。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="BD2",
        tool_name="BD2 daily behavior tree",
        source="installed BD2 daily behavior tree",
        mapping_note="已发现复合行为树；其中包含登录、活动和坐标兜底，且没有逐 Todo 选择或结构化事件。",
        parameters=(
            DailyParameterSpec("emulator_readiness", "模拟器准备状态", "notice", "requires_adapter", note="先完成真实阶段与证据映射后，可将模拟器参数接入每日配置。"),
        ),
    ),
    GameIntegrationCandidate(
        game_id="CZN",
        tool_name="CZN daily checklist",
        source="CZN read-only checklist and verified-action helpers",
        mapping_note="目前只有候选清单与坐标辅助；国服每日工具、原生完成证据与 Manager Adapter 均未接入。",
        parameters=(
            DailyParameterSpec("emulator_readiness", "模拟器准备状态", "notice", "requires_adapter", note="先确定国服客户端与每日页映射后，再把玩法参数接入执行。"),
        ),
    ),
)


def registration_for(game_id: str) -> GameIntegrationRegistration | None:
    return next((item for item in _REGISTRATIONS if item.game_id == game_id), None)


def candidate_for(game_id: str) -> GameIntegrationCandidate | None:
    return next((item for item in _CANDIDATES if item.game_id == game_id), None)


def integration_game_ids() -> tuple[str, ...]:
    """Return every catalogued integration game in stable registration order."""

    return tuple(
        dict.fromkeys(item.game_id for item in (*_REGISTRATIONS, *_CANDIDATES))
    )
