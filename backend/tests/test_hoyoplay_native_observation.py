from contextlib import contextmanager
import copy
import hashlib
import json
import re
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from yeyu_gamer_manager.services import hoyoplay_native_observation as native
from yeyu_gamer_manager.services.game_launcher import GameLaunchError
from yeyu_gamer_manager.services.window_capture import _encode_bgra_png


class NativeHoYoPlaySamplerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hno-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.events = []
        self.cancelled = False
        self.in_guard = False

        @contextmanager
        def guard():
            self.in_guard = True
            self.events.append("enter-guard")
            try:
                yield
            finally:
                self.events.append("leave-guard")
                self.in_guard = False

        self.guard = guard
        self.registry = []
        self.sampler = native.NativeHoYoPlaySampler(
            native.FIXED_GAME_EXECUTABLE, cancelled=lambda: self.cancelled,
            register_helper=self.registry.append, task_id=str(uuid.uuid4()),
            manager_id="fixture-manager", launch_guard=guard,
        )
        self.sampler._deadline = time.monotonic() + 15
        self.identity = {"pid": 71, "createdAt": 12345678,
                         "sampleId": self.sampler.task_id,
                         "imagePath": str(native.FIXED_LAUNCHER_ROOT / "1.18.0.380/HYP.exe"),
                         "signatureStatus": "Valid", "signer": "miHoYo Co.,Ltd.",
                         "productName": "米哈游启动器", "fileVersion": "1.18.0.380"}
        for target in ("ctypes.WinDLL", "subprocess.Popen", "socket.socket", "os.startfile"):
            patcher = mock.patch(target, side_effect=AssertionError("unexpected native effect"), create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(native.GameLaunchService, "_require_queue_local_path")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pin = mock.MagicMock()
        self.pin.__enter__.return_value = self.pin
        patcher = mock.patch.object(native, "_PinnedLauncher", return_value=self.pin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self):
        png = _encode_bgra_png(2, 2, b"\x01\x02\x03\xff" * 4)
        (self.output / "window-0.png").write_bytes(png)
        frame = {"path": "window-0.png", "pid": 71, "createdAt": 12345678, "hwnd": 91,
                 "sampleId": self.sampler.task_id, "acquisition": self.interval(40, 50),
                 "capturedAt": "2026-10-24T00:00:00Z", "width": 2, "height": 2,
                 "captureCompletedAt": "2026-10-24T00:00:00Z",
                 "method": "PrintWindow", "watermarked": False}
        checks = [{**self.interval(left, right), "sampleId": self.sampler.task_id, "index": index,
                   "phase": phase, "enumerationComplete": True, "scope": "all-Win32_Process",
                   "policy": "known-games+game-root+same-session-readable", "processCount": 7,
                   "unreadableImagePathCount": 2, "sameSessionUnreadableCount": 0,
                   "knownGameMatches": 0, "gameRootMatches": 0, "unknownExecutableAbsence": "unknown",
                   "rulesSha256": hashlib.sha256(json.dumps(native._known_game_names()).encode()).hexdigest()}
                  for index, (phase, left, right) in enumerate([
                      ("admission", 1, 2), ("before-ax", 10, 11), ("before-capture", 30, 31), ("final", 70, 71)])]
        ax = {"schemaVersion": 2, "selectedGameVerified": False, "actions": [],
              "nodeByteBudget": native.MAX_NODE_BYTES, "nodeByteCount": 0,
              "qualification": False, "productionUpdateAllowed": False,
              "acquisition": {**self.interval(0, 100), "sampleId": self.sampler.task_id,
                  "workerPid": 77, "clock": "System.Diagnostics.Stopwatch.GetTimestamp",
                  "clockFrequency": 10000000, "atomicSnapshot": False},
              "installationAssociation": {"status": "configuration-only", "primaryGame": "nap_cn",
                  "gameExecutable": str(native.FIXED_GAME_EXECUTABLE), "configSha256": "a" * 64},
              "inventoryChecks": checks,
              "windows": [{"pid": 71, "createdAt": 12345678, "hwnd": 91, "nodes": [],
                  "sampleId": self.sampler.task_id, "acquisition": self.interval(20, 21),
                  "inventoryBefore": 1, "inventoryAfter": 2, "contentAssociation": {
                      "status": "unknown", "gameBiz": "unknown", "channel": "unknown", "installPath": "unknown"}}]}
        (self.output / "ax.json").write_text(json.dumps(ax), encoding="utf-8")
        return {"ok": True, "processes": [dict(self.identity)], "axPath": "ax.json",
                "screenshots": [frame], "truncated": False}

    @staticmethod
    def interval(start, end):
        return {"startedTicks": start, "completedTicks": end,
                "startedAt": "2026-10-24T00:00:00Z", "completedAt": "2026-10-24T00:00:00Z"}

    def read_ax(self):
        return json.loads((self.output / "ax.json").read_text(encoding="utf-8"))

    def write_ax(self, data):
        (self.output / "ax.json").write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def uia_node(**changes):
        raw = {"runtimeId": [42, 71, 1], "ownerPid": 71, "ownerHwnd": 91,
               "invokePatternAvailable": True, "selectionItemPatternAvailable": True,
               "selected": True, "selectionContainer": {"ownerPid": 71, "runtimeId": [42, 71]},
               "occlusion": "unknown", **changes}
        return {"index": 0, "parent": -1, "uia": raw}

    def test_generic_selected_and_invoke_availability_never_qualify_game(self):
        payload = self.fixture()
        ax = self.read_ax()
        ax["windows"][0]["nodes"] = [self.uia_node()]
        self.write_ax(ax)
        self.sampler._validate_result(payload, self.output)
        self.assertFalse(ax["qualification"])
        self.assertFalse(ax["selectedGameVerified"])
        self.assertFalse(ax["productionUpdateAllowed"])
        self.assertEqual(ax["actions"], [])
        self.assertEqual(ax["windows"][0]["contentAssociation"]["gameBiz"], "unknown")

    def test_unsupported_properties_remain_unknown_without_rejecting_raw_evidence(self):
        payload = self.fixture()
        ax = self.read_ax()
        ax["windows"][0]["nodes"] = [self.uia_node(runtimeId="unknown", selected="unknown",
            invokePatternAvailable="unknown", selectionItemPatternAvailable="unknown", selectionContainer="unknown")]
        self.write_ax(ax)
        self.sampler._validate_result(payload, self.output)

    def test_complete_inventory_and_unknown_foreign_paths_are_distinct(self):
        payload = self.fixture()
        ax = self.read_ax()
        self.assertGreater(ax["inventoryChecks"][0]["unreadableImagePathCount"], 0)
        self.sampler._validate_result(payload, self.output)
        for field, value in (("enumerationComplete", False), ("rulesSha256", "b" * 64),
                             ("sameSessionUnreadableCount", 1), ("knownGameMatches", 1),
                             ("gameRootMatches", 1), ("processCount", 0), ("unknownExecutableAbsence", True)):
            changed = copy.deepcopy(ax)
            changed["inventoryChecks"][1][field] = value
            self.write_ax(changed)
            with self.subTest(field=field), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)

    def test_ax_identity_and_inventory_cannot_be_detached_from_same_sample(self):
        payload = self.fixture()
        ax = self.read_ax()
        for location in ("root", "window", "inventory", "process", "frame"):
            changed, report = copy.deepcopy(ax), copy.deepcopy(payload)
            target = {"root": changed["acquisition"], "window": changed["windows"][0],
                      "inventory": changed["inventoryChecks"][0], "process": report["processes"][0],
                      "frame": report["screenshots"][0]}[location]
            target["sampleId"] = str(uuid.uuid4())
            self.write_ax(changed)
            with self.subTest(location=location), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(report, self.output)

    def test_future_or_reversed_ax_and_capture_intervals_are_rejected(self):
        payload = self.fixture()
        ax = self.read_ax()
        for field, value in (("startedTicks", 110), ("completedTicks", 19), ("startedAt", "2026-10-24T00:00:00")):
            changed = copy.deepcopy(ax)
            changed["windows"][0]["acquisition"][field] = value
            self.write_ax(changed)
            with self.subTest(field=field), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)
        self.write_ax(ax)
        payload["screenshots"][0]["acquisition"]["startedTicks"] = 29
        with self.assertRaises(GameLaunchError):
            self.sampler._validate_result(payload, self.output)

    def test_raw_selection_container_and_runtime_identity_are_bounded(self):
        payload = self.fixture()
        ax = self.read_ax()
        for change in ({"runtimeId": [1] * 33}, {"runtimeId": [True]}, {"runtimeId": [2**32]},
                       {"selectionContainer": {"ownerPid": 999, "runtimeId": [1]}},
                       {"selectionItemPatternAvailable": False}, {"ownerHwnd": 92}, {"occlusion": False}):
            ax["windows"][0]["nodes"] = [self.uia_node(**change)]
            self.write_ax(ax)
            with self.subTest(change=change), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)

    def test_configuration_or_caller_fields_cannot_qualify_content(self):
        payload = self.fixture()
        ax = self.read_ax()
        for field, value in (("gameBiz", "nap_cn"), ("channel", "cn"),
                             ("installPath", str(native.FIXED_GAME_EXECUTABLE)), ("status", "verified")):
            changed = copy.deepcopy(ax)
            changed["windows"][0]["contentAssociation"][field] = value
            self.write_ax(changed)
            with self.subTest(field=field), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)
        for field in ("qualification", "productionUpdateAllowed"):
            changed = copy.deepcopy(ax)
            changed[field] = True
            self.write_ax(changed)
            with self.subTest(field=field), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)

    def test_same_sample_metadata_is_inside_original_ax_digest(self):
        payload = self.fixture()
        self.sampler._validate_result(payload, self.output)
        before = payload["axSha256"]
        ax = self.read_ax()
        ax["windows"][0]["nodes"] = [self.uia_node(selected=False)]
        self.write_ax(ax)
        self.sampler._validate_result(payload, self.output)
        self.assertNotEqual(before, payload["axSha256"])
        self.assertEqual(payload["axSha256"], hashlib.sha256((self.output / "ax.json").read_bytes()).hexdigest())

    def test_inventory_chronology_and_exact_schema_are_required(self):
        payload = self.fixture()
        ax = self.read_ax()
        for field, value in (("schemaVersion", 1), ("schemaVersion", True)):
            changed = copy.deepcopy(ax)
            changed[field] = value
            self.write_ax(changed)
            with self.subTest(value=value), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)
        ax["inventoryChecks"][0].update(startedTicks=12, completedTicks=13)
        self.write_ax(ax)
        with self.assertRaises(GameLaunchError):
            self.sampler._validate_result(payload, self.output)

    def test_node_budget_does_not_expand_the_ax_limit(self):
        payload = self.fixture()
        ax = self.read_ax()
        for field, value in (("nodeByteBudget", native.MAX_AX_BYTES),
                             ("nodeByteCount", native.MAX_NODE_BYTES + 1), ("nodeByteCount", True)):
            changed = copy.deepcopy(ax)
            changed[field] = value
            self.write_ax(changed)
            with self.subTest(field=field), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(payload, self.output)

    def test_existing_launcher_observation_requires_ax_and_png(self):
        fixture = self.fixture()
        with mock.patch.object(self.sampler, "_validate_inputs"), mock.patch.object(
            self.sampler, "_run_worker", side_effect=[{"processes": [self.identity]}, fixture]
        ), mock.patch.object(self.sampler, "_start_launcher") as start:
            result = self.sampler.sample(self.output, allow_start=False)
        start.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["selectedGameVerified"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["axSha256"], hashlib.sha256((self.output / "ax.json").read_bytes()).hexdigest())
        self.assertEqual(result["screenshots"][0]["sha256"], hashlib.sha256((self.output / "window-0.png").read_bytes()).hexdigest())

    def test_no_launcher_without_start_permission_is_blocked(self):
        with mock.patch.object(self.sampler, "_validate_inputs"), mock.patch.object(
            self.sampler, "_run_worker", return_value={"processes": []}
        ) as worker:
            result = self.sampler.sample(self.output, allow_start=False)
        self.assertEqual(result["reasonCode"], "zzz_observation_launcher_not_running")
        self.assertEqual(worker.call_count, 1)

    def test_stop_before_sampling_never_starts_worker(self):
        self.cancelled = True
        with mock.patch.object(self.sampler, "_run_worker") as worker:
            result = self.sampler.sample(self.output, allow_start=True)
        worker.assert_not_called()
        self.assertEqual(result["status"], "cancelled")

    def test_stop_wins_inside_launch_guard(self):
        @contextmanager
        def stopped_guard():
            self.cancelled = True
            yield
        self.sampler.launch_guard = stopped_guard
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(native.GameLaunchCancelled):
            self.sampler._start_launcher(self.output, self.pin)
        popen.assert_not_called()

    def test_no_argument_launch_is_inside_guard_but_identity_read_is_outside(self):
        def popen(arguments, **options):
            self.assertTrue(self.in_guard)
            self.assertEqual(arguments, [str(native.FIXED_LAUNCHER_EXECUTABLE), "--force-renderer-accessibility"])
            self.assertFalse(options["shell"])
            return SimpleNamespace(pid=88)
        def identity(pid):
            self.assertFalse(self.in_guard)
            self.assertEqual(pid, 88)
            return SimpleNamespace(executable=native.FIXED_LAUNCHER_EXECUTABLE, created_at=99)
        with mock.patch("subprocess.Popen", side_effect=popen), mock.patch.object(
            native.GameLaunchService, "_queue_process_identity", side_effect=identity
        ):
            self.sampler._start_launcher(self.output, self.pin)
        self.assertEqual([item["createdAt"] for item in self.registry], [None, 99])
        self.assertTrue(self.sampler._launches[0]["noUiActions"])
        self.pin.verify.assert_called_once()
        self.pin.close.assert_called_once()

    def test_file_pin_failure_prevents_admission_and_launch(self):
        self.pin.__enter__.side_effect = native._failure("zzz_observation_launcher_pin_failed", "fixture")
        with mock.patch.object(self.sampler, "_validate_inputs"), mock.patch.object(
            self.sampler, "_run_worker"
        ) as worker, mock.patch("subprocess.Popen") as popen:
            result = self.sampler.sample(self.output, allow_start=True)
        worker.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(result["reasonCode"], "zzz_observation_launcher_pin_failed")

    def test_pin_final_path_failure_prevents_popen(self):
        self.pin.verify.side_effect = native._failure("zzz_observation_launcher_pin_changed", "fixture")
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(GameLaunchError):
            self.sampler._start_launcher(self.output, self.pin)
        popen.assert_not_called()

    def test_file_pin_spans_admission_and_launch(self):
        events = []
        self.pin.__enter__.side_effect = lambda: (events.append("pin"), self.pin)[1]
        self.pin.__exit__.side_effect = lambda *args: events.append("unpin")
        fixture = self.fixture()
        def worker(mode, output):
            events.append(mode)
            return {"processes": []} if mode == "admit" else fixture
        with mock.patch.object(self.sampler, "_validate_inputs"), mock.patch.object(
            self.sampler, "_run_worker", side_effect=worker
        ), mock.patch.object(self.sampler, "_start_launcher", side_effect=lambda *args: events.append("launch")):
            result = self.sampler.sample(self.output, allow_start=True)
        self.assertEqual(events, ["pin", "admit", "launch", "unpin", "observe"])
        self.assertEqual(result["status"], "completed")

    def test_unverified_cleanup_cannot_report_completed(self):
        fixture = self.fixture()
        def worker(mode, output):
            if mode == "observe":
                self.sampler._helper_closed = False
                self.sampler._remaining_helpers.add(199)
                return fixture
            return {"processes": [self.identity]}
        with mock.patch.object(self.sampler, "_validate_inputs"), mock.patch.object(self.sampler, "_run_worker", side_effect=worker):
            result = self.sampler.sample(self.output, allow_start=False)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["helperClosed"])
        self.assertEqual(result["remainingHelperPids"], [199])

    def test_successful_launch_record_survives_registration_error_and_cannot_retry(self):
        self.sampler.register_helper = mock.Mock(side_effect=RuntimeError("fixture register error"))
        with mock.patch.object(self.sampler, "_validate_inputs"), mock.patch.object(
            self.sampler, "_run_worker", return_value={"processes": []}
        ), mock.patch("subprocess.Popen", return_value=SimpleNamespace(pid=88)) as popen:
            result = self.sampler.sample(self.output, allow_start=True)
            again = self.sampler.sample(self.output, allow_start=True)
        self.assertEqual(result["launches"][0]["pid"], 88)
        self.assertEqual(result["reasonCode"], "zzz_observation_unexpected_failure")
        self.assertEqual(again["reasonCode"], "zzz_observation_already_sampled")
        self.assertEqual(popen.call_count, 1)

    def test_bad_identity_cannot_reach_observation(self):
        for key, value in (("signer", "foreign"), ("productName", "other"), ("createdAt", 0),
                           ("imagePath", r"D:\other\HYP.exe"), ("signatureStatus", "NotSigned")):
            with self.subTest(key=key), self.assertRaises(GameLaunchError):
                self.sampler._validate_processes([dict(self.identity, **{key: value})])

    def test_missing_png_is_not_completed(self):
        fixture = self.fixture()
        fixture["screenshots"] = []
        with self.assertRaises(GameLaunchError):
            self.sampler._validate_result(fixture, self.output)

    def test_foreign_hwnd_or_path_cannot_be_imported(self):
        fixture = self.fixture()
        for key, value in (("path", "../outside.png"), ("hwnd", 100), ("createdAt", 42), ("method", "BitBlt")):
            frame = dict(fixture["screenshots"][0], **{key: value})
            with self.subTest(key=key), self.assertRaises(GameLaunchError):
                self.sampler._validate_result(dict(fixture, screenshots=[frame]), self.output)

    def test_ax_cannot_claim_game_selection_or_actions(self):
        fixture = self.fixture()
        path = self.output / "ax.json"
        data = json.loads(path.read_text())
        data["selectedGameVerified"] = True
        path.write_text(json.dumps(data))
        with self.assertRaises(GameLaunchError):
            self.sampler._validate_result(fixture, self.output)

    def test_ax_limit_is_256_kib(self):
        fixture = self.fixture()
        (self.output / "ax.json").write_bytes(b" " * (256 * 1024 + 1))
        with self.assertRaises(GameLaunchError) as raised:
            self.sampler._validate_result(fixture, self.output)
        self.assertEqual(raised.exception.reason_code, "zzz_observation_ax_limit")

    def test_worker_is_suspended_assigned_registered_then_resumed(self):
        events = []
        process = SimpleNamespace(pid=99, returncode=None, stdin=None)
        process.poll = lambda: process.returncode
        def start(arguments, **options):
            events.append("create")
            self.assertTrue(options["creationflags"] & 4)
            encoded = options["env"]["YEYU_HYP_GAME_NAMES"]
            self.assertEqual(json.loads(encoded), native._known_game_names())
            self.assertEqual(options["env"]["YEYU_HYP_GAME_NAMES_COUNT"], str(len(native._known_game_names())))
            self.assertEqual(options["env"]["YEYU_HYP_GAME_NAMES_SHA256"], hashlib.sha256(encoded.encode()).hexdigest())
            process.stream = options["stdout"]
            return process
        class Job:
            def assign(_, child): events.append("assign")
            def resume(_, child):
                events.append("resume")
                process.stream.write(b'{"ok":true,"processes":[]}')
                process.stream.flush()
                process.returncode = 0
            def close(_): events.append("close")
            def finish(_):
                events.append("finish")
                return True, []
        self.sampler.register_helper = lambda value: events.append("register")
        powershell = Path(native.os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        with mock.patch.object(native, "_HelperJob", return_value=Job()), mock.patch(
            "subprocess.Popen", side_effect=start
        ), mock.patch.object(native.GameLaunchService, "_queue_process_identity",
                             return_value=SimpleNamespace(executable=powershell, created_at=123)):
            result = self.sampler._run_worker("admit", self.output)
        self.assertTrue(result["ok"])
        self.assertEqual(events, ["create", "assign", "register", "resume", "finish", "close"])
        self.assertEqual(self.sampler._helpers[0]["exitCode"], 0)
        self.assertTrue(self.sampler._helpers[0]["killOnJobClose"])
        self.assertTrue(self.sampler._helper_closed)

    def test_all_registered_games_and_emulators_are_blocked_names(self):
        names = native._known_game_names()
        for required in ("zenlesszonezero.exe", "pgr.exe", "nikke.exe", "starrail.exe"):
            self.assertIn(required, names)
        expected = set(native.window_capture._LDPLAYER_CAPTURE_PROCESS_NAMES)
        for rule in native.window_capture._RULES.values():
            expected.update(rule.process_names)
        self.assertEqual(names, sorted(name.casefold() for name in expected))
        self.assertGreater(len(names), 13)

    def cleanup_fixture(self, *, cleanup_ok, fail_log):
        process = SimpleNamespace(pid=99, returncode=None, stdin=None)
        process.poll = lambda: process.returncode
        def start(arguments, **options):
            process.stream = options["stdout"]
            return process
        class Job:
            def assign(_, child): pass
            def resume(_, child):
                process.stream.write(b'{"ok":true,"processes":[]}')
                process.stream.flush()
                process.returncode = 0
            def close(_): pass
            def finish(_): return (True, []) if cleanup_ok else (False, [199])
        real_write = Path.write_text
        def write(path, *args, **kwargs):
            if fail_log and path.name.endswith(".process.json"):
                raise OSError("fixture ledger failure")
            return real_write(path, *args, **kwargs)
        powershell = Path(native.os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        with mock.patch.object(native, "_HelperJob", return_value=Job()), mock.patch(
            "subprocess.Popen", side_effect=start
        ), mock.patch.object(native.GameLaunchService, "_queue_process_identity",
                             return_value=SimpleNamespace(executable=powershell, created_at=123)), mock.patch.object(
            self.sampler, "_validate_inputs"
        ), mock.patch.object(Path, "write_text", write):
            return self.sampler.sample(self.output, allow_start=False)

    def test_job_members_must_be_zero_before_releasing_owner(self):
        result = self.cleanup_fixture(cleanup_ok=False, fail_log=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["remainingHelperPids"], [199])
        self.assertFalse(result["helperClosed"])
        self.assertEqual(result["helpers"][0]["exitCode"], 0)

    def test_ledger_write_failure_preserves_verified_cleanup_in_memory(self):
        result = self.cleanup_fixture(cleanup_ok=True, fail_log=True)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["helperClosed"])
        self.assertEqual(result["remainingHelperPids"], [])
        self.assertEqual(result["helpers"][0]["exitCode"], 0)
        self.assertIn("endedAt", result["helpers"][0])

    def test_native_source_has_no_input_or_desktop_capture_fallback(self):
        for forbidden in ("SendInput", "SendKeys", "BitBlt", "CopyFromScreen", "Start-Process", "SetForegroundWindow"):
            self.assertNotIn(forbidden, native._NATIVE_WORKER)
        self.assertIsNone(re.search(r"\.(Invoke|Select|AddToSelection|RemoveFromSelection|SetValue|SetFocus)\s*\(", native._NATIVE_WORKER))
        self.assertIn("IsInvokePatternAvailableProperty", native._NATIVE_WORKER)
        self.assertIn("GetRuntimeId()", native._NATIVE_WORKER)
        self.assertIn("PrintWindow", native._NATIVE_WORKER)


class NativeFixtureDirectoryTests(unittest.TestCase):
    def test_staged_fixture_keeps_build_top_level_clean_and_removes_output(self):
        with tempfile.TemporaryDirectory(prefix="hno-layout-") as layout:
            build = Path(layout) / "candidate"
            staged_tests = build / "staged-app/backend/tests"
            staged_tests.mkdir(parents=True)
            fixture = NativeHoYoPlaySamplerTests("test_bad_identity_cannot_reach_observation")
            output = None
            try:
                with mock.patch.dict(globals(), {
                    "__file__": str(staged_tests / "test_hoyoplay_native_observation.py"),
                }):
                    fixture.setUp()
                output = fixture.output
                self.assertEqual(output.parent, Path(tempfile.gettempdir()))
                self.assertEqual([item.name for item in build.iterdir()], ["staged-app"])
            finally:
                fixture.doCleanups()
            self.assertIsNotNone(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
