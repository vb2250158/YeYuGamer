from __future__ import annotations

import unittest
from unittest import mock

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.domain.models import ConfigPatchRequest, RunControlRequest
from yeyu_gamer_manager.services.adapter_protocol import AdapterRunResult
from yeyu_gamer_manager.services.game_launcher import GameLaunchHumanRequired, GameLaunchReceipt
from yeyu_gamer_manager.services.queue_parking import parking_record


class QueueParkingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.store = self.fixture.store
        self.store.update_config({"queue_timeout_budget": {"human_takeover_seconds": 0}})

    def runs(self):
        first = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_ALPHA)])
        second = self.fixture._create_run([self.fixture._todo_id(fixtures.EXECUTE_BETA)])
        return first, second, self.fixture._create_batch_with_runs([first, second])

    def completed(self, run_id, game_id, callback, *, plan, **kwargs):
        callback(AdapterRunResult(
            run_id=run_id, run_attempt_id=plan.run_attempt_id, game_id=game_id,
            status="completed", transport_outcome="clean", attempted_todo_instance_ids=(),
            completed_todo_instance_ids=(), unresolved_todo_instance_ids=(),
            exit_code=0, protocol_valid=True, code="fixture_completed", message="offline terminal",
        ))
        return 43210

    def _park_first_and_run_second(self):
        first, second, batch = self.runs()
        self.manager.game_launcher.ensure_started.side_effect = [
            GameLaunchHumanRequired("fixture_human", "offline human gate"),
            GameLaunchReceipt("started", 4321, "fixture.exe"),
        ]
        with mock.patch.object(self.manager.adapter_host, "execute", side_effect=self.completed) as execute:
            self.manager._run_batch(batch["batch_id"], [
                {"runId": first["run_id"], "gameId": fixtures.GAME_ID},
                {"runId": second["run_id"], "gameId": fixtures.GAME_ID},
            ], batch["batch_id"])
        self.assertEqual(execute.call_count, 1, repr([
            self.store.get_game_run(first["run_id"]), self.store.get_game_run(second["run_id"]),
            self.store.list_resources("queue-parking", 10),
        ]))
        self.assertEqual(execute.call_args.args[0], second["run_id"])
        self.assertEqual(self.store.get_game_run(first["run_id"])["state"], "review_required")
        parked = parking_record(self.manager, first["run_id"])
        self.assertIsNotNone(parked)
        self.assertFalse(parked["document"]["businessCompleted"])
        self.assertIn("evidenceStatus", parked["document"])
        result = self.store.get_batch(batch["batch_id"])["result"]
        self.assertNotIn("sealVersion", result)
        self.assertFalse(result["acceptedDone"])
        self.assertIn(second["run_id"], result["completedRunIds"])
        self.assertFalse(self.manager._current_human_batch(self.store.get_batch(batch["batch_id"])))
        return first, second, batch

    def test_unattended_human_gate_yields_queue_without_success_or_seal(self):
        self._park_first_and_run_second()

    def test_late_parked_result_stays_on_original_attempt_without_changing_next_member(self):
        first, second, batch = self._park_first_and_run_second()
        attempt = self.store.list_run_attempts(run_id=first["run_id"], limit=1)[0]
        plan = mock.Mock(run_id=first["run_id"], run_attempt_id=attempt["run_attempt_id"])
        before_second = self.store.get_game_run(second["run_id"])
        before_batch = self.store.get_batch(batch["batch_id"])
        late = mock.Mock(status="completed", code="fixture_late_completed")
        self.assertIsNone(self.manager._finish_adapter_result(plan, late))
        self.assertEqual(self.store.get_game_run(first["run_id"])["state"], "review_required")
        self.assertEqual(self.store.get_game_run(second["run_id"]), before_second)
        self.assertEqual(self.store.get_batch(batch["batch_id"]), before_batch)

    def test_late_parked_event_cannot_capture_or_persist_success(self):
        first, second, batch = self._park_first_and_run_second()
        attempt = self.store.list_run_attempts(run_id=first["run_id"], limit=1)[0]
        plan = mock.Mock(run_id=first["run_id"], run_attempt_id=attempt["run_attempt_id"])
        event = mock.Mock(event_type="todo_terminal")
        self.manager._handle_adapter_event_inner(plan, event)
        self.assertEqual(self.store.get_game_run(first["run_id"])["state"], "review_required")
        self.assertEqual(self.store.list_adapter_events(run_attempt_id=attempt["run_attempt_id"]), [])

    def test_formal_release_remains_available_on_parked_original_run(self):
        first, second, batch = self._park_first_and_run_second()
        before_runs = [member["run_id"] for member in self.store.get_batch(batch["batch_id"])["run_memberships"]]
        with mock.patch.object(self.manager.adapter_host, "execute") as execute:
            self.manager.create_run_control_request(
                first["run_id"], "release-takeover",
                RunControlRequest(reason="offline explicit release", requested_by="test"),
                idempotency_key="parked-release", request_id=None,
                path=f"/api/v1/game-runs/{first['run_id']}/takeover-release-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
            execute.assert_not_called()
        current = self.store.get_batch(batch["batch_id"])
        self.assertNotIn("sealVersion", current["result"])
        self.assertEqual([member["run_id"] for member in current["run_memberships"]], before_runs)
        self.assertEqual(self.store.get_game_run(first["run_id"])["state"], "review_required")

    def test_parked_original_run_cannot_resume_from_a_written_requirement_alone(self):
        first, second, batch = self._park_first_and_run_second()
        with self.assertRaisesRegex(Exception, "parked_run_reconciliation_required"):
            self.manager.create_run_control_request(
                first["run_id"], "resume", RunControlRequest(reason="offline resume", requested_by="test"),
                idempotency_key="parked-resume", request_id=None,
                path=f"/api/v1/game-runs/{first['run_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )

    def test_new_takeover_revokes_old_queue_yield_without_unfencing_old_callbacks(self):
        first, second, batch = self._park_first_and_run_second()
        attempt_id = parking_record(self.manager, first["run_id"])["resource_id"]
        with mock.patch.object(self.manager.adapter_host, "cancel_run"):
            self.manager.create_run_control_request(
                first["run_id"], "takeover", RunControlRequest(reason="offline new takeover", requested_by="test"),
                idempotency_key="parked-takeover", request_id=None,
                path=f"/api/v1/game-runs/{first['run_id']}/takeover-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        self.assertIsNone(parking_record(self.manager, first["run_id"]))
        resource = self.store.get_resource("queue-parking", attempt_id)
        self.assertEqual(resource["state"], "reclaimed")
        self.assertEqual(self.store.get_game_run(first["run_id"])["state"], "human_required")
        self.manager.create_run_control_request(
            first["run_id"], "release-takeover", RunControlRequest(reason="offline release", requested_by="test"),
            idempotency_key="reclaimed-release", request_id=None,
            path=f"/api/v1/game-runs/{first['run_id']}/takeover-release-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        with self.assertRaisesRegex(Exception, "parked_run_reconciliation_required"):
            self.manager.create_run_control_request(
                first["run_id"], "resume", RunControlRequest(reason="offline resume", requested_by="test"),
                idempotency_key="reclaimed-resume", request_id=None,
                path=f"/api/v1/game-runs/{first['run_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )

    def test_parked_members_cannot_be_sealed_through_another_aggregation_entry(self):
        first, second, batch = self._park_first_and_run_second()
        with self.assertRaisesRegex(Exception, "Unresolved parked"):
            self.manager._seal_batch_from_todos(
                batch_id=batch["batch_id"], initial_result={}, game_ids=[fixtures.GAME_ID],
                cadence="daily", state="review_required", completed_run_ids=[second["run_id"]],
                failed_run_ids=[first["run_id"]], final_run_ids=[first["run_id"], second["run_id"]],
                reason="offline aggregation",
            )
        self.assertNotIn("sealVersion", self.store.get_batch(batch["batch_id"])["result"])

    def test_expired_parking_is_idempotent_and_does_not_release_successor_owner(self):
        first, second, batch = self._park_first_and_run_second()
        host = self.manager.adapter_host
        process = mock.Mock()
        process.poll.return_value = None
        host._active_process = process
        host._active_plan = mock.Mock(run_attempt_id="new-owner")
        attempt = self.store.list_run_attempts(run_id=first["run_id"], limit=1)[0]
        self.assertFalse(host.release_terminal_owner(attempt["run_attempt_id"]))
        self.assertIs(host._active_process, process)
        host._active_process = None
        host._active_plan = None

    def test_config_validation_rejects_boolean_and_unknown_budget(self):
        for value in ({"human_takeover_seconds": True}, {"unknown": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ConfigPatchRequest(queue_timeout_budget=value)
        request = ConfigPatchRequest(queue_timeout_budget={"human_takeover_seconds": 0})
        self.assertEqual(request.queue_timeout_budget["human_takeover_seconds"], 0)

    def test_terminal_owner_release_is_attempt_scoped_and_never_kills_live_owner(self):
        host = self.manager.adapter_host
        process = mock.Mock()
        host._active_process = process
        host._active_plan = mock.Mock(run_attempt_id="old-attempt")
        process.poll.return_value = None
        self.assertFalse(host.release_terminal_owner("old-attempt"))
        process.poll.return_value = 0
        self.assertFalse(host.release_terminal_owner("other-attempt"))
        self.assertFalse(host.release_terminal_owner("old-attempt"))
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertIs(host._active_process, process)
        host._active_process = None
        host._active_plan = None
        self.assertTrue(host.release_terminal_owner("old-attempt"))

    def test_oversized_budget_is_rejected_before_datetime_or_wait_overflow(self):
        for values in ({"human_takeover_seconds": 1e300}, {"poll_seconds": 6}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ConfigPatchRequest(queue_timeout_budget=values)


if __name__ == "__main__":
    unittest.main()
