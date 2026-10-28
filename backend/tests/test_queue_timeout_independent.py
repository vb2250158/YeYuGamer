"""Independent coordinator regressions: temporary SQLite, inert runners, virtual waits."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services import manager as manager_module
from yeyu_gamer_manager.services.adapter_host import AdapterRunResult
from yeyu_gamer_manager.services.adapter_protocol import AdapterExecutionPlan
from yeyu_gamer_manager.services.completion_contract import adjudicate_completion


class VirtualClock:
    def __init__(self):
        self.origin = datetime.now(timezone.utc)
        self.seconds = 0.0
        self.events = []
        self.on_advance = None

    def now(self):
        return self.origin + timedelta(seconds=self.seconds)

    def advance(self, seconds):
        self.seconds += max(float(seconds or 0), 0.01)
        if self.seconds > 20000:
            raise AssertionError("Coordinator exceeded the independent virtual wait bound")
        if self.on_advance is not None:
            self.on_advance(self.seconds)

    def event(self):
        clock = self

        class Event:
            def __init__(self):
                self.ready = False

            def set(self):
                self.ready = True

            def is_set(self):
                return self.ready

            def wait(self, timeout=None):
                if not self.ready:
                    if timeout is None:
                        raise AssertionError("Unbounded coordinator wait")
                    clock.advance(timeout)
                return self.ready

        event = Event()
        self.events.append(event)
        return event


class IndependentQueueTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch("subprocess.Popen", side_effect=AssertionError("No child processes permitted")))
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.store = self.fixture.store
        self.clock = VirtualClock()
        self.started = []
        self.started_at = {}
        self.plans = {}
        self.logs = {}
        self.late_woke_next = False
        self.late_callback = None
        thread_proxy = SimpleNamespace(**vars(threading))
        thread_proxy.Event = self.clock.event
        time_proxy = SimpleNamespace(**vars(time))
        time_proxy.monotonic = lambda: self.clock.seconds
        time_proxy.sleep = self.clock.advance
        self.patches.enter_context(mock.patch.object(manager_module, "threading", thread_proxy))
        self.patches.enter_context(mock.patch.object(manager_module, "time", time_proxy))
        self.patches.enter_context(mock.patch.object(manager_module, "utc_now", self.clock.now))
        parking_module_name = "yeyu_gamer_manager.services.queue_parking"
        if importlib.util.find_spec(parking_module_name) is not None:
            parking_module = importlib.import_module(parking_module_name)
            self.patches.enter_context(mock.patch.object(parking_module, "threading", thread_proxy))
            self.patches.enter_context(mock.patch.object(parking_module, "time", time_proxy))
        runtime = self.manager.adapter_host.execution_bindings(fixtures.GAME_ID)
        self.patches.enter_context(mock.patch.object(self.manager.adapter_host, "execution_bindings", return_value=runtime))
        self.patches.enter_context(mock.patch.object(self.manager, "_build_execution_plan", side_effect=self.plan_for_run))
        self.store.import_legacy(config_values={}, games=[dict(game_id="ZZZ", display_name="Fixture next game",
            order_index=2, enabled=True, policy={})], source_info={"fixture": "independent-queue"}, policy_projection={})
        definition = dict(self.store.get_todo_definition(fixtures.EXECUTE_BETA))
        definition.update(todo_definition_id="todo.test.zzz.queue-next", game_id="ZZZ")
        self.store.sync_todo_definitions([definition])
        self.store.reconcile_todo_instances(self.manager._todo_instance_candidates(["ZZZ"], "daily"),
            intent="reconcile", requested_by="independent-qa", reason="owned temporary fixture")
        next_todo = next(item for item in self.store.list_todo_instances(game_id="ZZZ", cadence="daily", limit=1000)
                         if item["todo_definition_id"] == definition["todo_definition_id"])
        self.first = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        self.second = self.store.create_game_run(dict(game_id="ZZZ", cadence="daily", state="queued",
            mode="execute", requested_by="independent-qa", todo_instance_ids=[next_todo["todo_instance_id"]]))
        self.batch = self.fixture._create_batch_with_runs([self.first, self.second])
        self.store.update_batch(self.batch["batch_id"], state="queued", result={**self.batch["result"],
            "candidateGameIds": [fixtures.GAME_ID, "ZZZ"], "executableGameIds": [fixtures.GAME_ID, "ZZZ"]})

    def result(self, plan, status):
        succeeded = status == "completed"
        return AdapterRunResult(run_id=plan.run_id, run_attempt_id=plan.run_attempt_id, game_id=plan.game_id,
            status=status, transport_outcome="clean", attempted_todo_instance_ids=plan.executable_todo_instance_ids,
            completed_todo_instance_ids=plan.executable_todo_instance_ids if succeeded else (),
            unresolved_todo_instance_ids=() if succeeded else plan.executable_todo_instance_ids,
            exit_code=0 if succeeded else 30, protocol_valid=True, code="fixture_" + status,
            message="independent inert runner result")

    def plan_for_run(self, run, **kwargs):
        todo = self.store.get_todo_instance(run["todo_instance_ids"][0])
        return AdapterExecutionPlan.from_document(dict(schemaVersion=1, protocolVersion="1.1", requestType="execute",
            runId=run["run_id"], runAttemptId=str(uuid4()), fencingToken=uuid4().hex, cancelAuthority=uuid4().hex,
            gameId=run["game_id"], cadence="daily", managerStateVersion=self.store.latest_event_sequence(),
            catalogVersion=fixtures.CATALOG_VERSION, policyDigest="sha256:" + "a" * 64,
            issuedAt=self.clock.now().isoformat(), expiresAt=(self.clock.now() + timedelta(seconds=600)).isoformat(),
            timeoutSeconds=120, preserveClientOnStop=True, executableTodoInstanceIds=[todo["todo_instance_id"]],
            todos=[dict(todoInstanceId=todo["todo_instance_id"], todoDefinitionId=todo["todo_definition_id"],
                definitionVersion=1, operation=todo["operation"], risk=todo["risk"],
                adapterCapabilityRef=fixtures.CAPABILITY_REF, priorAttempts=0, executionDisposition="executable")]))

    def record_terminal(self, plan, status, *, finalize=True):
        todo = plan.todos[0]
        todo_attempt = self.store.start_todo_attempt(dict(todo_attempt_id=str(uuid4()), run_attempt_id=plan.run_attempt_id,
            run_id=plan.run_id, todo_instance_id=todo.todo_instance_id, attempt_number=todo.prior_attempts + 1,
            operation=todo.operation))
        evidence = str(uuid4())
        file_name = evidence + ".png"
        directory = self.fixture.settings.data_dir / "artifacts"
        directory.mkdir(exist_ok=True)
        (directory / file_name).write_bytes(fixtures.PNG_1X1)
        self.store.create_resource("artifact", resource_id=evidence, state="captured", document=dict(
            kind="game-ui-task-result", contentType="image/png", source="independent-qa", raw=True,
            gameId=plan.game_id, runId=plan.run_id, runAttemptId=plan.run_attempt_id,
            todoAttemptId=todo_attempt["todo_attempt_id"], todoInstanceId=todo.todo_instance_id,
            gameDayKey=self.store.get_todo_instance(todo.todo_instance_id)["period_key"],
            capturedAt=self.clock.now().isoformat(), hash=hashlib.sha256(fixtures.PNG_1X1).hexdigest(),
            sizeBytes=len(fixtures.PNG_1X1), fileName=file_name, relativePath=file_name))
        log_id = str(uuid4())
        log_name = log_id + ".txt"
        log_content = b"independent fixture: original official step did not complete"
        (directory / log_name).write_bytes(log_content)
        document = dict(self.store.get_resource("artifact", evidence)["document"])
        document.update(kind="tool-log", contentType="text/plain", fileName=log_name, relativePath=log_name,
                        hash=hashlib.sha256(log_content).hexdigest(), sizeBytes=len(log_content))
        self.store.create_resource("artifact", resource_id=log_id, state="captured", document=document)
        self.logs[plan.run_id] = log_id
        self.store.finish_todo_attempt(todo_attempt["todo_attempt_id"], status="blocked" if status == "failed" else status,
            reason_code="fixture_" + status, reason="owned synthetic terminal", retryable=False, evidence_refs=[evidence, log_id])
        if finalize:
            self.store.update_run_attempt(plan.run_attempt_id, state=status, completed=True,
                result={"code": "fixture_" + status, "evidenceRefs": [evidence, log_id]})
            self.store.update_game_run(plan.run_id, state="review_required" if status == "completed" else status,
                completed_todo_instance_ids=list(plan.executable_todo_instance_ids) if status == "completed" else [],
                message="owned synthetic terminal")
            self.store.update_batch_run_membership(self.batch["batch_id"], plan.run_id, state="terminal",
                latest_run_attempt_id=plan.run_attempt_id, terminal_outcome=status)
        return evidence

    def release_fixture_lease(self, plan):
        lease = self.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True)
        if lease is not None:
            self.store.transition_controller_lease(lease["controller_lease_id"], state="released",
                reason_code="fixture_released", reason="ordinary terminal fixture", ended_at=self.clock.now().isoformat())

    def run_case(self, first_status, *, keep_lease=True, missing_callback=False, on_next=None,
                 normal_finish=False, owner_mode=None):
        def start(run_id, completed_callback):
            self.clock.advance(1)
            self.started.append(run_id)
            self.started_at[run_id] = self.clock.seconds
            if run_id == self.first["run_id"]:
                plan = self.manager._prepare_run_attempt(self.store.get_game_run(run_id))
                self.plans[run_id] = plan
                self.original_lease = self.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True)
                self.assertIsNotNone(self.original_lease)
                self.assertEqual(self.original_lease["run_attempt_id"], plan.run_attempt_id)
                self.late_callback = completed_callback
                if not missing_callback:
                    self.evidence_id = self.record_terminal(plan, first_status, finalize=not normal_finish)
                    self.evidence_before = self.store.get_resource("artifact", self.evidence_id)
                    self.log_before = self.store.get_resource("artifact", self.logs[plan.run_id])
                    if normal_finish:
                        self.manager._finish_adapter_result(plan, self.result(plan, first_status))
                    if not keep_lease:
                        self.release_fixture_lease(plan)
                    completed_callback(self.result(plan, first_status))
                elif owner_mode is not None:
                    host = self.manager.adapter_host
                    host._active_plan = plan
                    host._active_process = SimpleNamespace(pid=87654321, poll=lambda: None if owner_mode == "hung" else 0)
                    self.unknown_owner = host._active_process
                    self.addCleanup(lambda: setattr(host, "_active_plan", None))
                    self.addCleanup(lambda: setattr(host, "_active_process", None))
                    self.store.update_run_attempt(plan.run_attempt_id, state="running", completed=False,
                                                  process_id=87654321)
                return plan, 0
            active = self.store.get_controller_lease_for_attempt(self.plans[self.first["run_id"]].run_attempt_id,
                                                                active_only=True)
            self.assertIsNone(active, "Next game started before the original desktop lease was released")
            plan = self.manager._prepare_run_attempt(self.store.get_game_run(run_id))
            self.plans[run_id] = plan
            self.store.update_run_attempt(plan.run_attempt_id, state="running", completed=False)
            if on_next is not None:
                on_next(plan)
            self.record_terminal(plan, "completed")
            self.release_fixture_lease(plan)
            completed_callback(self.result(plan, "completed"))
            return plan, 0

        with mock.patch.object(self.manager, "_start_game_run", side_effect=start):
            self.manager._run_batch(self.batch["batch_id"], [dict(runId=run["run_id"], gameId=run["game_id"])
                for run in (self.first, self.second)], self.batch["batch_id"])

    def assert_next_game_finished_without_false_success(self):
        self.assertFalse(self.late_woke_next, "Original callback woke the next game's coordinator waiter")
        self.assertEqual(self.started, [self.first["run_id"], self.second["run_id"]])
        previous = self.store.get_game_run(self.first["run_id"])
        self.assertNotIn(previous["state"], {"done", "completed"})
        self.assertEqual(previous["completed_todo_instance_ids"], [])
        self.assertFalse(adjudicate_completion(self.manager._completion_contract_snapshot(self.first["run_id"])).accepted_done)
        original_plan = self.plans[self.first["run_id"]]
        self.assertNotEqual(self.store.get_run_attempt(original_plan.run_attempt_id)["state"], "completed")
        self.assertNotEqual(self.store.get_todo_instance(self.first["todo_instance_ids"][0])["status"], "completed")
        if hasattr(self, "evidence_before"):
            for resource in (self.evidence_before, self.log_before):
                self.assertEqual(self.store.get_resource("artifact", resource["resource_id"]), resource)
                document = resource["document"]
                self.assertEqual(document["runId"], original_plan.run_id)
                self.assertEqual(document["runAttemptId"], original_plan.run_attempt_id)
                content = (self.fixture.settings.data_dir / "artifacts" / document["relativePath"]).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), document["hash"])
        later = self.store.list_run_attempts(run_id=self.second["run_id"], limit=10)
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["state"], "completed")
        self.assertEqual(self.store.get_todo_instance(self.second["todo_instance_ids"][0])["status"], "completed")
        self.assertEqual(len(self.store.list_game_runs(100)), 2, "Timeout must not rebuild the queue")

    def test_ordinary_failure_control_fixture_continues(self):
        self.run_case("failed", keep_lease=False)
        self.assert_next_game_finished_without_false_success()

    def test_unattended_human_gate_expires_and_next_game_finishes(self):
        self.run_case("human_required")
        self.assert_next_game_finished_without_false_success()

    def test_unresolved_review_releases_lease_before_next_game(self):
        self.run_case("review_required")
        self.assert_next_game_finished_without_false_success()

    def test_dead_owner_without_callback_cannot_park_the_queue(self):
        self.run_case("review_required", missing_callback=True)
        self.assert_next_game_finished_without_false_success()

    def test_unknown_hung_owner_yields_queue_without_desktop_handoff(self):
        self.run_case("review_required", missing_callback=True, owner_mode="hung")
        self.assert_unknown_owner_quarantines_without_input()

    def test_unknown_killed_owner_yields_queue_without_desktop_handoff(self):
        self.run_case("review_required", missing_callback=True, owner_mode="killed")
        self.assert_unknown_owner_quarantines_without_input()

    def assert_unknown_owner_quarantines_without_input(self):
        from yeyu_gamer_manager.services import input_recovery

        plan = self.plans[self.first["run_id"]]
        owner = self.unknown_owner
        self.assertEqual(self.started, [self.first["run_id"]])
        self.assertIs(self.manager.adapter_host._active_plan, plan)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.pid, 87654321)
        self.assertEqual(self.store.get_game_run(plan.run_id)["state"], "review_required")
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["state"], "review_required")
        self.assertEqual(self.store.get_controller_lease_for_attempt(plan.run_attempt_id)["state"], "revoked")
        quarantine = self.store.get_resource(input_recovery.QUARANTINE_RESOURCE, input_recovery.DESKTOP_ID)
        self.assertEqual(quarantine["state"], "quarantined")
        self.assertEqual(quarantine["document"]["runAttemptId"], plan.run_attempt_id)
        self.assertEqual(quarantine["document"]["controllerLeaseGeneration"], self.original_lease["generation"])
        import json
        import sqlite3

        durable_store = sqlite3.connect(self.fixture.settings.database_path.as_uri() + "?mode=ro", uri=True)
        try:
            state, document = durable_store.execute(
                "SELECT state, document_json FROM manager_resources WHERE resource_type=? AND resource_id=?",
                (input_recovery.QUARANTINE_RESOURCE, input_recovery.DESKTOP_ID)).fetchone()
            self.assertEqual(state, "quarantined")
            self.assertEqual(json.loads(document), quarantine["document"])
        finally:
            durable_store.close()
        self.assertFalse(quarantine["document"]["proofVerified"])
        self.assertFalse(quarantine["document"]["businessCompleted"])
        self.assertTrue(quarantine["document"]["ownerRetained"])
        self.assertEqual(self.store.get_game_run(self.second["run_id"])["state"], "review_required")
        member = self.store.get_batch_run_membership(self.batch["batch_id"], self.second["run_id"])
        self.assertEqual(member["state"], "terminal")
        self.assertEqual(member["terminal_outcome"], "not_started_desktop_quarantine")
        self.assertEqual(self.store.list_run_attempts(run_id=self.second["run_id"], limit=10), [])
        self.assertNotEqual(self.store.get_todo_instance(self.second["todo_instance_ids"][0])["status"], "completed")
        from yeyu_gamer_manager.services import dispatch_reservation
        from yeyu_gamer_manager.services.queue_timeout_budget import load_queue_timeout_budget

        dispatch = self.store.get_resource(dispatch_reservation.RESOURCE_TYPE,
                                           dispatch_reservation._resource_id(plan))
        deadline = datetime.fromisoformat(dispatch["document"]["deadlineAt"])
        budget = load_queue_timeout_budget(self.store.get_config()["values"])
        limit = (deadline - self.clock.origin).total_seconds() + budget.callback_cancel_grace_seconds
        self.assertLessEqual(self.clock.seconds, limit)
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.game_launcher.close_for_queue.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()
        self.assertNotIn(self.store.get_batch(self.batch["batch_id"])["state"], {"queued", "running"})
        self.assertIs(self.manager.adapter_host._active_process, owner)
        from yeyu_gamer_manager.services.dispatch_reservation import DispatchReservationRejected

        with self.assertRaises(DispatchReservationRejected):
            input_recovery.require_desktop_available(self.manager)
        self.assertFalse(adjudicate_completion(self.manager._completion_contract_snapshot(plan.run_id)).accepted_done)

    def test_owned_hung_root_recovers_and_next_game_finishes(self):
        from owned_recovery_fixture import run_owned_case

        run_owned_case(self, "hung")
        self.assert_next_game_finished_without_false_success()

    def test_owned_killed_root_recovers_and_next_game_finishes(self):
        from owned_recovery_fixture import run_owned_case

        run_owned_case(self, "killed")
        self.assert_next_game_finished_without_false_success()

    def test_production_manager_callback_commits_before_owned_handoff(self):
        from owned_recovery_fixture import run_owned_case

        run_owned_case(self, "hung", manager_bridge=True)

    def test_normal_review_result_releases_its_original_lease(self):
        self.run_case("review_required", normal_finish=True)
        self.assert_next_game_finished_without_false_success()

    def test_late_original_terminal_cannot_release_the_next_controller(self):
        self.run_case("failed", keep_lease=False, on_next=self.inject_late_original_result)
        self.assert_next_game_finished_without_false_success()

    def inject_late_original_result(self, next_plan, *, coordinator_callback=False):
        previous = self.plans[self.first["run_id"]]
        lease_before = self.store.get_controller_lease_for_attempt(next_plan.run_attempt_id, active_only=True)
        self.assertIsNotNone(lease_before)
        batch_before = self.store.get_batch(self.batch["batch_id"])
        for _ in range(2):
            self.manager._finish_adapter_result(previous, self.result(previous, "completed"))
            if coordinator_callback:
                next_waiter = self.clock.events[-1]
                self.assertFalse(next_waiter.is_set())
                self.late_callback(self.result(previous, "completed"))
                self.late_woke_next = next_waiter.is_set()
                self.assertFalse(next_waiter.is_set(), "Original callback woke the next game's coordinator waiter")
            self.assertEqual(self.store.get_controller_lease_for_attempt(next_plan.run_attempt_id, active_only=True), lease_before)
            self.assertEqual(self.store.get_batch(self.batch["batch_id"]), batch_before)
            self.assertEqual(self.store.get_run_attempt(next_plan.run_attempt_id)["state"], "running")
            self.assertNotIn(self.store.get_game_run(previous.run_id)["state"], {"done", "completed"})

    def test_late_expired_human_result_preserves_running_next_owner(self):
        self.run_case("human_required", on_next=lambda plan: self.inject_late_original_result(plan, coordinator_callback=True))
        self.assert_next_game_finished_without_false_success()

    def test_late_coordinator_callback_cannot_wake_the_next_game(self):
        self.run_case("failed", keep_lease=False,
                      on_next=lambda plan: self.inject_late_original_result(plan, coordinator_callback=True))
        self.assert_next_game_finished_without_false_success()

    def test_configured_human_deadline_is_frozen_and_evidence_survives(self):
        self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 3, "poll_seconds": 1}})

        def while_waiting(seconds):
            first_started = self.started_at.get(self.first["run_id"])
            if first_started is None or self.second["run_id"] in self.started:
                return
            if seconds < first_started + 3:
                original = self.plans[self.first["run_id"]]
                self.assertIsNotNone(self.store.get_controller_lease_for_attempt(original.run_attempt_id, active_only=True))
                self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 1000, "poll_seconds": 1}})

        self.clock.on_advance = while_waiting
        self.run_case("human_required")
        self.assert_next_game_finished_without_false_success()
        elapsed = self.started_at[self.second["run_id"]] - self.started_at[self.first["run_id"]]
        self.assertGreaterEqual(elapsed, 3)
        self.assertLessEqual(elapsed, 5)
        original = self.plans[self.first["run_id"]]
        parking = self.store.get_resource("queue-parking", original.run_attempt_id)
        self.assertEqual(parking["state"], "parked")
        self.assertEqual(parking["document"]["budgetSeconds"], 3)
        self.assertFalse(parking["document"]["businessCompleted"])
        self.assertEqual(parking["document"]["runId"], self.first["run_id"])
        self.assertIn(self.evidence_id, parking["document"]["evidenceRefs"])
        self.assertIn(self.logs[original.run_id], parking["document"]["evidenceRefs"])

    def test_repeated_human_expiry_does_not_rebuild_the_original_run(self):
        self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 2, "poll_seconds": 1}})
        self.run_case("human_required")
        self.assert_next_game_finished_without_false_success()
        parking_module = importlib.import_module("yeyu_gamer_manager.services.queue_parking")
        original = self.plans[self.first["run_id"]]
        before = self.store.get_resource("queue-parking", original.run_attempt_id)
        attempts_before = self.store.list_run_attempts(run_id=original.run_id, limit=100)
        parking_module.park_human_gate(self.manager, self.batch["batch_id"], original.run_id, self.clock.now)
        self.assertEqual(self.store.get_resource("queue-parking", original.run_attempt_id), before)
        self.assertEqual(self.store.list_run_attempts(run_id=original.run_id, limit=100), attempts_before)
        self.assertEqual(len(self.store.list_game_runs(100)), 2)


if __name__ == "__main__":
    unittest.main()
