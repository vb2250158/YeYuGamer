"""Static contract checks for the three fixed OpenKuro daily integrations.

This deliberately does not start a game or a daily tool.  It verifies that the
Adapter passes a run-scoped selection/event bridge and that each patched
upstream DailyTask consumes only its supported Manager operations.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "adapter-host" / "openkuro-runner" / "Program.cs"
WW_DAILY = Path(r"C:\Game\ok-ww\data\apps\ok-ww\working\src\task\DailyTask.py")
ENDFIELD_DAILY = Path(r"C:\Game\ok-ef\data\apps\ok-ef\working\src\tasks\onetime\DailyTask.py")
ENDFIELD_BRIDGE = ROOT / "adapter-host" / "openkuro-runner" / "EndfieldYeYuBridge.py"
GF2_DAILY = Path(r"C:\Game\ok-gf2\working\src\tasks\DailyTask.py")


def source(path: Path) -> str:
    value = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        ast.parse(value, filename=str(path))
    return value


def require_operations(text: str, operations: set[str], path: Path) -> None:
    for operation in operations:
        assert operation in text, f"{path}: missing mapping for {operation}"


def main() -> None:
    runner = source(RUNNER)
    assert 'start.EnvironmentVariables["YEYU_GAMER_STAGE_FILE"]' in runner
    assert 'start.EnvironmentVariables["YEYU_GAMER_SELECTED_OPERATIONS"]' in runner
    assert '(skipped || completedStage) ? "completed" : "review_required"' in runner
    assert 'failed || (!skipped && !completedStage)' in runner
    assert 'String.IsNullOrWhiteSpace(stageDetail)' in runner
    assert '"reason", safeStageDetail' in runner
    # A launched process/window is transport progress, not proof that the game
    # or its DailyTask bridge became ready.  The runner must wait for scoped
    # stage telemetry and must not fabricate downstream attempts on failure.
    assert 'formal GUI launched; waiting for its DailyTask runtime' in runner
    assert 'if (!attempted.Contains(target.Item1)) return;' in runner
    assert 'if (seenStageLines.Count == 0)' in runner
    assert 'telemetry_missing: fixed DailyTask emitted no run-scoped stage event' in runner
    assert 'WaitForEndfieldClientTransport' not in runner
    assert 'endfieldGameWindow=ready' not in runner
    assert 'DetectFormalBlockingState(binding, process)' in runner
    assert runner.index('var process = Process.Start(start)') < runner.index(
        'DetectFormalBlockingState(binding, formal)'
    )
    assert 'launcher_download_required: formal launcher requires the user to continue the download' in runner
    assert 'if (elements[index].Current.IsOffscreen) continue;' in runner
    assert 'new string[] { binding.Executable, binding.InnerPython }' in runner
    assert 'DetectFormalBlockingWindow(binding, candidate)' in runner
    assert 'client_update_required: the game client requires a foreground update' in runner
    assert 'client_update_required: " + updateError' in runner
    assert 'telemetry_missing:formal_gui_update_check_timeout' in runner
    assert 'telemetry_missing:formal_gui_inner_bind_timeout' in runner
    assert 'StopStaleFormalProcesses(binding)' in runner
    assert 'binding.GameId == "WW" && exitCode == 70 && completed.Count == ids.Length' in runner
    assert 'CaptureWwRewardEvidence(binding, staging, phase, afterClaim' in runner
    assert 'state == "capture_before"' in runner
    assert 'state == "capture_after"' in runner
    assert 'stageFile + ".capture.ack"' in runner
    assert '"game-ui-daily-reward-before"' in runner
    assert '"game-ui-daily-reward-raw"' in runner
    assert '"game-ui-daily-reward-watermarked"' in runner
    assert '"game-ui-daily-activity-100"' in runner
    assert 'daily_activity_points=' in runner
    assert 'wwDailyActivityPoints >= 100' in runner
    assert 'completion_contract_failed' in runner
    assert 'DateTime.UtcNow.AddHours(8).ToString("yyyy-MM-dd HH:mm:ss")' in runner
    assert '"transportOutcome", cancelled ? "cancelled" : (transportClean ? "clean" : "crashed")' in runner
    assert 'Path.Combine(root, "ok-ww.exe")' in runner
    assert 'Path.Combine(root, "ok-ef.exe")' in runner
    assert 'String.Empty, Path.Combine(working, "configs", "DailyTask.json")' in runner
    assert '"-t 1 -e"' in runner
    assert 'Path.Combine(working, "configs", "DailyTask.json"), dailyTaskProfile, root)' in runner
    assert 'WaitForFormalUpdateAndStartInner' in runner
    assert 'FormalUpdateReady' in runner
    assert 'YEYU_GAMER_FORMAL_UPDATE_CHECK_SECONDS' in runner
    assert 'YEYU_GAMER_FORMAL_UPDATE_IDLE_SECONDS' in runner
    assert 'YEYU_GAMER_FORMAL_UPDATE_HARD_CAP_SECONDS' in runner
    assert 'formal_gui_update_deadline_extended' in runner
    assert 'SupportsFormalUpdateRetry(binding.GameId)' in runner
    assert '"game-ui-launch-timeout"' in runner
    assert 'CaptureLaunchTimeoutEvidence(binding' in runner
    assert 'UseShellExecute = useShellExecute' in runner
    assert 'binding.GameId == "WW" || binding.GameId == "Endfield" || binding.GameId == "GF2"' in runner
    assert "bool useShellExecute = shellLaunchedFormal" in runner
    assert 'if (!shellLaunchedFormal) DrainToolOutput(process)' in runner
    assert 'WriteFormalRunSidecar(binding, stageFile, selectedOperations)' in runner
    assert 'ClearFormalRunSidecars(binding)' in runner
    assert 'YeYuGamerRun.json' in runner
    assert 'ConfigureToolOutput(start)' in runner
    assert 'DrainToolOutput(process)' in runner
    assert 'FindFormalInner(binding, formalStartedAtUtc)' in runner
    assert 'EnsureWwGuiStreamGuard(binding)' in runner
    assert 'EnsureFormalGuiManagerBridge(binding)' in runner
    assert 'EnsureEndfieldManagerBridge(binding)' in runner
    assert 'EndfieldYeYuBridge.py' in runner
    assert 'YEYU_GAMER_GUI_STREAM_GUARD_V1' in runner
    assert 'YEYU_GAMER_GUI_RUN_BRIDGE_V2' in runner
    assert "_yeyu_sys.argv.extend(['--task', '1'])" in runner
    assert "_yeyu_sys.argv.append('--exit')" in runner
    assert "_yeyu_guard_gui_stream('stdout')" in runner
    assert "_yeyu_guard_gui_stream('stderr')" in runner
    assert 'DateTime.UtcNow.AddSeconds(180)' in runner
    assert 'formalGuiAutoStart=true; innerGuiBound=true' in runner
    assert 'Process.GetProcessesByName(Path.GetFileNameWithoutExtension(binding.InnerPython))' in runner
    assert 'formalGuiUpdate=checked' in runner
    assert 'AttemptNumber(target.Item2)' in runner
    assert 'todo["priorAttempts"]' in runner
    assert 'Path.Combine(appRoot, "python", "pythonw.exe")' in runner
    assert 'EnsureGf2FormalEntry(root, workingDirectory)' in runner
    assert 'Path.Combine(root, "ok-gf2.exe")' in runner
    assert 'PyAppify resolves app.json beside its signed bootstrap EXE' in runner
    assert 'null, root)' in runner
    assert 'formal_gui_entry_restore_failed' in runner
    assert 'formal_gui_profile_state_invalid' in runner
    assert "FileShare.ReadWrite | FileShare.Delete" in runner
    assert 'Path.Combine(toolRoot, "working", "main.py")' in runner
    assert 'configured_gui_entry_missing' in runner
    assert '-m ok.cli run_task' not in runner
    assert "unsafe_daily_config_monthly_card" not in runner

    ww = source(WW_DAILY)
    assert "def yeyu_stage" in ww
    assert "YEYU_GAMER_STAGE_FILE" in ww
    assert "YEYU_GAMER_SELECTED_OPERATIONS" in ww
    assert "def _load_yeyu_run_bridge" in ww
    assert "createdAtUnix" in ww
    assert "YeYuGamerRun.json" in ww
    assert "def yeyu_capture_stage" in ww
    assert "'capture_before'" in ww
    assert "'capture_after'" in ww
    assert "'.capture.ack'" in ww
    assert "manager_requires_nightmare = (" in ww
    assert "manager_requires_nightmare and self._last_nightmare_daily_present" in ww
    assert "and not self._last_nightmare_daily_completed" in ww
    assert "daily activity is {} (<100); refusing to claim or report completion" in ww
    ww_main = source(Path(r"C:\Game\ok-ww\data\apps\ok-ww\working\main.py"))
    assert "YEYU_GAMER_GUI_RUN_BRIDGE_V1" in ww_main
    assert "sys.argv.extend(['--task', '1'])" in ww_main
    assert "sys.argv.append('--exit')" in ww_main
    # The very first real DailyTask action is waiting for the game world.  It
    # must be visible to YeYu Gamer before the upstream one-time setup can
    # block on a connection/login screen.
    assert ww.index("self.yeyu_stage('attach-world', 'started'") < ww.index("WWOneTimeTask.run(self)")
    require_operations(
        ww,
        {
            "attach-world",
            "inspect-daily-progress",
            "farm-nightmare-daily-echo",
            "spend-waveplates",
            "claim-daily-reward",
            "claim-mail",
            "claim-battle-pass",
        },
        WW_DAILY,
    )

    endfield = source(ENDFIELD_DAILY)
    endfield_bridge = source(ENDFIELD_BRIDGE)
    assert "src.tasks.onetime.DailyTask" in endfield_bridge
    assert "协议空间[·・]高阶培养IV" in endfield_bridge
    assert "扩装铳械塔" in endfield_bridge
    assert "current reflection-tube icon changed" in endfield_bridge
    assert "_load_run_bridge" in endfield_bridge
    assert "skip opening a local summary file" in endfield_bridge
    assert 're.compile(r"月卡剩余天数")' in endfield_bridge
    assert "current monthly-card page confirmed by OCR" in endfield_bridge
    assert "PostMessage(\n                    proxy_hwnd" not in endfield_bridge
    require_operations(
        endfield_bridge,
        {
            "attach-world",
            "mail",
            "spend-sanity",
            "delivery-commission",
            "collect-credit",
            "dijiang-harvest",
            "claim-daily-reward",
        },
        ENDFIELD_DAILY,
    )

    gf2 = source(GF2_DAILY)
    assert "build_yeyu_gamer_tasks" in gf2
    assert "Manager run requires a pre-confirmed in-game global auto setting" in gf2
    assert "航线解锁|解锁|升级|购买|充值|补差价|￥|¥" in gf2
    require_operations(
        gf2,
        {
            "attach-home",
            "mail",
            "public-area-dispatch",
            "spend-stamina",
            "squad-tasks",
            "claim-daily-missions",
        },
        GF2_DAILY,
    )
    print("openkuro selected-daily contract: passed")


if __name__ == "__main__":
    main()
