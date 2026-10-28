from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace, ModuleType
import unittest
from unittest import mock
from uuid import uuid4

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.domain.models import RunControlRequest
from yeyu_gamer_manager.services import queue_parking
from yeyu_gamer_manager.services.manager_errors import ManagerConflict
from yeyu_gamer_manager.store.sqlite_store import SqliteStore


QA_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_HASHES = {
    "services/human_gates.py": "a2ea026dd0834f7c89500c8143db6aa8d91eba701cfe705e0c2f210acfc4e2f0",
    "store/sqlite_store.py": "db85cd6497eac66109528b64f848725a969b3b2b7bd71c56fcdaefa6f2c56e45",
    "services/serial_observation_recovery.py": "df40c76229405a024d1f75462f867fd1a59109ca0596a7b47027d8578daf09db",
    "services/zzz_launcher_observation.py": "1d78f8eb1e3f9d8919d2e9fd1588497cb073b07e771f125c08359d8c930185c2",
}


def reference_function(source, name, namespace, class_name=None):
    syntax = ast.parse(source)
    nodes = syntax.body
    if class_name:
        nodes = next(node for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name).body
    function = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), "fixed-reference/" + name, "exec"), namespace)
    return namespace[name]


class ParkedHumanOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "PARKED_HUMAN_REFERENCE_ROOT" not in os.environ:
            raise unittest.SkipTest("Set PARKED_HUMAN_REFERENCE_ROOT to the explicit fixed-SHA source reference")
        root = Path(os.environ["PARKED_HUMAN_REFERENCE_ROOT"]) / "backend/yeyu_gamer_manager"
        cls.sources = {}
        for relative, expected in REFERENCE_HASHES.items():
            content = (root / relative).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected:
                raise AssertionError("Reference SHA mismatch: " + relative)
            cls.sources[relative] = content.decode("utf-8-sig")
        (QA_ROOT / ".cache").mkdir(parents=True, exist_ok=True)
        cls.overlay = tempfile.TemporaryDirectory(prefix="parked-human-", dir=QA_ROOT / ".cache")
        cls.addClassCleanup(cls.overlay.cleanup)
        target = Path(cls.overlay.name) / "backend/yeyu_gamer_manager/services/human_gates.py"
        target.parent.mkdir(parents=True)
        target.write_text(cls.sources["services/human_gates.py"], encoding="utf-8")
        subprocess.run(["git", "apply", "--unsafe-paths", "--directory=" + cls.overlay.name,
                        str(QA_ROOT / "integration-patches/parked-human-ownership.patch")],
                       cwd=QA_ROOT, check=True, capture_output=True)
        specification = importlib.util.spec_from_file_location(
            "yeyu_gamer_manager.services._parked_human_reference", target)
        cls.gates = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.gates)
        original = ModuleType("original_human_gates")
        original.__package__ = "yeyu_gamer_manager.services"
        exec(compile(cls.sources["services/human_gates.py"], "fixed-reference/human_gates.py", "exec"), vars(original))
        cls.original = original

    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager, self.store = self.fixture.manager, self.fixture.store
        for method_name in ("list_human_gate_candidates", "daily_stop_owned_run_ids"):
            method = reference_function(self.sources["store/sqlite_store.py"], method_name,
                                        {}, class_name="SqliteStore")
            patch = mock.patch.object(SqliteStore, method_name, method, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        self.run = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        self.batch = self.fixture._create_batch_with_runs([self.run], state="running")
        self.plan = self.manager._prepare_run_attempt(self.run)
        self.todo_id = self.plan.executable_todo_instance_ids[0]
        todo = self.store.get_todo_instance(self.todo_id)
        todo_attempt = self.store.start_todo_attempt({
            "todo_attempt_id": str(uuid4()), "run_id": self.run["run_id"],
            "run_attempt_id": self.plan.run_attempt_id, "todo_instance_id": self.todo_id,
            "attempt_number": todo["attempts"] + 1, "operation": todo["operation"],
        })
        artifact_id = str(uuid4())
        self.store.create_resource("artifact", resource_id=artifact_id, state="recorded", document={
            "runAttemptId": self.plan.run_attempt_id, "todoAttemptId": todo_attempt["todo_attempt_id"],
            "todoInstanceId": self.todo_id,
        })
        self.store.finish_todo_attempt(todo_attempt["todo_attempt_id"], status="human_required",
            reason_code="offline_human", reason="offline blocker", retryable=False, evidence_refs=[artifact_id])
        self.blocker, _ = self.store.create_or_reuse_todo_blocker({
            "manager_id": self.manager.manager_id, "game_id": self.run["game_id"],
            "run_id": self.run["run_id"], "run_attempt_id": self.plan.run_attempt_id,
            "todo_instance_id": self.todo_id, "todo_attempt_id": todo_attempt["todo_attempt_id"],
            "game_day_key": todo["period_key"], "kind": "human_required", "code": "offline_human",
            "reason": "offline blocker", "artifact_refs": [artifact_id],
        })
        self.store.update_run_attempt(self.plan.run_attempt_id, state="human_required", completed=True)
        self.store.update_game_run(self.run["run_id"], state="human_required", message="offline human")
        self.store.update_batch_run_membership(self.batch["batch_id"], self.run["run_id"], state="terminal",
            latest_run_attempt_id=self.plan.run_attempt_id, terminal_outcome="human_required")
        self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 0}})

    def owners(self):
        return self.gates.current_human_gate_owners(self.store)

    def park(self):
        self.assertTrue(queue_parking.park_human_gate(self.manager, self.batch["batch_id"], self.run["run_id"],
                                                    lambda: datetime.now(timezone.utc)))
        self.assertEqual(self.owners(), [])

    def resource(self):
        return self.store.get_resource("queue-parking", self.plan.run_attempt_id)

    def update_document(self, **changes):
        resource = self.resource()
        self.store.update_resource("queue-parking", resource["resource_id"], state=resource["state"],
                                   document={**resource["document"], **changes})

    def assert_blocked(self):
        self.assertTrue(self.owners())
        with self.assertRaises(ManagerConflict):
            self.gates.require_new_execution_allowed(self.store)

    def test_real_blocker_parking_changes_unified_owners_without_mutation(self):
        self.assert_blocked()
        self.park()
        before = self.store.latest_event_sequence(), self.store.connection.total_changes
        with mock.patch("subprocess.Popen", side_effect=AssertionError("Read must not launch")):
            self.assertEqual(self.owners(), [])
            self.gates.require_new_execution_allowed(self.store)
        self.assertEqual(before, (self.store.latest_event_sequence(), self.store.connection.total_changes))
        self.assertTrue(self.original.current_human_gate_owners(self.store))
        self.assertEqual(self.store.get_todo_blocker(self.blocker["blocker_id"]), self.blocker)
        self.assertEqual(self.store.get_game_run(self.run["run_id"])["state"], "review_required")

    def test_reclaimed_resource_immediately_restores_human_fence(self):
        self.park()
        self.manager.create_run_control_request(
            self.run["run_id"], "takeover", RunControlRequest(reason="offline reclaim", requested_by="test"),
            idempotency_key="parked-human-reclaim", request_id=None,
            path=f"/api/v1/game-runs/{self.run['run_id']}/takeover-requests",
            expected_state_version=self.store.latest_event_sequence())
        self.assertEqual(self.resource()["state"], "reclaimed")
        self.assert_blocked()
        self.assertEqual(self.store.get_todo_blocker(self.blocker["blocker_id"]), self.blocker)

    def test_new_attempt_cannot_reuse_previous_parking(self):
        self.park()
        self.store.create_run_attempt({"run_attempt_id": str(uuid4()), "run_id": self.run["run_id"],
            "game_id": self.run["game_id"], "cadence": "daily", "attempt_ordinal": 2,
            "fencing_token_hash": "a" * 64, "cancel_authority_hash": "sha256:" + "b" * 64,
            "plan": self.store.get_run_attempt(self.plan.run_attempt_id)["plan"]})
        self.assert_blocked()

    def test_replacement_lease_on_same_attempt_cannot_be_hidden(self):
        self.park()
        old = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)
        replacement_hash = "sha256:" + hashlib.sha256(b"offline-replacement").hexdigest()
        with self.store.atomic():
            self.store.connection.execute("UPDATE run_attempts SET fencing_token_hash=? WHERE run_attempt_id=?",
                                          (replacement_hash, self.plan.run_attempt_id))
        newer = self.store.create_controller_lease({**old, "controller_lease_id": str(uuid4()),
            "generation": old["generation"] + 1, "state": "active",
            "fencing_hash": replacement_hash, "fencing_fingerprint": replacement_hash[:23],
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()})
        self.assert_blocked()
        self.assertEqual(self.store.get_controller_lease_private(newer["controller_lease_id"])["state"], "active")

    def test_member_mismatch_is_not_an_exemption(self):
        self.park()
        self.store.update_batch_run_membership(self.batch["batch_id"], self.run["run_id"],
                                               state="resume_pending", latest_run_attempt_id=self.plan.run_attempt_id)
        self.assert_blocked()

    def test_terminal_member_with_wrong_attempt_is_not_an_exemption(self):
        self.park()
        with self.store.atomic():
            self.store.connection.execute(
                "UPDATE batch_run_memberships SET latest_run_attempt_id=NULL WHERE batch_id=? AND run_id=?",
                (self.batch["batch_id"], self.run["run_id"]))
        self.assert_blocked()

    def test_late_human_blocker_is_not_covered_by_old_yield(self):
        self.park()
        with self.store.atomic():
            self.store.connection.execute("UPDATE todo_blockers SET raised_at=? WHERE blocker_id=?",
                ((datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(), self.blocker["blocker_id"]))
        self.assert_blocked()

    def test_nonzero_frozen_budget_is_honored_without_reloading_configuration(self):
        self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 0.01, "poll_seconds": 0.01}})
        self.park()
        resource = self.resource()
        self.assertEqual(resource["document"]["budgetSeconds"], 0.01)
        self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 3600}})
        self.assertEqual(self.owners(), [])

    def test_lease_ended_before_parking_does_not_require_a_second_revocation(self):
        lease = self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)
        self.store.transition_controller_lease(lease["controller_lease_id"], state="revoked",
                                               reason_code="launch_human_required", reason="offline launcher gate")
        self.park()
        self.assertIsNone(self.resource()["document"]["controllerLeaseId"])
        self.assertEqual(self.store.get_controller_lease_for_attempt(self.plan.run_attempt_id)["end_reason_code"],
                         "launch_human_required")

    def test_forged_record_without_committed_expiry_event_is_not_authority(self):
        self.park()
        with self.store.atomic():
            self.store.connection.execute("DELETE FROM events WHERE event_type='queue.parking.expired' AND entity_id=?",
                                          (self.plan.run_attempt_id,))
        self.assert_blocked()

    def test_identity_and_receipt_mutations_fail_closed(self):
        self.park()
        original = self.resource()
        for changes in ({"runId": "foreign"}, {"batchId": "foreign"}, {"runAttemptId": "foreign"},
                        {"gameId": "foreign"}, {"ownershipReleased": False}, {"businessCompleted": True},
                        {"controllerLeaseId": "foreign"}, {"controllerLeaseGeneration": 99},
                        {"gateKind": "completion_review"}, {"parkedAt": "invalid"}):
            with self.subTest(changes=changes):
                self.store.update_resource("queue-parking", original["resource_id"], state="parked",
                                           document={**original["document"], **changes})
                self.assert_blocked()

    def test_unexpired_or_inconsistent_budget_cannot_yield(self):
        self.park()
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.update_document(startedAt=future.isoformat(), expiresAt=future.isoformat(), parkedAt=future.isoformat())
        self.assert_blocked()

    def test_awaiting_even_after_deadline_is_not_a_committed_yield(self):
        self.park()
        resource = self.resource()
        self.store.update_resource("queue-parking", resource["resource_id"], state="awaiting", document=resource["document"])
        self.assert_blocked()

    def test_foreign_live_membership_is_not_hidden(self):
        self.park()
        other = self.fixture._create_batch_with_runs([self.run], state="running")
        self.assertNotEqual(other["batch_id"], self.batch["batch_id"])
        self.assert_blocked()

    def test_foreign_human_owner_still_blocks(self):
        self.park()
        other = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.store.update_game_run(other["run_id"], state="human_required", message="foreign gate")
        self.assertEqual({owner["runId"] for owner in self.owners()}, {other["run_id"]})

    def test_actual_serial_guard_passes_human_stage_but_keeps_host_fence(self):
        self.park()
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        self.store.add_batch_run_membership(self.batch["batch_id"], second["run_id"], ordinal=1, role="initial")
        self.manager.daily_execution = SimpleNamespace(read=lambda: {"state": "running", "generation": 1},
            stopping=SimpleNamespace(is_set=lambda: False), release_pending=lambda: False)
        namespace = {"__package__": "yeyu_gamer_manager.services",
                     "current_human_gate_owners": self.gates.current_human_gate_owners}

        namespace["ManagerConflict"] = ManagerConflict
        reference_function(self.sources["services/serial_observation_recovery.py"], "require", namespace)
        guard = reference_function(self.sources["services/serial_observation_recovery.py"], "_current_ownership", namespace)
        launcher = ModuleType("yeyu_gamer_manager.services.zzz_launcher_observation")
        launcher.OWNER_KEY = "maintenance.zzz_launcher_observation"
        launcher.ManagerConflict = ManagerConflict
        for name in ("observation_active", "require_observation_idle"):
            reference_function(self.sources["services/zzz_launcher_observation.py"], name, vars(launcher))
        with mock.patch.dict(sys.modules, {launcher.__name__: launcher}):
            guard(self.manager, child_run_id=second["run_id"], allow_stopped=False)
            host = self.manager.adapter_host
            host._active_process = SimpleNamespace(pid=12345, poll=lambda: None)
            host._active_plan = self.plan
            try:
                with self.assertRaisesRegex(ManagerConflict, "host_owned"):
                    guard(self.manager, child_run_id=second["run_id"], allow_stopped=False)
            finally:
                host._active_process = None
                host._active_plan = None


if __name__ == "__main__":
    unittest.main()
