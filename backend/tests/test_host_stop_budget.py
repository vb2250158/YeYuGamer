import io
from dataclasses import replace
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import test_adapter_protocol as fixtures
from yeyu_gamer_manager.services import adapter_host as host_module
from yeyu_gamer_manager.services.adapter_host import AdapterExecutionRejected
from yeyu_gamer_manager.services.adapter_protocol import verify_execution_package
from yeyu_gamer_manager.services.queue_timeout_budget import load_queue_timeout_budget


class HostStopBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdapterHostSupervisorTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.host = self.fixture.host_service
        self.plan = self.fixture.plan
        self.manifest = verify_execution_package(self.fixture.runtime / "adapters" / "legacy-night-rain-gamer")
        self.budget = load_queue_timeout_budget({"queue_timeout_budget": {"host_stop_grace_seconds": 0.01}})
        self.host._queue_timeout_budget_resolver = lambda: self.budget
        self.binding = self.fixture.root / "offline-binding.json"
        self.binding.write_text("offline test binding", encoding="utf-8")
        self.authority = self.host._write_control_authority(self.plan)

    def process(self, *, alive=False):
        process = SimpleNamespace(pid=31415, stdin=None, stdout=io.BytesIO(), stderr=io.BytesIO(),
            poll=mock.Mock(return_value=None if alive else 0), wait=mock.Mock(return_value=0),
            terminate=mock.Mock(), kill=mock.Mock())
        self.host._active_process = process
        self.host._active_plan = self.plan
        return process

    def watch(self, process, complete, **kwargs):
        with mock.patch.object(self.host, "_send_cancel_locked", return_value=True):
            self.host._watch_execution_bound(process=process, plan=self.plan, manifest=self.manifest,
                host_probe={}, on_event=None, on_complete=complete, installation_binding_path=self.binding, **kwargs)

    def assert_retained(self, process, complete):
        self.assertIs(self.host._active_process, process)
        self.assertIs(self.host._active_plan, self.plan)
        self.assertTrue(self.binding.exists())
        self.assertTrue(self.authority.exists())
        complete.assert_not_called()

    def test_three_failed_stop_waits_are_bounded_and_cannot_ack_or_clear_owner(self):
        process = self.process(alive=True)
        process.wait.side_effect = subprocess.TimeoutExpired("offline-fixed-host", 0.01)
        self.host._cancel_sent = True
        complete = mock.Mock()
        self.watch(process, complete)
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=0.01)] * 3)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assert_retained(process, complete)

    def test_stop_errors_retain_owner_and_recovery_files(self):
        for stage in ("wait", "terminate", "kill"):
            with self.subTest(stage=stage):
                process = self.process(alive=True)
                process.wait.side_effect = subprocess.TimeoutExpired("offline-fixed-host", 0.01)
                getattr(process, stage).side_effect = OSError("offline stop failure")
                if stage == "kill":
                    process.wait.side_effect = subprocess.TimeoutExpired("offline-fixed-host", 0.01)
                self.host._cancel_sent = True
                complete = mock.Mock()
                self.watch(process, complete)
                self.assert_retained(process, complete)

    def test_live_reader_prevents_finish_callback_and_busy_pipe_close(self):
        process = self.process()
        release = threading.Event()
        returned = threading.Event()

        def blocked_read(limit):
            release.wait(2)
            returned.set()
            return b""

        output = SimpleNamespace(readline=blocked_read, close=mock.Mock())
        process.stdout = output
        complete = mock.Mock()
        try:
            self.watch(process, complete)
            self.assert_retained(process, complete)
            output.close.assert_not_called()
        finally:
            release.set()
            self.assertTrue(returned.wait(2))

    def test_callback_without_handoff_keeps_exited_owner_and_material(self):
        process = self.process()
        complete = mock.Mock()
        self.watch(process, complete)
        complete.assert_called_once()
        self.assertIs(self.host._active_process, process)
        self.assertTrue(self.binding.exists())
        self.assertTrue(self.authority.exists())

    def test_callback_failure_before_handoff_preserves_owner(self):
        process = self.process()
        complete = mock.Mock(side_effect=RuntimeError("offline persistence failure"))
        self.watch(process, complete)
        complete.assert_called_once()
        self.assertIs(self.host._active_process, process)
        self.assertTrue(self.authority.exists())
        self.assertTrue(self.binding.exists())

    def test_handoff_is_one_use_and_clears_owner_before_notification(self):
        process = self.process()
        outcomes = []

        def complete(result, *, handoff):
            self.assertIs(self.host._active_process, process)
            outcomes.extend([handoff(), handoff()])
            self.assertIsNone(self.host.active_execution())
            self.assertFalse(result.protocol_valid)

        self.watch(process, complete)
        self.assertEqual(outcomes, [True, False])
        self.assertIsNone(self.host._active_process)
        self.assertFalse(self.authority.exists())
        self.assertFalse(self.binding.exists())

    def test_post_handoff_notification_failure_does_not_restore_old_owner(self):
        process = self.process()

        def complete(result, *, handoff):
            self.assertIs(handoff(), True)
            raise RuntimeError("offline notification failure after committed handoff")

        self.watch(process, complete)
        self.assertIsNone(self.host._active_process)
        self.assertFalse(self.authority.exists())

    def test_post_commit_cleanup_failure_keeps_scoped_debt_not_old_desktop_ownership(self):
        process = self.process()
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == self.authority:
                raise OSError("offline authority file locked")
            return original_unlink(path, *args, **kwargs)

        def complete(result, *, handoff):
            self.assertIs(handoff(), True)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink), \
                mock.patch.object(host_module._log, "warning") as warning:
            self.watch(process, complete)
        self.assertIsNone(self.host._active_process)
        self.assertTrue(self.authority.exists())
        self.assertFalse(self.binding.exists())
        self.assertTrue(any("cleanup_pending" in call.args[0] for call in warning.call_args_list))

    def test_late_handoff_cannot_clear_replacement_owner(self):
        process = self.process()
        received = []
        self.watch(process, lambda result, *, handoff: received.append(handoff))
        successor = self.process(alive=True)
        self.host._active_plan = replace(self.plan, run_attempt_id="88888888-8888-4888-8888-888888888888")
        self.assertIs(received[0](), False)
        self.assertIs(self.host._active_process, successor)
        self.assertTrue(self.authority.exists())
        self.assertTrue(self.binding.exists())

    def test_late_handoff_after_unacknowledged_return_or_error_is_closed(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                process = self.process()
                received = []

                def complete(result, *, handoff):
                    received.append(handoff)
                    if fail:
                        raise RuntimeError("offline callback failure before handoff")

                self.watch(process, complete)
                self.assertIs(received[0](), False)
                self.assertIs(self.host._active_process, process)
                self.assertTrue(self.authority.exists())
                self.assertTrue(self.binding.exists())

    def test_concurrent_late_handoff_cannot_change_a_sealed_gc_decision(self):
        process = self.process()
        release = threading.Event()
        outcomes = []
        workers = []

        def complete(result, *, handoff):
            def late():
                release.wait(2)
                outcomes.append(handoff())

            worker = threading.Thread(target=late)
            workers.append(worker)
            worker.start()

        try:
            self.watch(process, complete)
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [False])
        self.assertIs(self.host._active_process, process)
        self.assertTrue(self.authority.exists())

    def test_concurrent_handoff_before_seal_is_collected_once(self):
        process = self.process()
        outcomes = []

        def complete(result, *, handoff):
            worker = threading.Thread(target=lambda: outcomes.append(handoff()))
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())

        self.watch(process, complete)
        self.assertEqual(outcomes, [True])
        self.assertIsNone(self.host._active_process)
        self.assertFalse(self.authority.exists())

    def test_old_gc_does_not_touch_an_owner_started_during_queue_notification(self):
        process = self.process()
        successor_plan = replace(self.plan, run_attempt_id="88888888-8888-4888-8888-888888888888")
        successor = SimpleNamespace(pid=27182, poll=lambda: None)
        successor_binding = self.fixture.root / "successor-binding.json"
        successor_authority = []

        def complete(result, *, handoff):
            self.assertIs(handoff(), True)
            self.host._active_process = successor
            self.host._active_plan = successor_plan
            self.host._cancel_sent = True
            successor_authority.append(self.host._write_control_authority(successor_plan))
            successor_binding.write_text("offline successor binding", encoding="utf-8")

        self.watch(process, complete)
        self.assertIs(self.host._active_process, successor)
        self.assertIs(self.host._active_plan, successor_plan)
        self.assertTrue(self.host._cancel_sent)
        self.assertTrue(successor_authority[0].exists())
        self.assertTrue(successor_binding.exists())
        self.assertFalse(self.authority.exists())

    def test_execute_refuses_exited_but_unacknowledged_owner(self):
        process = self.process()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No process allowed")) as spawn:
            with self.assertRaises(AdapterExecutionRejected) as rejected:
                self.host.execute(self.plan.run_id, self.plan.game_id, mock.Mock(), plan=self.plan,
                    installation_binding=self.fixture.installation_binding)
        self.assertEqual(rejected.exception.code, "adapter_busy")
        spawn.assert_not_called()
        self.assertIs(self.host._active_process, process)

    def test_budget_is_resolved_once_before_dispatch_and_frozen_for_watcher(self):
        self.host._active_process = None
        self.host._active_plan = None
        process = self.process()
        self.host._active_process = None
        self.host._active_plan = None
        process.stdin = io.BytesIO()
        resolver = mock.Mock(side_effect=[self.budget, AssertionError("Budget must not be reloaded mid-attempt")])
        self.host._queue_timeout_budget_resolver = resolver
        watcher = mock.Mock()

        def complete(result, *, handoff):
            self.assertIs(handoff(), True)

        with mock.patch("subprocess.Popen", return_value=process), \
                mock.patch("yeyu_gamer_manager.services.adapter_host.threading.Thread", return_value=watcher) as create:
            self.host.execute(self.plan.run_id, self.plan.game_id, complete, plan=self.plan,
                installation_binding=self.fixture.installation_binding)
        kwargs = create.call_args.kwargs["kwargs"]
        self.assertIs(kwargs["timeout_budget"], self.budget)
        self.host._watch_execution_bound(**kwargs)
        resolver.assert_called_once()
        process.wait.assert_called_once_with(timeout=0.01)

    def test_invalid_budget_fails_before_any_process_or_new_owner(self):
        self.host._active_process = None
        self.host._active_plan = None
        self.host._queue_timeout_budget_resolver = lambda: load_queue_timeout_budget(
            {"queue_timeout_budget": {"host_stop_grace_seconds": 0}})
        with mock.patch("subprocess.Popen") as spawn, self.assertRaises(ValueError):
            self.host.execute(self.plan.run_id, self.plan.game_id, mock.Mock(), plan=self.plan,
                installation_binding=self.fixture.installation_binding)
        spawn.assert_not_called()
        self.assertIsNone(self.host._active_process)

    def test_startup_write_failure_wait_is_bounded_and_unknown_exit_retains_material(self):
        process = self.process(alive=True)
        self.host._active_process = None
        self.host._active_plan = None
        process.stdin = SimpleNamespace(write=mock.Mock(side_effect=BrokenPipeError("offline broken pipe")),
                                        flush=mock.Mock())
        process.wait.side_effect = subprocess.TimeoutExpired("offline-fixed-host", 0.01)
        complete = mock.Mock()
        with mock.patch("subprocess.Popen", return_value=process) as spawn, self.assertRaises(subprocess.TimeoutExpired):
            self.host.execute(self.plan.run_id, self.plan.game_id, complete, plan=self.plan,
                installation_binding=self.fixture.installation_binding)
        process.wait.assert_called_once_with(timeout=0.01)
        process.terminate.assert_called_once()
        complete.assert_not_called()
        self.assertIs(self.host._active_process, process)
        self.assertTrue(self.authority.exists())
        self.assertTrue(Path(spawn.call_args.kwargs["env"]["YEYU_GAMER_INSTALLATION_BINDING_PATH"]).exists())


if __name__ == "__main__":
    unittest.main()
