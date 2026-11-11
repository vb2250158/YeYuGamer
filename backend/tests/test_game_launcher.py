from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import os
import base64
import ctypes
import json
import subprocess
import sys
import tempfile
import threading
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
from yeyu_gamer_manager.services.window_capture import registered_game_process_names


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

    def test_nte_official_launcher_is_delegated_without_launch_or_ready_claim(self) -> None:
        executable = Path(self.temporary.name) / "NTELauncher.exe"
        executable.write_bytes(b"fixture")
        observations = []
        with mock.patch.object(self.launcher, "_list_running", return_value={222: "NTEGame.exe"}), mock.patch.object(
            self.launcher, "_ensure_started"
        ) as start:
            receipt = self.launcher.ensure_started("NTE", str(executable), observer=observations.append)
        start.assert_not_called()
        self.assertEqual(receipt.state, "official-tool-pending")
        self.assertEqual(receipt.baseline_process_ids, (222,))
        self.assertIsNone(receipt.ready_window_pid)
        self.assertFalse(receipt.as_result()["managerOwned"])
        self.assertEqual([item.phase for item in observations], ["official-launch-pending"])

    def test_nte_upgrade_prompt_is_cleared_by_a_bounded_watcher(self) -> None:
        executable = Path(self.temporary.name) / "NTELauncher.exe"
        executable.write_bytes(b"fixture")
        invoked = threading.Event()
        calls: list[tuple[Path, bool]] = []

        def fake_probe(root, *, allow_invoke, cancel_requested):
            calls.append((root, allow_invoke))
            invoked.set()
            return "invoked:nte-launcher-upgrade:removed"

        with mock.patch.object(GameLaunchService, "_probe_nte_launcher_prompt", side_effect=fake_probe), mock.patch.object(
            GameLaunchService, "NTE_LAUNCHER_UPGRADE_PROBE_SECONDS", 0.05
        ), mock.patch.object(self.launcher, "_list_running", return_value={222: "NTEGame.exe"}), mock.patch.object(
            self.launcher, "_ensure_started"
        ) as start:
            self.launcher.ensure_started("NTE", str(executable), observer=lambda _item: None)
            self.assertTrue(invoked.wait(5.0))

        # The official launcher stays with the tool: only the audited upgrade
        # label is cleared, and the watcher is allowed to click it by default.
        start.assert_not_called()
        self.assertEqual(GameLaunchService.NTE_LAUNCHER_UPGRADE_LABEL, "立即体验")
        self.assertEqual(calls[0], (executable.parent, True))

    def test_nikke_wegame_bootstrap_gets_a_longer_start_budget(self) -> None:
        # The WeGame bootstrap routinely needs more than the 45s default before
        # the NIKKE client appears (observed 2026-09-18: game_start_failed at
        # 46.2s with no registered process).
        self.assertEqual(GameLaunchService.START_TIMEOUT_OVERRIDES.get("NIKKE"), 600.0)
        self.assertGreater(
            GameLaunchService.START_TIMEOUT_OVERRIDES["NIKKE"],
            GameLaunchService.START_TIMEOUT_SECONDS,
        )

    def test_delegated_launch_cleanup_preserves_baseline_and_closes_new_client(self) -> None:
        receipt = GameLaunchReceipt("official-tool-pending", None, "NTEGame.exe", (222,), ("ntegame.exe", "htgame.exe"))
        with mock.patch.object(self.launcher, "_list_running", return_value={222: "NTEGame.exe", 333: "HTGame.exe"}), mock.patch.object(
            self.launcher, "_list_zombies", return_value={}
        ), mock.patch.object(self.launcher, "_list_enumerated", return_value={}), mock.patch.object(
            self.launcher, "_request_graceful_close"
        ) as close, mock.patch.object(self.launcher, "_wait_for_owned_exit", return_value=set()):
            result = self.launcher.close_started(receipt)
        close.assert_called_once_with({333})
        self.assertEqual(result.requested_process_ids, (333,))

    def test_zzz_launch_preserves_official_login_branch_without_starting_exe(self) -> None:
        executable = Path(self.temporary.name) / "ZenlessZoneZero.exe"
        executable.write_bytes(b"fixture")
        observations = []
        with mock.patch.object(self.launcher, "_list_running", return_value={}), mock.patch.object(
            self.launcher, "_ensure_started"
        ) as start:
            receipt = self.launcher.ensure_started("ZZZ", str(executable), observer=observations.append)
        start.assert_not_called()
        self.assertEqual(receipt.state, "official-tool-pending")
        self.assertIsNone(receipt.process_id)
        self.assertIsNone(receipt.ready_window_pid)
        self.assertEqual(observations[0].phase, "official-launch-pending")
        self.assertEqual(observations[0].detail["operation"], "OpenAndEnterGame.execute")

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

    def test_nikke_wegame_install_launches_formal_entry_and_waits_for_client(self) -> None:
        client = Path(self.temporary.name) / "nikke.exe"
        client.write_bytes(b"client")
        entry = client.parent / "WeGameLauncher" / "launcher.exe"
        entry.parent.mkdir()
        entry.write_bytes(b"official launcher fixture")
        with mock.patch("yeyu_gamer_manager.services.game_launcher.os.name", "nt"), mock.patch.object(
            self.launcher, "_list_running", side_effect=[{}, {456: "nikke.exe"}],
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=123)) as popen, mock.patch.object(
            self.launcher, "_wait_until_ready", return_value=(456, "nikke.exe", 1920, 1080),
        ) as ready:
            receipt = self.launcher.ensure_started("NIKKE", str(client))
        self.assertEqual(popen.call_args.args[0], [str(entry)])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(entry.parent))
        self.assertEqual(receipt.ready_window_pid, 456)
        self.assertNotIn("launcher.exe", receipt.expected_process_names)
        self.assertEqual(ready.call_args.args[2], client.parent)

    def test_nikke_incomplete_wegame_install_never_falls_back_to_bare_client(self) -> None:
        client = Path(self.temporary.name) / "nikke.exe"
        client.write_bytes(b"client")
        (client.parent / "WeGameLauncher").mkdir()
        with mock.patch("yeyu_gamer_manager.services.game_launcher.os.name", "nt"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen"
        ) as popen:
            with self.assertRaisesRegex(GameLaunchError, "WeGame"):
                self.launcher.ensure_started("NIKKE", str(client))
        popen.assert_not_called()

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

    def test_launch_executable_reaps_the_spawned_process_handle(self) -> None:
        """The Manager must stop holding the handle of every client it starts.

        ``Popen.__del__`` keeps a still-running child alive in
        ``subprocess._active``, and the launch path only reads ``.pid``, so the
        Manager held the handle of every client it started for its whole
        lifetime.  That is not what kept the dead entries enumerable (measured
        2026-09-22: the holders were Windows' own ``RpcSs``/``Themes``/
        ``Audiosrv`` services and the clients' own thread handles), but leaving
        our own handles open is still wrong, so the launch path must wait on the
        child instead of dropping the ``Popen``.
        """

        reaped = threading.Event()
        process = mock.Mock(pid=4242)
        process.wait.side_effect = lambda *args, **kwargs: reaped.set()

        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ):
            started = self.launcher._start_launch_executable(
                "StarRail", self.executable, self.executable, None,
            )

        self.assertEqual(started.pid, 4242)
        self.assertTrue(
            reaped.wait(5.0),
            "the spawned client handle must be released by waiting on the child",
        )

    def test_launch_is_refused_while_a_leftover_client_owns_the_guard_mutex(self) -> None:
        """A leftover that still owns the single-instance mutex makes launch futile.

        Measured 2026-09-22 with handle64: the enumerated-but-not-live
        ``PGR.exe``/``GF2_Exilium.exe`` entries still held
        ``comkurogameharukuro`` and
        ``ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default``, because
        the kernel releases a mutex only when its owner is destroyed and those
        clients never finished terminating.  Every later launch therefore died
        instantly (PGR exit 0 with an untouched log directory) or said "Another
        instance is already running" (GF2).  Burning the readiness timeout hides
        the mechanism and leaves one more unreapable leftover, so the launch has
        to be refused up front with the mutex and the process ids named.
        """

        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.named_mutex_is_held",
            return_value=True,
        ), mock.patch.object(
            self.launcher, "_list_zombies", return_value={6392: "PGR.exe", 38192: "PGR.exe"},
        ) as zombies, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen"
        ) as popen:
            with self.assertRaises(GameLaunchError) as caught:
                self.launcher._start_launch_executable(
                    "PGR", self.executable, self.executable, None,
                )

        message = str(caught.exception)
        self.assertIn("comkurogameharukuro", message)
        self.assertIn("6392", message)
        self.assertIn("38192", message)
        self.assertNotIsInstance(caught.exception, GameLaunchHumanRequired)
        self.assertEqual(zombies.call_args.args[0], set(registered_game_process_names("PGR")))
        popen.assert_not_called()

    def test_launch_proceeds_when_the_guard_mutex_is_free(self) -> None:
        """Stale entries alone are not a reason to refuse: the mutex decides."""

        process = mock.Mock(pid=4244)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.named_mutex_is_held",
            return_value=False,
        ), mock.patch.object(
            self.launcher, "_list_zombies", return_value={6392: "PGR.exe"},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ) as popen:
            started = self.launcher._start_launch_executable(
                "PGR", self.executable, self.executable, None,
            )

        self.assertEqual(started.pid, 4244)
        popen.assert_called_once()

    def test_launch_proceeds_when_the_mutex_holder_is_not_a_leftover(self) -> None:
        """A held mutex with no exited leftover is the already-running path."""

        process = mock.Mock(pid=4245)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.named_mutex_is_held",
            return_value=True,
        ), mock.patch.object(
            self.launcher, "_list_zombies", return_value={},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ) as popen:
            started = self.launcher._start_launch_executable(
                "GF2", self.executable, self.executable, None,
            )

        self.assertEqual(started.pid, 4245)
        popen.assert_called_once()

    def test_games_without_a_measured_guard_are_never_refused(self) -> None:
        """The registry is evidence-based; unmeasured games keep launching."""

        process = mock.Mock(pid=4246)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.named_mutex_is_held",
        ) as mutex, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ) as popen:
            started = self.launcher._start_launch_executable(
                "StarRail", self.executable, self.executable, None,
            )

        self.assertEqual(started.pid, 4246)
        mutex.assert_not_called()
        popen.assert_called_once()

    def test_endfield_launcher_reaps_the_spawned_process_handle(self) -> None:
        """The Endfield activation dispatch must release its handle too."""

        reaped = threading.Event()
        process = mock.Mock(pid=4243)
        process.wait.side_effect = lambda *args, **kwargs: reaped.set()

        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ):
            started = self.launcher._start_endfield_launcher(self.executable)

        self.assertEqual(started.pid, 4243)
        self.assertTrue(
            reaped.wait(5.0),
            "the spawned Endfield launcher handle must be released by waiting on it",
        )

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
        self.assertLess(script.index("$actions.Count -ne 1"), script.index("Write-Output (@{ entry = 'ready:ww-enter-game'"))
        self.assertLess(script.index("TryGetCurrentPattern([Windows.Automation.InvokePattern]"), script.index("Write-Output (@{ entry = 'ready:ww-enter-game'"))
        self.assertLess(script.index("Write-Output (@{ entry = 'ready:ww-enter-game'"), script.index("$pattern.Invoke()"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell rule fixtures")
    def test_ww_uia_rules_distinguish_toolbar_news_primary_update_and_modal(self) -> None:
        primary = "launcher-button launcher-button-normal status-btn"
        cases = [
            ({"Name": "修复游戏", "Kind": "ControlType.Text"}, "ignore"),
            ({"Name": "更新失败处理与登录失效说明", "Kind": "ControlType.Hyperlink"}, "ignore"),
            ({"Name": "正在更新的版本资讯", "Kind": "ControlType.Text"}, "ignore"),
            ({"Name": "进入游戏", "Kind": "ControlType.Button", "ClassName": primary}, "entry"),
            ({"Name": "进入中", "Kind": "ControlType.Button", "ClassName": primary}, "busy"),
            ({"Name": "更新游戏", "Kind": "ControlType.Button", "ClassName": primary}, "update"),
            ({"Name": "修复游戏", "Kind": "ControlType.Button", "ClassName": primary}, "update"),
            ({"Name": "重试", "Kind": "ControlType.Button", "ClassName": primary}, "retry"),
            ({"Name": "正在下载", "Kind": "ControlType.Button", "ClassName": primary}, "busy"),
            ({"Name": "下载中…", "Kind": "ControlType.Button", "ClassName": primary}, "busy"),
            ({"Name": "校验中", "Kind": "ControlType.Button", "ClassName": primary}, "busy"),
            ({"Name": "更新中", "Kind": "ControlType.Button", "ClassName": primary}, "busy"),
            ({"Name": "等待中", "Kind": "ControlType.Button", "ClassName": primary}, "busy"),
            ({"Name": "神秘状态", "Kind": "ControlType.Button", "ClassName": primary}, "human:unrecognized-primary-action"),
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
        ) as list_ww, mock.patch.object(self.launcher, "_wait_until_ready", side_effect=error), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=111),
        ), mock.patch.object(self.launcher, "close_started") as close:
            with self.assertRaises(GameLaunchHumanRequired):
                self.launcher.ensure_started("WW", str(executable), observations.append)
        close.assert_not_called()
        self.assertEqual(
            [item.phase for item in observations],
            ["launcher-action", "launch-human-required"],
        )
        self.assertEqual(observations[0].detail["operation"], "launch-executable-started")
        self.assertEqual(observations[0].detail["processId"], 111)
        self.assertEqual(observations[1].process_ids, frozenset({222}))
        # A gate that must stay human is never probed for recyclable debris: the
        # re-enumeration belongs to the recyclable branch only, so a login gate
        # touches nothing and simply preserves the scene.
        self.assertEqual(list_ww.call_count, 2)

    def test_ww_formal_launcher_gates_login_update_and_unknown_ui(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        for outcome, reason in (
            ("human:login-or-consent", "ww_launcher_login_or_consent"),
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

    def test_ww_formal_launcher_invokes_update_then_waits_for_ready_window(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        ready = (33, "client-win64-shipping.exe", 1280, 720)
        outcomes = iter(["invoked:ww-update", "waiting:ww-busy", "invoked:ww-enter-game"])
        frames = iter([None] * 20 + [ready, ready])
        clock = iter(range(1000))
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe", 33: ready[1]}), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", side_effect=lambda *args, **kwargs: next(outcomes, "waiting:game-window")) as action, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock)),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False,
        ):
            result = self.launcher._wait_until_ready("WW", {"launcher_main.exe", ready[1]}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(result, ready)
        self.assertGreaterEqual(action.call_count, 2)

    def test_ww_formal_launcher_retries_update_until_cap(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        clock = iter(range(1000))
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="invoked:ww-retry"), mock.patch.object(
            self.launcher, "WW_LAUNCHER_MAX_UPDATE_ACTIONS", 2,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
            with self.assertRaises(GameLaunchError) as caught:
                self.launcher._wait_until_ready("WW", {"client-win64-shipping.exe"}, launcher.parent, ww_launcher=launcher)
        self.assertIn("update/retry was dispatched too many times", str(caught.exception))

    def test_ww_recycle_reclaims_its_own_dead_client_shells(self) -> None:
        """Debris the Manager started must not permanently wedge the launcher."""

        running = {
            1: "launcher_main.exe",
            2: "Wuthering Waves.exe",
            3: "Client-Win64-Shipping.exe",
        }
        with mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=None,
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes", return_value=33 * 1024 * 1024,
        ):
            targets = GameLaunchService._ww_launcher_recycle_targets(running)
        self.assertEqual(targets, {1, 2, 3})
        self.assertIn(
            "ww_launcher_game_window_missing",
            GameLaunchService.WW_LAUNCHER_RECYCLABLE_GATES,
        )

    def test_ww_recycle_never_touches_a_live_client_window(self) -> None:
        running = {1: "launcher_main.exe", 2: "Client-Win64-Shipping.exe"}
        with mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=4242,
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes", return_value=8 * 1024 * 1024,
        ):
            self.assertEqual(GameLaunchService._ww_launcher_recycle_targets(running), set())

    def test_ww_recycle_never_touches_a_hung_live_client(self) -> None:
        """A windowless but memory-heavy client is a live session, not debris."""

        running = {1: "launcher_main.exe", 2: "Wuthering Waves.exe"}
        with mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=None,
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes", return_value=2 * 1024 ** 3,
        ):
            self.assertEqual(GameLaunchService._ww_launcher_recycle_targets(running), set())

    def test_ww_recycle_stays_conservative_when_identity_is_unreadable(self) -> None:
        running = {1: "launcher_main.exe", 2: "Wuthering Waves.exe"}
        with mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=None,
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes", return_value=None,
        ):
            self.assertEqual(GameLaunchService._ww_launcher_recycle_targets(running), set())

    def test_ww_post_launch_gate_recycles_the_debris_this_attempt_started(self) -> None:
        """A gate raised after our own launch must self-heal instead of failing.

        Measured 2026-09-22 05:17 and 05:25 on this host: the post-launch handler
        read an undefined ``running`` and raised ``NameError``, so a recyclable
        WW gate was reported as ``adapter_start_failed`` while the launcher and
        client debris that this very attempt had started stayed in place.  The
        recycle candidates must come from the *post-launch* observation.
        """

        launcher = Path(self.temporary.name) / "launcher.exe"
        launcher.write_bytes(b"fixture")
        debris = {
            22: "launcher_main.exe",
            33: "Wuthering Waves.exe",
            44: "Client-Win64-Shipping.exe",
        }
        ready = (55, "client-win64-shipping.exe", 1280, 720)
        gate = GameLaunchHumanRequired(
            "ww_launcher_game_window_missing",
            "WW formal launcher did not yield a stable game window; "
            "inspect the preserved scene before resuming.",
        )
        listing: list[int] = []

        def list_ww(_launcher, _expected_names):
            listing.append(1)
            # The first call is the pre-launch baseline: nothing was running yet.
            return {} if len(listing) == 1 else dict(debris)

        outcomes = iter([gate, ready])

        def wait(*_args, **_kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        clock = iter(range(100000))
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            GameLaunchService, "_verify_ww_launcher"
        ), mock.patch.object(
            self.launcher, "_list_ww_running", side_effect=list_ww
        ), mock.patch.object(
            self.launcher, "_wait_until_ready", side_effect=wait
        ), mock.patch.object(
            self.launcher, "_start_launch_executable",
            return_value=mock.Mock(pid=9001),
        ) as start, mock.patch.object(
            self.launcher, "_recycle_ww_launcher", return_value=set()
        ) as recycle, mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=None
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes",
            return_value=33 * 1024 * 1024,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            receipt = self.launcher._ensure_started(
                "WW", str(launcher), None, 0.0, None,
            )

        recycle.assert_called_once_with(set(debris))
        self.assertEqual(start.call_count, 2)
        self.assertEqual(receipt.state, "started")
        self.assertEqual(receipt.ready_window_pid, 55)

    def test_ww_post_launch_gate_keeps_a_live_client_at_the_human_gate(self) -> None:
        """Recycling must never reach a live client that still holds a session.

        Same post-launch path as above: the pre-launch baseline is empty, so the
        gate is raised for a launch this attempt started, and only the
        post-launch observation can decide whether recycling is allowed.
        """

        launcher = Path(self.temporary.name) / "launcher.exe"
        launcher.write_bytes(b"fixture")
        gate = GameLaunchHumanRequired(
            "ww_launcher_game_window_missing",
            "WW formal launcher did not yield a stable game window; "
            "inspect the preserved scene before resuming.",
        )
        listing: list[int] = []

        def list_ww(_launcher, _expected_names):
            listing.append(1)
            if len(listing) == 1:
                return {}
            return {22: "launcher_main.exe", 44: "Client-Win64-Shipping.exe"}

        clock = iter(range(100000))
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            GameLaunchService, "_verify_ww_launcher"
        ), mock.patch.object(
            self.launcher, "_list_ww_running", side_effect=list_ww
        ), mock.patch.object(
            self.launcher, "_wait_until_ready", side_effect=gate
        ), mock.patch.object(
            self.launcher, "_start_launch_executable",
            return_value=mock.Mock(pid=9001),
        ), mock.patch.object(
            self.launcher, "_recycle_ww_launcher", return_value=set()
        ) as recycle, mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=4242
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes",
            return_value=8 * 1024 * 1024,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._ensure_started("WW", str(launcher), None, 0.0, None)

        self.assertEqual(caught.exception.reason_code, "ww_launcher_game_window_missing")
        recycle.assert_not_called()
        self.assertGreater(len(listing), 1)

    def test_ww_recycle_restart_gets_a_fresh_grace_window(self) -> None:
        """A launcher restarted by the recycle branch must be able to become ready.

        Measured 2026-09-24 on this host (attempt ``3ac5ba8f``, batch
        ``7da2bded``): the post-launch gate fired at 370s, the recycle branch
        replaced the wedged ``launcher_main.exe`` (pid 31120) and successfully
        started a fresh ``launcher.exe`` (pid 2080, logged as
        ``launch-executable-started``) -- and then the method raised
        ``ww_launcher_process_not_observed`` with ``pids=[]`` one millisecond
        later.  Cause: the blocking ``_wait_until_ready`` call consumes up to
        ``READY_TIMEOUT_SECONDS`` *inside* the loop body while the loop
        condition still uses the ``START_TIMEOUT_SECONDS`` deadline computed
        before it, so the ``continue`` after a restart re-tests an already
        expired deadline and exits the loop without ever observing the process
        it just started.  The recovery branch could therefore never work, and
        the human gate parked the whole game day for 16 hours.

        The fix must give the restarted launcher its own grace window.  Time
        here advances realistically: the wait call burns past the outer
        deadline, exactly as the blocking wait does in production.
        """

        launcher = Path(self.temporary.name) / "launcher.exe"
        launcher.write_bytes(b"fixture")
        debris = {31120: "launcher_main.exe"}
        ready = (55, "client-win64-shipping.exe", 1280, 720)
        gate = GameLaunchHumanRequired(
            "ww_launcher_game_window_missing",
            "WW formal launcher did not yield a stable game window; "
            "inspect the preserved scene before resuming.",
        )
        listing: list[int] = []

        def list_ww(_launcher, _expected_names):
            listing.append(1)
            # 1st call: pre-launch baseline (nothing running).  Every later call
            # is the post-launch observation of the wedged launcher.
            return {} if len(listing) == 1 else dict(debris)

        now = [1000.0]

        def monotonic() -> float:
            return now[0]

        outcomes = iter([gate, ready])

        def wait(*_args, **_kwargs):
            # Mirrors the blocking ready-wait: it spends the full readiness
            # budget inside one loop iteration, so wall time passes the outer
            # start deadline before the recycle branch runs.
            now[0] += float(GameLaunchService.START_TIMEOUT_SECONDS) + 300.0
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            GameLaunchService, "_verify_ww_launcher"
        ), mock.patch.object(
            self.launcher, "_list_ww_running", side_effect=list_ww
        ), mock.patch.object(
            self.launcher, "_wait_until_ready", side_effect=wait
        ), mock.patch.object(
            self.launcher, "_start_launch_executable",
            return_value=mock.Mock(pid=9001),
        ) as start, mock.patch.object(
            self.launcher, "_recycle_ww_launcher", return_value=set()
        ) as recycle, mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=None
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes",
            return_value=33 * 1024 * 1024,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=monotonic,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            receipt = self.launcher._ensure_started("WW", str(launcher), None, 0.0, None)

        recycle.assert_called_once_with(set(debris))
        self.assertEqual(start.call_count, 2)
        self.assertEqual(receipt.state, "started")
        self.assertEqual(receipt.ready_window_pid, 55)

    def test_ww_gate_recycles_shells_that_appeared_during_the_blocking_wait(self) -> None:
        """The recycle set must be re-read when the gate fires, not taken stale.

        Measured 2026-09-24 21:37 (attempt ``a9f79fd1``): the blocking ready-wait
        saw ``running={26752 launcher_main, 31972 Wuthering Waves.exe,
        44620 Client-Win64-Shipping.exe}``, but the recycle branch ran with the
        snapshot its loop iteration had taken *before* that wait, so only the
        launcher was replaced.  The two surviving client shells (8.5MB and
        32.5MB, no window -- exactly the recyclable debris
        ``_is_proven_dead_ww_client`` matches) then made the freshly started
        launcher fail at once with ``ww_launcher_existing_client_unready``, and
        the game day stayed blocked.  The gate handler must enumerate again.
        """

        launcher = Path(self.temporary.name) / "launcher.exe"
        launcher.write_bytes(b"fixture")
        pre_wait = {26752: "launcher_main.exe"}
        post_wait = {
            26752: "launcher_main.exe",
            31972: "Wuthering Waves.exe",
            44620: "Client-Win64-Shipping.exe",
        }
        gate = GameLaunchHumanRequired(
            "ww_launcher_game_window_missing",
            "WW formal launcher did not yield a stable game window; "
            "inspect the preserved scene before resuming.",
        )
        ready = (55, "client-win64-shipping.exe", 1280, 720)
        listing: list[int] = []

        def list_ww(_launcher, _expected_names):
            listing.append(1)
            # 1st: pre-launch baseline (empty).  Then the snapshot taken at the
            # top of the running iteration, which predates the shells.  Every
            # later call is the re-enumeration the gate handler performs.
            if len(listing) == 1:
                return {}
            if len(listing) == 2:
                return dict(pre_wait)
            return dict(post_wait)

        outcomes = iter([gate, ready])

        def wait(*_args, **_kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        clock = iter(range(100000))
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch.object(
            GameLaunchService, "_verify_ww_launcher"
        ), mock.patch.object(
            self.launcher, "_list_ww_running", side_effect=list_ww
        ), mock.patch.object(
            self.launcher, "_wait_until_ready", side_effect=wait
        ), mock.patch.object(
            self.launcher, "_start_launch_executable",
            return_value=mock.Mock(pid=9001),
        ), mock.patch.object(
            self.launcher, "_recycle_ww_launcher", return_value=set()
        ) as recycle, mock.patch.object(
            GameLaunchService, "_find_window_handle", return_value=None
        ), mock.patch.object(
            GameLaunchService, "_process_working_set_bytes",
            return_value=33 * 1024 * 1024,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            receipt = self.launcher._ensure_started("WW", str(launcher), None, 0.0, None)

        # Every shell the waiter could see must be replaced, not just the
        # launcher from the stale snapshot.
        recycle.assert_called_once_with(set(post_wait))
        self.assertEqual(receipt.state, "started")
        self.assertEqual(receipt.ready_window_pid, 55)

    def test_a_gone_process_id_is_not_reported_live(self) -> None:
        """A pid that cannot exist must not count as a live residual.

        Measured 2026-09-24: ``_process_is_live(2147483000)`` returned True even
        though OpenProcess failed with ERROR_INVALID_PARAMETER, so an exited
        StarRail client stayed a "residual" and turned a fully completed daily
        into ``game_cleanup_failed``.  Only a permission failure may stay
        conservative.
        """

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # A pid far above any live id: OpenProcess reports it does not exist.
        gone = 2147483000
        handle = kernel32.OpenProcess(0x1000, False, gone)
        if handle:
            kernel32.CloseHandle(handle)
            self.skipTest("this host resolved the probe pid; cannot test the gone case")
        self.assertNotIn(
            ctypes.get_last_error(), (), "get_last_error must be read after the call"
        )
        self.assertFalse(GameLaunchService._process_is_live(gone))

    def test_an_unreadable_process_stays_conservatively_live(self) -> None:
        """A process we may not query must still count as possibly live."""

        with mock.patch(
            "ctypes.WinDLL"
        ) as dll, mock.patch(
            "ctypes.get_last_error", return_value=5  # ERROR_ACCESS_DENIED
        ):
            kernel32 = mock.Mock()
            kernel32.OpenProcess.return_value = 0
            dll.return_value = kernel32
            self.assertTrue(GameLaunchService._process_is_live(4242))

    def test_exit_code_zero_with_unsignaled_process_remains_live(self) -> None:
        with mock.patch("ctypes.WinDLL", create=True) as dll:
            api = mock.Mock()
            dll.return_value = api
            api.OpenProcess.return_value = 99
            api.GetExitCodeProcess.side_effect = lambda _, output: setattr(output._obj, "value", 0) or 1
            for result in (258, 0xFFFFFFFF, 128):
                api.WaitForSingleObject.return_value = result
                self.assertTrue(GameLaunchService._process_is_live(4242))
            api.OpenProcess.assert_called_with(0x101000, False, 4242)
            api.WaitForSingleObject.assert_called_with(99, 0)
            self.assertEqual(api.CloseHandle.call_count, 3)

    def test_signaled_process_does_not_depend_on_stale_or_259_exit_code(self) -> None:
        with mock.patch("ctypes.WinDLL", create=True) as dll:
            api = mock.Mock()
            dll.return_value = api
            api.OpenProcess.return_value = 99
            api.WaitForSingleObject.return_value = 0
            api.GetExitCodeProcess.side_effect = AssertionError("Exit code alone is not termination proof")
            self.assertFalse(GameLaunchService._process_is_live(4242))
            api.CloseHandle.assert_called_once_with(99)

    def test_close_started_does_not_revive_a_client_that_already_exited(self) -> None:
        """A successful close must not be re-opened by a stale re-read.

        Measured 2026-09-24 23:18 on this host (StarRail attempt ``0db14fa7``):
        all four required Todos completed with ``upstream_task_succeeded`` and
        the Adapter ended cleanly (``adapter.watch.end exit=0 -> status=completed
        transport=clean``), yet the attempt was recorded as
        ``status=failed protocolValid=False code=game_cleanup_failed`` because
        ``attempt.game_cleanup`` reported ``requested=[39716]
        remaining=[39716]``.  The pid was gone seconds later -- the exit ladder
        had actually closed the client, and only the trailing
        ``remaining |= self._listed_cleanup_residuals(...)`` re-read brought the
        exited entry back as a blocker.  A finished daily must not be downgraded
        because Windows kept enumerating an exited process.
        """

        receipt = GameLaunchReceipt(
            "started", 39716, "StarRail.exe", (), ("starrail.exe",),
        )
        # The ladder sees the client once, requests its close, then the client
        # is gone from the running enumeration -- but the raw enumeration still
        # lists the exited entry.
        owned_calls = {"n": 0}

        def list_running(_names):
            owned_calls["n"] += 1
            return {39716: "StarRail.exe"} if owned_calls["n"] == 1 else {}

        with mock.patch.object(
            self.launcher, "_list_running", side_effect=list_running,
        ), mock.patch.object(
            self.launcher, "_list_enumerated", return_value={39716: "StarRail.exe"},
        ), mock.patch.object(
            self.launcher, "_request_graceful_close",
        ), mock.patch.object(
            self.launcher, "_terminate_owned",
        ) as terminate, mock.patch.object(
            GameLaunchService, "_process_is_live", return_value=False,
        ), mock.patch.object(
            GameLaunchService, "_list_zombies", return_value={},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ):
            result = self.launcher.close_started(receipt)

        # An already-exited entry is not a residual: the close succeeded and the
        # exit ladder did not have to force anything.
        self.assertEqual(result.state, "closed")
        self.assertEqual(result.remaining_process_ids, ())
        terminate.assert_not_called()

    def test_close_started_still_reports_a_live_unclosable_client(self) -> None:
        """The inverse: a client that really is alive must stay a residual.

        Guards the fix above from weakening cleanup -- only an exited entry may
        be dropped, never a live one.
        """

        receipt = GameLaunchReceipt(
            "started", 39716, "StarRail.exe", (), ("starrail.exe",),
        )
        with mock.patch.object(
            self.launcher, "_list_running", return_value={39716: "StarRail.exe"},
        ), mock.patch.object(
            self.launcher, "_list_enumerated", return_value={39716: "StarRail.exe"},
        ), mock.patch.object(
            self.launcher, "_request_graceful_close",
        ), mock.patch.object(
            self.launcher, "_terminate_owned",
        ), mock.patch.object(
            GameLaunchService, "_process_is_live", return_value=True,
        ), mock.patch.object(
            GameLaunchService, "_list_zombies", return_value={},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep"
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=[0.0, 1000.0] * 40,
        ):
            result = self.launcher.close_started(receipt)

        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (39716,))

    def test_exited_but_still_listed_entry_is_not_a_cleanup_residual(self) -> None:
        """An exited process kept listed by another handle must not block the queue.

        Measured 2026-09-21: PGR.exe stayed enumerable with exit code 0 and could
        not be terminated, so queue cleanup reported an incomplete residual and
        every following game stopped at queue_game_cleanup_incomplete.
        """

        with mock.patch.object(
            self.launcher, "_list_enumerated", return_value={6392: "PGR.exe"},
        ), mock.patch.object(
            self.launcher, "_process_is_live", return_value=False,
        ):
            self.assertEqual(
                self.launcher._listed_cleanup_residuals({"PGR.exe"}, set(), None), set(),
            )

    def test_live_process_is_still_a_cleanup_residual(self) -> None:
        with mock.patch.object(
            self.launcher, "_list_enumerated", return_value={6392: "PGR.exe"},
        ), mock.patch.object(
            self.launcher, "_process_is_live", return_value=True,
        ):
            self.assertEqual(
                self.launcher._listed_cleanup_residuals({"PGR.exe"}, set(), None), {6392},
            )

    def test_queue_close_snapshot_ignores_already_exited_entries(self) -> None:
        """An exited process must not block the queue-close snapshot.

        Measured 2026-09-22: PGR.exe stayed enumerable with exit code 0 and could
        not be closed, so the queue reported remainingProcessIds=[6392] and every
        following game stopped at queue_game_cleanup_incomplete.
        """

        root = Path(self.temporary.name)
        with mock.patch.object(
            self.launcher, "_list_enumerated", return_value={6392: "PGR.exe"},
        ), mock.patch.object(
            self.launcher, "_process_is_live", return_value=False,
        ), mock.patch.object(
            self.launcher, "_queue_process_identity",
        ) as identity:
            verified, unknown = self.launcher._queue_close_snapshot(root, {"PGR.exe"})
        self.assertEqual(verified, {})
        self.assertEqual(unknown, set())
        identity.assert_not_called()

    def test_endfield_launcher_actions_are_capped_even_when_every_action_is_acted(self) -> None:
        """`acted` must not loop forever.

        Measured 2026-09-21: every action reported "acted" while the client was a
        6MB shell with no window, so the no-effect counter kept resetting and the
        launcher dispatched audited foreground clicks (stealing focus) for 23
        minutes, which made the desktop unusable.
        """

        launcher = Path(self.temporary.name) / "launcher.exe"
        clock = iter(range(6000))
        with mock.patch.object(
            self.launcher, "_list_running", return_value={22: "launcher.exe"},
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ), mock.patch.object(
            self.launcher, "_drive_endfield_launcher", return_value="acted",
        ) as action, mock.patch.object(
            self.launcher, "LAUNCHER_UI_READY_TIMEOUT_SECONDS", 3600.0,
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            return_value=False,
        ):
            with self.assertRaises(GameLaunchError) as caught:
                self.launcher._wait_until_ready("Endfield", {"launcher.exe"}, launcher.parent)
        self.assertIn("audited actions", str(caught.exception))
        self.assertLessEqual(
            action.call_count, self.launcher.ENDFIELD_MAX_LAUNCHER_ACTIONS + 1
        )

    def test_ww_eight_megabyte_file_check_writes_are_not_human_required(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        ready = (33, "client-win64-shipping.exe", 1280, 720)
        clock = iter(range(1000))
        frames = iter([None] * 20 + [ready, ready])
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe", 33: ready[1]}), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="waiting:ww-busy") as action, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            return_value=True,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ):
            result = self.launcher._wait_until_ready("WW", {"launcher_main.exe", ready[1]}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(result, ready)
        self.assertTrue(action.called)
        self.assertTrue(all(not call.kwargs["allow_invoke"] for call in action.call_args_list))

    def test_nikke_wegame_surface_that_never_starts_the_client_gates_for_operator(self) -> None:
        """A WeGame surface that waits for a decision must not burn the whole budget."""

        with mock.patch.object(
            self.launcher, "_list_running", return_value={},
        ), mock.patch.object(
            self.launcher, "_probe_nikke_wegame_primary_action",
            return_value="waiting:wegame-primary-action-absent",
        ), mock.patch.object(self.launcher, "NIKKE_WEGAME_UI_READY_SECONDS", 5), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", return_value=100.0,
        ):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._drive_nikke_wegame_surface(
                    observer=None, game_id="NIKKE", started_at=0.0,
                    expected_names={"nikke.exe"}, actions=0, wait_started_at=0.0,
                    cancel_requested=None,
                )
        self.assertEqual(caught.exception.reason_code, "nikke_wegame_launch_required")

    def test_nikke_wegame_clicked_action_counts_and_reports_the_surface(self) -> None:
        """The surface that owns the start decision must be in the captured scene."""

        with mock.patch.object(
            self.launcher, "_list_running", return_value={77: "wegame.exe"},
        ), mock.patch.object(
            self.launcher, "_probe_nikke_wegame_primary_action",
            return_value="clicked:wegame-primary:启动:game-started",
        ) as probe, mock.patch.object(self.launcher, "_notify") as notify, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", return_value=100.0,
        ):
            actions, outcome = self.launcher._drive_nikke_wegame_surface(
                observer=None, game_id="NIKKE", started_at=0.0,
                expected_names={"nikke.exe"}, actions=0, wait_started_at=0.0,
                cancel_requested=None,
            )
        self.assertEqual(actions, 1)
        self.assertTrue(outcome.startswith("clicked:wegame-primary:"))
        self.assertTrue(probe.call_args.kwargs["allow_action"])
        action_call = [call for call in notify.call_args_list if call.args[2] == "launcher-action"]
        self.assertTrue(action_call)
        self.assertIn("wegame.exe", action_call[-1].args[4])
        self.assertEqual(action_call[-1].kwargs["process_ids"], frozenset({77}))

    def test_nikke_wegame_action_budget_stops_further_clicks(self) -> None:
        with mock.patch.object(
            self.launcher, "_list_running", return_value={77: "wegame.exe"},
        ), mock.patch.object(
            self.launcher, "_probe_nikke_wegame_primary_action",
            return_value="no-effect:wegame-primary:audited-primary-action",
        ) as probe, mock.patch.object(self.launcher, "_notify"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", return_value=100.0,
        ):
            self.launcher._drive_nikke_wegame_surface(
                observer=None, game_id="NIKKE", started_at=0.0,
                expected_names={"nikke.exe"}, actions=self.launcher.NIKKE_WEGAME_MAX_ACTION_ACTIONS,
                wait_started_at=99.0, cancel_requested=None,
            )
        self.assertFalse(probe.call_args.kwargs["allow_action"])

    def test_nikke_wegame_human_outcomes_map_to_dedicated_reason_codes(self) -> None:
        with mock.patch.object(
            self.launcher, "_list_running", return_value={77: "wegame.exe"},
        ), mock.patch.object(
            self.launcher, "_probe_nikke_wegame_primary_action",
            return_value="human:ambiguous-wegame-window:2",
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", return_value=100.0,
        ):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._drive_nikke_wegame_surface(
                    observer=None, game_id="NIKKE", started_at=0.0,
                    expected_names={"nikke.exe"}, actions=0, wait_started_at=0.0,
                    cancel_requested=None,
                )
        self.assertEqual(caught.exception.reason_code, "nikke_wegame_ambiguous_wegame_window")
        self.assertEqual(caught.exception.process_ids, frozenset({77}))

    def test_off_screen_game_window_is_restored_instead_of_reported_ready(self) -> None:
        """A parked window must be re-placed before the official tool starts.

        Measured 2026-09-22: the ZZZ client sat at (-32000,-32000) while
        IsWindowVisible stayed true, and the official tool looped on "enter game"
        for 35 minutes; restoring the window let it continue immediately.
        """

        ready = (55, "ZenlessZoneZero.exe", 1936, 1119)
        clock = iter(range(4000))
        frames = iter([None] * 12 + [ready, ready])
        with mock.patch.object(
            self.launcher, "_list_running", return_value={55: "ZenlessZoneZero.exe"},
        ), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(
            self.launcher, "_find_window_handle", return_value=999,
        ), mock.patch.object(
            self.launcher, "_window_is_proven_blank", return_value=False,
        ), mock.patch.object(
            self.launcher, "_window_is_off_screen", side_effect=[True, False],
        ), mock.patch.object(
            self.launcher, "_restore_window_on_screen", return_value=True,
        ) as restore, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            return_value=False,
        ):
            result = self.launcher._wait_until_ready(
                "ZZZ", {"ZenlessZoneZero.exe"}, Path(self.temporary.name),
            )
        self.assertEqual(result, ready)
        self.assertTrue(restore.called)

    def test_blank_game_window_delays_readiness_and_gates_for_operator(self) -> None:
        """A window that renders nothing must never be reported ready.

        Measured 2026-09-21: NIKKE was declared ready on a black frame, the
        official tool started 13s later and every behavior-tree node failed.
        """

        ready = (55, "nikke.exe", 1920, 1080)
        clock = iter(range(4000))
        with mock.patch.object(
            self.launcher, "_list_running", return_value={55: "nikke.exe"},
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=ready,
        ), mock.patch.object(
            self.launcher, "_find_window_handle", return_value=4242,
        ), mock.patch.object(
            self.launcher, "_window_is_proven_blank", return_value=True,
        ), mock.patch.object(
            self.launcher, "READY_TIMEOUT_OVERRIDES", {"NIKKE": 30.0},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            return_value=False,
        ):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._wait_until_ready(
                    "NIKKE", {"nikke.exe"}, Path(self.temporary.name),
                )
        self.assertEqual(caught.exception.reason_code, "game_window_blank")

    def test_uncapturable_game_window_is_not_treated_as_blank(self) -> None:
        """Fail-open: a frame we cannot capture must never block a launch."""

        with mock.patch.object(
            type(self.launcher), "_capture_window_frame", return_value=None,
        ):
            self.assertFalse(self.launcher._window_is_proven_blank(1234))
        with mock.patch.object(
            type(self.launcher), "_capture_window_frame", side_effect=OSError("no dc"),
        ):
            self.assertFalse(self.launcher._window_is_proven_blank(1234))


    def test_ww_busy_status_does_not_human_gate_while_launcher_checks_files(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        ready = (33, "client-win64-shipping.exe", 1280, 720)
        clock = iter(range(1000))
        frames = iter([None] * 20 + [ready, ready])
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe", 33: ready[1]}), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="waiting:ww-busy") as action, mock.patch.object(
            self.launcher, "WW_LAUNCHER_UI_READY_SECONDS", 5,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
            result = self.launcher._wait_until_ready("WW", {"launcher_main.exe", ready[1]}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(result, ready)
        action.assert_called()

    def test_ww_transient_uia_probe_fault_is_retried_within_ui_ready_window(self) -> None:
        # A healthy launcher whose WebView tree is still rendering can raise a
        # transient UIA/COM read fault.  That is a technical fault, not a human
        # decision: it must be retried inside the UI-ready window instead of
        # stopping the whole queue at a human gate.
        launcher = Path(self.temporary.name) / "launcher.exe"
        ready = (33, "client-win64-shipping.exe", 1280, 720)
        clock = iter(range(1000))
        frames = iter([None] * 6 + [ready, ready])
        outcomes = iter(
            ["human:uia-probe-failed", "human:invalid-probe-result", "waiting:ww-busy"]
        )
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(
            self.launcher, "_probe_ww_launcher",
            side_effect=lambda *args, **kwargs: next(outcomes, "waiting:ww-busy"),
        ) as probe, mock.patch.object(
            self.launcher, "WW_LAUNCHER_POLL_SECONDS", 0,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
            result = self.launcher._wait_until_ready("WW", {"launcher_main.exe", ready[1]}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(result, ready)
        self.assertGreaterEqual(probe.call_count, 3)

    def test_ww_persistent_probe_fault_still_reaches_bounded_human_gate(self) -> None:
        # Retrying must stay bounded: a launcher that never settles still ends
        # at the normal human gate rather than polling forever.
        launcher = Path(self.temporary.name) / "launcher.exe"
        clock = iter(range(100000))
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_main.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None,
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="human:uia-probe-failed"), mock.patch.object(
            self.launcher, "WW_LAUNCHER_UI_READY_SECONDS", 5,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ), mock.patch("yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe", return_value=False):
            with self.assertRaises(GameLaunchHumanRequired) as caught:
                self.launcher._wait_until_ready("WW", {"launcher_main.exe"}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(caught.exception.reason_code, "ww_launcher_uia_probe_failed")

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

    def test_ww_formal_launcher_waits_for_update_then_returns_ready_without_input(self) -> None:
        launcher = Path(self.temporary.name) / "launcher.exe"
        ready = (33, "client-win64-shipping.exe", 1280, 720)
        clock = iter(range(1000))
        frames = iter([None])
        updates = iter([True, True])
        with mock.patch.object(self.launcher, "_list_ww_running", return_value={22: "launcher_updater.exe", 33: ready[1]}), mock.patch.object(
            self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready),
        ), mock.patch.object(self.launcher, "_probe_ww_launcher", return_value="waiting:ww-busy") as action, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            side_effect=lambda *args, **kwargs: next(updates, False),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ):
            result = self.launcher._wait_until_ready("WW", {"launcher_updater.exe", ready[1]}, launcher.parent, ww_launcher=launcher)
        self.assertEqual(result, ready)
        self.assertTrue(action.called)
        self.assertTrue(all(not call.kwargs["allow_invoke"] for call in action.call_args_list))

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


    def test_launcher_download_wait_stops_on_persisted_cancellation(self) -> None:
        launcher_root = Path(self.temporary.name) / "NTELauncher"
        launcher_root.mkdir()
        cancellation_checks = iter((False, True))
        with mock.patch.object(
            self.launcher, "_list_running", return_value={10: "NTEGame.exe"}
        ), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(
            self.launcher, "_run_launcher_probe"
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
        self.assertEqual(
            [item.phase for item in observations], ["launcher-action", "launch-cancelled"]
        )
        self.assertEqual(observations[0].detail["operation"], "launch-executable-started")
        self.assertEqual(observations[0].detail["processId"], 1234)
        self.assertEqual(
            observations[1].detail["reasonCode"], "manager_cancel_requested"
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
        self.assertEqual(classify(3, "not-ready:launcher-restore-requested;pid=10;accepted=True"), "restore-requested")
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
        self.assertIn("$selectors.Count -eq 1", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("$regions.Count -eq 0", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("InvokePattern", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("no-effect:launcher-primary-action", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("WindowFromPoint", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("blocked-by-foreign-window:", self.launcher._ENDFIELD_UIA_SCRIPT)
        self.assertIn("keybd_event(0x12", self.launcher._ENDFIELD_UIA_SCRIPT)

    def test_startup_delay_keeps_duration_and_honors_cancellation_without_input(self) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                clock = [0.0]
                def sleep(seconds):
                    clock[0] += seconds
                with mock.patch.object(self.launcher, "POLL_INTERVAL_SECONDS", 1), mock.patch.object(
                    self.launcher, "_list_running", return_value={222: "PGR.exe"}
                ), mock.patch(
                    "yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: clock[0]
                ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep", side_effect=sleep), mock.patch(
                    "yeyu_gamer_manager.services.game_launcher.ctypes.WinDLL"
                ) as native:
                    if cancel:
                        with self.assertRaises(GameLaunchCancelled):
                            self.launcher._handoff_started_game("PGR", 222, cancel_requested=lambda: clock[0] >= 3)
                    else:
                        self.launcher._handoff_started_game("PGR", 222)
                self.assertEqual(clock[0], 3 if cancel else 75)
                native.assert_not_called()

    def test_endfield_observation_reports_the_start_action_deadline_that_actually_expires(self) -> None:
        clock = [0.0]
        observations = []
        def sleep(seconds):
            clock[0] += seconds
        self.launcher.READY_TIMEOUT_OVERRIDES = {"Endfield": 20.0}
        self.launcher.LAUNCHER_UI_READY_TIMEOUT_SECONDS = 5.0
        self.launcher.LAUNCH_OBSERVATION_INTERVAL_SECONDS = 1.0
        self.launcher.ENDFIELD_LAUNCHER_POLL_SECONDS = 1.0
        self.launcher.POLL_INTERVAL_SECONDS = 1.0
        with mock.patch.object(self.launcher, "_list_running", return_value={10: "Games.exe"}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(self.launcher, "_drive_endfield_launcher", return_value="not-ready"), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: clock[0]
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(GameLaunchError, "start-action wait expired"):
                self.launcher._wait_until_ready(
                    "Endfield", {"games.exe", "endfield.exe"}, self.executable.parent,
                    observer=observations.append,
                )
        self.assertEqual(clock[0], 5.0)
        self.assertEqual([item.detail["secondsUntilDeadline"] for item in observations], [4.0, 3.0, 2.0, 1.0])
        self.assertTrue(all(item.detail["deadlineTrigger"] == "Endfield launcher start-action wait timeout"
                            for item in observations))
        self.assertTrue(all("start/update action" in item.detail["waitingFor"] for item in observations))

    def test_endfield_disappeared_process_still_reports_wait_without_relaunch(self) -> None:
        import itertools
        observations = []
        self.launcher.ENDFIELD_REACTIVATION_READY_SECONDS = 1
        self.launcher.LAUNCH_OBSERVATION_INTERVAL_SECONDS = 0.1
        with mock.patch.object(self.launcher, "_list_running", return_value={}), mock.patch.object(
            self.launcher, "_find_ready_window", return_value=None
        ), mock.patch.object(self.launcher, "_drive_endfield_launcher") as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=itertools.count(0, 0.05)
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.sleep"):
            with self.assertRaises(GameLaunchHumanRequired):
                self.launcher._wait_until_ready(
                    "Endfield", {"games.exe", "endfield.exe"}, observer=observations.append,
                    endfield_reactivation=(self.executable.parent, frozenset({10})),
                )
        self.assertTrue(observations)
        self.assertTrue(all(item.phase == "launcher-waiting" for item in observations))
        self.assertTrue(all(item.process_ids == frozenset() for item in observations))
        self.assertIn("window", observations[-1].detail["waitingFor"])
        drive.assert_not_called()

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

    def test_endfield_headless_reactivation_ignores_unrelated_wegame_launcher(self) -> None:
        launcher = Path(self.temporary.name) / "Hypergryph Launcher" / "Launcher.exe"
        launcher.parent.mkdir()
        launcher.write_bytes(b"fixture")
        with mock.patch.object(self.launcher, "_resolve_endfield_launcher", return_value=launcher), mock.patch.object(
            self.launcher, "_list_running", side_effect=[{10: "Games.exe", 99: "launcher.exe"}, {}],
        ), mock.patch.object(self.launcher, "_run_launcher_probe", return_value=subprocess.CompletedProcess([], 0, "trusted:headless", "")) as probe, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen", return_value=mock.Mock(pid=11),
        ) as popen, mock.patch.object(self.launcher, "_wait_until_ready", return_value=(20, "endfield.exe", 1920, 1080)) as wait, mock.patch.object(
            self.launcher, "close_started",
        ) as close:
            receipt = self.launcher.ensure_started("Endfield", str(self.executable))
        self.assertEqual("10", probe.call_args.kwargs["environment"]["YEYU_ENDFIELD_REACTIVATE_PID"])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(launcher), "--game=endfield", "--reason=4"])
        self.assertEqual(wait.call_args.kwargs["endfield_reactivation"], (self.executable.parent, frozenset({10, 11})))
        self.assertEqual(receipt.baseline_process_ids, (10, 99))
        self.assertEqual(receipt.state, "started")
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

    def test_endfield_reactivation_waits_for_update_then_returns_ready_without_input(self) -> None:
        ready = (20, "endfield.exe", 1920, 1080)
        clock = iter(range(1000))
        frames = iter([None])
        updates = iter([True, True])
        with mock.patch.object(self.launcher, "_list_running", return_value={10: "Games.exe", 20: "Endfield.exe"}), mock.patch.object(
            self.launcher, "_process_executable_path", return_value=self.executable.parent / "Endfield.exe",
        ), mock.patch.object(self.launcher, "_find_ready_window", side_effect=lambda *args: next(frames, ready)), mock.patch.object(
            self.launcher, "_drive_endfield_launcher",
        ) as drive, mock.patch(
            "yeyu_gamer_manager.services.game_launcher._LaunchProgressMonitor.observe",
            side_effect=lambda *args, **kwargs: next(updates, False),
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.time.monotonic", side_effect=lambda: float(next(clock))), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.time.sleep",
        ):
            result = self.launcher._wait_until_ready("Endfield", {"games.exe", "endfield.exe"}, self.executable.parent,
                endfield_reactivation=(self.executable.parent, frozenset({10, 11})))
        self.assertEqual(result, ready)
        drive.assert_not_called()

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

    @unittest.skipUnless(os.name == "nt", "production identity predicate uses Windows PowerShell")
    def test_endfield_identity_diagnostics_preserve_each_filter(self) -> None:
        source = self.launcher._ENDFIELD_UIA_SCRIPT
        predicate = source[source.index("$expected ="):source.index("$process = Get-Process")]
        script = Path(self.temporary.name) / "identity-probe.ps1"
        script.write_text(
            "function Get-CimInstance { (ConvertFrom-Json $env:YEYU_TEST_PROCESSES) | ForEach-Object { $_ } }\n"
            + predicate + "\nWrite-Output 'matched'; exit 0\n", encoding="utf-8-sig",
        )
        root = Path(self.temporary.name) / "Hypergryph Launcher"
        valid = {"ExecutablePath": str(root / "1.5.0" / "Games.exe"),
                 "CommandLine": 'Games.exe --game=Endfield --region=CN'}
        cases = [
            ([], 3, "observed=0;pathMatched=0;gameMatched=0;regionCompatible=0"),
            ([{**valid, "ExecutablePath": ""}], 3, "observed=1;pathMatched=0;gameMatched=0;regionCompatible=0"),
            ([{**valid, "CommandLine": "Games.exe --game=Other --region=CN"}], 3, "gameMatched=0;regionCompatible=0"),
            ([{**valid, "CommandLine": "Games.exe --game=endfield --reason=4"}], 0, "matched"),
            ([{**valid, "CommandLine": "Games.exe --game=Endfield --region=US"}], 3, "gameMatched=1;regionCompatible=0"),
            ([{**valid, "CommandLine": "Games.exe --game=Endfield --region=CN --region=CN"}], 3, "regionCompatible=0"),
            ([{**valid, "CommandLine": "Games.exe --game=Endfield --game=Other"}], 3, "gameMatched=0"),
            ([valid, valid], 3, "games-exe-candidates=2"),
            ([valid], 0, "matched"),
        ]
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        for processes, exit_code, expected in cases:
            with self.subTest(expected=expected):
                result = subprocess.run(
                    [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script.read_text(encoding="utf-8-sig")],
                    env={**os.environ, "YEYU_ENDFIELD_LAUNCHER_ROOT": str(root),
                         "YEYU_TEST_PROCESSES": json.dumps(processes)},
                    capture_output=True, text=True, errors="replace", timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertIn(expected, result.stdout)

    @unittest.skipUnless(os.name == "nt", "production click result uses Windows PowerShell")
    def test_endfield_uia_click_reports_whether_input_was_dispatched(self) -> None:
        source = self.launcher._ENDFIELD_UIA_SCRIPT
        start = source.index("    $clicked = Invoke-YeYuPhysicalClick", source.index("foreach ($button"))
        end = source.index("\n}\nif ($env:YEYU_ENDFIELD_ALLOW_FALLBACK", start)
        result_branch = source[start:end]
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        for value, changed, code, expected in (
            ("$null", False, 3, "not-ready:foreground-not-acquired;requester=fixture"),
            ("$null", True, 3, "not-ready:foreground-not-acquired;requester=fixture"),
            ("$false", False, 4, "no-effect:start"),
            ("$true", False, 0, "clicked:start:game-started"),
        ):
            with self.subTest(value=value, changed=changed):
                # Execute the production result branch without loading input APIs.
                # An unrelated button change after foreground denial must not
                # become a claim that this probe clicked the button.
                script = (
                    f"function Invoke-YeYuPhysicalClick {{ return {value} }}\n"
                    "$script:lastForegroundProbe='requester=fixture'\n"
                    "$window=0; $process=@{Id=1}; $x=0; $y=0; $name='start'; $initialEnabled=$true\n"
                    f"$button=@{{Current=@{{Name='{('changed' if changed else 'start')}';IsEnabled=$true}}}}\n"
                    + result_branch
                )
                result = subprocess.run(
                    [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, text=True, errors="replace", timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_endfield_bridge_rejects_non_launcher_root(self) -> None:
        with self.assertRaises(GameLaunchError):
            self.launcher._drive_endfield_launcher(Path(self.temporary.name))

    def test_existing_endfield_wait_cancellation_never_recycles_client(self) -> None:
        with mock.patch.object(self.launcher, "_resolve_endfield_launcher", return_value=self.executable), mock.patch.object(
            self.launcher, "_list_running", return_value={10: "Endfield.exe"},
        ), mock.patch.object(self.launcher, "_wait_until_ready", side_effect=GameLaunchCancelled("test cancellation")), mock.patch.object(
            self.launcher, "_request_graceful_close",
        ) as close, mock.patch.object(self.launcher, "_terminate_owned") as terminate, mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
        ) as start:
            with self.assertRaises(GameLaunchCancelled):
                self.launcher.ensure_started("Endfield", str(self.executable))
        close.assert_not_called()
        terminate.assert_not_called()
        start.assert_not_called()


    def test_new_pgr_reports_configured_startup_wait_before_adapter(self) -> None:
        observations = []
        process = mock.Mock(pid=1234)
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch(
            # The leftover-instance guard reads real machine state; this test is
            # about the startup-wait reporting, so keep it hermetic.
            "yeyu_gamer_manager.services.game_launcher.named_mutex_is_held",
            return_value=False,
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
            receipt = self.launcher.ensure_started("PGR", str(self.executable), observer=observations.append)

        self.assertEqual(receipt.state, "started")
        handoff.assert_called_once_with("PGR", 1234)
        self.assertEqual(
            [item.phase for item in observations],
            ["launcher-action", "client-startup-wait", "ready"],
        )
        self.assertEqual(observations[0].detail["operation"], "launch-executable-started")
        self.assertEqual(observations[0].detail["processId"], 1234)
        self.assertEqual(observations[1].process_ids, frozenset({1234}))
        self.assertEqual(observations[1].detail["requiredWaitSeconds"], 75)
        self.assertIn("official Adapter dispatch", observations[1].detail["waitingFor"])

    def test_pgr_launch_does_not_rewrite_unity_window_preferences(self) -> None:
        self.assertEqual(self.launcher._prepare_pgr_window_preferences(), {})

    def test_pgr_launch_without_preferences_key_is_a_noop(self) -> None:
        with mock.patch.object(
            type(self.launcher), "PGR_PLAYER_PREFS_KEY", r"Software\YeYuGamerTests\missing-" + uuid.uuid4().hex
        ):
            self.assertEqual(self.launcher._prepare_pgr_window_preferences(), {})


    def test_spawned_process_is_recorded_even_when_it_dies_before_any_window(self) -> None:
        """The spawn must be attributable from the run log alone.

        Measured 2026-09-22: `PGR.exe` was spawned by `YeYuGamer.exe` at
        08:20:24 and exited within the same second, while every following
        ``launch.launcher-waiting`` sample reported ``pids=[]`` for the whole
        300s window.  Proving a process had been started at all needed
        enumeration outside the Manager, so a client that dies before it can
        write a single log line must still leave a spawn record with its pid.
        """

        process = mock.Mock(pid=36880)
        observations = []
        with mock.patch(
            "yeyu_gamer_manager.services.game_launcher.os.name", "nt"
        ), mock.patch(
            # Same as above: the leftover-instance guard would read the real
            # (occupied) PGR mutex; this test only asserts the spawn record.
            "yeyu_gamer_manager.services.game_launcher.named_mutex_is_held",
            return_value=False,
        ), mock.patch.object(
            self.launcher, "_list_running", return_value={},
        ), mock.patch(
            "yeyu_gamer_manager.services.game_launcher.subprocess.Popen",
            return_value=process,
        ), mock.patch.object(
            self.launcher,
            "_wait_until_ready",
            side_effect=GameLaunchError("fixture: client window never appeared"),
        ), mock.patch.object(self.launcher, "close_started"):
            with self.assertRaises(GameLaunchError):
                self.launcher.ensure_started(
                    "PGR", str(self.executable), observer=observations.append
                )

        spawns = [
            item for item in observations
            if item.detail.get("operation") == "launch-executable-started"
        ]
        self.assertEqual(len(spawns), 1, [item.phase for item in observations])
        self.assertEqual(spawns[0].phase, "launcher-action")
        self.assertEqual(spawns[0].detail["processId"], 36880)
        self.assertEqual(spawns[0].detail["executable"], self.executable.name)
        self.assertEqual(spawns[0].process_ids, frozenset({36880}))

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
        ) as run, mock.patch.object(
            # The subject is name filtering, and pid 2 is arbitrary.  Liveness is
            # pinned because `_process_is_live` now reports a nonexistent id as
            # not live (see the sibling test below, which exercises that).
            type(self.launcher), "_process_is_live", return_value=True,
        ):
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
        ), mock.patch.object(
            # The subject is "a *live* enumerated residual must not be reported as
            # already-closed", so the fixture pins liveness; the pid itself is
            # arbitrary and does not exist on the host.
            GameLaunchService, "_process_is_live", return_value=True,
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
        # Every test in this class drives queue-close and path-identity rules
        # through fixtures whose pids (42, 10, 20, ...) do not exist on the host.
        # `_process_is_live` now answers "not live" for an id Windows reports as
        # nonexistent -- the fix for a completed StarRail daily being recorded as
        # `game_cleanup_failed` -- so liveness is pinned here to keep each test's
        # subject its own rule rather than the host's pid table.  Tests that care
        # about liveness override this patch themselves.
        liveness = mock.patch.object(
            GameLaunchService, "_process_is_live", return_value=True
        )
        liveness.start()
        self.addCleanup(liveness.stop)

    def test_preexisting_authorized_client_closes_gracefully_with_memory_observations(self) -> None:
        present = ({42: self.identity}, set())
        before = {"availablePhysicalBytes": 100, "availableCommitBytes": 200}
        after = {"availablePhysicalBytes": 150, "availableCommitBytes": 250}
        with mock.patch.object(self.launcher, "_queue_close_snapshot", side_effect=[present, present, ({}, set()), ({}, set())]), mock.patch.object(
            self.launcher, "_close_verified_queue_processes", return_value=(),
        ) as close, mock.patch.object(self.launcher, "_available_memory", side_effect=[before, after]):
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        self.assertEqual(result.state, "closed")
        self.assertEqual(result.requested_process_ids, (42,))
        self.assertEqual(result.remaining_process_ids, ())
        close.assert_called_once_with({42: self.identity}, force=False, cancel_requested=None, on_close=None)
        self.assertEqual(result.as_result()["memoryBefore"], before)
        self.assertEqual(result.as_result()["memoryAfter"], after)
        self.assertNotIn("releasedBytes", result.as_result())

    def test_escalation_is_bounded_and_rechecks_actual_enumeration(self) -> None:
        closed = False
        stages: list[bool] = []
        def close(_targets, *, force, cancel_requested, on_close=None):
            nonlocal closed
            stages.append(force)
            closed = force
            return ()
        with mock.patch.object(self.launcher, "_queue_close_snapshot", side_effect=lambda *_: ({}, set()) if closed else ({42: self.identity}, set())), mock.patch.object(
            self.launcher, "_close_verified_queue_processes", side_effect=close,
        ):
            result = self.launcher.close_for_queue("StarRail", str(self.executable))
        self.assertEqual(stages, [False, False, True])
        self.assertEqual(result.state, "closed")

    def test_successful_termination_request_does_not_hide_remaining_process(self) -> None:
        with mock.patch.object(self.launcher, "_queue_close_snapshot", return_value=({42: self.identity}, set())), mock.patch.object(
            self.launcher, "_close_verified_queue_processes", return_value=(),
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
        ), mock.patch.object(
            # Fixture pids are arbitrary; pin liveness so the subject under test
            # stays the path-identity rule rather than Windows' answer for an id
            # that happens not to exist.
            GameLaunchService, "_process_is_live", return_value=True,
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
        replacement = _QueueProcessIdentity(self.executable, self.identity.created_at + 1)
        for force, observed in ((False, replacement), (True, replacement), (True, None)):
            with self.subTest(force=force, identity=observed):
                api = mock.Mock()
                api.OpenProcess.return_value = 99
                with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
                    self.launcher, "_queue_identity_from_handle", return_value=observed,
                ), mock.patch.object(self.launcher, "_request_graceful_close") as graceful:
                    report = self.launcher._close_verified_queue_processes({42: self.identity}, force=force, cancel_requested=None)
                self.assertEqual(report[0]["outcome"], "identity-unavailable" if observed is None else "identity-mismatch")
                graceful.assert_not_called()
                api.TerminateProcess.assert_not_called()
                api.WaitForSingleObject.assert_not_called()
                api.CloseHandle.assert_called_once_with(99)

    def test_force_uses_verified_handle_and_does_not_spawn_taskkill(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 99
        api.TerminateProcess.return_value = 1
        api.WaitForSingleObject.return_value = 258
        audit = []
        def record(item):
            audit.append(item)
            self.assertEqual(api.TerminateProcess.called, item["auditPhase"] == "result")
        with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
            self.launcher, "_queue_identity_from_handle", return_value=self.identity,
        ), mock.patch("yeyu_gamer_manager.services.game_launcher.subprocess.run") as run:
            self.launcher._close_verified_queue_processes({42: self.identity}, force=True, cancel_requested=None, on_close=record)
        api.TerminateProcess.assert_called_once_with(99, 1)
        api.OpenProcess.assert_called_once_with(0x101001, False, 42)
        self.assertEqual(api.WaitForSingleObject.call_args_list, [mock.call(99, 0), mock.call(99, 0)])
        api.CloseHandle.assert_called_once_with(99)
        run.assert_not_called()
        self.assertEqual([item["auditPhase"] for item in audit], ["requested", "result"])
        self.assertEqual(audit[0]["processId"], 42)
        self.assertEqual(audit[-1]["waitAfter"]["state"], "not-signaled")

    def test_failed_close_audit_prevents_action_and_releases_handle(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 99
        with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
            self.launcher, "_queue_identity_from_handle", return_value=self.identity,
        ), self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.launcher._close_verified_queue_processes(
                {42: self.identity}, force=True, cancel_requested=None,
                on_close=mock.Mock(side_effect=RuntimeError("audit unavailable")),
            )
        api.TerminateProcess.assert_not_called()
        api.CloseHandle.assert_called_once_with(99)

    def test_native_termination_result_and_wait_cannot_hide_enumerated_residual(self) -> None:
        for returned, wait_value in ((0, 258), (1, 258), (1, 0)):
            with self.subTest(terminate_return=returned, wait_value=wait_value):
                api = mock.Mock()
                api.OpenProcess.return_value = 99
                def terminate(*_):
                    ctypes.set_last_error(5)
                    return returned
                def wait(*_):
                    # A subsequent call changes last error; failure must already
                    # have been captured, and success must ignore stale errors.
                    ctypes.set_last_error(87)
                    return wait_value
                api.TerminateProcess.side_effect = terminate
                api.WaitForSingleObject.side_effect = wait
                with mock.patch.object(self.launcher, "_queue_close_snapshot", return_value=({42: self.identity}, set())), mock.patch.object(
                    self.launcher, "_queue_process_api", return_value=api,
                ), mock.patch.object(self.launcher, "_queue_identity_from_handle", return_value=self.identity), mock.patch.object(
                    self.launcher, "_request_graceful_close",
                ):
                    receipt = self.launcher.close_for_queue("StarRail", str(self.executable))
                self.assertEqual(receipt.state, "close-failed")
                self.assertEqual(receipt.remaining_process_ids, (42,))
                observations = receipt.as_result()["processCloseObservations"]
                self.assertEqual([item["phase"] for item in observations], ["wm-close", "wm-close", "terminate"])
                last = observations[-1]
                self.assertEqual(last["terminateProcess"], {"apiReturn": returned, "succeeded": bool(returned), "winError": None if returned else 5})
                self.assertEqual(last["outcome"], "termination-request-accepted" if returned else "termination-request-failed")
                self.assertEqual(last["waitAfter"], {"value": wait_value, "state": "signaled" if wait_value == 0 else "not-signaled", "winError": None})
                api.TerminateProcess.assert_called_once_with(99, 1)
                self.assertEqual(api.WaitForSingleObject.call_args_list, [mock.call(99, 0), mock.call(99, 0)])
                self.assertEqual(api.CloseHandle.call_count, 3)

    def test_native_open_failure_records_error_without_dispatch(self) -> None:
        api = mock.Mock()
        def denied(*_):
            ctypes.set_last_error(5)
            return 0
        api.OpenProcess.side_effect = denied
        with mock.patch.object(self.launcher, "_queue_process_api", return_value=api):
            report = self.launcher._close_verified_queue_processes({42: self.identity}, force=True, cancel_requested=None)
        self.assertEqual(report[0]["outcome"], "open-process-failed")
        self.assertEqual(report[0]["openProcess"], {"accessMask": 0x101001, "succeeded": False, "winError": 5})
        api.TerminateProcess.assert_not_called()
        api.WaitForSingleObject.assert_not_called()
        api.CloseHandle.assert_not_called()

    def test_native_wait_failure_is_explicit_and_does_not_add_wait_time(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 99
        api.TerminateProcess.return_value = 1
        def wait_failed(*_):
            ctypes.set_last_error(6)
            return 0xFFFFFFFF
        api.WaitForSingleObject.side_effect = wait_failed
        with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
            self.launcher, "_queue_identity_from_handle", return_value=self.identity,
        ):
            report = self.launcher._close_verified_queue_processes({42: self.identity}, force=True, cancel_requested=None)
        self.assertEqual(report[0]["waitBefore"], {"value": 0xFFFFFFFF, "state": "failed", "winError": 6})
        self.assertEqual(report[0]["waitAfter"], report[0]["waitBefore"])
        self.assertEqual(api.WaitForSingleObject.call_args_list, [mock.call(99, 0), mock.call(99, 0)])
        api.CloseHandle.assert_called_once_with(99)

    def test_cancel_or_human_during_wait_observation_prevents_termination(self) -> None:
        for exception in (GameLaunchCancelled("cancelled"), GameLaunchHumanRequired("login", "preserve")):
            with self.subTest(kind=type(exception).__name__):
                pending = False
                api = mock.Mock()
                api.OpenProcess.return_value = 99
                def wait(*_):
                    nonlocal pending
                    pending = True
                    return 258
                def cancelled():
                    if pending:
                        raise exception
                    return False
                api.WaitForSingleObject.side_effect = wait
                with mock.patch.object(self.launcher, "_queue_process_api", return_value=api), mock.patch.object(
                    self.launcher, "_queue_identity_from_handle", return_value=self.identity,
                ):
                    with self.assertRaises(type(exception)) as caught:
                        self.launcher._close_verified_queue_processes({42: self.identity}, force=True, cancel_requested=cancelled)
                self.assertIs(caught.exception, exception)
                api.TerminateProcess.assert_not_called()
                api.CloseHandle.assert_called_once_with(99)

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
            return ()
        with mock.patch.object(GameLaunchService, "_list_enumerated", side_effect=lambda _: dict(names)), mock.patch.object(
            GameLaunchService, "_queue_process_identity", side_effect=identities.get,
        ), mock.patch.object(
            # The fixture's pids are arbitrary, so liveness is pinned: this test
            # is about a live sibling surviving its bootstrap's exit, not about
            # how Windows reports a nonexistent id.
            GameLaunchService, "_process_is_live", return_value=True,
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
            return ()
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
        ), mock.patch.object(GameLaunchService, "_process_executable_path", return_value=None), mock.patch.object(
            # This case is about an *unreadable image path*, so the process is
            # pinned live explicitly: `_process_is_live` now answers "not live"
            # for an id Windows says does not exist, and letting the fixture's
            # arbitrary pid 42 decide that would test the wrong thing.
            GameLaunchService, "_process_is_live", return_value=True,
        ), mock.patch.object(self.launcher, "_request_graceful_close") as close:
            result = self.launcher.close_started(receipt)
        self.assertEqual(result.state, "close-failed")
        self.assertEqual(result.remaining_process_ids, (42,))
        close.assert_not_called()
