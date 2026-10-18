"""Static, no-game checks for the passive OpenKuro adapter contract."""
from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "adapter-host" / "openkuro-runner" / "Program.cs"
WW_OBSERVER = ROOT / "adapter-host" / "openkuro-runner" / "WwYeYuObserver.py"
WW_BASELINE = ROOT / "vendor-baselines" / "ok-wuthering-waves" / "src" / "task" / "DailyTask.py"
ENDFIELD_BATTLE_BASELINE = ROOT / "vendor-baselines" / "ok-end-field" / "src" / "tasks" / "daily" / "daily_battle_mixin.py"
WW_WORKING_MAIN = Path(r"C:\Game\ok-ww\data\apps\ok-ww\working\main.py")
ENDFIELD_WORKING_MAIN = Path(r"C:\Game\ok-ef\data\apps\ok-ef\working\main.py")
ENDFIELD_YEYU_PATCH = Path(r"C:\Game\ok-ef\data\apps\ok-ef\working\src\patches\yeyu_gamer_bridge.py")


def python_source(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    ast.parse(text, filename=str(path))
    return text


def require(text: str, values: set[str], path: Path) -> None:
    for value in values:
        assert value in text, f"{path}: missing {value!r}"


def main() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    # Both login diagnostics and the run-end whole-round upstream archive must
    # satisfy the existing run-artifact protocol; omission previously caused the
    # Host to discard the execution stream.
    diagnostics = re.findall(r'"run_artifact_staged", sequence\+\+, new Dictionary<string, object> \{(.*?)\n\s*\}\)\);', runner, re.S)
    assert len(diagnostics) == 4
    assert all('"todoInstanceId"' in frame for frame in diagnostics)
    # A run which never started a configured Todo (a multi-account task failing
    # before official account identity, for example) must still archive its raw
    # upstream log.  Otherwise that evidence stays in the staging inbox and the
    # failed round can only be diagnosed from the Manager's own records.
    archive_start = runner.index("// A run which never started one of the configured Todos")
    archive_end = runner.index("// A normal process exit does not erase an official task failure.")
    assert archive_start < archive_end
    archive_block = runner[archive_start:archive_end]
    assert '"run_artifact_staged"' in archive_block
    assert '"kind", "tool-log-upstream"' in archive_block
    assert '"-upstream-*.jsonl"' in archive_block
    assert 'stagedUpstreamLogs' in archive_block
    assert archive_start < runner.index('"run_terminal", sequence++')
    # The official Python entrypoint receives only official CLI arguments;
    # candidate PYTHONPATH supplies observation modules without source writes.
    require(runner, {
        '" --task 1 --exit"',
        '" --task 7 "',
        'YEYU_GAMER_WW_MULTI_OBSERVATION_FILE',
        'YEYU_GAMER_STAGE_FILE',
        'YEYU_GAMER_SELECTED_OPERATIONS',
        'YEYU_GAMER_UPSTREAM_LOG_FILE',
        'AppDomain.CurrentDomain.BaseDirectory + ";" + binding.WorkingDirectory',
        'PYTHONDONTWRITEBYTECODE',
        'upstream_task_succeeded',
        'upstream_observation_missing',
    }, RUNNER)
    require(runner, {
        '"game-ui-recognition-raw"',
        '"game-ui-recognition-replay-raw"',
        '"official-recognition-metadata"',
        'StepArtifactMimeType(kind)',
        'DetailValue(stageDetail, "accountAttribution") == "unconfirmed"',
    }, RUNNER)
    assert 'terminalTodo(operation, "skipped", "upstream DailyTask completed without an observable call for this operation")' not in runner
    assert 'terminalTodo(operation, "review_required", "upstream DailyTask completed without an observable child-task terminal for this operation")' not in runner
    assert 'onStage("official-tool-start", "tool_started"' in runner
    ww = python_source(WW_OBSERVER)
    require(ww, {
        'src.task.DailyTask', 'src.task.BaseWWTask',
        'src.task.NightmareNestTask', 'src.task.TacetTask',
        'src.task.ForgeryTask', 'src.task.SimulationTask',
        'attach-world', 'inspect-daily-progress', 'farm-nightmare-daily-echo',
        'spend-waveplates', 'claim-daily-reward', 'claim-mail',
        'claim-battle-pass', 'upstream-daily-task',
        'from upstream_logging import install as install_logging',
        'from src.task.MultiAccountDailyTask import MultiAccountDailyTask',
        'from OfficialStepCapture import install_recognition_observers, recognition_scope',
        'except Exception:\n        pass',
    }, WW_OBSERVER)
    assert 'AccountBridgeGate' not in runner
    assert 'YEYU_GAMER_ACCOUNT_LABEL' not in runner
    assert 'WwAccountYeYuBridge' not in runner
    assert 'TargetDoneSet' not in runner
    # The baseline, rather than any locally modified installation, is the
    # authority for the configuration/call ordering that the observer follows.
    baseline = python_source(WW_BASELINE)
    require(baseline, {
        "'Farm Nightmare Nest for Daily Echo'", "'Which to Farm'",
        'self.claim_daily()', 'self.claim_mail()', 'self.claim_battle_pass()',
    }, WW_BASELINE)

    endfield_baseline = python_source(ENDFIELD_BATTLE_BASELINE)
    require(endfield_baseline, {
        'CFG_STAGE_REWARD_TIER = "体力本奖励档位"',
        'CFG_STAMINA_START_DATE = "刷体力开始日期"',
        'CFG_PRE_ENTER_TEAM_SWITCH = "指定的队伍编号"',
        '"刷本序列"', '_resolve_stage_from_sequence',
        'if stage_name in self.REWARD_TIER_STAGE_SET',
        'stage_options_with_tiers.append(f"{stage}{self.REWARD_TIER_LOW}")',
        'stage_options_with_tiers.append(f"{stage}{self.REWARD_TIER_HIGH}")',
    }, ENDFIELD_BATTLE_BASELINE)
    # The runner maps the complete Manager profile to these published keys.
    # Its suffix rule mirrors the official parser: only its four tiered stages
    # accept an immediate `低阶` or `高阶` suffix.
    require(runner, {
        'ApplyEndfieldDailyTaskProfile(binding)',
        '"staminaStage", "rewardTier", "staminaRotationStartDate", "staminaRotation", "teamSlot"',
        'config["体力本"] = staminaStage',
        'config["体力本奖励档位"] = rewardTier',
        'config["刷体力开始日期"] = rotationStartDate',
        'config["刷本序列"] = rotation.ToArray()',
        'config["指定的队伍编号"] = teamSlot',
        '"干员经验", "干员进阶", "技能提升", "武器进阶"',
        'value.EndsWith(tier, StringComparison.Ordinal)',
    }, RUNNER)
    # GF2 profile values are official DailyTask keys. Null or omitted members
    # preserve the existing official setting, while false remains a real value.
    require(runner, {
        'ApplyGf2DailyTaskProfile(binding)',
        '"autoBattleConfirmed", "staminaStage", "extraCreditBattle", "autonomousLoop"',
        '"squadDust", "eventStageName", "drinkDurations", "mealDuration"',
        '"autoBattleConfirmed", "已确认启用游戏内全局自动功能"',
        '"staminaStage", "体力本"',
        '"extraCreditBattle", "刷钱本"',
        '"autonomousLoop", "自主循环"',
        '"squadDust", "尘烟"',
        '"eventStageName", "当前物资关卡名称"',
        '"drinkDurations", "喝水"',
        '"mealDuration", "吃饭"',
        'value != null',
        'Gf2StaminaStages',
        'config["情报和战前补给"] = selected.Contains("activity-supply")',
        'config["闪耀星愿"] = selected.Contains("star-wish")',
        'config["活动自律"] = selected.Contains("event-battle")',
        'config["探索领取"] = selected.Contains("exploration-claim")',
        'if (!IsGf2MainSourceBinding(binding)) config["情报补给"] = false',
        'config["活动层"] = false',
        'config["竞技场"] = false',
        'config["购买免费礼包"] = false',
        'config["商店心愿单购买"] = false',
        'gf2_main_source_required',
        'Gf2MainOnlyOperationsSelected(todosByOperation.Keys)',
        'IsGf2MainSourceBinding(binding)',
    }, RUNNER)
    assert runner.index('Gf2MainOnlyOperationsSelected(todosByOperation.Keys)') < runner.index('RunDaily(binding, todosByOperation.Keys.ToArray()')
    assert 'from src.patches.yeyu_gamer_bridge import install_yeyu_gamer_bridge' not in runner
    if WW_WORKING_MAIN.is_file():
        ww_main = WW_WORKING_MAIN.read_text(encoding="utf-8")
        for token in ("yeyu", "YeYu", "YEYU_GAMER"):
            assert token not in ww_main, f"WW working main.py still contains {token!r}"
    if ENDFIELD_WORKING_MAIN.is_file():
        ef_main = ENDFIELD_WORKING_MAIN.read_text(encoding="utf-8")
        for token in ("yeyu", "YeYu", "YEYU_GAMER", "yeyu_gamer_bridge"):
            assert token not in ef_main, f"Endfield working main.py still contains {token!r}"
        assert "install_startup_patches()" in ef_main
        assert "ok.start()" in ef_main
    assert not ENDFIELD_YEYU_PATCH.exists(), "Endfield HUD/input insurance patch must not remain in the official working tree"

    print("openkuro passive observer contract: passed")


if __name__ == "__main__":
    main()
