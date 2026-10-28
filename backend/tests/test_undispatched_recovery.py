from datetime import datetime, timedelta, timezone
import hashlib
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services import dispatch_reservation as dispatch
from yeyu_gamer_manager.services.adapter_protocol import AdapterRunResult


class UndispatchedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager, self.store = self.fixture.manager, self.fixture.store
        self.now = datetime.now(timezone.utc)
        self.addCleanup(mock.patch.stopall)
        mock.patch("yeyu_gamer_manager.services.manager.utc_now", side_effect=lambda: self.now).start()
        mock.patch.object(dispatch._log, "exception").start()
        self.run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.batch = self.fixture._create_batch_with_runs([self.run])
        self.spawn = mock.patch("subprocess.Popen", side_effect=AssertionError("No external process permitted")).start()
        self.plan = self.manager._prepare_run_attempt(self.run)
        self.lease = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)
        self.deadline = datetime.fromisoformat(self.resource()["document"]["deadlineAt"])

    def resource(self):
        return self.store.get_resource(dispatch.RESOURCE_TYPE, dispatch._resource_id(self.plan))

    def recover(self):
        self.now = self.deadline
        return dispatch.recover_undispatched(self.manager, self.batch["batch_id"], self.plan, lambda: self.deadline)

    def snapshot(self):
        return (self.resource(), self.store.get_run_attempt(self.plan.run_attempt_id),
                self.store.get_game_run(self.plan.run_id),
                self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id),
                self.store.get_batch_run_membership(self.batch["batch_id"], self.plan.run_id))

    def assert_rejected_unchanged(self):
        before = self.snapshot()
        self.assertFalse(self.recover())
        self.assertEqual(before, self.snapshot())

    def test_prepare_registers_exact_scope_and_frozen_deadline(self):
        document = self.resource()["document"]
        self.assertEqual(self.resource()["state"], "reserved")
        self.assertEqual(document["scope"]["version"], 2)
        self.assertEqual(document["scope"]["runAttemptId"], self.plan.run_attempt_id)
        self.assertEqual(document["controllerLeaseId"], self.lease["controller_lease_id"])
        self.assertEqual(document["controllerLeaseGeneration"], self.lease["generation"])
        self.store.update_config({"queue_timeout_budget": {"callback_overhead_seconds": 200}})
        self.assertEqual(datetime.fromisoformat(self.resource()["document"]["deadlineAt"]), self.deadline)
        with self.assertRaises(dispatch.DispatchReservationRejected):
            dispatch.register_prepared(self.manager, self.plan, self.lease["controller_lease_id"], self.lease["generation"])

    def test_recovery_terminalizes_only_original_without_business_success(self):
        todo_before = self.store.get_todo_instance(self.run["todo_instance_ids"][0])
        self.assertTrue(self.recover())
        self.assertEqual(self.resource()["state"], "expired")
        attempt = self.store.get_run_attempt(self.plan.run_attempt_id)
        self.assertEqual(attempt["state"], "review_required")
        self.assertTrue(attempt["completed_at"])
        self.assertFalse(attempt["result"]["businessCompleted"])
        self.assertEqual(self.store.get_game_run(self.plan.run_id)["state"], "review_required")
        self.assertEqual(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)["state"], "revoked")
        self.assertEqual(self.store.get_batch_run_membership(self.batch["batch_id"], self.plan.run_id)["state"], "terminal")
        self.assertEqual(self.store.get_todo_instance(todo_before["todo_instance_id"]), todo_before)
        self.assert_rejected_unchanged()
        with self.assertRaises(dispatch.DispatchReservationRejected):
            dispatch.claim_start(self.manager, self.plan)
        self.spawn.assert_not_called()

    def test_claim_is_irreversible_and_validation_does_not_reclaim(self):
        self.assertTrue(dispatch.claim_start(self.manager, self.plan))
        self.assertTrue(dispatch.validate_start(self.manager, self.plan))
        with self.assertRaises(dispatch.DispatchReservationRejected):
            dispatch.claim_start(self.manager, self.plan)
        self.assert_rejected_unchanged()
        self.assertEqual(self.resource()["state"], "claimed")
        self.spawn.assert_not_called()

    def test_manager_host_guard_rejects_reserved_and_expired_before_probe_or_materials(self):
        host = self.manager.adapter_host
        for state in ("reserved", "expired"):
            if state == "expired":
                self.assertTrue(self.recover())
            with self.subTest(state=state), mock.patch.object(host, "probe") as probe, \
                    mock.patch.object(host, "_artifact_staging_dir") as staging, \
                    mock.patch.object(host, "_write_installation_binding") as binding, \
                    mock.patch.object(host, "_write_control_authority") as authority:
                with self.assertRaises(dispatch.DispatchReservationRejected):
                    host.execute(self.plan.run_id, self.plan.game_id, mock.Mock(), plan=self.plan,
                                 installation_binding={"gamePath": "fixture-only"})
                probe.assert_not_called()
                staging.assert_not_called()
                binding.assert_not_called()
                authority.assert_not_called()
                self.spawn.assert_not_called()

    def test_manager_host_guard_validates_claimed_before_probe(self):
        host = self.manager.adapter_host
        dispatch.claim_start(self.manager, self.plan)

        def probe_after_guard():
            guard.assert_called_once_with(self.manager, self.plan)
            raise RuntimeError("offline probe boundary")

        with mock.patch.object(dispatch, "validate_start", wraps=dispatch.validate_start) as guard, \
                mock.patch.object(host, "probe", side_effect=probe_after_guard):
            with self.assertRaisesRegex(RuntimeError, "offline probe boundary"):
                host.execute(self.plan.run_id, self.plan.game_id, mock.Mock(), plan=self.plan,
                             installation_binding={"gamePath": "fixture-only"})
        self.assertEqual(self.resource()["state"], "claimed")
        self.spawn.assert_not_called()

    def test_dispatch_resource_does_not_collide_with_human_parking(self):
        parking = self.store.create_resource("queue-parking", resource_id=self.plan.run_attempt_id,
            state="awaiting", document={"runAttemptId": self.plan.run_attempt_id})
        self.assertEqual(self.resource()["state"], "reserved")
        self.assertTrue(self.recover())
        self.assertEqual(self.store.get_resource("queue-parking", self.plan.run_attempt_id), parking)

    def test_missing_legacy_record_is_not_synthesized(self):
        with self.store.atomic():
            self.store.connection.execute("DELETE FROM manager_resources WHERE resource_type=? AND resource_id=?",
                                          (dispatch.RESOURCE_TYPE, dispatch._resource_id(self.plan)))
        before = self.store.latest_event_sequence()
        self.assertFalse(self.recover())
        with self.assertRaises(Exception):
            dispatch.claim_start(self.manager, self.plan)
        with self.assertRaises(Exception):
            dispatch.validate_start(self.manager, self.plan)
        self.assertEqual(self.store.latest_event_sequence(), before)

    def test_deadline_must_have_elapsed(self):
        before = self.snapshot()
        self.assertFalse(dispatch.recover_undispatched(self.manager, self.batch["batch_id"], self.plan,
                                                     lambda: self.deadline - timedelta(microseconds=1)))
        self.assertEqual(self.snapshot(), before)

    def test_owner_process_plan_job_and_compatibility_each_block_recovery(self):
        host = self.manager.adapter_host
        for name, value in (("_active_process", SimpleNamespace(pid=123)), ("_active_plan", self.plan),
                            ("_owned_job", object()), ("_owned_jobs", {"unknown": object()})):
            with self.subTest(name=name), mock.patch.object(host, name, value, create=True), \
                    mock.patch.object(host, "active_execution", return_value=None):
                self.assert_rejected_unchanged()
        with mock.patch.object(host.compatibility_adapter, "active_execution", return_value={"exited": True}):
            self.assert_rejected_unchanged()
        with mock.patch.object(host, "active_execution", side_effect=OSError("unknown owner")):
            self.assert_rejected_unchanged()

    def test_pid_running_cancel_seal_and_changed_member_are_rejected(self):
        original_attempt = self.store.get_run_attempt(self.plan.run_attempt_id)
        for changes in ({"process_id": 123}, {"state": "running"},
                        {"state": "cancelling", "result": {"code": "user_cancelled"}}):
            with self.subTest(changes=changes):
                with mock.patch.object(self.store, "get_run_attempt", return_value={**original_attempt, **changes}):
                    self.assert_rejected_unchanged()
        for state in ("cancelled", "cancelling"):
            original_run = self.store.get_game_run(self.plan.run_id)
            with self.subTest(state=state), mock.patch.object(self.store, "get_game_run", return_value={**original_run, "state": state}):
                self.assert_rejected_unchanged()
        original_batch = self.store.get_batch(self.batch["batch_id"])
        with mock.patch.object(self.store, "get_batch", return_value={**original_batch, "result": {"sealVersion": 1}}):
            self.assert_rejected_unchanged()
        with mock.patch.object(self.store, "get_active_batch_cancel_request", return_value={"state": "requested"}):
            self.assert_rejected_unchanged()
        self.store.update_batch_run_membership(self.batch["batch_id"], self.plan.run_id, state="reconciliation_required")
        self.assert_rejected_unchanged()

    def test_scope_latest_attempt_and_lease_aba_are_rejected(self):
        with mock.patch.object(self.store, "list_run_attempts", return_value=[{"run_attempt_id": "successor"}]):
            self.assert_rejected_unchanged()
        for field, value in (("controller_lease_id", "replacement"), ("generation", self.lease["generation"] + 1)):
            with self.subTest(field=field), mock.patch.object(self.store, "get_controller_lease_for_attempt",
                                                             return_value={**self.lease, field: value}):
                self.assert_rejected_unchanged()
        with mock.patch.object(self.store, "list_controller_leases_private", return_value=[
                self.lease, {**self.lease, "generation": self.lease["generation"] + 1, "state": "released"}]):
            self.assert_rejected_unchanged()
        resource = self.resource()
        self.store.update_resource(dispatch.RESOURCE_TYPE, dispatch._resource_id(self.plan), state="reserved",
            document={**resource["document"], "scope": {**resource["document"]["scope"], "version": 1}})
        self.assert_rejected_unchanged()

    def test_timeout_generated_cancelling_can_recover(self):
        self.store.update_run_attempt(self.plan.run_attempt_id, state="cancelling", completed=False,
                                     result={"code": "manager_batch_wait_timeout"})
        self.assertTrue(self.recover())

    def run_coordinator_timeout(self, *, claimed=False, overhead=None, start_elapsed=0):
        self.assertTrue(self.recover())
        run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        batch = self.fixture._create_batch_with_runs([run])
        waits = []
        plans = []
        case = self

        class Waiter:
            def wait(self, seconds):
                waits.append(seconds)
                case.now += timedelta(seconds=seconds)
                return False

            def set(self):
                raise AssertionError("No callback is expected in this timeout fixture")

        def start(run_id, complete):
            plan = self.manager._prepare_run_attempt(self.store.get_game_run(run_id))
            plans.append(plan)
            if claimed:
                dispatch.claim_start(self.manager, plan)
                self.store.update_run_attempt(plan.run_attempt_id, state="running", completed=False)
            if overhead is not None:
                self.store.update_config({"queue_timeout_budget": {"callback_overhead_seconds": overhead}})
            self.now += timedelta(seconds=start_elapsed)
            return plan, 0

        proxy = SimpleNamespace(**vars(threading))
        proxy.Event = Waiter
        with mock.patch("yeyu_gamer_manager.services.manager.threading", proxy), \
                mock.patch.object(self.manager, "_start_game_run", side_effect=start), \
                mock.patch.object(self.manager.adapter_host, "cancel", return_value=False) as cancel:
            self.manager._run_batch(batch["batch_id"], [{"runId": run["run_id"], "gameId": run["game_id"]}], batch["batch_id"])
        return run, plans[0], waits, cancel

    def test_registered_deadline_survives_overhead_shrink_during_start(self):
        run, plan, waits, cancel = self.run_coordinator_timeout(overhead=1, start_elapsed=7)
        resource = self.store.get_resource(dispatch.RESOURCE_TYPE, dispatch._resource_id(plan))
        frozen_seconds = (datetime.fromisoformat(resource["document"]["deadlineAt"])
                          - datetime.fromisoformat(plan.issued_at)).total_seconds()
        self.assertEqual(waits, [frozen_seconds - 7])
        cancel.assert_not_called()
        self.assertEqual(resource["state"], "expired")
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")

    def test_first_timeout_reserved_skips_cancel_and_grace(self):
        run, plan, waits, cancel = self.run_coordinator_timeout()
        self.assertEqual(len(waits), 1)
        cancel.assert_not_called()
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["state"], "review_required")

    def test_start_overrun_and_larger_live_budget_do_not_extend_frozen_deadline(self):
        run, plan, waits, cancel = self.run_coordinator_timeout(overhead=200, start_elapsed=500)
        self.assertEqual(waits, [0.0])
        cancel.assert_not_called()
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["state"], "review_required")

    def test_callback_deadline_has_no_legacy_or_wrong_scope_fallback(self):
        resource = self.resource()
        self.store.update_resource(dispatch.RESOURCE_TYPE, dispatch._resource_id(self.plan), state="reserved",
            document={**resource["document"], "scope": {**resource["document"]["scope"], "version": 1}})
        with self.assertRaises(dispatch.DispatchReservationRejected):
            dispatch.callback_wait_seconds(self.manager, self.plan, lambda: self.now)

    def test_first_timeout_claimed_still_cancels_waits_grace_and_fences(self):
        run, plan, waits, cancel = self.run_coordinator_timeout(claimed=True)
        self.assertEqual(len(waits), 2)
        cancel.assert_called_once()
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["state"], "cancelling")
        self.assertEqual(self.store.get_resource(dispatch.RESOURCE_TYPE, dispatch._resource_id(plan))["state"], "claimed")
        self.assertIsNotNone(self.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True))

    def test_recovery_rolls_back_each_partial_write(self):
        for operation in ("update_resource", "update_run_attempt", "update_game_run",
                          "transition_controller_lease", "update_batch_run_membership", "append_event"):
            with self.subTest(operation=operation):
                before = self.snapshot()
                sequence = self.store.latest_event_sequence()
                with mock.patch.object(self.store, operation, side_effect=OSError("injected Store failure")):
                    self.assertFalse(self.recover())
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.store.latest_event_sequence(), sequence)

    def test_prepare_registration_failure_rolls_back_attempt_lease_and_member(self):
        self.assertTrue(self.recover())
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        batch = self.fixture._create_batch_with_runs([second])
        before = self.store.get_batch_run_membership(batch["batch_id"], second["run_id"])
        with mock.patch.object(dispatch, "register_prepared", side_effect=OSError("registration failed")):
            with self.assertRaises(OSError):
                self.manager._prepare_run_attempt(second)
        self.assertEqual(self.store.list_run_attempts(run_id=second["run_id"], limit=10), [])
        self.assertEqual(self.store.list_controller_leases(run_id=second["run_id"]), [])
        self.assertEqual(self.store.get_batch_run_membership(batch["batch_id"], second["run_id"]), before)

    def test_claim_commit_failure_does_not_authorize_start(self):
        before = self.snapshot()
        append = self.store.append_event

        def fail_claim(event_type, *args, **kwargs):
            if event_type == "attempt-dispatch.updated":
                raise OSError("claim transaction failure")
            return append(event_type, *args, **kwargs)

        with mock.patch.object(self.store, "append_event", side_effect=fail_claim), self.assertRaises(OSError):
            dispatch.claim_start(self.manager, self.plan)
        self.assertEqual(self.snapshot(), before)
        self.spawn.assert_not_called()

    def test_claim_and_recovery_race_has_only_one_winner(self):
        barrier = threading.Barrier(2)
        winners = []

        def claim():
            barrier.wait(timeout=5)
            try:
                winners.append(("claim", dispatch.claim_start(self.manager, self.plan)))
            except dispatch.DispatchReservationRejected:
                winners.append(("claim", False))

        def recover():
            barrier.wait(timeout=5)
            winners.append(("recover", self.recover()))

        threads = [threading.Thread(target=claim), threading.Thread(target=recover)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(winners), 2)
        self.assertEqual(sum(won for _, won in winners), 1)

    def test_recovery_never_queries_host_under_store_lock(self):
        host = self.manager.adapter_host
        original = host.active_execution

        def view():
            self.assertEqual(self.store._transaction_depth, 0)
            self.assertFalse(self.store._lock._is_owned())
            return original()

        with mock.patch.object(host, "active_execution", side_effect=view):
            self.assertTrue(self.recover())

    def test_start_claims_before_logs_or_any_launcher_or_host_effect(self):
        self.assertTrue(self.recover())
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        observed = []

        def effect(*args, **kwargs):
            latest = self.store.list_run_attempts(run_id=second["run_id"], limit=1)[0]
            self.assertEqual(self.store.get_resource(dispatch.RESOURCE_TYPE, "dispatch:" + latest["run_attempt_id"])["state"], "claimed")
            observed.append(True)

        with mock.patch.object(self.manager, "_open_attempt_log", side_effect=effect), \
                mock.patch.object(self.manager, "_close_other_batch_games", side_effect=effect), \
                mock.patch.object(self.manager.adapter_host, "execute", side_effect=lambda *args, **kwargs: (effect(), 123)[1]):
            self.manager.game_launcher.ensure_started.side_effect = lambda *args, **kwargs: (effect(), None)[1]
            self.manager._start_game_run(second["run_id"])
        self.assertGreaterEqual(len(observed), 4)

    def test_failed_start_claim_has_no_external_effects(self):
        self.assertTrue(self.recover())
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        with mock.patch.object(dispatch, "claim_start", side_effect=dispatch.DispatchReservationRejected("rejected")), \
                mock.patch.object(self.manager, "_open_attempt_log") as logs, \
                mock.patch.object(self.manager, "_close_other_batch_games") as cleanup, \
                mock.patch.object(self.manager.adapter_host, "execute") as execute:
            with self.assertRaises(dispatch.DispatchReservationRejected):
                self.manager._start_game_run(second["run_id"])
        logs.assert_not_called()
        cleanup.assert_not_called()
        execute.assert_not_called()
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.game_launcher.close_for_queue.assert_not_called()
        self.spawn.assert_not_called()

    def test_late_result_and_event_cannot_change_recovered_attempt(self):
        self.assertTrue(self.recover())
        next_run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        next_plan = self.manager._prepare_run_attempt(next_run)
        dispatch.claim_start(self.manager, next_plan)
        self.store.update_run_attempt(next_plan.run_attempt_id, state="running", completed=False)
        next_lease = self.store.get_controller_lease_for_attempt(next_plan.run_attempt_id)
        before = self.snapshot()
        result = AdapterRunResult(run_id=self.plan.run_id, run_attempt_id=self.plan.run_attempt_id,
            game_id=self.plan.game_id, status="completed", transport_outcome="clean", protocol_valid=True,
            attempted_todo_instance_ids=self.plan.executable_todo_instance_ids,
            completed_todo_instance_ids=self.plan.executable_todo_instance_ids, unresolved_todo_instance_ids=(),
            exit_code=0, code="late", message="late")
        self.assertIsNone(self.manager._finish_adapter_result(self.plan, result))
        with self.assertRaises(ValueError):
            self.store.append_adapter_event(run_attempt_id=self.plan.run_attempt_id, sequence=0,
                event_type="hello", payload={}, fencing_token_hash="sha256:" + hashlib.sha256(self.plan.fencing_token.encode()).hexdigest())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.get_controller_lease_for_attempt(next_plan.run_attempt_id), next_lease)
        self.assertEqual(self.store.get_run_attempt(next_plan.run_attempt_id)["state"], "running")
        with self.assertRaises(dispatch.DispatchReservationRejected):
            dispatch.claim_start(self.manager, self.plan)
        self.assert_rejected_unchanged()


if __name__ == "__main__":
    unittest.main()
