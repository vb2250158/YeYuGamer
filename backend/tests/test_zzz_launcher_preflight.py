from __future__ import annotations
import ast
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from yeyu_gamer_manager.services import game_launcher as launch
from yeyu_gamer_manager.services import zzz_launcher_preflight as module
from yeyu_gamer_manager.services.hoyoplay_launcher import LauncherSnapshot, HoYoPlayLauncher

GAME = Path("D:/fixture/ZenlessZoneZero.exe")
BASE_TIME = datetime(2026, 10, 23, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.value = 0.0
    def monotonic(self): return self.value
    def now(self): return BASE_TIME + timedelta(seconds=self.value)
    def sleep(self, value): self.value += value


class Driver:
    def __init__(self, clock, states):
        self.clock, self.states = clock, list(states)
        self.calls, self.prepared = [], False
        self.index = 0
        self.change = None
    def prepare(self): self.prepared = True
    def observe(self):
        item = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        if isinstance(item, Exception): raise item
        state, progress = item if isinstance(item, tuple) else (item, None)
        snapshot = LauncherSnapshot(state, self.clock.value, 71, 81, str(GAME),
                                    frozenset({"check", "update", "verify"}), progress)
        return self.change(snapshot) if self.change else snapshot
    def invoke(self, action, snapshot):
        self.calls.append(action)


class PreflightTests(unittest.TestCase):
    def make(self, states, **overrides):
        clock = Clock()
        driver = Driver(clock, states)
        events = []
        options = dict(cancel_requested=lambda: False, authority_valid=lambda: True,
                       deadline_at=BASE_TIME + timedelta(hours=2),
                       notify=lambda *args: events.append(args),
                       clock=clock.monotonic, utc_now=clock.now, sleep=clock.sleep)
        options.update(overrides)
        gate = module.ZzzLauncherPreflight(driver, GAME, **options)
        return gate, driver, clock, events

    def assertReason(self, gate, reason):
        with self.assertRaises(launch.GameLaunchError) as raised:
            gate.run()
        self.assertEqual(raised.exception.reason_code, reason)
        return raised.exception

    def test_equal_version_still_runs_official_check_and_integrity(self):
        gate, driver, _, _ = self.make(["ready", "checking", "ready", "verifying", "ready"])
        result = gate.run()
        self.assertEqual(driver.calls, ["check", "verify"])
        self.assertEqual(result["state"], "launcher-verified")

    def test_update_rechecks_and_verifies_before_handoff(self):
        gate, driver, _, _ = self.make(["update-available", ("downloading", ("bytes", 100)),
            ("downloading", ("bytes", 200)), "installing", "ready", "checking", "ready", "verifying", "ready"])
        self.assertEqual(gate.run()["updates"], 1)
        self.assertEqual(driver.calls, ["update", "check", "verify"])

    def test_version_current_notice_only_satisfies_check_not_integrity(self):
        gate, driver, _, _ = self.make(["ready", "checking", "up-to-date", "verifying", "verification-complete", "ready"])
        gate.run()
        self.assertEqual(driver.calls, ["check", "verify"])

    def test_old_completion_labels_cannot_prove_no_effect_actions(self):
        for states in (["up-to-date", "up-to-date", "verification-complete", "ready"],
                       ["ready", "checking", "ready", "verification-complete", "ready"]):
            gate, driver, _, _ = self.make(states)
            self.assertReason(gate, "zzz_launcher_progress_stalled")
            self.assertLessEqual(len(driver.calls), 2)

    def test_verification_that_requires_update_is_verified_again(self):
        gate, driver, _, _ = self.make(["ready", ("checking", ("files", 50)), "ready", ("verifying", ("files", 50)),
            "update-available", "downloading", "ready", ("checking", ("files", 0)), "ready", ("verifying", ("files", 0)), "ready"])
        result = gate.run()
        self.assertEqual(driver.calls, ["check", "verify", "update", "check", "verify"])
        self.assertEqual(result["verifications"], 2)

    def test_unchanged_ready_does_not_fabricate_completed_check(self):
        gate, driver, _, events = self.make(["ready"])
        self.assertReason(gate, "zzz_launcher_progress_stalled")
        self.assertEqual(driver.calls, ["check"])
        self.assertLessEqual(len(events), 16)

    def test_no_version_field_or_process_writes_can_skip_gate(self):
        gate, driver, _, _ = self.make(["ready", "ready"])
        driver.local_version = driver.official_version = "3.2.0"
        driver.bytes_written = 10**12
        self.assertReason(gate, "zzz_launcher_progress_stalled")
        self.assertEqual(driver.calls, ["check"])

    def test_stop_before_prepare_has_no_driver_effect(self):
        gate, driver, _, _ = self.make(["ready"], cancel_requested=lambda: True)
        self.assertReason(gate, "manager_cancel_requested")
        self.assertFalse(driver.prepared)

    def test_generation_change_after_observe_prevents_action(self):
        gate, driver, _, _ = self.make(["ready"])
        driver.change = lambda item: (setattr(gate, "cancel_requested", lambda: True) or item)
        self.assertReason(gate, "manager_cancel_requested")
        self.assertEqual(driver.calls, [])

    def test_expiry_is_not_user_stop(self):
        gate, driver, _, _ = self.make(["waiting"], deadline_at=BASE_TIME + timedelta(seconds=1))
        self.assertReason(gate, "zzz_client_update_deadline_expired")
        self.assertEqual(driver.calls, [])

    def test_hard_cap_is_not_reset_by_real_progress(self):
        gate, driver, clock, _ = self.make(["downloading"])
        driver.change = lambda item: replace(item, progress=("bytes", int(clock.value)))
        with mock.patch.object(module, "UPDATE_HARD_CAP_SECONDS", 3):
            self.assertReason(gate, "zzz_client_update_hard_timeout")

    def test_deadline_callback_is_pure_boolean_and_retains_reason(self):
        gate, _, clock, _ = self.make(["ready"], deadline_at=BASE_TIME + timedelta(seconds=1))
        clock.value = 2
        self.assertTrue(gate.interrupted())
        self.assertEqual(gate.interruption, "zzz_client_update_deadline_expired")

    def test_authority_fault_is_closed_and_callback_does_not_throw(self):
        def broken(): raise RuntimeError("store unavailable")
        gate, driver, _, _ = self.make(["ready"], authority_valid=broken)
        self.assertTrue(gate.interrupted())
        self.assertReason(gate, "zzz_client_update_authority_unavailable")
        self.assertFalse(driver.prepared)

    def test_missing_or_revoked_authority_does_not_prepare(self):
        for value in (None, lambda: False):
            gate, driver, _, _ = self.make(["ready"], authority_valid=value)
            self.assertReason(gate, "zzz_client_update_authority_lost")
            self.assertFalse(driver.prepared)

    def test_deadline_requires_timezone(self):
        gate, _, _, _ = self.make(["ready"], deadline_at=datetime(2026, 10, 23))
        self.assertReason(gate, "zzz_client_update_deadline_missing")

    def test_unknown_dialog_never_invokes_button_behind_it(self):
        gate, driver, _, _ = self.make(["unknown"])
        self.assertReason(gate, "zzz_launcher_unknown")
        self.assertEqual(driver.calls, [])

    def test_credentials_and_captcha_preserve_human_gate(self):
        for state in ("credentials", "captcha"):
            gate, driver, _, _ = self.make([state])
            with self.assertRaises(launch.GameLaunchHumanRequired):
                gate.run()
            self.assertEqual(driver.calls, [])

    def test_stale_changed_game_or_missing_birth_rejects(self):
        changes = [
            (lambda s: replace(s, observed_at=-10), "zzz_launcher_stale_observation"),
            (lambda s: replace(s, game_path="D:/other/Game.exe"), "zzz_launcher_game_binding_changed"),
            (lambda s: replace(s, created_at=0), "zzz_launcher_process_identity_missing"),
            (lambda s: replace(s, actions=frozenset({"enter-game"})), "zzz_launcher_action_outside_scope"),
        ]
        for change, reason in changes:
            gate, driver, _, _ = self.make(["ready"])
            driver.change = change
            self.assertReason(gate, reason)
            self.assertEqual(driver.calls, [])

    def test_progress_stall_and_regression_are_not_retried(self):
        gate, driver, _, _ = self.make(["update-available", ("downloading", ("bytes", 10)),
                                      ("downloading", ("bytes", 9))])
        self.assertReason(gate, "zzz_launcher_progress_regressed")
        self.assertEqual(driver.calls, ["update"])

    def test_repeated_update_is_not_clicked_again(self):
        gate, driver, _, _ = self.make(["update-available", "downloading", "ready",
                                      "checking", "update-available"])
        self.assertReason(gate, "zzz_launcher_update_repeated")
        self.assertEqual(driver.calls, ["update", "check"])

    def test_self_update_pid_gap_cannot_reuse_old_capture_scope(self):
        gate, driver, _, events = self.make(["ready", "waiting", "unknown"])
        self.assertReason(gate, "zzz_launcher_unknown")
        # Waiting never retains PID 71; a fresh non-waiting observation must
        # establish its own identity before any subsequent input.
        driver.states = ["waiting"]
        snapshot = driver.observe()
        gate.validate(snapshot)
        self.assertEqual(gate.pids, frozenset())

    def test_production_boundary_rejects_without_any_process(self):
        gate, _, _, _ = self.make(["ready"])
        gate.driver = HoYoPlayLauncher(object(), GAME, mock.Mock(), gate.interrupted)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("process forbidden")):
            self.assertReason(gate, "zzz_launcher_native_context_missing")

    def test_progress_stays_visible_without_repeated_frames(self):
        gate, driver, clock, events = self.make(["downloading"])
        driver.change = lambda s: replace(s, progress=("bytes", int(clock.value * 100)))
        with mock.patch.object(module, "UPDATE_HARD_CAP_SECONDS", 91):
            self.assertReason(gate, "zzz_client_update_hard_timeout")
        gate.emit("blocked", terminal=True)
        self.assertEqual(len(events), 5)  # t=0,30,60,90 plus terminal
        self.assertEqual(sum(e[1]["captureRequested"] for e in events), 2)
        self.assertEqual(events[-2][1]["progressValue"], 9000)


class ExistingChainTests(unittest.TestCase):
    def test_real_candidate_wrapper_rejects_before_official_handoff(self):
        service = launch.GameLaunchService()
        seen = []
        with mock.patch.object(service, "validate_configured_executable", return_value=GAME), \
             mock.patch.object(service, "_list_running", side_effect=AssertionError("name scan forbidden")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("process forbidden")):
            with self.assertRaises(launch.GameLaunchError) as raised:
                service.ensure_started("ZZZ", str(GAME), seen.append,
                    cancel_requested=lambda: False,
                    preflight_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    preflight_authority_valid=lambda: True)
        self.assertEqual(raised.exception.reason_code, "zzz_launcher_native_context_missing")
        self.assertNotIn("official-launch-pending", [s.phase for s in seen])
        self.assertTrue(all(not s.process_ids for s in seen))

    def test_completed_preflight_still_delegates_to_official_tool(self):
        service = launch.GameLaunchService()
        seen = []
        native_context = object()
        authority_valid = lambda: True
        with mock.patch.object(service, "validate_configured_executable", return_value=GAME), \
             mock.patch.object(service, "_list_running", return_value={}), \
             mock.patch.object(service, "_ensure_started") as bare_start, \
             mock.patch.object(module, "ensure_zzz_launcher", return_value={"state": "launcher-verified"}) as preflight:
            receipt = service.ensure_started("ZZZ", str(GAME), seen.append,
                cancel_requested=lambda: False, preflight_deadline_at=BASE_TIME,
                preflight_authority_valid=authority_valid,
                preflight_native_context=native_context)
        self.assertEqual(receipt.state, "official-tool-pending")
        bare_start.assert_not_called()
        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.kwargs["deadline_at"], BASE_TIME)
        self.assertIs(preflight.call_args.kwargs["authority_valid"], authority_valid)
        self.assertIs(preflight.call_args.kwargs["native_context"], native_context)
        self.assertEqual(seen[-1].detail["operation"], "OpenAndEnterGame.execute")

    def test_preflight_error_prevents_official_handoff(self):
        service = launch.GameLaunchService()
        seen = []
        with mock.patch.object(service, "validate_configured_executable", return_value=GAME), \
             mock.patch.object(service, "_list_running", side_effect=AssertionError("name scan forbidden")), \
             mock.patch.object(module, "ensure_zzz_launcher", side_effect=launch.GameLaunchCancelled(
                 "stop", reason_code="manager_cancel_requested", process_ids=frozenset())):
            with self.assertRaises(launch.GameLaunchCancelled):
                service.ensure_started("ZZZ", str(GAME), seen.append, cancel_requested=lambda: False)
        self.assertEqual([s.phase for s in seen], ["launch-cancelled"])
        self.assertEqual(seen[0].process_ids, frozenset())


def load_manager_method(name):
    candidate = Path(__file__).parents[1] / "yeyu_gamer_manager/services/manager.py"
    tree = ast.parse(candidate.read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and any(
        isinstance(item, ast.FunctionDef) and item.name == name for item in node.body))
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"hashlib": hashlib, "utc_now": lambda: BASE_TIME, "AdapterExecutionPlan": object,
                 "LaunchObservation": launch.LaunchObservation, "_log": mock.Mock(),
                 "bind_log_context": lambda **kw: nullcontext(),
                 "WindowCaptureError": RuntimeError, "ManagerValidation": ValueError}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(candidate), "exec"), namespace)
    return namespace[name]


class LeaseTests(unittest.TestCase):
    def test_exact_live_lease_only(self):
        method = load_manager_method("_zzz_launcher_authority_valid")
        plan = SimpleNamespace(run_attempt_id="attempt", run_id="run", game_id="ZZZ",
                               fencing_token="fixture-token", expires_at=(BASE_TIME + timedelta(hours=1)).isoformat())
        lease = dict(manager_id="manager", run_id="run", run_attempt_id="attempt", game_id="ZZZ",
                     desktop_id="windows-interactive-desktop", holder_principal_id="manager-adapter",
                     state="active", fencing_hash="sha256:" + hashlib.sha256(b"fixture-token").hexdigest(),
                     expires_at=plan.expires_at)
        owner = SimpleNamespace(manager_id="manager", store=mock.Mock(),
                                _contract_datetime=datetime.fromisoformat)
        owner.store.get_controller_lease_for_attempt.return_value = lease
        self.assertTrue(method(owner, plan))
        for key, bad in dict(manager_id="other", run_id="other", run_attempt_id="old", game_id="PGR",
                             desktop_id="other", holder_principal_id="other",
                             state="revoked", fencing_hash="wrong", expires_at=BASE_TIME.isoformat()).items():
            owner.store.get_controller_lease_for_attempt.return_value = {**lease, key: bad}
            self.assertFalse(method(owner, plan), key)
        owner.store.get_controller_lease_for_attempt.return_value = None
        self.assertFalse(method(owner, plan))
        owner.store.get_controller_lease_for_attempt.return_value = lease
        plan.expires_at = BASE_TIME.isoformat()
        self.assertFalse(method(owner, plan))


class ManagerWiringTests(unittest.TestCase):
    def test_production_call_forwards_only_zzz_context_and_live_authority(self):
        candidate = Path(__file__).parents[1] / "yeyu_gamer_manager/services/manager.py"
        tree = ast.parse(candidate.read_text(encoding="utf-8-sig"))
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                     and ast.unparse(node.test) == "plan.game_id == 'ZZZ'"
                     and any(isinstance(child, ast.Assign) and any(
                         isinstance(target, ast.Name) and target.id == "preflight_options"
                         for target in child.targets) for child in node.body))
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "self.game_launcher.ensure_started")
        statements = [block, ast.Assign(targets=[ast.Name(id="receipt", ctx=ast.Store())], value=call)]
        code = compile(ast.fix_missing_locations(ast.Module(body=statements, type_ignores=[])), str(candidate), "exec")
        for game_id in ("ZZZ", "PGR"):
            owner = SimpleNamespace(game_launcher=mock.Mock(), _record_launch_observation=mock.Mock(),
                _launch_cancel_requested=mock.Mock(return_value=False),
                _zzz_launcher_authority_valid=mock.Mock(return_value=True),
                _contract_datetime=datetime.fromisoformat)
            plan = SimpleNamespace(game_id=game_id, run_attempt_id="attempt", expires_at=BASE_TIME.isoformat())
            namespace = dict(self=owner, plan=plan, run={"game_id": game_id}, game_path=str(GAME),
                             preflight_options={}, __package__="yeyu_gamer_manager.services")
            with mock.patch("yeyu_gamer_manager.services.hoyoplay_manager_session.manager_context", return_value="context") as context:
                exec(code, namespace)
            options = owner.game_launcher.ensure_started.call_args.kwargs
            if game_id == "ZZZ":
                self.assertEqual(options["preflight_deadline_at"], BASE_TIME)
                self.assertEqual(options["preflight_native_context"], "context")
                self.assertIs(context.call_args.args[2], options["preflight_authority_valid"])
                self.assertTrue(options["preflight_authority_valid"]())
                owner._launch_cancel_requested.return_value = True
                self.assertFalse(options["preflight_authority_valid"]())
                owner._launch_cancel_requested.return_value = False
                owner._zzz_launcher_authority_valid.return_value = False
                self.assertFalse(options["preflight_authority_valid"]())
            else:
                context.assert_not_called()
                self.assertFalse(any(key.startswith("preflight_") for key in options))


class ManagerObservationTests(unittest.TestCase):
    def record(self, phase, pids, detail):
        method = load_manager_method("_record_launch_observation")
        store = mock.Mock()
        store.atomic.side_effect = nullcontext
        store.get_run_attempt.return_value = {"state": "running", "exit_code": None}
        owner = SimpleNamespace(store=store, window_capture=mock.Mock(),
                                _store_manager_capture=mock.Mock(return_value="fixture-artifact"),
                                LAUNCH_PHASE_ARTIFACT_KINDS={})
        plan = SimpleNamespace(run_id="run", run_attempt_id="attempt", game_id="ZZZ")
        observation = launch.LaunchObservation("ZZZ", phase, 10, frozenset({"hyp.exe"}), pids, detail)
        method(owner, plan, observation)
        store.update_run_attempt.assert_called_once()
        store.append_event.assert_called_once()
        return owner

    def test_empty_pid_scope_never_expands_on_any_zzz_exit_phase(self):
        for phase in ("client-update", "launch-failed", "launch-cancelled", "launch-human-required"):
            owner = self.record(phase, frozenset(), {})
            owner.window_capture.capture_processes.assert_not_called()

    def test_lightweight_progress_records_without_a_frame(self):
        owner = self.record("client-update", frozenset({71}), {"captureRequested": False, "progressValue": 100})
        owner.window_capture.capture_processes.assert_not_called()

    def test_transition_frame_uses_exact_pid_scope(self):
        owner = self.record("client-update", frozenset({71}), {"captureRequested": True})
        self.assertEqual(owner.window_capture.capture_processes.call_args.kwargs["allowed_pids"], frozenset({71}))


if __name__ == "__main__":
    unittest.main()
