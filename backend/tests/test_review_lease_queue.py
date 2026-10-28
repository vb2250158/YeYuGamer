from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services.queue_parking import park_review_gate, review_gate_released
from yeyu_gamer_manager.services.adapter_protocol import AdapterRunResult


class ReviewLeaseQueueTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager, self.store = self.fixture.manager, self.fixture.store
        self.run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        self.batch = self.fixture._create_batch_with_runs([self.run], state="running")
        self.plan = self.manager._prepare_run_attempt(self.run)
        self.lease = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id, active_only=True)
        self.store.update_run_attempt(self.plan.run_attempt_id, state="review_required", completed=True)
        self.store.update_game_run(self.run["run_id"], state="review_required", message="offline exceptional retained lease")
        self.store.update_batch_run_membership(self.batch["batch_id"], self.run["run_id"], state="terminal",
            latest_run_attempt_id=self.plan.run_attempt_id, terminal_outcome="review_required")
        self.store.update_config({"queue_timeout_budget": {"completion_review_seconds": 0}})

    def test_expired_terminal_review_releases_only_its_frozen_lease_without_success(self):
        self.assertTrue(park_review_gate(self.manager, self.batch["batch_id"], self.run["run_id"], lambda: datetime.now(timezone.utc)))
        self.assertIsNone(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id, active_only=True))
        resource = self.store.get_resource("queue-parking", self.plan.run_attempt_id)
        self.assertEqual(resource["document"]["gateKind"], "completion_review")
        self.assertEqual(resource["document"]["controllerLeaseId"], self.lease["controller_lease_id"])
        self.assertFalse(resource["document"]["businessCompleted"])
        self.assertEqual(self.store.get_game_run(self.run["run_id"])["state"], "review_required")
        before = self.store.latest_event_sequence()
        self.assertFalse(park_review_gate(self.manager, self.batch["batch_id"], self.run["run_id"], lambda: datetime.now(timezone.utc)))
        self.assertEqual(self.store.latest_event_sequence(), before)

    def wait_case(self, transition):
        self.store.update_config({"queue_timeout_budget": {"completion_review_seconds": 1}})
        now = [datetime.now(timezone.utc)]

        def advance(timeout):
            now[0] += timedelta(seconds=timeout)
            transition(now[0])

        event = mock.Mock()
        event.wait.side_effect = advance
        with mock.patch("yeyu_gamer_manager.services.queue_parking.threading", SimpleNamespace(Event=lambda: event)):
            parked = park_review_gate(self.manager, self.batch["batch_id"], self.run["run_id"], lambda: now[0])
        return parked, now[0]

    def release_lease(self, at):
        self.store.transition_controller_lease(self.lease["controller_lease_id"], state="released",
            reason_code="offline_review_resolved", reason="offline normal review release", ended_at=at.isoformat())

    def test_normal_lease_release_during_wait_does_not_create_a_parking_debt(self):
        parked, at = self.wait_case(self.release_lease)
        self.assertFalse(parked)
        self.assertTrue(review_gate_released(self.manager, self.batch["batch_id"], self.run["run_id"], self.plan.run_attempt_id, lambda: at))
        resource = self.store.get_resource("queue-parking", self.plan.run_attempt_id)
        self.assertEqual(resource["state"], "released")
        self.assertEqual(self.store.get_game_run(self.run["run_id"])["state"], "review_required")

    def test_normal_review_state_change_and_lease_release_are_not_fenced_as_unknown(self):
        def resolve(at):
            self.release_lease(at)
            self.store.update_game_run(self.run["run_id"], state="blocked", message="offline reviewed incomplete")

        parked, at = self.wait_case(resolve)
        self.assertFalse(parked)
        self.assertTrue(review_gate_released(self.manager, self.batch["batch_id"], self.run["run_id"], self.plan.run_attempt_id, lambda: at))
        self.assertEqual(self.store.get_game_run(self.run["run_id"])["state"], "blocked")

    def test_new_owner_is_not_revoked_by_old_deadline(self):
        replacement = []

        def reacquire(at):
            self.release_lease(at)
            newer = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
            with mock.patch("yeyu_gamer_manager.services.manager.utc_now", return_value=at):
                plan = self.manager._prepare_run_attempt(newer)
            replacement.append(self.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True))

        parked, at = self.wait_case(reacquire)
        self.assertFalse(parked)
        active = self.store.get_controller_lease_private(replacement[0]["controller_lease_id"])
        self.assertEqual(active["state"], "active")
        self.assertFalse(review_gate_released(self.manager, self.batch["batch_id"], self.run["run_id"], self.plan.run_attempt_id, lambda: at))

    def test_exited_but_unhanded_off_host_is_not_assumed_silent(self):
        host = self.manager.adapter_host
        host._active_process = SimpleNamespace(pid=123456, poll=lambda: 0)
        host._active_plan = self.plan
        try:
            self.assertFalse(park_review_gate(self.manager, self.batch["batch_id"], self.run["run_id"], lambda: datetime.now(timezone.utc)))
            self.assertIsNotNone(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id, active_only=True))
        finally:
            host._active_process = None
            host._active_plan = None

    def test_queue_continues_when_review_finishes_during_the_wait(self):
        self.release_lease(datetime.now(timezone.utc))
        self.run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.batch = self.fixture._create_batch_with_runs([self.run, second])
        self.store.update_config({"queue_timeout_budget": {"completion_review_seconds": 1}})
        now = [datetime.now(timezone.utc)]
        started = []

        def start(run_id, callback):
            started.append(run_id)
            if run_id == self.run["run_id"]:
                plan = self.manager._prepare_run_attempt(self.store.get_game_run(run_id))
                self.plan = plan
                self.lease = self.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True)
                status = "review_required"
                self.store.update_run_attempt(plan.run_attempt_id, state=status, completed=True)
                self.store.update_game_run(run_id, state="review_required", message="offline retained lease")
            else:
                plan = self.manager._prepare_run_attempt(self.store.get_game_run(run_id))
                status = "completed"
                self.store.update_run_attempt(plan.run_attempt_id, state=status, completed=True)
                self.store.transition_controller_lease_for_attempt(plan.run_attempt_id, state="released",
                    reason_code="offline_finished", reason="offline second member finished")
            self.store.update_batch_run_membership(self.batch["batch_id"], run_id, state="terminal",
                latest_run_attempt_id=plan.run_attempt_id, terminal_outcome=status)
            callback(AdapterRunResult(run_id=run_id, run_attempt_id=plan.run_attempt_id, game_id=fixtures.GAME_ID,
                status=status, transport_outcome="clean", attempted_todo_instance_ids=(), completed_todo_instance_ids=(),
                unresolved_todo_instance_ids=(), exit_code=0, protocol_valid=True, code="offline_terminal", message="offline terminal"))
            return plan, 0

        def release_during_wait(seconds):
            now[0] += timedelta(seconds=seconds)
            self.release_lease(now[0])
            self.store.update_game_run(self.run["run_id"], state="blocked", message="offline review resolved as incomplete")

        event = mock.Mock()
        event.wait.side_effect = release_during_wait
        with mock.patch.object(self.manager, "_start_game_run", side_effect=start), \
                mock.patch("yeyu_gamer_manager.services.manager.utc_now", side_effect=lambda: now[0]), \
                mock.patch("yeyu_gamer_manager.services.queue_parking.threading", SimpleNamespace(Event=lambda: event)):
            self.manager._run_batch(self.batch["batch_id"], [
                {"runId": self.run["run_id"], "gameId": fixtures.GAME_ID},
                {"runId": second["run_id"], "gameId": fixtures.GAME_ID},
            ], self.batch["batch_id"])
        self.assertEqual(started, [self.run["run_id"], second["run_id"]])
        self.assertEqual(self.store.list_run_attempts(run_id=second["run_id"], limit=1)[0]["state"], "completed")
        self.assertEqual(self.store.get_game_run(self.run["run_id"])["state"], "blocked")
        self.assertNotEqual(self.store.get_batch(self.batch["batch_id"])["result"].get("recoveryPhase", {}).get("status"), "awaiting_reconciliation")


if __name__ == "__main__":
    unittest.main()
