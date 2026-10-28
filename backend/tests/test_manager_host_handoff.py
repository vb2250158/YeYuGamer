from dataclasses import replace
import io
from types import SimpleNamespace
import unittest
from unittest import mock

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services.adapter_protocol import AdapterRunResult, verify_execution_package


class ManagerHostHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager, self.store = self.fixture.manager, self.fixture.store
        self.run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.notifications = mock.Mock()
        received = []

        def execute(run_id, game_id, complete, **kwargs):
            received.append(complete)
            return 31415

        with mock.patch("subprocess.Popen", side_effect=AssertionError("No real process in Manager handoff test")), \
                mock.patch.object(self.manager.adapter_host, "execute", side_effect=execute):
            self.plan, _ = self.manager._start_game_run(self.run["run_id"], self.notifications)
        self.finished = received[0]
        self.process = SimpleNamespace(pid=31415, poll=lambda: 0)
        self.manager.adapter_host._active_process = self.process
        self.manager.adapter_host._active_plan = self.plan
        self.addCleanup(self.clear_owner)
        self.result = AdapterRunResult(run_id=self.plan.run_id, run_attempt_id=self.plan.run_attempt_id,
            game_id=self.plan.game_id, status="failed", transport_outcome="failed",
            attempted_todo_instance_ids=tuple(self.run["todo_instance_ids"]), completed_todo_instance_ids=(),
            unresolved_todo_instance_ids=tuple(self.run["todo_instance_ids"]), exit_code=70,
            protocol_valid=False, code="offline_fixture_failure", message="offline failure")

    def clear_owner(self):
        self.manager.adapter_host._active_process = None
        self.manager.adapter_host._active_plan = None

    def release(self):
        attempt = self.store.get_run_attempt(self.plan.run_attempt_id)
        self.assertEqual(attempt["state"], "failed")
        self.assertTrue(attempt["completed_at"])
        self.notifications.assert_not_called()
        self.clear_owner()
        return True

    def test_terminal_commit_and_owner_release_precede_queue_notification(self):
        self.notifications.side_effect = lambda result: self.assertIsNone(self.manager.adapter_host.active_execution())
        handoff = mock.Mock(side_effect=self.release)
        self.finished(self.result, handoff=handoff)
        handoff.assert_called_once_with()
        self.notifications.assert_called_once()

    def test_handoff_false_does_not_signal_the_queue(self):
        handoff = mock.Mock(return_value=False)
        self.finished(self.result, handoff=handoff)
        handoff.assert_called_once()
        self.notifications.assert_not_called()
        self.assertIs(self.manager.adapter_host._active_process, self.process)

    def test_missing_handoff_cannot_bypass_a_real_host_owner(self):
        self.finished(self.result)
        self.notifications.assert_not_called()
        self.assertIs(self.manager.adapter_host._active_process, self.process)

    def test_swallowed_primary_and_fallback_store_failure_is_not_an_ack(self):
        handoff = mock.Mock(return_value=True)
        with mock.patch.object(self.store, "update_run_attempt", side_effect=RuntimeError("offline Store failure")):
            self.finished(self.result, handoff=handoff)
        handoff.assert_not_called()
        self.notifications.assert_not_called()
        self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "running")
        self.assertIs(self.manager.adapter_host._active_process, self.process)

    def test_failed_fallback_run_write_rolls_back_attempt_and_does_not_ack(self):
        handoff = mock.Mock(return_value=True)
        with mock.patch.object(self.manager, "_finish_adapter_result", side_effect=RuntimeError("offline primary failure")), \
                mock.patch.object(self.store, "update_game_run", side_effect=RuntimeError("offline fallback Run write failure")):
            self.finished(self.result, handoff=handoff)
        self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "running")
        self.assertIsNone(self.store.get_run_attempt(self.plan.run_attempt_id)["completed_at"])
        self.assertEqual(self.store.get_game_run(self.plan.run_id)["state"], "running")
        handoff.assert_not_called()
        self.notifications.assert_not_called()

    def test_failed_fallback_lease_or_membership_write_is_atomic_and_unacknowledged(self):
        for operation in ("_end_controller_lease", "_terminalize_unsealed_batch_memberships"):
            with self.subTest(operation=operation):
                handoff = mock.Mock()
                with mock.patch.object(self.manager, "_finish_adapter_result", side_effect=RuntimeError("offline primary failure")), \
                        mock.patch.object(self.manager, operation, side_effect=RuntimeError("offline fallback write failure")):
                    self.finished(self.result, handoff=handoff)
                self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "running")
                self.assertEqual(self.store.get_game_run(self.plan.run_id)["state"], "running")
                self.assertIsNotNone(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id, active_only=True))
                handoff.assert_not_called()
                self.notifications.assert_not_called()

    def test_complete_fallback_never_notifies_the_original_success_result(self):
        success = replace(self.result, status="completed", transport_outcome="clean", protocol_valid=True,
                          completed_todo_instance_ids=tuple(self.run["todo_instance_ids"]), unresolved_todo_instance_ids=())
        handoff = mock.Mock(side_effect=self.release)
        with mock.patch.object(self.manager, "_finish_adapter_result", side_effect=RuntimeError("offline primary failure")):
            self.finished(success, handoff=handoff)
        handoff.assert_called_once()
        notified = self.notifications.call_args.args[0]
        self.assertEqual(notified.status, "failed")
        self.assertEqual(notified.code, "manager_persistence_failed")
        self.assertFalse(notified.protocol_valid)
        self.assertEqual(notified.completed_todo_instance_ids, ())

    def test_partial_member_write_rolls_back_without_touching_a_queued_peer(self):
        peer = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        batch = self.fixture._create_batch_with_runs([self.run, peer], state="running")
        self.store.update_batch_run_membership(batch["batch_id"], self.plan.run_id, state="active",
            latest_run_attempt_id=self.plan.run_attempt_id)
        original = self.store.get_batch_run_membership(batch["batch_id"], self.plan.run_id)
        peer_before = self.store.get_batch_run_membership(batch["batch_id"], peer["run_id"])
        observed = []

        def partial(**kwargs):
            self.store.update_batch_run_membership(batch["batch_id"], self.plan.run_id, state="terminal",
                latest_run_attempt_id=self.plan.run_attempt_id, terminal_outcome="failed")
            observed.append(self.store.get_batch_run_membership(batch["batch_id"], self.plan.run_id)["state"])
            raise RuntimeError("offline failure after actual membership write")

        handoff = mock.Mock()
        with mock.patch.object(self.manager, "_finish_adapter_result", side_effect=RuntimeError("offline primary failure")), \
                mock.patch.object(self.manager, "_terminalize_unsealed_batch_memberships", side_effect=partial):
            self.finished(self.result, handoff=handoff)
        self.assertEqual(observed, ["terminal"])
        self.assertEqual(self.store.get_batch_run_membership(batch["batch_id"], self.plan.run_id), original)
        self.assertEqual(self.store.get_batch_run_membership(batch["batch_id"], peer["run_id"]), peer_before)
        self.assertEqual(self.store.get_game_run(peer["run_id"])["state"], "queued")
        self.assertEqual(self.store.list_run_attempts(run_id=peer["run_id"], limit=1), [])
        handoff.assert_not_called()
        self.notifications.assert_not_called()

    def test_terminal_human_can_ack_host_without_revoking_its_human_lease(self):
        def persist(plan, result, **kwargs):
            self.store.update_run_attempt(plan.run_attempt_id, state="human_required", completed=True)
            self.store.update_game_run(plan.run_id, state="human_required", message="offline human gate")
            return result

        handoff = mock.Mock(return_value=True)
        with mock.patch.object(self.manager, "_finish_adapter_result", side_effect=persist):
            self.finished(replace(self.result, status="human_required", protocol_valid=True,
                                  transport_outcome="clean"), handoff=handoff)
        handoff.assert_called_once()
        self.assertIsNotNone(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id, active_only=True))
        self.notifications.assert_called_once()
        self.assertEqual(self.notifications.call_args.args[0].status, "human_required")

    def test_notification_failure_after_handoff_does_not_repeat_or_roll_back_commit(self):
        self.notifications.side_effect = RuntimeError("offline coordinator notification failure")
        handoff = mock.Mock(side_effect=self.release)
        with self.assertRaisesRegex(RuntimeError, "notification failure"):
            self.finished(self.result, handoff=handoff)
        handoff.assert_called_once()
        self.notifications.assert_called_once()
        self.assertIsNone(self.manager.adapter_host._active_process)
        self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "failed")

    def test_host_budget_provider_reads_typed_config_without_mutating_it(self):
        self.assertEqual(self.manager.adapter_host._queue_timeout_budget_resolver().host_stop_grace_seconds, 5)
        self.store.update_config({"queue_timeout_budget": {"host_stop_grace_seconds": 0.125}})
        before = self.store.latest_event_sequence()
        self.assertEqual(self.manager.adapter_host._queue_timeout_budget_resolver().host_stop_grace_seconds, 0.125)
        self.assertEqual(self.store.latest_event_sequence(), before)

    def test_ack_read_failure_does_not_release_owner_or_notify(self):
        original_finish = self.manager._finish_adapter_result
        original_read = self.store.get_run_attempt
        committed = []

        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            committed.append(True)
            return result

        def read(*args, **kwargs):
            if committed:
                raise RuntimeError("offline terminal ACK read failure")
            return original_read(*args, **kwargs)

        handoff = mock.Mock()
        with mock.patch.object(self.manager, "_finish_adapter_result", side_effect=finish), \
                mock.patch.object(self.store, "get_run_attempt", side_effect=read), \
                self.assertRaisesRegex(RuntimeError, "ACK read failure"):
            self.finished(self.result, handoff=handoff)
        handoff.assert_not_called()
        self.notifications.assert_not_called()
        self.assertIs(self.manager.adapter_host._active_process, self.process)

    def test_log_close_failure_prevents_handoff(self):
        handoff = mock.Mock()
        with mock.patch.object(self.manager, "_close_attempt_log", side_effect=RuntimeError("offline log close failure")), \
                self.assertRaisesRegex(RuntimeError, "log close failure"):
            self.finished(self.result, handoff=handoff)
        handoff.assert_not_called()
        self.notifications.assert_not_called()
        self.assertIs(self.manager.adapter_host._active_process, self.process)
        self.manager._close_attempt_log(self.plan.run_attempt_id)

    def actual_watcher(self):
        host = self.manager.adapter_host
        manifest = verify_execution_package(host.compatibility_adapter.execution_package_root(self.plan.game_id))
        self.process.stdin = None
        self.process.stdout = io.BytesIO()
        self.process.stderr = io.BytesIO()
        self.process.wait = mock.Mock(return_value=0)
        self.process.terminate = mock.Mock()
        self.process.kill = mock.Mock()
        authority = host._write_control_authority(self.plan)
        host._watch_execution_bound(process=self.process, plan=self.plan, manifest=manifest,
            host_probe={}, on_event=None, on_complete=self.finished)
        return authority

    def test_actual_watcher_and_manager_deliver_only_after_store_ack_and_host_release(self):
        self.notifications.side_effect = lambda result: self.assertIsNone(self.manager.adapter_host.active_execution())
        authority = self.actual_watcher()
        self.notifications.assert_called_once()
        self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "failed")
        self.assertIsNone(self.manager.adapter_host._active_process)
        self.assertFalse(authority.exists())

    def test_actual_watcher_keeps_owner_and_material_when_manager_cannot_commit(self):
        with mock.patch.object(self.store, "update_run_attempt", side_effect=RuntimeError("offline Store unavailable")):
            authority = self.actual_watcher()
        self.notifications.assert_not_called()
        self.assertIs(self.manager.adapter_host._active_process, self.process)
        self.assertTrue(authority.exists())


if __name__ == "__main__":
    unittest.main()
