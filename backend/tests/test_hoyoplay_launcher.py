from __future__ import annotations

from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from unittest import mock

from yeyu_gamer_manager.services.game_launcher import GameLaunchCancelled, GameLaunchError
from yeyu_gamer_manager.services.hoyoplay_launcher import HoYoPlayLauncher, LauncherSnapshot


class HoYoPlayLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = mock.Mock()
        self.notify = mock.Mock()
        self.cancel = mock.Mock(return_value=False)
        self.snapshot = LauncherSnapshot(
            "ready", 100.0, 71, 81, r"D:\fixture\ZenlessZoneZero.exe",
            frozenset({"check", "update", "verify"}),
        )
        self.effects = ExitStack()
        self.addCleanup(self.effects.close)
        for target in (
            "subprocess.Popen", "subprocess.run", "os.startfile",
            "ctypes.WinDLL", "socket.socket", "pathlib.Path.read_text",
            "pathlib.Path.read_bytes", "pathlib.Path.resolve",
        ):
            self.effects.enter_context(mock.patch(
                target, side_effect=AssertionError("unexpected live effect"), create=True,
            ))
        self.driver = HoYoPlayLauncher(
            self.service, Path(self.snapshot.game_path), self.notify, self.cancel,
        )

    def tearDown(self) -> None:
        self.assertEqual(self.service.mock_calls, [])
        self.notify.assert_not_called()

    def assertRejected(self, operation, reason) -> None:
        with self.assertRaises(GameLaunchError) as raised:
            operation()
        self.assertEqual(raised.exception.reason_code, "zzz_launcher_" + reason)
        self.assertEqual(raised.exception.process_ids, frozenset())
        self.assertIsNone(self.driver.session)
        self.assertIsNone(self.driver._snapshot)

    def test_constructor_has_no_effects_and_does_not_consult_service(self) -> None:
        self.cancel.assert_not_called()

    def test_prepare_rejects_before_any_live_effect(self) -> None:
        self.assertRejected(self.driver.prepare, "native_context_missing")

    def test_observe_does_not_create_a_synthetic_success(self) -> None:
        self.assertRejected(self.driver.observe, "session_not_prepared")

    def test_each_permitted_action_still_requires_a_real_contract(self) -> None:
        for action in ("check", "update", "verify"):
            with self.subTest(action=action):
                self.assertRejected(lambda: self.driver.invoke(action, self.snapshot), "stale_observation")

    def test_enter_game_and_unknown_actions_are_forbidden(self) -> None:
        for action in ("start", "launch", "enter-game", "", "repair", "UPDATE"):
            with self.subTest(action=action), self.assertRaises(GameLaunchError) as raised:
                self.driver.invoke(action, self.snapshot)
            self.assertEqual(raised.exception.reason_code, "zzz_launcher_action_forbidden")

    def test_claimed_fixture_profile_on_service_cannot_enable_production(self) -> None:
        self.service.hoyoplay_ui_profile = {"qualified": True}
        self.service.hoyoplay_driver = mock.Mock()
        self.assertRejected(self.driver.prepare, "native_context_missing")

    def test_repeated_prepare_never_advances_to_prepared(self) -> None:
        self.assertRejected(self.driver.prepare, "native_context_missing")
        self.assertRejected(self.driver.prepare, "native_context_missing")
        self.assertRejected(self.driver.observe, "session_not_prepared")

    def test_fresh_caller_snapshot_does_not_authorize_native_input(self) -> None:
        with mock.patch("yeyu_gamer_manager.services.hoyoplay_launcher.time.monotonic", return_value=100.0):
            for action in ("check", "update", "verify"):
                with self.subTest(action=action):
                    self.assertRejected(lambda: self.driver.invoke(action, self.snapshot), "stale_observation")
        self.assertEqual(self.driver._counts, {"check": 0, "verify": 0, "update": 0})

    def test_cancelled_entry_points_stop_without_live_effects(self) -> None:
        self.cancel.return_value = True
        for operation in (
            self.driver.prepare, self.driver.observe,
            lambda: self.driver.invoke("update", self.snapshot),
        ):
            with self.subTest(operation=operation), self.assertRaises(GameLaunchCancelled):
                operation()

    def test_cancellation_reader_failure_propagates_without_fallback(self) -> None:
        self.cancel.side_effect = RuntimeError("fixture cancellation unavailable")
        with self.assertRaisesRegex(RuntimeError, "fixture cancellation unavailable"):
            self.driver.prepare()

    def test_snapshot_is_immutable_and_preserves_observation_fields(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.snapshot.state = "up-to-date"
        self.assertEqual(self.snapshot.process_id, 71)
        self.assertEqual(self.snapshot.created_at, 81)
        self.assertEqual(self.snapshot.observed_at, 100.0)
        self.assertIsNone(self.snapshot.progress)


if __name__ == "__main__":
    unittest.main()
