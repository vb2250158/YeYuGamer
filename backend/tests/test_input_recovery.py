from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services import dispatch_reservation as dispatch
from yeyu_gamer_manager.services import input_recovery as recovery
from yeyu_gamer_manager.store.sqlite_store import SqliteStore


class Receipt:
    pass


class OwnedContext:
    def __init__(self, scope):
        self.scope = MappingProxyType(scope)
        self.receipt = Receipt()
        self.stopped = []

    def matches(self, scope):
        return dict(scope) == dict(self.scope)

    def stop_and_drain(self, seconds):
        self.stopped.append(seconds)
        return self.receipt

    def verify_receipt(self, receipt, scope):
        return receipt is self.receipt and self.matches(scope)


class InputRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.setUp()
        self.manager, self.store = self.fixture.manager, self.fixture.store
        self.original_host = self.manager.adapter_host
        self.spawn = mock.patch("subprocess.Popen", side_effect=AssertionError("No external process permitted")).start()
        self.addCleanup(mock.patch.stopall)
        self.run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        self.next_run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.batch = self.fixture._create_batch_with_runs([self.run, self.next_run])
        self.plan = self.manager._prepare_run_attempt(self.run)
        dispatch.claim_start(self.manager, self.plan)
        self.scope = recovery.scope_for_plan(self.manager, self.plan, "sha256:" + "a" * 64)
        self.ctx = OwnedContext(self.scope)
        self.host = SimpleNamespace(owned_execution_for=mock.Mock(return_value=self.ctx),
                                    active_execution=mock.Mock(return_value=None),
                                    release_owned_execution=mock.Mock(side_effect=self.release))
        self.manager.adapter_host = self.host
        self.deadline = datetime.fromisoformat(self.store.get_resource(
            "attempt-dispatch", f"dispatch:{self.plan.run_attempt_id}")["document"]["deadlineAt"])

    def release(self, plan, ctx, receipt):
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["state"], "review_required")
        self.assertIsNotNone(recovery.attempt_recovery_record(self.manager, plan.run_attempt_id))
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        return ctx.verify_receipt(receipt, ctx.scope)

    def recover(self):
        return recovery.recover_attempt(self.manager, self.batch["batch_id"], self.plan, lambda: self.deadline)

    def snapshot(self):
        return (self.store.get_game_run(self.plan.run_id), self.store.get_run_attempt(self.plan.run_attempt_id),
                self.store.get_batch(self.batch["batch_id"]),
                self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id),
                self.store.latest_event_sequence())

    def test_scope_contains_exact_lease_and_frozen_membership(self):
        lease = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)
        self.assertEqual(set(self.scope), {"runId", "runAttemptId", "gameId", "accountId", "desktopId",
                                          "controllerLeaseId", "controllerLeaseGeneration", "memberBatchId", "packageDigest"})
        self.assertEqual(self.scope["controllerLeaseId"], lease["controller_lease_id"])
        self.assertEqual(self.scope["controllerLeaseGeneration"], lease["generation"])
        self.assertEqual(self.scope["memberBatchId"], self.batch["batch_id"])
        attempt = self.store.get_run_attempt(self.plan.run_attempt_id)
        self.assertEqual(attempt["result"]["packageDigest"], self.scope["packageDigest"])
        self.assertEqual(attempt["result"]["ownedInputScope"], self.scope)

    def test_same_owned_scope_is_idempotent_without_changing_state_or_event_version(self):
        before = self.snapshot()
        self.assertEqual(recovery.scope_for_plan(self.manager, self.plan, self.scope["packageDigest"]), self.scope)
        self.assertEqual(self.snapshot(), before)

    def test_changed_package_or_persisted_scope_is_rejected_without_start(self):
        before = self.snapshot()
        with self.assertRaises(dispatch.DispatchReservationRejected):
            recovery.scope_for_plan(self.manager, self.plan, "sha256:" + "d" * 64)
        self.assertEqual(self.snapshot(), before)
        self.store.update_run_attempt(self.plan.run_attempt_id, state="starting", completed=False,
            result={"ownedInputScope": {**self.scope, "memberBatchId": "wrong-batch"}})
        before = self.snapshot()
        with self.assertRaises(dispatch.DispatchReservationRejected):
            recovery.scope_for_plan(self.manager, self.plan, self.scope["packageDigest"])
        self.assertEqual(self.snapshot(), before)
        self.spawn.assert_not_called()

    def test_scope_persistence_failure_precedes_host_job_and_popen(self):
        self.store.update_run_attempt(self.plan.run_attempt_id, state="starting", completed=False,
                                     result={"ownedInputScope": None, "packageDigest": None})
        before = self.snapshot()
        self.assertTrue(self.original_host._owned_input_scope_resolver is not None)
        with mock.patch.object(self.original_host, "probe", return_value={"hostHealthy": True}), \
                mock.patch.object(self.original_host, "_verified_execution_manifest",
                                  return_value=SimpleNamespace(package_digest=self.scope["packageDigest"])), \
                mock.patch.object(self.original_host, "_owned_input_qualification_resolver", return_value=True), \
                mock.patch("yeyu_gamer_manager.services.owned_input_execution.OwnedInputExecution") as job:
            with self.assertRaises(dispatch.DispatchReservationRejected):
                self.original_host.execute(self.plan.run_id, self.plan.game_id, mock.Mock(), plan=self.plan,
                                           installation_binding={"gamePath": "offline-only"})
            job.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.spawn.assert_not_called()

    def test_scope_db_write_failure_rolls_back_before_returning_to_host(self):
        self.store.update_run_attempt(self.plan.run_attempt_id, state="starting", completed=False,
                                     result={"packageDigest": None})
        before = self.snapshot()
        with mock.patch.object(self.store, "update_run_attempt", side_effect=OSError("offline scope write failure")), \
                mock.patch.object(self.original_host, "probe", return_value={"hostHealthy": True}), \
                mock.patch.object(self.original_host, "_verified_execution_manifest",
                                  return_value=SimpleNamespace(package_digest=self.scope["packageDigest"])), \
                mock.patch.object(self.original_host, "_owned_input_qualification_resolver", return_value=True), \
                mock.patch("yeyu_gamer_manager.services.owned_input_execution.OwnedInputExecution") as job:
            with self.assertRaises(OSError):
                self.original_host.execute(self.plan.run_id, self.plan.game_id, mock.Mock(), plan=self.plan,
                                           installation_binding={"gamePath": "offline-only"})
            job.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.spawn.assert_not_called()

    def test_recovery_rejects_ctx_proof_when_store_scope_evidence_differs(self):
        self.store.update_run_attempt(self.plan.run_attempt_id, state="starting", completed=False,
            result={"ownedInputScope": {**self.scope, "packageDigest": "sha256:" + "e" * 64}})
        self.assertTrue(self.recover())
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.host.release_owned_execution.assert_not_called()
        self.assertEqual(self.ctx.stopped, [])

    def test_verified_receipt_commits_terminal_non_success_before_exact_release(self):
        todo = self.store.get_todo_instance(self.run["todo_instance_ids"][0])
        self.assertTrue(self.recover())
        self.host.release_owned_execution.assert_called_once_with(self.plan, self.ctx, self.ctx.receipt)
        self.assertEqual(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)["state"], "revoked")
        self.assertEqual(self.store.get_game_run(self.plan.run_id)["state"], "review_required")
        self.assertEqual(self.store.get_todo_instance(todo["todo_instance_id"]), todo)
        self.assertIsNone(recovery.desktop_quarantine(self.manager))
        self.assertFalse(self.recover())
        self.assertEqual(self.host.release_owned_execution.call_count, 1)
        self.spawn.assert_not_called()

    def test_quiet_receipt_without_scene_evidence_is_explicitly_missing_without_capture(self):
        artifacts_before = self.store.list_resources("artifact", 1000)
        with mock.patch.object(self.manager.window_capture, "capture") as capture:
            self.assertTrue(self.recover())
            capture.assert_not_called()
        document = recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["document"]
        self.assertTrue(document["proofVerified"])
        self.assertEqual(document["evidenceStatus"], "verified_internal_receipt")
        self.assertEqual(document["sceneEvidenceStatus"], "missing")
        self.assertEqual(document["evidenceRefs"], [])
        self.assertEqual(document["startedAt"], self.plan.issued_at)
        self.assertEqual(document["deadlineAt"], self.deadline.isoformat())
        self.assertEqual(document["logScope"], {"runId": self.plan.run_id, "runAttemptId": self.plan.run_attempt_id})
        self.assertEqual(self.store.list_resources("artifact", 1000), artifacts_before)

    def test_recovery_copies_only_existing_original_run_refs_and_preserves_artifacts(self):
        for run, artifact_id in ((self.run, "offline-original-evidence"), (self.next_run, "offline-other-evidence")):
            self.store.create_resource("artifact", resource_id=artifact_id, state="accepted",
                document={"kind": "offline-existing-evidence", "contentType": "application/json", "runId": run["run_id"]})
            self.store.transition_todo_instance(run["todo_instance_ids"][0], status="review_required",
                reason="offline preexisting evidence", evidence_refs=[artifact_id], run_id=run["run_id"],
                increment_attempt=False, requested_by="test")
        artifacts_before = self.store.list_resources("artifact", 1000)
        todo_before = self.store.get_todo_instance(self.run["todo_instance_ids"][0])
        with mock.patch.object(self.manager.window_capture, "capture") as capture:
            self.assertTrue(self.recover())
            capture.assert_not_called()
        document = recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["document"]
        self.assertEqual(document["evidenceRefs"], ["offline-original-evidence"])
        self.assertEqual(document["sceneEvidenceStatus"], "recorded")
        self.assertEqual(self.store.list_resources("artifact", 1000), artifacts_before)
        self.assertEqual(self.store.get_todo_instance(self.run["todo_instance_ids"][0]), todo_before)

    def test_unknown_owner_quarantines_and_preserves_host(self):
        self.host.owned_execution_for.return_value = None
        self.assertTrue(self.recover())
        self.host.release_owned_execution.assert_not_called()
        self.assertEqual(self.ctx.stopped, [])
        quarantine = recovery.desktop_quarantine(self.manager)
        self.assertEqual(quarantine["document"]["runAttemptId"], self.plan.run_attempt_id)
        self.assertTrue(quarantine["document"]["ownerRetained"])
        self.assertFalse(quarantine["document"]["businessCompleted"])
        self.assertNotIn(self.plan.run_attempt_id, recovery._pending_for(self.manager) or {})

    def test_mapping_receipt_is_never_accepted_even_if_verifier_returns_true(self):
        self.ctx.receipt = {"verified": True}
        self.assertTrue(self.recover())
        self.host.release_owned_execution.assert_not_called()
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))

    def test_scope_mismatch_does_not_stop_or_release_owner(self):
        self.ctx.scope = MappingProxyType({**self.scope, "controllerLeaseGeneration": self.scope["controllerLeaseGeneration"] + 1})
        self.assertTrue(self.recover())
        self.assertEqual(self.ctx.stopped, [])
        self.host.release_owned_execution.assert_not_called()
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.assertNotIn(self.plan.run_attempt_id, recovery._pending_for(self.manager) or {})

    def test_stop_exception_is_durable_quarantine_not_idle(self):
        self.ctx.stop_and_drain = mock.Mock(side_effect=TimeoutError("offline"))
        self.assertTrue(self.recover())
        self.host.release_owned_execution.assert_not_called()
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))

    def test_failed_release_retains_durable_admission_fence(self):
        self.host.release_owned_execution.side_effect = None
        self.host.release_owned_execution.return_value = False
        self.assertTrue(self.recover())
        self.assertEqual(recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["state"], "blocking")
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))

    def fail_final_clear(self):
        original = self.store.update_resource

        def fail(resource_type, resource_id, *, state, document):
            if resource_type == recovery.QUARANTINE_RESOURCE and state == "released":
                raise OSError("injected final quarantine clear failure")
            return original(resource_type, resource_id, state=state, document=document)

        with mock.patch.object(self.store, "update_resource", side_effect=fail):
            with self.assertRaises(OSError):
                self.recover()
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.host.release_owned_execution.assert_called_once_with(self.plan, self.ctx, self.ctx.receipt)
        self.assertEqual(recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["state"], "expired")

    def test_final_clear_failure_retains_exact_receipt_and_retries_without_releasing_twice(self):
        self.fail_final_clear()
        pending = recovery._pending_for(self.manager)[self.plan.run_attempt_id]
        self.assertIs(pending.plan, self.plan)
        self.assertIs(pending.ctx, self.ctx)
        self.assertIs(pending.receipt, self.ctx.receipt)
        self.assertTrue(pending.owner_released)
        self.assertTrue(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertIsNone(recovery.desktop_quarantine(self.manager))
        self.assertEqual(self.host.release_owned_execution.call_count, 1)
        self.assertNotIn(self.plan.run_attempt_id, recovery._pending_for(self.manager))
        self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))

    def test_admission_retries_only_outside_store_transaction(self):
        self.fail_final_clear()
        original = self.ctx.verify_receipt

        def verify(receipt, scope):
            self.assertFalse(self.store.connection.in_transaction)
            return original(receipt, scope)

        with mock.patch.object(self.ctx, "verify_receipt", side_effect=verify) as verifier:
            with self.store.atomic():
                self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
                with self.assertRaises(dispatch.DispatchReservationRejected):
                    recovery.require_desktop_available(self.manager)
                verifier.assert_not_called()
            recovery.require_desktop_available(self.manager)
            verifier.assert_called_once()
        self.assertIsNone(recovery.desktop_quarantine(self.manager))

    def test_release_false_retains_capability_and_formal_retry_can_release_later(self):
        self.host.release_owned_execution.side_effect = None
        self.host.release_owned_execution.return_value = False
        self.assertTrue(self.recover())
        self.assertEqual(recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["state"], "blocking")
        self.assertFalse(recovery._pending_for(self.manager)[self.plan.run_attempt_id].owner_released)
        self.host.release_owned_execution.side_effect = self.release
        self.assertTrue(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertEqual(self.host.release_owned_execution.call_count, 2)
        self.assertIsNone(recovery.desktop_quarantine(self.manager))

    def test_retry_rejects_aba_even_after_successful_physical_release(self):
        self.fail_final_clear()
        lease = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)
        self.store.create_run_attempt({"run_attempt_id": "offline-successor-attempt",
            "run_id": self.next_run["run_id"], "game_id": fixtures.GAME_ID, "cadence": "daily",
            "fencing_token_hash": "sha256:" + "b" * 64, "cancel_authority_hash": "sha256:" + "c" * 64,
            "plan": {"executableTodoInstanceIds": self.next_run["todo_instance_ids"],
                     "todos": [{"todoInstanceId": todo_id} for todo_id in self.next_run["todo_instance_ids"]]}})
        self.store.create_controller_lease({**lease, "controller_lease_id": "offline-successor-lease",
            "run_id": self.next_run["run_id"], "run_attempt_id": "offline-successor-attempt",
            "fencing_hash": "sha256:" + "b" * 64, "fencing_fingerprint": "sha256:" + "b" * 16,
            "generation": lease["generation"] + 1, "state": "active"})
        self.store.transition_controller_lease("offline-successor-lease", state="revoked",
                                               reason_code="offline", reason="closed successor does not erase ABA")
        self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.assertEqual(self.host.release_owned_execution.call_count, 1)

    def test_retry_rejects_forged_capability_and_fake_receipt(self):
        self.fail_final_clear()
        pending = recovery._pending_for(self.manager)
        original = pending[self.plan.run_attempt_id]
        for fake in (dict(plan=self.plan, ctx=self.ctx, receipt=self.ctx.receipt, owner_released=True),
                     replace(original, receipt={"verified": True}), replace(original, receipt=Receipt())):
            with self.subTest(fake=type(fake).__name__):
                pending[self.plan.run_attempt_id] = fake
                self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
                self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        pending[self.plan.run_attempt_id] = original
        self.assertTrue(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))

    def test_retry_rejects_disk_proof_without_this_manager_memory(self):
        self.fail_final_clear()
        rebuilt = SimpleNamespace(store=self.store, manager_id=self.manager.manager_id, adapter_host=self.host)
        self.assertFalse(recovery.retry_committed_recovery(rebuilt, self.plan.run_attempt_id))
        with self.assertRaises(dispatch.DispatchReservationRejected):
            recovery.require_desktop_available(rebuilt)
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))

    def test_retry_rejects_changed_membership_and_wrong_attempt(self):
        self.fail_final_clear()
        self.assertFalse(recovery.retry_committed_recovery(self.manager, "wrong-attempt"))
        self.store.update_batch_run_membership(self.batch["batch_id"], self.plan.run_id,
                                               state="terminal", latest_run_attempt_id=self.plan.run_attempt_id,
                                               terminal_outcome="not_started_changed")
        self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))

    def test_retry_rejects_cancelled_batch_without_touching_host(self):
        self.fail_final_clear()
        self.store.create_or_get_batch_cancel_request({"batch_id": self.batch["batch_id"],
            "reason": "offline cancellation", "requested_by": "test", "active_run_attempt_ids": []})
        self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.assertEqual(self.host.release_owned_execution.call_count, 1)

    def initial_drain_timeout(self):
        original = self.ctx.stop_and_drain

        def drain(seconds):
            self.assertFalse(self.store.connection.in_transaction)
            if not drain.called:
                drain.called = True
                return None
            return original(seconds)

        drain.called = False
        self.ctx.stop_and_drain = mock.Mock(side_effect=drain)
        self.assertTrue(self.recover())
        capability = recovery._pending_for(self.manager)[self.plan.run_attempt_id]
        self.assertIs(capability.ctx, self.ctx)
        self.assertIsNone(capability.receipt)
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.host.release_owned_execution.assert_not_called()

    def test_drain_timeout_can_later_retry_at_admission_without_business_success(self):
        self.initial_drain_timeout()
        todo_before = self.store.get_todo_instance(self.run["todo_instance_ids"][0])

        def release_after_late_proof(plan, ctx, receipt):
            self.assertTrue(recovery.attempt_recovery_record(self.manager, plan.run_attempt_id)["document"]["lateProofVerified"])
            self.assertFalse(self.store.connection.in_transaction)
            return self.release(plan, ctx, receipt)

        self.host.release_owned_execution.side_effect = release_after_late_proof
        recovery.require_desktop_available(self.manager)
        self.host.release_owned_execution.assert_called_once_with(self.plan, self.ctx, self.ctx.receipt)
        self.assertEqual(self.ctx.stop_and_drain.call_count, 2)
        self.assertIsNone(recovery.desktop_quarantine(self.manager))
        document = recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["document"]
        self.assertTrue(document["proofVerified"])
        self.assertFalse(document["businessCompleted"])
        self.assertEqual(document["sceneEvidenceStatus"], "missing")
        self.assertEqual(self.store.get_game_run(self.plan.run_id)["state"], "review_required")
        self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "review_required")
        self.assertEqual(self.store.get_todo_instance(todo_before["todo_instance_id"]), todo_before)
        self.spawn.assert_not_called()

    def test_late_drain_bad_proof_retains_quarantine_and_capability(self):
        self.initial_drain_timeout()
        before = self.snapshot()
        for receipt in (None, {"verified": True}, Receipt()):
            with self.subTest(receipt=type(receipt).__name__):
                self.ctx.stop_and_drain.side_effect = None
                self.ctx.stop_and_drain.return_value = receipt
                self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
                self.assertEqual(self.snapshot(), before)
                self.assertIsNone(recovery._pending_for(self.manager)[self.plan.run_attempt_id].receipt)
                self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
                self.host.release_owned_execution.assert_not_called()

    def test_late_proof_store_failure_rolls_back_and_retains_real_receipt_for_retry(self):
        self.initial_drain_timeout()
        original = self.store.update_resource
        before = self.snapshot()

        def fail_late_proof(resource_type, resource_id, *, state, document):
            if resource_type == recovery.QUARANTINE_RESOURCE and document.get("lateProofVerified"):
                raise OSError("offline late-proof transaction failure")
            return original(resource_type, resource_id, state=state, document=document)

        with mock.patch.object(self.store, "update_resource", side_effect=fail_late_proof):
            with self.assertRaises(OSError):
                recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id)
        self.assertEqual(self.snapshot(), before)
        self.assertIs(recovery._pending_for(self.manager)[self.plan.run_attempt_id].receipt, self.ctx.receipt)
        self.host.release_owned_execution.assert_not_called()
        self.assertTrue(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertEqual(self.ctx.stop_and_drain.call_count, 2)

    def test_late_proof_final_clear_failure_retries_without_redrain_or_double_release(self):
        self.initial_drain_timeout()
        original = self.store.update_resource

        def fail_clear(resource_type, resource_id, *, state, document):
            if resource_type == recovery.QUARANTINE_RESOURCE and state == "released":
                raise OSError("offline final clear failure after late proof")
            return original(resource_type, resource_id, state=state, document=document)

        with mock.patch.object(self.store, "update_resource", side_effect=fail_clear):
            with self.assertRaises(OSError):
                recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id)
        self.assertTrue(recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id)["document"]["lateProofVerified"])
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.assertTrue(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertEqual(self.ctx.stop_and_drain.call_count, 2)
        self.assertEqual(self.host.release_owned_execution.call_count, 1)

    def test_late_drain_rejects_aba_before_host_or_ctx_effect(self):
        self.initial_drain_timeout()
        lease = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)
        self.store.create_run_attempt({"run_attempt_id": "offline-late-drain-successor",
            "run_id": self.next_run["run_id"], "game_id": fixtures.GAME_ID, "cadence": "daily",
            "fencing_token_hash": "sha256:" + "b" * 64, "cancel_authority_hash": "sha256:" + "c" * 64,
            "plan": {"executableTodoInstanceIds": self.next_run["todo_instance_ids"],
                     "todos": [{"todoInstanceId": todo_id} for todo_id in self.next_run["todo_instance_ids"]]}})
        self.store.create_controller_lease({**lease, "controller_lease_id": "offline-late-drain-lease",
            "run_id": self.next_run["run_id"], "run_attempt_id": "offline-late-drain-successor",
            "fencing_hash": "sha256:" + "b" * 64, "fencing_fingerprint": "sha256:" + "b" * 16,
            "generation": lease["generation"] + 1, "state": "active"})
        self.store.transition_controller_lease("offline-late-drain-lease", state="revoked",
            reason_code="offline", reason="revoked successor still fences late-drain retry")
        self.assertFalse(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertEqual(self.ctx.stop_and_drain.call_count, 1)
        self.host.release_owned_execution.assert_not_called()
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))

    def test_unproven_live_owner_cannot_be_recovered_by_another_manager(self):
        self.initial_drain_timeout()
        rebuilt = SimpleNamespace(store=self.store, manager_id=self.manager.manager_id, adapter_host=self.host)
        self.assertFalse(recovery.retry_committed_recovery(rebuilt, self.plan.run_attempt_id))
        self.assertEqual(self.ctx.stop_and_drain.call_count, 1)
        self.host.release_owned_execution.assert_not_called()

    def test_retry_second_transaction_rechecks_cas_after_host_release(self):
        def release_and_change(plan, ctx, receipt):
            self.release(plan, ctx, receipt)
            self.store.update_game_run(plan.run_id, state="review_required", message="concurrent owned-run change")
            return True

        self.host.release_owned_execution.side_effect = release_and_change
        self.assertTrue(self.recover())
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.assertTrue(recovery._pending_for(self.manager)[self.plan.run_attempt_id].owner_released)
        self.assertTrue(recovery.retry_committed_recovery(self.manager, self.plan.run_attempt_id))
        self.assertEqual(self.host.release_owned_execution.call_count, 1)

    def test_database_failures_roll_back_all_writes_and_never_release(self):
        for operation in ("create_resource", "update_run_attempt", "update_game_run",
                          "transition_controller_lease", "update_batch_run_membership", "append_event"):
            with self.subTest(operation=operation):
                before = self.snapshot()
                with mock.patch.object(self.store, operation, side_effect=OSError("injected DB failure")):
                    with self.assertRaises(OSError):
                        self.recover()
                self.assertEqual(self.snapshot(), before)
                self.assertIsNone(recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id))
                self.assertIsNone(recovery.desktop_quarantine(self.manager))
                self.host.release_owned_execution.assert_not_called()

    def test_changed_attempt_during_drain_is_rejected_without_release(self):
        original = self.ctx.stop_and_drain

        def changed(seconds):
            self.store.update_run_attempt(self.plan.run_attempt_id, state="running", process_id=123)
            return original(seconds)

        self.ctx.stop_and_drain = changed
        self.assertFalse(self.recover())
        self.host.release_owned_execution.assert_not_called()
        self.assertIsNone(recovery.attempt_recovery_record(self.manager, self.plan.run_attempt_id))

    def test_deadline_not_yet_expired_has_no_host_effect(self):
        self.assertFalse(recovery.recover_attempt(self.manager, self.batch["batch_id"], self.plan,
                                                 lambda: datetime.now(timezone.utc)))
        self.host.owned_execution_for.assert_not_called()

    def test_scope_producer_rejects_lease_aba(self):
        self.store.transition_controller_lease(self.scope["controllerLeaseId"], state="revoked",
                                               reason_code="fixture", reason="offline lease ABA")
        with self.assertRaises(dispatch.DispatchReservationRejected):
            recovery.scope_for_plan(self.manager, self.plan, self.scope["packageDigest"])
        self.host.owned_execution_for.assert_not_called()

    def test_quarantine_survives_independent_store_reconstruction(self):
        self.host.owned_execution_for.return_value = None
        self.assertTrue(self.recover())
        reopened = SqliteStore(self.fixture.settings.database_path)
        reopened.initialize()
        self.addCleanup(reopened.close)
        rebuilt = SimpleNamespace(store=reopened)
        with self.assertRaises(dispatch.DispatchReservationRejected):
            recovery.require_desktop_available(rebuilt)
        self.assertEqual(recovery.desktop_quarantine(rebuilt)["document"]["controllerLeaseId"], self.scope["controllerLeaseId"])

    def test_quarantined_member_is_not_started_and_admission_precedes_effects(self):
        self.host.owned_execution_for.return_value = None
        self.assertTrue(self.recover())
        next_id = self.next_run["run_id"]
        with mock.patch.object(self.manager.game_launcher, "ensure_started") as launcher:
            with self.assertRaises(dispatch.DispatchReservationRejected):
                self.manager._start_game_run(next_id)
            with self.assertRaises(dispatch.DispatchReservationRejected):
                self.manager._prepare_run_attempt(self.next_run)
            self.assertTrue(recovery.skip_quarantined_member(self.manager, self.batch["batch_id"], next_id))
            launcher.assert_not_called()
        self.assertEqual(self.store.list_run_attempts(run_id=next_id), [])
        member = self.store.get_batch_run_membership(self.batch["batch_id"], next_id)
        self.assertEqual(member["terminal_outcome"], "not_started_desktop_quarantine")
        self.assertEqual(self.store.get_game_run(next_id)["state"], "review_required")
        self.spawn.assert_not_called()

    def test_late_results_and_events_do_not_touch_successor(self):
        self.assertTrue(self.recover())
        before = self.store.get_game_run(self.next_run["run_id"])
        self.manager._handle_adapter_event_inner(self.plan, SimpleNamespace(event_type="todo_terminal"))
        self.assertIsNone(self.manager._finish_adapter_result(self.plan, SimpleNamespace(status="completed", code="late")))
        self.assertEqual(self.store.get_game_run(self.next_run["run_id"]), before)
        self.assertEqual(self.store.get_game_run(self.run["run_id"])["state"], "review_required")
        self.assertEqual(self.store.list_adapter_events(self.plan.run_attempt_id), [])

    def test_batch_scans_all_quarantined_members_without_start_or_cleanup(self):
        self.host.owned_execution_for.return_value = None
        self.assertTrue(self.recover())
        third = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.store.add_batch_run_membership(self.batch["batch_id"], third["run_id"], ordinal=2,
                                            role="initial", state="queued")
        with mock.patch.object(self.manager, "_start_game_run") as start, \
                mock.patch.object(self.manager, "_close_finished_batch_game") as cleanup:
            self.manager._run_batch(self.batch["batch_id"], [
                {"runId": run["run_id"], "gameId": fixtures.GAME_ID} for run in (self.next_run, third)
            ], self.batch["batch_id"])
            start.assert_not_called()
            cleanup.assert_not_called()
        for run in (self.next_run, third):
            self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")
            member = self.store.get_batch_run_membership(self.batch["batch_id"], run["run_id"])
            self.assertEqual(member["terminal_outcome"], "not_started_desktop_quarantine")
        self.assertFalse(self.store.get_batch(self.batch["batch_id"])["result"]["acceptedDone"])
        self.spawn.assert_not_called()


class InputRecoveryStartFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.setUp()
        self.manager, self.store = self.fixture.manager, self.fixture.store
        self.first = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        self.second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.batch = self.fixture._create_batch_with_runs([self.first, self.second])
        self.plan = None

    def failed_execute(self, *args, plan, **kwargs):
        self.plan = plan
        self.manager.adapter_host._active_plan = plan
        raise TimeoutError("partial owned dispatch did not converge")

    def run_batch(self):
        return self.manager._run_batch(self.batch["batch_id"], [
            {"runId": run["run_id"], "gameId": fixtures.GAME_ID} for run in (self.first, self.second)
        ], self.batch["batch_id"])

    def test_start_failure_preserves_exact_owner_then_quarantines_before_next_member(self):
        with mock.patch.object(self.manager.adapter_host, "execute", side_effect=self.failed_execute) as execute, \
                mock.patch.object(self.manager.adapter_host, "release_owned_execution") as release, \
                mock.patch.object(self.manager, "_close_finished_batch_game") as cleanup:
            self.run_batch()
            execute.assert_called_once()
            release.assert_not_called()
            cleanup.assert_not_called()
        self.assertIs(self.manager.adapter_host.active_plan_for_run(self.first["run_id"]), self.plan)
        self.assertEqual(self.store.get_game_run(self.first["run_id"])["state"], "review_required")
        self.assertEqual(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)["state"], "revoked")
        self.assertIsNotNone(recovery.desktop_quarantine(self.manager))
        self.assertEqual(self.store.list_run_attempts(run_id=self.second["run_id"]), [])
        self.assertEqual(self.store.get_game_run(self.second["run_id"])["state"], "review_required")
        self.assertEqual(self.manager.game_launcher.ensure_started.call_count, 1)

    def test_start_failure_recovery_db_error_does_not_release_or_start_next(self):
        original_create = self.store.create_resource

        def fail_recovery(resource_type, *args, **kwargs):
            if resource_type == recovery.RECOVERY_RESOURCE:
                raise OSError("offline recovery transaction failure")
            return original_create(resource_type, *args, **kwargs)

        with mock.patch.object(self.manager.adapter_host, "execute", side_effect=self.failed_execute) as execute, \
                mock.patch.object(self.store, "create_resource", side_effect=fail_recovery), \
                mock.patch.object(self.manager.adapter_host, "release_owned_execution") as release:
            self.run_batch()
            execute.assert_called_once()
            release.assert_not_called()
        self.assertIs(self.manager.adapter_host.active_plan_for_run(self.first["run_id"]), self.plan)
        self.assertEqual(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)["state"], "active")
        self.assertEqual(self.store.get_run_attempt(self.plan.run_attempt_id)["state"], "running")
        self.assertEqual(self.store.get_game_run(self.second["run_id"])["state"], "queued")
        self.assertEqual(self.manager.game_launcher.ensure_started.call_count, 1)


if __name__ == "__main__":
    unittest.main()
