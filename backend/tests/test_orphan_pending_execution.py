from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services.manager import ManagerConflict
from yeyu_gamer_manager.domain.models import RunControlRequest
from yeyu_gamer_manager.services.orphan_pending_execution import fail_resume_successor, recover_orphan_pending_executions, validate_resume_intent
from yeyu_gamer_manager.services.game_launcher import GameLaunchCancelled


class OrphanPendingExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.store = self.fixture.store

    def attempt(self, run, *, state="review_required", ordinal=1):
        todo_ids = run["todo_instance_ids"]
        attempt = self.store.create_run_attempt({
            "run_attempt_id": str(uuid4()), "run_id": run["run_id"],
            "game_id": fixtures.GAME_ID, "cadence": "daily", "attempt_ordinal": ordinal,
            "fencing_token_hash": "sha256:" + "a" * 64, "cancel_authority_hash": "sha256:" + "b" * 64,
            "plan": {"executableTodoInstanceIds": todo_ids, "todos": [{"todoInstanceId": todo_id} for todo_id in todo_ids]},
        })
        if state != "starting":
            attempt = self.store.update_run_attempt(attempt["run_attempt_id"], state=state,
                                                     result={"code": "offline_fixture"},
                                                     completed=state not in {"running", "cancelling"})
        return attempt

    def intent(self, run, attempt, *, state="successor_failed", revision=1):
        intent, _ = self.store.create_or_get_resume_intent({
            "run_id": run["run_id"], "predecessor_run_attempt_id": attempt["run_attempt_id"],
            "source_run_revision": revision, "source_attempt_revision": 1,
            "decision": {"fixtureRevision": revision}, "state": state,
        })
        return intent

    def orphan(self, *, intent_state="successor_failed", attempt_state="review_required", copies=1):
        run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        attempt = self.attempt(run, state=attempt_state)
        intents = [self.intent(run, attempt, state=intent_state, revision=index + 1) for index in range(copies)]
        batch = self.fixture._create_batch_with_runs([run], state="review_required")
        self.store.update_batch_run_membership(batch["batch_id"], run["run_id"], state="terminal",
                                               latest_run_attempt_id=attempt["run_attempt_id"],
                                               terminal_outcome=attempt_state)
        self.store.update_game_run(run["run_id"], state="pending_execution", message="offline failed successor reservation")
        return run, attempt, intents[-1], batch

    def test_failed_successor_orphan_does_not_occupy_execution_and_query_is_read_only(self):
        run, attempt, intent, batch = self.orphan(copies=5)
        before = self.store.latest_event_sequence()
        self.assertNotIn(run["run_id"], [row["runId"] for row in self.manager.execution_occupancy()["gameRuns"]])
        self.assertIn(run["run_id"], [row["runId"] for row in self.store.active_execution_summary()["gameRuns"]])
        self.assertEqual(len(self.store.orphan_pending_execution_candidates(run["run_id"])), 1)
        self.assertEqual(self.store.latest_event_sequence(), before)
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "pending_execution")

    def test_pending_execution_cancellation_is_formal_and_idempotent(self):
        run, attempt, intent, batch = self.orphan()
        arguments = dict(idempotency_key="offline-orphan-cancel", request_id=None,
                         path=f"/api/v1/game-runs/{run['run_id']}/cancel-requests",
                         expected_state_version=self.store.latest_event_sequence())
        request = {"reason": "offline cancellation", "requestedBy": "test"}
        first = self.manager.cancel_game_run(run["run_id"], request, **arguments)
        sequence = self.store.latest_event_sequence()
        second = self.manager.cancel_game_run(run["run_id"], request, **arguments)
        self.assertEqual(first.command_id, second.command_id)
        self.assertEqual(self.store.latest_event_sequence(), sequence)
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "cancelled")
        self.assertFalse(first.result["cancellationPending"])
        self.assertEqual(self.store.get_run_attempt(attempt["run_attempt_id"])["state"], "review_required")

    def test_successor_failure_restores_last_terminal_outcome_without_waiting_for_budget(self):
        run, attempt, intent, batch = self.orphan(intent_state="successor_pending")
        self.assertIn(run["run_id"], [row["runId"] for row in self.store.active_execution_summary()["gameRuns"]])
        closed = fail_resume_successor(self.manager, intent["resume_intent_id"], datetime.now(timezone.utc))
        self.assertEqual(closed, [run["run_id"]])
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")
        self.assertEqual(self.store.get_resume_intent(intent["resume_intent_id"])["state"], "successor_failed")
        self.assertNotIn("sealVersion", self.store.get_batch(batch["batch_id"])["result"])

    def test_orphan_timeout_is_bounded_idempotent_and_never_promotes_completion(self):
        run, attempt, intent, batch = self.orphan(attempt_state="completed")
        started = datetime.fromisoformat(self.store.get_game_run(run["run_id"])["updated_at"])
        self.assertEqual(recover_orphan_pending_executions(self.manager, started + timedelta(seconds=119)), [])
        self.assertEqual(recover_orphan_pending_executions(self.manager, started + timedelta(seconds=121)), [run["run_id"]])
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")
        sequence = self.store.latest_event_sequence()
        self.assertEqual(recover_orphan_pending_executions(self.manager, started + timedelta(seconds=1000)), [])
        self.assertEqual(sequence, self.store.latest_event_sequence())

    def test_host_owner_prevents_automatic_closeout_and_formal_unowned_cancel(self):
        run, attempt, intent, batch = self.orphan()
        with mock.patch.object(self.manager.adapter_host, "active_execution", return_value={"runId": run["run_id"]}):
            self.assertIn(run["run_id"], [row["runId"] for row in self.manager.execution_occupancy()["gameRuns"]])
            self.assertEqual(recover_orphan_pending_executions(self.manager, datetime.now(timezone.utc) + timedelta(hours=1)), [])
            with self.assertRaisesRegex(ManagerConflict, "owned Host"):
                self.manager.cancel_game_run(run["run_id"], {"reason": "offline"},
                    idempotency_key="owned-host-cancel", request_id=None, path="/offline/cancel",
                    expected_state_version=self.store.latest_event_sequence())
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "pending_execution")
        with mock.patch.object(self.manager.adapter_host, "active_execution", return_value={}):
            self.assertIn(run["run_id"], [row["runId"] for row in self.manager.execution_occupancy()["gameRuns"]])
        with mock.patch.object(self.manager.adapter_host, "active_execution", return_value={"runId": "offline-other-run"}):
            self.assertNotIn(run["run_id"], [row["runId"] for row in self.manager.execution_occupancy()["gameRuns"]])

    def test_active_attempt_or_pending_membership_or_new_intent_is_not_an_orphan(self):
        for kind in ("attempt", "membership", "intent"):
            with self.subTest(kind=kind):
                run, attempt, intent, batch = self.orphan()
                if kind == "attempt":
                    self.attempt(run, state="running", ordinal=2)
                elif kind == "membership":
                    self.store.update_batch_run_membership(batch["batch_id"], run["run_id"], state="resume_pending")
                else:
                    self.intent(run, attempt, state="successor_pending", revision=2)
                self.assertEqual(self.store.orphan_pending_execution_candidates(run["run_id"]), [])
                self.assertIn(run["run_id"], [row["runId"] for row in self.store.active_execution_summary()["gameRuns"]])

    def test_late_failure_of_old_intent_cannot_close_a_new_successor_reservation(self):
        run, attempt, old, batch = self.orphan(intent_state="successor_pending")
        newer = self.intent(run, attempt, state="successor_pending", revision=2)
        self.assertEqual(fail_resume_successor(self.manager, old["resume_intent_id"], datetime.now(timezone.utc)), [])
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "pending_execution")
        self.assertEqual(self.store.get_resume_intent(newer["resume_intent_id"])["state"], "successor_pending")

    def test_active_controller_lease_is_not_treated_as_an_expired_reservation(self):
        run, attempt, intent, batch = self.orphan()
        now = datetime.now(timezone.utc)
        todo = self.store.get_todo_instance(run["todo_instance_ids"][0])
        self.store.create_controller_lease({
            "controller_lease_id": str(uuid4()), "desktop_id": "offline-desktop", "manager_id": self.manager.manager_id,
            "run_id": run["run_id"], "run_attempt_id": attempt["run_attempt_id"], "game_id": fixtures.GAME_ID,
            "game_day_key": todo["period_key"], "holder_principal_id": "offline-manager",
            "fencing_hash": "sha256:" + "a" * 64, "fencing_fingerprint": "sha256:" + "a" * 16,
            "acquired_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(),
        })
        self.assertEqual(self.store.orphan_pending_execution_candidates(run["run_id"]), [])
        self.assertIn(run["run_id"], [row["runId"] for row in self.store.active_execution_summary()["gameRuns"]])

    def test_fresh_pending_without_failure_proof_remains_a_reservation(self):
        run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        self.store.update_game_run(run["run_id"], state="pending_execution", message="offline fresh reservation")
        self.assertEqual(self.store.orphan_pending_execution_candidates(run["run_id"]), [])
        self.assertIn(run["run_id"], [row["runId"] for row in self.store.active_execution_summary()["gameRuns"]])

    def test_resume_start_failure_through_formal_manager_path_restores_terminal_run(self):
        run, attempt, intent, batch = self.orphan()
        self.store.update_batch(batch["batch_id"], state="review_required", result={
            **batch["result"], "recoveryPhase": {"schemaVersion": 1, "status": "ready_for_resume"},
        })
        with mock.patch.object(self.manager, "_start_game_run", side_effect=ManagerConflict("offline successor admission failure")) as start:
            receipt = self.manager.create_run_control_request(
                run["run_id"], "resume", RunControlRequest(reason="offline resume failure", requested_by="test"),
                idempotency_key="orphan-start-failure", request_id=None,
                path=f"/api/v1/game-runs/{run['run_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        self.assertEqual(start.call_count, 1, repr(receipt.result))
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")
        self.assertFalse(any(self.store.active_execution_summary().values()))

    def test_context_omits_only_pure_same_batch_reservations_and_keeps_batch_lifecycle_guard(self):
        first = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        for run in (first, second):
            self.store.update_game_run(run["run_id"], state="queued", message="offline queued sibling")
        batch = self.fixture._create_batch_with_runs([first, second], state="running")
        before = self.store.active_execution_summary()
        scoped = self.store.active_execution_summary(owned_batch_id=batch["batch_id"], current_run_id=first["run_id"])
        self.assertIn(second["run_id"], [row["runId"] for row in before["gameRuns"]])
        self.assertNotIn(second["run_id"], [row["runId"] for row in scoped["gameRuns"]])
        self.assertEqual(scoped["batches"], before["batches"])
        self.attempt(second, state="running")
        scoped = self.store.active_execution_summary(owned_batch_id=batch["batch_id"], current_run_id=first["run_id"])
        self.assertIn(second["run_id"], [row["runId"] for row in scoped["gameRuns"]])

    def test_context_never_exempts_ambiguous_or_foreign_batch_reservations(self):
        first = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        for run in (first, second):
            self.store.update_game_run(run["run_id"], state="queued", message="offline queued sibling")
        batch = self.fixture._create_batch_with_runs([first, second], state="running")
        foreign = self.fixture._create_batch_with_runs([second], state="queued")
        scoped = self.store.active_execution_summary(owned_batch_id=batch["batch_id"], current_run_id=first["run_id"])
        self.assertIn(second["run_id"], [row["runId"] for row in scoped["gameRuns"]])
        self.assertIn(foreign["batch_id"], [row["batchId"] for row in scoped["batches"]])

    def test_old_intent_cannot_commit_a_lease_after_pending_review_pending_aba_during_package_io(self):
        run, attempt, old, batch = self.orphan(intent_state="successor_pending")
        snapshot = self.store.get_game_run(run["run_id"])
        entered, release = Event(), Event()
        now = datetime.now(timezone.utc)
        plan = SimpleNamespace(run_id=run["run_id"], run_attempt_id=str(uuid4()), game_id=fixtures.GAME_ID,
            fencing_token="fixture-fence", cancel_authority="fixture-cancel", executable_todo_instance_ids=tuple(run["todo_instance_ids"]),
            issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=1)).isoformat())

        def delayed_package(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("offline package wait")
            return plan

        with mock.patch.object(self.manager, "_build_execution_plan", side_effect=delayed_package), ThreadPoolExecutor(max_workers=1) as pool:
            preparing = pool.submit(self.manager._prepare_run_attempt, snapshot, resume_intent_id=old["resume_intent_id"])
            try:
                self.assertTrue(entered.wait(5))
                self.store.update_resume_intent(old["resume_intent_id"], state="successor_failed")
                self.store.update_game_run(run["run_id"], state="review_required", message="offline old failure")
                newer = self.intent(run, attempt, state="successor_pending", revision=2)
                self.store.update_game_run(run["run_id"], state="pending_execution", message="offline new reservation")
            finally:
                release.set()
            with self.assertRaisesRegex(GameLaunchCancelled, "generation changed"):
                preparing.result(timeout=5)
        self.assertEqual(len(self.store.list_run_attempts(run_id=run["run_id"])), 1)
        self.assertEqual(self.store.list_controller_leases(run_id=run["run_id"], active_only=True), [])
        self.assertEqual(self.store.get_resume_intent(newer["resume_intent_id"])["state"], "successor_pending")

    def test_resume_intent_must_own_the_exact_successor_at_host_handoff(self):
        run, attempt, intent, batch = self.orphan(intent_state="successor_pending")
        self.store.update_resume_intent(intent["resume_intent_id"], state="successor_pending",
                                       successor_run_attempt_id=attempt["run_attempt_id"])
        with self.assertRaisesRegex(GameLaunchCancelled, "does not own"):
            validate_resume_intent(self.manager, intent["resume_intent_id"], run["run_id"], successor_id=str(uuid4()))


if __name__ == "__main__":
    unittest.main()
