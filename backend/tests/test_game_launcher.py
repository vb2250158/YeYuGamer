from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import os
import base64
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from yeyu_gamer_manager.services.game_launcher import (
    GameLaunchCancelled,
    GameLaunchError,
    GameLaunchHumanRequired,
    GameLaunchReceipt,
    GameLaunchService,
    _QueueProcessIdentity,
)


class GameLaunchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-game-launch-")
        self.addCleanup(self.temporary.cleanup)
        self.executable = Path(self.temporary.name) / "StarRail.exe"
        self.executable.write_bytes(b"test")
        self.launcher = GameLaunchService()

    def test_reuses_registered_running_game_without_launching(self) -> None:
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher, "_list_running", return_value={222: "StarRail.exe"}
        ), mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            return_value=(222, "StarRail.exe", 1920, 1080),
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen"
        ) as popen:
            receipt = self.launcher.ensure_started("StarRail", str(self.executable))

        self.assertEqual(receipt.state, "already-running")
        self.assertEqual(receipt.baseline_process_ids, (222,))
        self.assertEqual(receipt.ready_window_pid, 222)
        popen.assert_not_called()

    def test_waits_for_registered_game_before_returning(self) -> None:
        process = mock.Mock(pid=1234)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher,
            "_list_running",
            side_effect=[{}, {1234: "StarRail.exe"}],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ) as popen, mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            return_value=(1234, "StarRail.exe", 1920, 1080),
        ), mock.patch.object(
            self.launcher,
            "_handoff_started_game",
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            receipt = self.launcher.ensure_started("StarRail", str(self.executable))

        self.assertEqual(receipt.state, "started")
        self.assertEqual(receipt.process_id, 1234)
        self.assertEqual(receipt.observed_process_name, "StarRail.exe")
        self.assertEqual(receipt.expected_process_names, ("starrail.exe",))
        self.assertEqual(receipt.ready_window_width, 1920)
        self.assertEqual(popen.call_args.args[0], [str(self.executable)])
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_ww_waits_for_real_client_window_not_launcher(self) -> None:
        with mock.patch.object(
            self.launcher,
            "_list_running",
            return_value={10: "Wuthering Waves.exe", 20: "Client-Win64-Shipping.exe"},
        ), mock.patch.object(
            self.launcher,
            "_find_ready_window",
            side_effect=[None, (20, "client-win64-shipping.exe", 2560, 1440), (20, "client-win64-shipping.exe", 2560, 1440)],
        ) as find_ready, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 11.1, 11.1],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            ready = self.launcher._wait_until_ready(
                "WW", {"wuthering waves.exe", "client-win64-shipping.exe"}
            )

        self.assertEqual(ready[0], 20)
        for call in find_ready.call_args_list:
            self.assertEqual(call.args[1], {"client-win64-shipping.exe"})
            self.assertEqual(call.args[2], "WW")

    def test_ww_launch_matches_upstream_dx11_windows_start_semantics(self) -> None:
        executable = Path(self.temporary.name) / "Wuthering Waves.exe"
        executable.write_bytes(b"test")
        process = mock.Mock(pid=1234)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher,
            "_list_running",
            side_effect=[{}, {1234: "Client-Win64-Shipping.exe"}],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ) as popen, mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            return_value=(1234, "Client-Win64-Shipping.exe", 1920, 1080),
        ):
            receipt = self.launcher.ensure_started("WW", str(executable))

        self.assertEqual(receipt.state, "started")
        self.assertEqual(
            popen.call_args.args[0],
            f'start "" /b "{executable}" -dx11 -d3d11 -force-d3d11',
        )
        self.assertIs(popen.call_args.kwargs["shell"], True)
        self.assertIn("env", popen.call_args.kwargs)

    def test_ww_formal_launcher_uses_only_configured_signed_entry_without_dx_flags(self) -> None:
        executable = Path(self.temporary.name) / "launcher.exe"
        executable.write_bytes(b"fixture")
        with mock.patch.object(self.launcher, "_verify_ww_launcher") as verify, mock.patch.object(
            self.launcher, "_list_ww_running", side_effect=[{}, {222: "launcher_main.exe"}],
        ), mock.patch.object(
            self.launcher, "_wait_until_ready", return_value=(333, "client-win64-shipping.exe", 1280, 720),
        ) as wait, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=111),
        ) as popen:
            receipt = self.launcher.ensure_started("WW", str(executable))
        verify.assert_called_once_with(executable.resolve(), cancel_requested=None)
        self.assertEqual(popen.call_args.args[0], [str(executable)])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(wait.call_args.kwargs["ww_launcher"], executable.resolve())
        self.assertEqual(receipt.ready_window_pid, 333)
        self.assertEqual(receipt.ww_launcher_path, str(executable.resolve()))
        self.assertIn("launcher_main.exe", receipt.expected_process_names)

    def test_ww_untrusted_formal_launcher_is_not_started(self) -> None:
        executable = Path(self.temporary.name) / "launcher.exe"
        executable.write_bytes(b"fixture")
        with mock.patch.object(
            GameLaunchService, "_ww_launcher_probe", return_value=subprocess.CompletedProcess([], 2, "untrusted:ww-launcher", ""),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.Popen") as popen:
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher.ensure_started("WW", str(executable))
        self.assertEqual(caught.exception.reason_code, "ww_launcher_identity_unverified")
        popen.assert_not_called()

    def test_ww_scope_rejects_foreign_and_unverifiable_processes(self) -> None:
        root = Path(self.temporary.name)
        launcher = root / "launcher.exe"
        identities = {
            1: launcher,
            2: root / "2.6.5.0" / "launcher_main.exe",
            3: root / "Wuthering Waves Game" / "Client" / "Binaries" / "Win64" / "Client-Win64-Shipping.exe",
            4: root.parent / "foreign" / "launcher.exe",
            5: root / "other-game" / "launcher_main.exe",
            6: None,
        }
        with mock.patch.object(GameLaunchService, "_list_running", return_value={pid: "fixture.exe" for pid in identities}), mock.patch.object(
            GameLaunchService, "_process_executable_path", side_effect=identities.get,
        ):
            self.assertEqual(set(self.launcher._list_ww_running(launcher, set())), {1, 2, 3})

    def test_ww_probe_is_scoped_and_has_no_unverified_input_fallback(self) -> None:
        with mock.patch.object(
            GameLaunchService, "_run_launcher_probe", return_value=subprocess.CompletedProcess([], 0, "invoked:ww-enter-game", ""),
        ) as probe:
            outcome = self.launcher._probe_ww_launcher(
                Path(self.temporary.name) / "launcher.exe", frozenset({17}), allow_invoke=True, cancel_requested=None,
            )
        self.assertEqual(outcome, "invoked:ww-enter-game")
        environment = probe.call_args.kwargs["environment"]
        self.assertEqual(environment["YEYU_WW_LAUNCHER_PIDS"], "17")
        self.assertEqual(environment["YEYU_WW_ALLOW_INVOKE"], "1")
        script = probe.call_args.args[0][-1]
        self.assertIn("$Name -ceq '进入游戏'", script)
        self.assertIn("Test-YeYuWwElementOwner -OwnerPid $button.Current.ProcessId", script)
        self.assertIn("Get-AuthenticodeSignature", script)
        for forbidden in ("SetCursorPos", "mouse_event", "SendKeys", "SendInput", "开始游戏", "启动游戏"):
            self.assertNotIn(forbidden, script)

    def test_ww_readonly_classification_reports_ready_without_invocation(self) -> None:
        with mock.patch.object(GameLaunchService, "_ww_launcher_probe", return_value=subprocess.CompletedProcess([], 3, "ready:ww-enter-game", "")) as probe:
            result = self.launcher._probe_ww_launcher(self.executable, frozenset({17}), allow_invoke=False, cancel_requested=None)
        self.assertEqual(result, "ready:ww-enter-game")
        self.assertFalse(probe.call_args.kwargs["allow_invoke"])
        script = self.launcher._WW_LAUNCHER_UIA_SCRIPT
        self.assertLess(script.index("$buttons.Count -ne 1"), script.index("Write-Output 'ready:ww-enter-game'"))
        self.assertLess(script.index("TryGetCurrentPattern([Windows.Automation.InvokePattern]"), script.index("Write-Output 'ready:ww-enter-game'"))
        self.assertLess(script.index("Write-Output 'ready:ww-enter-game'"), script.index("$pattern.Invoke()"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell rule fixtures")
    def test_ww_uia_rules_distinguish_toolbar_news_primary_update_and_modal(self) -> None:
        primary = "launcher-button launcher-button-normal status-btn"
        cases = [
            ({"Name": "修复游戏", "Kind": "ControlType.Text"}, "ignore"),
            ({"Name": "更新失败处理与登录失效说明", "Kind": "ControlType.Hyperlink"}, "ignore"),
            ({"Name": "正在更新的版本资讯", "Kind": "ControlType.Text"}, "ignore"),
            ({"Name": "进入游戏", "Kind": "ControlType.Button", "ClassName": primary}, "entry"),
            ({"Name": "更新游戏", "Kind": "ControlType.Button", "ClassName": primary}, "human:update-or-error"),
            ({"Name": "修复游戏", "Kind": "ControlType.Button", "ClassName": primary}, "human:update-or-error"),
            ({"Name": "正在下载", "Kind": "ControlType.Button", "ClassName": primary}, "human:update-or-error"),
            ({"Name": "确认", "Kind": "ControlType.Button"}, "human:modal-dialog"),
            ({"Name": "修复确认", "Kind": "ControlType.Pane", "IsModal": True}, "human:modal-dialog"),
            ({"Name": "通知", "Kind": "ControlType.Pane", "LocalizedKind": "对话框"}, "human:modal-dialog"),
            ({"Name": "密码", "Kind": "ControlType.Edit", "IsPassword": True}, "human:login-or-consent"),
            ({"Name": "同意并继续", "Kind": "ControlType.Button"}, "human:login-or-consent"),
            ({"Name": "进入游戏", "Kind": "ControlType.Button", "ClassName": "unrelated"}, "human:unrecognized-primary-action"),
        ]
        script = self.launcher._WW_LAUNCHER_UIA_RULES_SCRIPT + r'''
$cases = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:YEYU_WW_RULE_CASES)) | ConvertFrom-Json
$results = @(foreach ($case in $cases) {
    Get-YeYuWwElementDisposition -Name $case.Name -Kind $case.Kind -ClassName $case.ClassName `
        -LocalizedKind $case.LocalizedKind -IsPassword ([bool]$case.IsPassword) -IsModal ([bool]$case.IsModal)
})
ConvertTo-Json -InputObject $results -Compress
'''
        environment = self.launcher._external_process_environment()
        environment["YEYU_WW_RULE_CASES"] = base64.b64encode(json.dumps([item[0] for item in cases], ensure_ascii=False).encode("utf-8")).decode("ascii")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = self.launcher._run_launcher_probe([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script], environment=environment, cancel_requested=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [item[1] for item in cases])

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell metadata fixtures")
    def test_ww_webview_identity_rejects_wrong_parent_path_and_signer(self) -> None:
        good = {"LauncherImage": r"C:\Game\Wuthering Waves\2.6.5.0\launcher_main.exe", "WindowPid": 40,
                "ParentPid": 40, "Image": r"C:\Game\Wuthering Waves\2.6.5.0\KRWebViewRuntime\msedgewebview2.exe",
                "SignatureStatus": "Valid", "Signer": "Microsoft Corporation"}
        cases = [good, {**good, "ParentPid": 99}, {**good, "Image": r"C:\foreign\msedgewebview2.exe"},
                 {**good, "Image": r"C:\Game\Wuthering Waves\2.6.4.0\KRWebViewRuntime\msedgewebview2.exe"},
                 {**good, "Signer": "Untrusted Corporation"}, {**good, "SignatureStatus": "NotSigned"}]
        script = self.launcher._WW_LAUNCHER_UIA_RULES_SCRIPT + r'''
$cases = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:YEYU_WW_RULE_CASES)) | ConvertFrom-Json
$results = @(foreach ($case in $cases) {
    Test-YeYuWwWebViewMetadata -LauncherImage $case.LauncherImage -WindowPid $case.WindowPid -ParentPid $case.ParentPid `
        -Image $case.Image -SignatureStatus $case.SignatureStatus -Signer $case.Signer
})
ConvertTo-Json -InputObject $results -Compress
'''
        environment = self.launcher._external_process_environment()
        environment["YEYU_WW_RULE_CASES"] = base64.b64encode(json.dumps(cases).encode("utf-8")).decode("ascii")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = self.launcher._run_launcher_probe([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script], environment=environment, cancel_requested=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False, False, False, False, False])

    def test_ww_probe_rejects_unexpected_or_duplicate_dispatch_result(self) -> None:
        for output, allow in (("invoked:unknown", True), ("invoked:ww-enter-game", False)):
            with self.subTest(output=output, allow=allow), mock.patch.object(
                GameLaunchService, "_ww_launcher_probe", return_value=subprocess.CompletedProcess([], 0, output, ""),
            ):
                self.assertEqual(self.launcher._probe_ww_launcher(
                    Path(self.temporary.name) / "launcher.exe", frozenset({17}), allow_invoke=allow, cancel_requested=None,
                ), "human:invalid-probe-result")

    def test_ww_human_launch_gate_preserves_scene_and_emits_scoped_observation(self) -> None:
        executable = Path(self.temporary.name) / "launcher.exe"
        executable.write_bytes(b"fixture")
        observations = []
        error = GameLaunchHumanRequired("ww_launcher_login_or_consent", "needs login", process_ids=frozenset({222}))
        with mock.patch.object(self.launcher, "_verify_ww_launcher"), mock.patch.object(
            self.launcher, "_list_ww_running", side_effect=[{}, {222: "launcher_main.exe"}],
        ), mock.patch.object(self.launcher, "_wait_until_ready", side_effect=error), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=111),
        ), mock.patch.object(self.launcher, "close_started") as close:
            with self.assertRaises(GameLaunchHumanRequired):
                self.launcher.ensure_started("WW", str(executable), observations.append)
        close.assert_not_called()
        self.assertEqual([item.phase for item in observations], ["launch-human-required"])
        self.assertEqual(observations[0].process_ids, frozenset({222}))

    def test_ww_formal_launcher_gates_login_update_and_unknown_ui(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        for outcome, reason in (
            ("human:login-or-consent", "ww_launcher_login_or_consent"),
            ("human:update-or-error", "ww_launcher_update_or_error"),
            ("waiting:unrecognized-launcher-ui", "ww_launcher_ui_unknown"),
        ):
            clock = iter(range(1000))
            with self.subTest(outcome=outcome), mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe"}), mock.patch.object(
                self.launcher, "_find_ready_window", return_value=None,
            ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value=outcome), mock.patch.object(
                self.launcher, "WW_LAUNCHER_UI_READY_SECONDS", 5,
            ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
                "yeyu_gamer_manager.services.game_launcher.time.sleep",
            ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
                with self.assertRaises(GameLaunchHumanRequired) as caught:
                    self.launcher._wait_until_ready("WW", {"client-win64-shipping.exe"}, launcher.parent, ww_launcher=launcher)
            self.assertEqual(caught.exception.reason_code, reason)
            self.assertEqual(caught.exception.process_ids, frozenset({22}))

    def test_ww_formal_launcher_dispatches_once_and_never_counts_launcher_as_ready(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        clock = iter(range(1000))
        def probe(*args, **kwargs):
            return "invoked:ww-enter-game" if kwargs["allow_invoke"] else "waiting:game-window"
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ) as find, mock.patch.object(self.launcher, "_probe_ww_launcher", side_effect=probe) as action, mock.patch.object(
            self.launcher, "READY_TIMEOUT_SECONDS", 20,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._wait_until_ready("WW", {"launcher_main.exe", "client-win64-shipping.exe"}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(caught.exception.reason_code, "ww_launcher_game_window_missing")
        self.assertTrue(caught.exception.detail["enterGameInvoked"])
        self.assertEqual(sum(call.kwargs["allow_invoke"] for call in action.call_args_list), 1)
        self.assertTrue(all(call.args[1] == {"client-win64-shipping.exe"} for call in find.call_args_list))

    def test_ww_formal_launcher_active_update_preserves_scene_without_action(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_updater.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ), mock.patch.object(self.launcher, "_probe_ww_launcher") as action, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=True,
        ):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._wait_until_ready("WW", {"launcher_updater.exe"}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(caught.exception.reason_code, "ww_launcher_update_observed")
        action.assert_not_called()

    def test_ww_formal_launcher_returns_only_after_shipping_window_is_stable(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        clock = iter(range(1000))
        frames = iter([None])
        ready = (33, "client-win64-shipping.exe", 1280, 720)
        processes = iter([{22: "launcher_main.exe"}])
        with mock.patch.object(self.launcher, "_list_ww_running", side_effect=lambda *args: next(processes, {22: "launcher_main.exe", 33: ready[1]})), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="invoked:ww-enter-game") as action, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock)),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False,
        ):
            result = self.launcher._wait_until_ready("WW", {"launcher_main.exe", ready[1]}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(result, ready)
        self.assertEqual(action.call_count, 1)
        self.assertTrue(action.call_args.kwargs["allow_invoke"])
        self.assertGreaterEqual(next(clock), 14)

    def test_ww_formal_launcher_cleanup_keeps_installation_scope(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        receipt = GameLaunchReceipt("started", 11, "client-win64-shipping.exe", (), ("launcher.exe", "client-win64-shipping.exe"), ww_launcher_path=str(launcher))
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={33: "client-win64-shipping.exe"}), mock.patch.object(
            self.launcher, "_list_zombies", return_value={},
        ), mock.patch.object(self.launcher, "_list_enumerated", return_value={}), mock.patch.object(
            self.launcher, "_request_graceful_close") as close, mock.patch.object(
            self.launcher, "_wait_for_owned_exit", return_value=set(),
        ) as wait, mock.patch.object(self.launcher, "_list_running") as unscoped:
            result = self.launcher.close_started(receipt)
        self.assertEqual(result.state, "closed")
        close.assert_called_once_with({33})
        self.assertEqual(wait.call_args.kwargs["ww_launcher"], launcher)
        unscoped.assert_not_called()

    def test_ww_existing_windowless_client_cannot_trigger_another_launch(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        clock = iter(range(1000))
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe", 33: "Client-Win64-Shipping.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="waiting:game-window") as action, mock.patch.object(
            self.launcher, "WW_LAUNCHER_UI_READY_SECONDS", 5,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._wait_until_ready("WW", {"launcher_main.exe", "client-win64-shipping.exe"}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(caught.exception.reason_code, "ww_launcher_existing_client_unready")
        self.assertTrue(all(not call.kwargs["allow_invoke"] for call in action.call_args_list))

    def test_external_environment_removes_packaged_python_state(self) -> None:
        bundle = Path(self.temporary.name) / "desktop-host"
        internal = bundle / "_internal"
        other = Path(self.temporary.name) / "safe-bin"
        with mock.patch.object(sys, "executable", str(bundle / "YeYuGamer.exe")), mock.patch.object(
            sys, "_MEIPASS", str(internal), create=True
        ), mock.patch.dict(
            os.environ,
            {
                "PATH": os.pathsep.join((str(internal), str(bundle), str(other))),
                "PYTHONHOME": str(internal),
                "PYTHONPATH": "attacker-path",
                "_PYI_APPLICATION_HOME_DIR": str(internal),
                "YEYU_TEST_KEEP": "kept",
            },
            clear=True,
        ):
            environment = self.launcher._external_process_environment()

        self.assertEqual(environment["PATH"], str(other))
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", environment)
        self.assertEqual(environment["YEYU_TEST_KEEP"], "kept")

    def test_nte_waits_for_real_client_and_drives_only_fixed_launcher(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        with mock.patch.object(
            self.launcher,
            "_list_running",
            return_value={10: "NTEGame.exe", 20: "HTGame.exe"},
        ), mock.patch.object(
            self.launcher,
            "_find_ready_window",
            side_effect=[None, (20, "htgame.exe", 1920, 1080), (20, "htgame.exe", 1920, 1080)],
        ) as find_ready, mock.patch.object(
            self.launcher, "_drive_nte_launcher", return_value="acted"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 3.1, 3.1],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            ready = self.launcher._wait_until_ready(
                "NTE", {"ntegame.exe", "htgame.exe"}, launcher_root
            )

        self.assertEqual(ready[0], 20)
        drive.assert_called()
        for call in find_ready.call_args_list:
            self.assertEqual(
                call.args[1],
                {"htgame.exe", "nte.exe", "neverness to everness.exe"},
            )
            self.assertEqual(call.args[2], "NTE")
        self.assertIn("InvokePattern", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("AllowSetForegroundWindow", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("AttachThreadInput", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("SetForegroundWindow", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("mouse_event", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("keybd_event(0x20", self.launcher._NTE_UIA_SCRIPT)
        self.assertNotIn("LegacyIAccessiblePattern", self.launcher._NTE_UIA_SCRIPT)
        self.assertNotIn("1..2 | ForEach-Object", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("no-effect:", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("WindowFromPoint", self.launcher._NTE_UIA_SCRIPT)
        self.assertIn("blocked-by-foreign-window:", self.launcher._NTE_UIA_SCRIPT)

    def test_nte_fails_after_three_audited_actions_have_no_effect(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "NTEGame.exe"}
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_drive_nte_launcher", return_value="no-effect"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 4.0, 4.0, 8.0, 8.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            with self.assertRaisesRegex(GameLaunchError, "had no visible effect"):
                self.launcher._wait_until_ready(
                    "NTE", {"ntegame.exe", "htgame.exe"}, launcher_root
                )

        self.assertEqual(drive.call_count, 3)

    def test_nte_launcher_still_loading_is_not_counted_as_no_effect(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        # Six "not-ready" probes (launcher still rendering) must not trip the
        # no-effect breaker; the client then appears and the gate opens.
        with mock.patch.object(
            self.launcher,
            "_list_running",
            return_value={10: "NTEGame.exe", 20: "HTGame.exe"},
        ), mock.patch.object(
            self.launcher,
            "_find_ready_window",
            side_effect=[None] * 6 + [(20, "htgame.exe", 1920, 1080)] * 2,
        ), mock.patch.object(
            self.launcher, "_drive_nte_launcher", return_value="not-ready"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0] + [float(3 * index) for index in range(1, 7) for _ in (0, 1)] + [21.0, 21.0, 40.0, 40.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            ready = self.launcher._wait_until_ready(
                "NTE", {"ntegame.exe", "htgame.exe"}, launcher_root
            )

        self.assertEqual(ready[0], 20)
        self.assertGreaterEqual(drive.call_count, 4)

    def test_nte_blank_launcher_fails_with_typed_message_after_ui_bound(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        bound = self.launcher.LAUNCHER_UI_READY_TIMEOUT_SECONDS
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "NTEGame.exe"}
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_drive_nte_launcher", return_value="not-ready"
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, bound + 1.0, bound + 1.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            with self.assertRaisesRegex(GameLaunchError, "never exposed its audited start action"):
                self.launcher._wait_until_ready(
                    "NTE", {"ntegame.exe", "htgame.exe"}, launcher_root
                )

    def test_nte_foreign_foreground_window_fails_immediately(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "NTEGame.exe"}
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_drive_nte_launcher", return_value="foreground-interference"
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            with self.assertRaisesRegex(GameLaunchError, "external-hotkey-or-foreground-interference"):
                self.launcher._wait_until_ready(
                    "NTE", {"ntegame.exe", "htgame.exe"}, launcher_root
                )

    def test_launcher_download_activity_suspends_no_effect_strikes(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        # While the launcher writes the update to disk the audited button is
        # hidden; the gate must wait instead of counting no-effect strikes.
        # Samples: 0s (baseline), 15s (+64MB -> updating), 30s (+64MB), 45s
        # (no growth -> idle again), then the client window appears.
        writes = iter([0, 64 << 20, 128 << 20, 128 << 20, 128 << 20])
        clock = iter(
            [0.0, 0.0]
            + [t for t in (0.0, 15.0, 30.0, 45.0, 60.0, 63.0, 66.0) for _ in (0, 1)]
        )
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "NTEGame.exe"}
        ), mock.patch.object(
            self.launcher,
            "_find_ready_window",
            side_effect=[None] * 5 + [(10, "htgame.exe", 1920, 1080)] * 2,
        ), mock.patch.object(
            self.launcher, "_drive_nte_launcher", return_value="no-effect"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._process_bytes_written",
            side_effect=lambda pid: next(writes),
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=clock,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            ready = self.launcher._wait_until_ready(
                "NTE", {"ntegame.exe", "htgame.exe"}, launcher_root
            )
        self.assertEqual(ready[0], 10)
        # Driven at 0s, 45s and 60s; the two "updating" polls skipped it and
        # reset the strike counter, so three no-effect results never accumulate.
        self.assertEqual(drive.call_count, 3)

    def test_launcher_download_wait_stops_on_persisted_cancellation(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        cancellation_checks = iter((False, True))
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "NTEGame.exe"}
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_drive_nte_launcher"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            return_value=True,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 1.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ) as sleep:
            with self.assertRaises(GameLaunchCancelled):
                self.launcher._wait_until_ready(
                    "NTE",
                    {"ntegame.exe", "htgame.exe"},
                    launcher_root,
                    cancel_requested=lambda: next(cancellation_checks),
                )

        drive.assert_not_called()
        sleep.assert_called_once_with(self.launcher.POLL_INTERVAL_SECONDS)

    def test_cancellation_preserves_owned_launcher_and_records_its_own_phase(self) -> None:
        process = mock.Mock(pid=1234)
        observations = []
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher,
            "_list_running",
            side_effect=[{}, {1234: "StarRail.exe"}, {1234: "StarRail.exe"}],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ), mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            side_effect=GameLaunchCancelled("fixture cancellation"),
        ), mock.patch.object(
            self.launcher, "close_started"
        ) as close_started:
            with self.assertRaises(GameLaunchCancelled):
                self.launcher.ensure_started(
                    "StarRail",
                    str(self.executable),
                    observer=observations.append,
                    cancel_requested=lambda: False,
                )

        close_started.assert_not_called()
        self.assertEqual([item.phase for item in observations], ["launch-cancelled"])
        self.assertEqual(
            observations[0].detail["reasonCode"], "manager_cancel_requested"
        )

    def test_cancellation_terminates_only_the_owned_launcher_probe(self) -> None:
        probe = mock.Mock()
        probe.poll.return_value = None
        cancellation_checks = iter((False, True))
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=probe,
        ) as popen, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            with self.assertRaises(GameLaunchCancelled):
                self.launcher._run_launcher_probe(
                    ["powershell.exe", "-Command", "fixture"],
                    environment={},
                    cancel_requested=lambda: next(cancellation_checks),
                )

        popen.assert_called_once()
        probe.terminate.assert_called_once_with()
        probe.wait.assert_called_once_with(timeout=2)
        probe.kill.assert_not_called()

    def test_observer_receives_failure_and_ready_checkpoints(self) -> None:
        observations = []
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher, "_list_running", return_value={222: "StarRail.exe"}
        ), mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            return_value=(222, "StarRail.exe", 1920, 1080),
        ):
            self.launcher.ensure_started(
                "StarRail", str(self.executable), observer=observations.append
            )
        self.assertEqual([item.phase for item in observations], ["ready"])
        self.assertEqual(observations[0].game_id, "StarRail")
        self.assertIn(222, observations[0].process_ids)
        self.assertIn("starrail.exe", observations[0].process_names)

        observations.clear()
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher, "_list_running", return_value={222: "StarRail.exe"}
        ), mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            side_effect=GameLaunchError("blank launcher"),
        ):
            with self.assertRaises(GameLaunchError):
                self.launcher.ensure_started(
                    "StarRail", str(self.executable), observer=observations.append
                )
        self.assertEqual([item.phase for item in observations], ["launch-failed"])
        self.assertEqual(observations[0].detail["error"], "blank launcher")

    def test_launcher_outcome_classification(self) -> None:
        classify = self.launcher._classify_launcher_outcome
        self.assertEqual(classify(0, "invoked:开始游戏:game-started\n"), "acted")
        self.assertEqual(classify(0, "clicked:launcher-primary-action:game-started"), "acted")
        self.assertEqual(classify(4, "no-effect:开始游戏"), "no-effect")
        self.assertEqual(classify(5, "blocked-by-foreign-window:开始游戏"), "foreground-interference")
        self.assertEqual(classify(3, ""), "not-ready")
        self.assertEqual(classify(1, "Add-Type : error"), "not-ready")

    def test_endfield_waits_for_real_client_and_drives_formal_launcher(self) -> None:
        launcher_root = Path(self.temporary.name) / "Hypergryph Launcher"
        launcher_root.mkdir()
        with mock.patch.object(
            self.launcher,
            "_list_running",
            return_value={10: "Games.exe", 20: "Endfield.exe"},
        ), mock.patch.object(
            self.launcher,
            "_find_ready_window",
            side_effect=[None, (20, "endfield.exe", 1920, 1080), (20, "endfield.exe", 1920, 1080)],
        ) as find_ready, mock.patch.object(
            self.launcher, "_drive_endfield_launcher", return_value="acted"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 3.1, 3.1],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            ready = self.launcher._wait_until_ready(
                "Endfield", {"games.exe", "endfield.exe"}, launcher_root
            )

        self.assertEqual(ready[0], 20)
        drive.assert_called_once_with(
            launcher_root, allow_fixed_fallback=False
        )
        for call in find_ready.call_args_list:
            self.assertEqual(
                call.args[1],
                {"endfield.exe", "endfield-win64-shipping.exe"},
            )
            self.assertEqual(call.args[2], "Endfield")
        self.assertIn("--game=Endfield", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("--region=CN", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("InvokePattern", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("no-effect:launcher-primary-action", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("WindowFromPoint", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("blocked-by-foreign-window:", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("keybd_event(0x12", self.launcher._ENDFIELD_UIA_SCRIPT)

    def test_endfield_headless_official_instance_is_activated_once_and_observed_without_input(self) -> None:
        launcher = Path(self.temporary.name) / "Hypergryph Launcher" / "Launcher.exe"
        launcher.parent.mkdir()
        launcher.write_bytes(b"fixture")
        observations = []
        with mock.patch.object(self.launcher, "_resolve_endfield_launcher", return_value=launcher), mock.patch.object(
            self.launcher, "_list_running", side_effect=[{10: "Games.exe"}, {}],
        ), mock.patch.object(self.launcher, "_run_launcher_probe", return_value=subprocess.CompletedProcess([], 0, "trusted:headless", "")), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=11),
        ) as popen, mock.patch.object(self.launcher, "_wait_until_ready", return_value=(20, "endfield.exe", 1920, 1080)) as wait, mock.patch.object(
            self.launcher, "close_started",
        ) as close:
            receipt = self.launcher.ensure_started("Endfield", str(self.executable), observations.append)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(launcher), "--game=endfield", "--reason=4"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(launcher.parent))
        self.assertIsNone(wait.call_args.args[2])
        self.assertEqual(wait.call_args.kwargs["endfield_reactivation"], (self.executable.parent, frozenset({10, 11})))
        self.assertEqual(receipt.baseline_process_ids, (10,))
        self.assertEqual(receipt.state, "started")
        self.assertEqual(receipt.process_id, 11)
        self.assertEqual(receipt.ready_window_pid, 20)
        self.assertEqual(observations[0].detail["action"], "endfield-official-reactivation")
        close.assert_not_called()

    def test_endfield_first_launch_uses_the_same_current_official_cli(self) -> None:
        with mock.patch.object(self.launcher, "_resolve_endfield_launcher", return_value=self.executable), mock.patch.object(
            self.launcher, "_list_running", side_effect=[{}, {20: "Endfield.exe"}],
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=11)) as popen, mock.patch.object(
            self.launcher, "_wait_until_ready", return_value=(20, "endfield.exe", 1920, 1080),
        ), mock.patch.object(self.launcher, "_handoff_started_game"):
            self.launcher.ensure_started("Endfield", str(self.executable))
        self.assertEqual(popen.call_args.args[0], [str(self.executable), "--game=endfield", "--reason=4"])

    def test_endfield_unverified_or_ambiguous_launcher_never_reactivates(self) -> None:
        for baseline, result in [({10: "Games.exe"}, subprocess.CompletedProcess([], 6, "", "")), ({10: "Games.exe", 11: "Games.exe"}, None)]:
            with self.subTest(baseline=baseline), mock.patch.object(self.launcher, "_run_launcher_probe", return_value=result), mock.patch(
                "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            ) as popen, mock.patch.object(self.launcher, "close_started") as close:
                with self.assertRaises(GameLaunchHumanRequired) as caught:
                    self.launcher._reactivate_headless_endfield_launcher(self.executable, baseline, cancel_requested=None)
                self.assertEqual(caught.exception.process_ids, frozenset())
                popen.assert_not_called()
                close.assert_not_called()

    def test_endfield_visible_launcher_or_client_appearing_during_probe_is_not_reactivated(self) -> None:
        for outcome, client in [("trusted:visible", {}), ("trusted:headless", {20: "Endfield.exe"})]:
            with self.subTest(outcome=outcome), mock.patch.object(self.launcher, "_run_launcher_probe", return_value=subprocess.CompletedProcess([], 0, outcome, "")), mock.patch.object(
                self.launcher, "_list_running", return_value=client,
            ), mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.Popen") as popen:
                self.assertIsNone(self.launcher._reactivate_headless_endfield_launcher(self.executable, {10: "Games.exe"}, cancel_requested=None))
                popen.assert_not_called()
        with mock.patch.object(self.launcher, "_run_launcher_probe") as probe:
            self.assertIsNone(self.launcher._reactivate_headless_endfield_launcher(self.executable, {10: "Games.exe", 20: "Endfield.exe"}, cancel_requested=None))
            probe.assert_not_called()

    def test_endfield_reactivation_probe_or_dispatch_error_preserves_scene(self) -> None:
        for probe_error, dispatch_error in [(subprocess.TimeoutExpired("probe", 30), None), (None, OSError("fixture")), (None, GameLaunchError("native DLL scope unavailable"))]:
            with self.subTest(probe_error=probe_error), mock.patch.object(
                self.launcher, "_run_launcher_probe", side_effect=probe_error,
                return_value=subprocess.CompletedProcess([], 0, "trusted:headless", ""),
            ), mock.patch.object(self.launcher, "_list_running", return_value={}), mock.patch(
                "yeyu_gamer_manager.services.game_launcher.subprocess.Popen", side_effect=dispatch_error,
            ) as popen, mock.patch.object(self.launcher, "close_started") as close:
                with self.assertRaises(GameLaunchHumanRequired):
                    self.launcher._reactivate_headless_endfield_launcher(self.executable, {10: "Games.exe"}, cancel_requested=None)
                close.assert_not_called()
                self.assertEqual(popen.call_count, 0 if probe_error else 1)

    def test_launcher_probes_isolate_builtin_modules_without_weakening_policy(self) -> None:
        inherited = {"PSModulePath": "C:/foreign-powershell/Modules", "KEEP": "fixture"}
        command = ["powershell.exe", "-NoProfile", "-Command", "fixture"]
        expected = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/Modules")
        process = mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        process.communicate.return_value = ("trusted:ww-launcher", "")
        for cancelled in (None, lambda: False):
            with self.subTest(cancellation_enabled=cancelled is not None), mock.patch(
                "yeyu_gamer_manager.services.game_launcher.subprocess.run", return_value=subprocess.CompletedProcess(command, 0, "trusted:ww-launcher", ""),
            ) as run, mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=process) as popen:
                self.launcher._run_launcher_probe(command, environment=inherited, cancel_requested=cancelled)
            invoked = run if cancelled is None else popen
            self.assertEqual(invoked.call_args.kwargs["env"], {"PSModulePath": expected, "KEEP": "fixture"})
            self.assertEqual(invoked.call_args.args[0], command)
        self.assertEqual(inherited["PSModulePath"], "C:/foreign-powershell/Modules")

    def test_endfield_cancellation_during_last_query_or_dll_scope_prevents_activation(self) -> None:
        for cancellation_point in ("process-query", "dll-scope"):
            cancelled = False
            def query(_names):
                nonlocal cancelled
                cancelled = cancellation_point == "process-query"
                return {}
            @contextmanager
            def dll_scope():
                nonlocal cancelled
                cancelled = cancelled or cancellation_point == "dll-scope"
                yield
            with self.subTest(cancellation_point=cancellation_point), mock.patch.object(
                self.launcher, "_run_launcher_probe", return_value=subprocess.CompletedProcess([], 0, "trusted:headless", ""),
            ), mock.patch.object(self.launcher, "_list_running", side_effect=query), mock.patch.object(
                self.launcher, "_native_child_dll_scope", side_effect=dll_scope,
            ), mock.patch.object(self.launcher, "_start_endfield_launcher") as start, mock.patch.object(self.launcher, "close_started") as close:
                with self.assertRaises(GameLaunchCancelled):
                    self.launcher._reactivate_headless_endfield_launcher(self.executable, {10: "Games.exe"}, cancel_requested=lambda: cancelled)
                start.assert_not_called()
                close.assert_not_called()

    def test_endfield_reactivation_wait_requires_stable_owned_game_window(self) -> None:
        seen = []
        def find(running, names, game_id):
            seen.append(running)
            return (20, "endfield.exe", 1920, 1080)
        with mock.patch.object(self.launcher, "_list_running", return_value={10: "Games.exe", 20: "Endfield.exe", 99: "Games.exe", 88: "Endfield.exe"}), mock.patch.object(
            self.launcher, "_process_executable_path", side_effect=lambda pid: self.executable.parent / "Endfield.exe" if pid == 20 else Path("C:/foreign/Endfield.exe"),
        ), mock.patch.object(self.launcher, "_find_ready_window", side_effect=find), mock.patch.object(self.launcher, "_drive_endfield_launcher") as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=[0, 0, 0, 0, 11, 11]), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ):
            ready = self.launcher._wait_until_ready("Endfield", {"games.exe", "endfield.exe"}, self.executable.parent,
                endfield_reactivation=(self.executable.parent, frozenset({10, 11})))
        self.assertEqual(ready[0], 20)
        self.assertEqual(seen, [{10: "Games.exe", 20: "Endfield.exe"}] * 2)
        drive.assert_not_called()

    def test_endfield_reactivation_no_effect_or_update_is_human_required_without_input(self) -> None:
        for updating in (False, True):
            with self.subTest(updating=updating), mock.patch.object(self.launcher, "_list_running", return_value={10: "Games.exe", 99: "Games.exe"}), mock.patch.object(
                self.launcher, "_find_ready_window", return_value=None,
            ), mock.patch.object(self.launcher, "_drive_endfield_launcher") as drive, mock.patch.object(self.launcher, "close_started") as close, mock.patch(
                "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=updating,
            ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=[0, 0, 0, 0, 91]), mock.patch(
                "yeyu_gamer_manager.services.game_launcher.time.sleep",
            ):
                with self.assertRaises(GameLaunchHumanRequired) as caught:
                    self.launcher._wait_until_ready("Endfield", {"games.exe", "endfield.exe"}, self.executable.parent,
                        endfield_reactivation=(self.executable.parent, frozenset({10, 11})))
                self.assertEqual(caught.exception.process_ids, frozenset({10}))
                self.assertIn("update_observed" if updating else "game_window_missing", caught.exception.reason_code)
                drive.assert_not_called()
                close.assert_not_called()

    def test_endfield_reactivation_observation_error_is_a_preserved_human_gate(self) -> None:
        observations = []
        with mock.patch.object(self.launcher, "_resolve_endfield_launcher", return_value=self.executable), mock.patch.object(
            self.launcher, "_list_running", return_value={10: "Games.exe"},
        ), mock.patch.object(self.launcher, "_reactivate_headless_endfield_launcher", return_value=11), mock.patch.object(
            self.launcher, "_wait_until_ready", side_effect=GameLaunchError("probe failed"),
        ), mock.patch.object(self.launcher, "close_started") as close:
            with self.assertRaises(GameLaunchHumanRequired):
                self.launcher.ensure_started("Endfield", str(self.executable), observations.append)
        self.assertEqual(observations[-1].phase, "launch-human-required")
        self.assertEqual(observations[-1].process_ids, frozenset({10, 11}))
        close.assert_not_called()

    def test_endfield_reactivation_empty_scope_does_not_widen_waiting_capture(self) -> None:
        observations = []
        with mock.patch.object(self.launcher, "_list_running", return_value={99: "Games.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=[0, 0, 31, 31, 91],
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep"):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._wait_until_ready("Endfield", {"games.exe", "endfield.exe"}, observer=observations.append,
                    endfield_reactivation=(self.executable.parent, frozenset({10, 11})))
        self.assertEqual(caught.exception.process_ids, frozenset())
        self.assertEqual(observations, [])

    def test_endfield_fails_after_three_audited_actions_have_no_effect(self) -> None:
        launcher_root = Path(self.temporary.name) / "Hypergryph Launcher"
        launcher_root.mkdir()
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "Games.exe"}
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_drive_endfield_launcher", return_value="no-effect"
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 6.0, 6.0, 12.0, 12.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            with self.assertRaisesRegex(GameLaunchError, "had no visible effect"):
                self.launcher._wait_until_ready(
                    "Endfield", {"games.exe", "endfield.exe"}, launcher_root
                )

        self.assertEqual(drive.call_count, 3)
        self.assertEqual(
            [call.kwargs["allow_fixed_fallback"] for call in drive.call_args_list],
            [False, True, True],
        )

    def test_endfield_fixed_fallback_opens_after_loaded_launcher_shows_no_button(self) -> None:
        launcher_root = Path(self.temporary.name) / "Hypergryph Launcher"
        launcher_root.mkdir()
        after = self.launcher.ENDFIELD_FIXED_FALLBACK_AFTER_SECONDS
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "Games.exe", 20: "Endfield.exe"}
        ), mock.patch.object(
            self.launcher,
            "_find_ready_window",
            side_effect=[None, None, (20, "endfield.exe", 1920, 1080), (20, "endfield.exe", 1920, 1080)],
        ), mock.patch.object(
            self.launcher, "_drive_endfield_launcher", side_effect=["not-ready", "acted"]
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, after + 1.0, after + 1.0, after + 2.0, after + 2.0, after + 5.0, after + 5.0],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            ready = self.launcher._wait_until_ready(
                "Endfield", {"games.exe", "endfield.exe"}, launcher_root
            )

        self.assertEqual(ready[0], 20)
        self.assertEqual(
            [call.kwargs["allow_fixed_fallback"] for call in drive.call_args_list],
            [False, True],
        )

    def test_endfield_empty_uia_tree_does_not_preempt_authorized_fallback(self) -> None:
        script = self.launcher._ENDFIELD_UIA_SCRIPT
        guarded_empty_tree = (
            "if ($descendants.Count -eq 0 -and "
            "$env:YEYU_ENDFIELD_ALLOW_FALLBACK -ne '1')"
        )
        fixed_fallback = (
            "Invoke-YeYuPhysicalClick -Handle $window -ProcessId $process.Id "
            "-X $x -Y $y -Label 'launcher-primary-action'"
        )

        self.assertIn(guarded_empty_tree, script)
        self.assertNotIn(
            "if ($descendants.Count -eq 0) { Write-Output "
            "'not-ready:web-surface-not-loaded'; exit 3 }",
            script,
        )
        self.assertLess(script.index(guarded_empty_tree), script.index(fixed_fallback))

    def test_endfield_bridge_rejects_non_launcher_root(self) -> None:
        with self.assertRaises(GameLaunchError):
            self.launcher._drive_endfield_launcher(Path(self.temporary.name))

    def test_endfield_retires_only_old_headless_orphan(self) -> None:
        running = {10: "Endfield.exe"}
        with mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_process_age_seconds", return_value=600.0
        ), mock.patch.object(
            self.launcher, "_request_graceful_close"
        ) as close, mock.patch.object(
            self.launcher, "_wait_for_owned_exit", return_value=set()
        ):
            retired = self.launcher._retire_stale_headless_endfield(running)

        self.assertTrue(retired)
        close.assert_called_once_with({10})

    def test_endfield_preserves_young_headless_client(self) -> None:
        running = {10: "Endfield.exe"}
        with mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_process_age_seconds", return_value=30.0
        ), mock.patch.object(self.launcher, "_request_graceful_close") as close:
            retired = self.launcher._retire_stale_headless_endfield(running)

        self.assertFalse(retired)
        close.assert_not_called()

    def test_endfield_preserves_headless_client_while_launcher_is_live(self) -> None:
        running = {10: "Endfield.exe", 20: "Games.exe"}
        with mock.patch.object(self.launcher, "_request_graceful_close") as close:
            retired = self.launcher._retire_stale_headless_endfield(running)

        self.assertFalse(retired)
        close.assert_not_called()

    def test_new_pgr_gets_one_audited_title_handoff_before_adapter(self) -> None:
        process = mock.Mock(pid=1234)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            self.launcher,
            "_list_running",
            side_effect=[{}, {1234: "PGR.exe"}],
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ), mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            return_value=(1234, "PGR.exe", 1920, 1080),
        ), mock.patch.object(
            self.launcher, "_handoff_started_game"
        ) as handoff:
            receipt = self.launcher.ensure_started("PGR", str(self.executable))

        self.assertEqual(receipt.state, "started")
        handoff.assert_called_once_with("PGR", 1234)

    def test_pgr_launch_pins_unity_window_preferences(self) -> None:
        import winreg

        test_key = r"Software\YeYuGamerTests\pgr-prefs-" + uuid.uuid4().hex
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, test_key)
        try:
            winreg.SetValueEx(key, "Screenmanager Fullscreen mode_h3630240806", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "Screenmanager Fullscreen mode Default_h401710285", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "Screenmanager Resolution Width_h182942802", 0, winreg.REG_DWORD, 1024)
            winreg.SetValueEx(key, "Screenmanager Resolution Height_h2627697771", 0, winreg.REG_DWORD, 768)
            winreg.SetValueEx(key, "Screenmanager Resolution Use Native_h1405027254", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "LastResolution_h1893709025", 0, winreg.REG_BINARY, b"2560,1440\x00")
            winreg.CloseKey(key)
            with mock.patch.object(type(self.launcher), "PGR_PLAYER_PREFS_KEY", test_key):
                applied = self.launcher._prepare_pgr_window_preferences()
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_key)
            self.assertEqual(winreg.QueryValueEx(key, "Screenmanager Fullscreen mode_h3630240806")[0], 3)
            # The "Default" twin is a different Unity preference and stays untouched.
            self.assertEqual(winreg.QueryValueEx(key, "Screenmanager Fullscreen mode Default_h401710285")[0], 1)
            self.assertEqual(winreg.QueryValueEx(key, "Screenmanager Resolution Width_h182942802")[0], 1280)
            self.assertEqual(winreg.QueryValueEx(key, "Screenmanager Resolution Height_h2627697771")[0], 720)
            self.assertEqual(winreg.QueryValueEx(key, "Screenmanager Resolution Use Native_h1405027254")[0], 0)
            self.assertEqual(winreg.QueryValueEx(key, "LastResolution_h1893709025")[0], b"1280,720\x00")
            self.assertEqual(len(applied), 5)
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key)

    def test_pgr_launch_without_preferences_key_is_a_noop(self) -> None:
        with mock.patch.object(
            type(self.launcher), "PGR_PLAYER_PREFS_KEY", r"Software\YeYuGamerTests\missing-" + uuid.uuid4().hex
        ):
            self.assertEqual(self.launcher._prepare_pgr_window_preferences(), {})

    def test_nte_bridge_rejects_non_launcher_root(self) -> None:
        with self.assertRaises(GameLaunchError):
            self.launcher._drive_nte_launcher(Path(self.temporary.name))

    def test_rejects_missing_executable_before_launch(self) -> None:
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), self.assertRaises(GameLaunchError):
            self.launcher.ensure_started("StarRail", str(self.executable.parent / "missing.exe"))

    def test_validation_rejects_a_directory_and_non_executable_file(self) -> None:
        non_executable = self.executable.with_suffix(".txt")
        non_executable.write_text("test", encoding="utf-8")
        with self.assertRaises(GameLaunchError):
            self.launcher.validate_configured_executable(str(self.executable.parent))
        with self.assertRaises(GameLaunchError):
            self.launcher.validate_configured_executable(str(non_executable))

    def test_process_listing_returns_only_registered_names(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tasklist"], 0, '"Other.exe","1","Console","1","1 K"\n"StarRail.exe","2","Console","1","1 K"\n', ""
        )
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.run",
            return_value=completed,
        ) as run:
            observed = self.launcher._list_running({"starrail.exe"})

        self.assertEqual(observed, {2: "StarRail.exe"})
        self.assertEqual(
            run.call_args.kwargs["creationflags"],
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_launch_listing_skips_observed_final_exit_codes(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tasklist"], 0,
            '"Client-Win64-Shipping.exe","66280","Console","1","39,880 K"\n'
            '"Client-Win64-Shipping.exe","18568","Console","1","38,000 K"\n', "",
        )
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.run",
            return_value=completed,
        ), mock.patch.object(
            type(self.launcher), "_process_is_live", side_effect=lambda pid: pid == 18568
        ):
            observed = self.launcher._list_running({"client-win64-shipping.exe"})

        self.assertEqual(observed, {18568: "Client-Win64-Shipping.exe"})

    def test_close_preserves_a_preexisting_game(self) -> None:
        receipt = GameLaunchReceipt(
            "already-running", None, "StarRail.exe", (222,), ("starrail.exe",)
        )

        result = self.launcher.close_started(receipt)

        self.assertEqual(result.state, "preserved-preexisting")

    def test_close_uses_graceful_then_bounded_owned_process_fallback(self) -> None:
        receipt = GameLaunchReceipt(
            "started", 1234, "StarRail.exe", (), ("starrail.exe",)
        )
        with mock.patch.object(
            self.launcher, "_list_running", return_value={1234: "StarRail.exe"}
        ), mock.patch.object(
            self.launcher, "_list_zombies", return_value={}
        ), mock.patch.object(
            self.launcher, "_list_enumerated", return_value={}
        ), mock.patch.object(
            self.launcher, "_request_graceful_close"
        ) as graceful, mock.patch.object(
            self.launcher,
            "_wait_for_owned_exit",
            side_effect=[{1234}, {1234}, set()],
        ), mock.patch.object(
            self.launcher, "_terminate_owned"
        ) as terminate:
            result = self.launcher.close_started(receipt)

        self.assertEqual(result.state, "closed")
        self.assertEqual(result.requested_process_ids, (1234,))
        self.assertEqual(result.zombie_process_ids, ())
        # Two WM_CLOSE passes before any termination.
        self.assertEqual(graceful.call_args_list, [mock.call({1234}), mock.call({1234})])
        terminate.assert_called_once_with({1234}, force=False)

    def test_legacy_reap_reports_enumeration_without_diagnosing_cause(self) -> None:
        # The retry is followed by one absent entry and one still-listed entry.
        # Thread/handle counts do not establish why the second entry remains.
        with mock.patch.object(
            GameLaunchService,
            "list_zombies",
            side_effect=[{10: "PGR.exe", 20: "StarRail.exe"}, {20: "StarRail.exe"}],
        ), mock.patch.object(
            GameLaunchService, "_terminate_process_handle", return_value=True
        ) as terminate, mock.patch.object(
            GameLaunchService,
            "_process_diagnostics",
            return_value={"threadCount": 1, "parentPid": 4, "handleCount": 0},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.run"
        ) as run, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            report = self.launcher.reap_zombies(["PGR", "StarRail"], settle_seconds=0)
        self.assertEqual(report["attempted"], {"10": "PGR.exe", "20": "StarRail.exe"})
        self.assertEqual(report["released"], [10])
        self.assertEqual(report["stuck"]["20"]["name"], "StarRail.exe")
        self.assertEqual(report["stuck"]["20"]["threadCount"], 1)
        self.assertEqual(terminate.call_count, 2)
        self.assertIn("/F", run.call_args.args[0])

    def test_reap_zombies_is_a_no_op_without_zombies(self) -> None:
        with mock.patch.object(GameLaunchService, "list_zombies", return_value={}), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.run"
        ) as run:
            report = self.launcher.reap_zombies(["PGR"])
        self.assertEqual(report, {"attempted": {}, "released": [], "stuck": {}})
        run.assert_not_called()

    def test_close_cannot_report_already_closed_for_an_enumerated_residual(self) -> None:
        receipt = GameLaunchReceipt(
            "started", 1234, "StarRail.exe", (), ("starrail.exe",)
        )
        with mock.patch.object(
            self.launcher, "_list_running", return_value={}
        ), mock.patch.object(
            self.launcher, "_list_zombies", return_value={1234: "StarRail.exe"}
        ), mock.patch.object(
            self.launcher, "_list_enumerated", return_value={1234: "StarRail.exe"}
        ):
            result = self.launcher.close_started(receipt)
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (1234,))
        self.assertEqual(result.zombie_process_ids, (1234,))
        self.assertEqual(result.as_result()["zombieProcessIds"], [1234])


class QueueGameCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="yeyu-queue-close-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / "StarRail.exe"
        self.executable.write_bytes(b"fixture")
        self.identity = _QueueProcessIdentity(self.executable, 123456)
        self.launcher = GameLaunchService()
        self.launcher.GRACEFUL_CLOSE_SECONDS = 0
        self.launcher.TERMINATE_CLOSE_SECONDS = 0
        memory = mock.patch.object(self.launcher, "_available_memory", return_value=None)
        memory.start()
        self.addCleanup(memory.stop)

    def test_preexisting_authorized_client_closes_gracefully_with_memory_observations(self) -> None:
        present = ({42: self.identity}, set())
        before = {"availablePhysicalBytes": 100, "availableCommitBytes": 200}
        after = {"availablePhysicalBytes": 150, "availableCommitBytes": 250}
        with mock.patch.object(self.launcher, "_queue_close_snapshot", side_effect=[present, present, ({}, set()), ({}, set())]), mock.patch.object(
            self.launcher, "_close_verified_queue_processes",
        ) as close, mock.patch.object(self.launcher, "_available_memory", side_effect=[before, after]):
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        self.assertEqual(result.state, "closed")
        self.assertEqual(result.requested_process_ids, (42,))
        self.assertEqual(result.remaining_process_ids, ())
        close.assert_called_once_with({42: self.identity}, force=False, cancel_requested=None)
        self.assertEqual(result.as_result()["memoryBefore"], before)
        self.assertEqual(result.as_result()["memoryAfter"], after)
        self.assertNotIn("releasedBytes", result.as_result())

    def test_escalation_is_bounded_and_rechecks_actual_enumeration(self) -> None:
        closed = False
        stages: list[bool] = []
        def close(_targets, *, force, cancel_requested):
            nonlocal closed
            stages.append(force)
            closed = force
        with mock.patch.object(self.launcher, "_queue_close_snapshot", side_effect=lambda *_: ({}, set()) if closed else ({42: self.identity}, set())), mock.patch.object(
            self.launcher, "_close_verified_queue_processes", side_effect=close,
        ):
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        self.assertEqual(stages, [False, False, True])
        self.assertEqual(result.state, "closed")

    def test_successful_termination_request_does_not_hide_remaining_process(self) -> None:
        with mock.patch.object(self.launcher, "_queue_close_snapshot", return_value=({42: self.identity}, set())), mock.patch.object(
            self.launcher, "_close_verified_queue_processes", return_value=True,
        ) as close:
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        self.assertEqual(close.call_count, 3)
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (42,))

    def test_enum_only_unopenable_identity_is_a_blocker_without_actions(self) -> None:
        with mock.patch.object(GameLaunchService, "_list_enumerated", return_value={42: "StarRail.exe"}), mock.patch.object(
            GameLaunchService, "_queue_process_identity", return_value=None,
        ), mock.patch.object(self.launcher, "_close_verified_queue_processes") as close:
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        close.assert_not_called()
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (42,))
        self.assertEqual(result.unverified_process_ids, (42,))
        self.assertEqual(result.requested_process_ids, ())

    def test_no_enumerated_client_is_already_closed(self) -> None:
        with mock.patch.object(self.launcher, "_queue_close_snapshot", return_value=({}, set())), mock.patch.object(
            self.launcher, "_close_verified_queue_processes",
        ) as close:
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        close.assert_not_called()
        self.assertEqual(result.state, "already-closed")

    def test_snapshot_excludes_proven_foreign_path_and_blocks_unverified_path(self) -> None:
        identities = {
            42: self.identity,
            43: _QueueProcessIdentity(self.root.parent / "foreign" / "StarRail.exe", 13),
            44: None,
            45: _QueueProcessIdentity(self.root / "Other.exe", 14),
        }
        with mock.patch.object(GameLaunchService, "_list_enumerated", return_value={pid: "StarRail.exe" for pid in identities}), mock.patch.object(
            GameLaunchService, "_queue_process_identity", side_effect=identities.get,
        ):
            verified, unknown = self.launcher._queue_close_snapshot(self.root, {"starrail.exe"})
        self.assertEqual(verified, {42: self.identity})
        self.assertEqual(unknown, {44, 45})

    def test_reused_pid_cannot_inherit_original_close_permission(self) -> None:
        replacement = _QueueProcessIdentity(self.executable, self.identity.created_at + 1)
        with mock.patch.object(self.launcher, "_queue_close_snapshot", side_effect=[({42: self.identity}, set()), ({42: replacement}, set()), ({42: replacement}, set())]), mock.patch.object(
            self.launcher, "_close_verified_queue_processes",
        ) as close:
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        close.assert_not_called()
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (42,))

    def test_handle_identity_is_rechecked_before_wm_close_or_termination(self) -> None:
        for force in (False, True):
            with self.subTest(force=force):
                api = mock.Mock()
                api.OpenProcess.return_value = 99
                replacement = _QueueProcessIdentity(self.executable, self.identity.created_at + 1)
                with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
                    self.launcher, "_queue_identity_from_handle", return_value=replacement,
                ), mock.patch.object(self.launcher, "_request_graceful_close") as graceful:
                    self.launcher._close_verified_queue_processes({42: self.identity}, force=force, cancel_requested=None)
                graceful.assert_not_called()
                api.TerminateProcess.assert_not_called()
                api.CloseHandle.assert_called_once_with(99)

    def test_force_uses_verified_handle_and_does_not_spawn_taskkill(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 99
        with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
            self.launcher, "_queue_identity_from_handle", return_value=self.identity,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.run") as run:
            self.launcher._close_verified_queue_processes({42: self.identity}, force=True, cancel_requested=None)
        api.TerminateProcess.assert_called_once_with(99, 1)
        api.CloseHandle.assert_called_once_with(99)
        run.assert_not_called()

    def test_cancellation_or_new_human_gate_during_identity_query_prevents_action(self) -> None:
        for exception in (GameLaunchCancelled("cancelled"), GameLaunchHumanRequired("login", "preserve")):
            with self.subTest(kind=type(exception).__name__):
                pending = False
                api = mock.Mock()
                api.OpenProcess.return_value = 99
                def queried(*_):
                    nonlocal pending
                    pending = True
                    return self.identity
                def cancelled():
                    if pending:
                        raise exception
                    return False
                with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
                    self.launcher, "_queue_identity_from_handle", side_effect=queried,
                ), mock.patch.object(self.launcher, "_request_graceful_close") as graceful:
                    with self.assertRaises(type(exception)) as caught:
                        self.launcher._close_verified_queue_processes({42: self.identity}, force=True, cancel_requested=cancelled)
                self.assertIs(caught.exception, exception)
                api.TerminateProcess.assert_not_called()
                graceful.assert_not_called()
                api.CloseHandle.assert_called_once_with(99)

    def test_cancel_after_snapshot_stops_before_any_close(self) -> None:
        cancelled = False
        def snapshot(*_):
            nonlocal cancelled
            cancelled = True
            return {42: self.identity}, set()
        with mock.patch.object(self.launcher, "_queue_close_snapshot", side_effect=snapshot), mock.patch.object(
            self.launcher, "_close_verified_queue_processes",
        ) as close:
            with self.assertRaises(GameLaunchCancelled):
                self.launcher.close_for_queue("StarRail", str(self.executable), cancel_requested=lambda: cancelled)
        close.assert_not_called()

    def test_binding_rejects_arbitrary_program_unregistered_game_and_network_path(self) -> None:
        other = self.root / "Other.exe"
        other.write_bytes(b"fixture")
        for game_id, path in (("Unknown", str(self.executable)), ("StarRail", str(other)), ("StarRail", r"\\server\games\StarRail.exe")):
            with self.subTest(game_id=game_id, path=path), mock.patch.object(self.launcher, "_queue_close_snapshot") as inspect:
                with self.assertRaises(GameLaunchError):
                    self.launcher.close_for_queue(game_id, path)
                inspect.assert_not_called()

    def test_binding_scopes_ww_launcher_to_game_subtree_only(self) -> None:
        launcher = self.root / "launcher.exe"
        launcher.write_bytes(b"fixture")
        game_root = self.root / "Wuthering Waves Game"
        game_root.mkdir()
        root, names = self.launcher._queue_close_binding("WW", str(launcher))
        self.assertEqual(root, game_root)
        self.assertEqual(names, {"wuthering waves.exe", "client-win64-shipping.exe"})
        self.assertNotIn("launcher.exe", names)

    def _nte_fixture(self) -> tuple[Path, Path, Path]:
        launcher_root = self.root / "NTE installation" / "NTELauncher"
        launcher_root.mkdir(parents=True)
        launcher = launcher_root / "NTELauncher.exe"
        bootstrap = launcher_root / "NTEGame.exe"
        launcher.write_bytes(b"fixture")
        bootstrap.write_bytes(b"fixture")
        configuration = launcher_root / "Config" / "Config.ini"
        configuration.parent.mkdir()
        configuration.write_text(
            "[General]\nLauncher=NTELauncher.exe\nClient=NTEGame.exe\n"
            "GameID=1289\nPackageName=com.hottagames.yh\n[Patcher]\nResRoot=/../Client\n",
            encoding="utf-8",
        )
        client = launcher_root.parent / "Client" / "WindowsNoEditor" / "HT" / "Binaries" / "win64" / "HTGame.exe"
        client.parent.mkdir(parents=True)
        client.write_bytes(b"fixture")
        return launcher, bootstrap, client

    def test_nte_launcher_and_registered_bootstrap_bind_both_narrow_roots(self) -> None:
        launcher, bootstrap, client = self._nte_fixture()
        for entry in (launcher, bootstrap):
            with self.subTest(entry=entry.name):
                roots, names = self.launcher._queue_close_binding("NTE", str(entry))
                self.assertEqual(roots, (launcher.parent, launcher.parent.parent / "Client"))
                self.assertNotIn(launcher.parent.parent, roots)
                self.assertTrue(any(client.is_relative_to(root) for root in roots))
                self.assertIn("htgame.exe", names)
                self.assertIn("ntegame.exe", names)

    def test_nte_bootstrap_exit_cannot_hide_live_sibling_client(self) -> None:
        launcher, bootstrap, client = self._nte_fixture()
        identities = {10: _QueueProcessIdentity(bootstrap, 10), 20: _QueueProcessIdentity(client, 20)}
        names = {10: "NTEGame.exe", 20: "HTGame.exe"}
        def close(_targets, **_):
            names.pop(10, None)
        with mock.patch.object(GameLaunchService, "_list_enumerated", side_effect=lambda _: dict(names)), mock.patch.object(
            GameLaunchService, "_queue_process_identity", side_effect=identities.get,
        ), mock.patch.object(self.launcher, "_close_verified_queue_processes", side_effect=close) as action:
            result = self.launcher.close_for_queue("NTE", str(launcher))
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.requested_process_ids, (10, 20))
        self.assertEqual(result.remaining_process_ids, (20,))
        self.assertEqual(set(action.call_args_list[0].args[0]), {10, 20})
        self.assertEqual(set(action.call_args_list[-1].args[0]), {20})

    def test_nte_both_clients_close_without_touching_other_installations_or_siblings(self) -> None:
        launcher, bootstrap, client = self._nte_fixture()
        identities = {
            10: _QueueProcessIdentity(bootstrap, 10),
            20: _QueueProcessIdentity(client, 20),
            30: _QueueProcessIdentity(launcher.parent.parent / "unrelated" / "HTGame.exe", 30),
            40: _QueueProcessIdentity(self.root / "Other NTE" / "Client" / "HTGame.exe", 40),
        }
        names = {10: "NTEGame.exe", 20: "HTGame.exe", 30: "HTGame.exe", 40: "HTGame.exe"}
        def close(targets, **_):
            for pid in targets:
                names.pop(pid)
        with mock.patch.object(GameLaunchService, "_list_enumerated", side_effect=lambda _: dict(names)), mock.patch.object(
            GameLaunchService, "_queue_process_identity", side_effect=identities.get,
        ), mock.patch.object(self.launcher, "_close_verified_queue_processes", side_effect=close) as action:
            result = self.launcher.close_for_queue("NTE", str(bootstrap))
        self.assertEqual(result.state, "closed")
        self.assertEqual(result.remaining_process_ids, ())
        self.assertEqual(result.requested_process_ids, (10, 20))
        self.assertEqual(set(names), {30, 40})
        self.assertEqual(action.call_count, 1)

    def test_nte_unknown_layout_or_resroot_does_not_expand_close_scope(self) -> None:
        launcher, bootstrap, _ = self._nte_fixture()
        config = launcher.parent / "Config" / "Config.ini"
        original = config.read_text(encoding="utf-8")
        for resroot in ("/../../", "C:/Program Files", "/../Other", "Client"):
            with self.subTest(resroot=resroot):
                config.write_text(original.replace("/../Client", resroot), encoding="utf-8")
                with self.assertRaises(GameLaunchError):
                    self.launcher._queue_close_binding("NTE", str(launcher))
        config.unlink()
        with self.assertRaises(GameLaunchError):
            self.launcher._queue_close_binding("NTE", str(bootstrap))

    def test_nte_client_reparse_point_cannot_expand_the_binding(self) -> None:
        launcher, _, _ = self._nte_fixture()
        client_root = launcher.parent.parent / "Client"
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == client_root:
                return mock.Mock(st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", new=lstat):
            with self.assertRaises(GameLaunchError):
                self.launcher._queue_close_binding("NTE", str(launcher))

    def test_network_binding_is_rejected_before_any_file_existence_probe(self) -> None:
        with mock.patch.object(GameLaunchService, "validate_configured_executable") as validate:
            with self.assertRaises(GameLaunchError):
                self.launcher._queue_close_binding("StarRail", r"\\server\games\StarRail.exe")
        validate.assert_not_called()

    def test_mapped_drive_and_reparse_path_cannot_authorize_queue_close(self) -> None:
        for drive_type, attributes in ((4, 0), (3, 0x400)):
            with self.subTest(drive_type=drive_type, attributes=attributes):
                api = mock.Mock()
                api.GetDriveTypeW.return_value = drive_type
                with mock.patch("yeyu_gamer_manager.services.game_launcher.ctypes.WinDLL", return_value=api), mock.patch.object(
                    Path, "lstat", return_value=mock.Mock(st_file_attributes=attributes),
                ):
                    with self.assertRaises(GameLaunchError):
                        self.launcher._require_queue_local_path(self.executable)

    def test_enumeration_timeout_is_bounded_and_cannot_report_empty(self) -> None:
        with mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.run", side_effect=subprocess.TimeoutExpired("tasklist", 20)) as run:
            with self.assertRaises(GameLaunchError):
                self.launcher._list_enumerated({"starrail.exe"})
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_unfiltered_enumeration_keeps_final_exit_entries(self) -> None:
        completed = subprocess.CompletedProcess(["tasklist"], 0, '"StarRail.exe","42"\n"Other.exe","99"\n', "")
        with mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.run", return_value=completed), mock.patch.object(
            GameLaunchService, "_process_is_live", return_value=False,
        ) as live:
            observed = self.launcher._list_enumerated({"starrail.exe"})
        self.assertEqual(observed, {42: "StarRail.exe"})
        live.assert_not_called()

    def test_failed_enumeration_cannot_mean_already_closed(self) -> None:
        with mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "error")), mock.patch.object(
            self.launcher, "_close_verified_queue_processes",
        ) as close:
            with self.assertRaises(GameLaunchError):
                self.launcher.close_for_queue("StarRail", str(self.executable))
        close.assert_not_called()

    def test_close_started_rechecks_enum_only_residual_after_successful_wait(self) -> None:
        receipt = GameLaunchReceipt("started", 42, "StarRail.exe", (), ("starrail.exe",))
        with mock.patch.object(self.launcher, "_list_running", return_value={42: "StarRail.exe"}), mock.patch.object(
            self.launcher, "_list_zombies", return_value={},
        ), mock.patch.object(self.launcher, "_request_graceful_close"), mock.patch.object(
            self.launcher, "_wait_for_owned_exit", return_value=set(),
        ), mock.patch.object(self.launcher, "_list_enumerated", return_value={42: "StarRail.exe"}):
            result = self.launcher.close_started(receipt)
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (42,))

    def test_ww_close_started_cannot_discard_unreadable_enumerated_identity(self) -> None:
        receipt = GameLaunchReceipt("started", 42, "Client-Win64-Shipping.exe", (), ("client-win64-shipping.exe",), ww_launcher_path=str(self.root / "launcher.exe"))
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={}), mock.patch.object(self.launcher, "_list_zombies", return_value={}), mock.patch.object(
            GameLaunchService, "_list_enumerated", return_value={42: "Client-Win64-Shipping.exe"},
        ), mock.patch.object(GameLaunchService, "_process_executable_path", return_value=None), mock.patch.object(self.launcher, "_request_graceful_close") as close:
            result = self.launcher.close_started(receipt)
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (42,))
        close.assert_not_called()
