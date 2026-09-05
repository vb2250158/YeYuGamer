from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.settings import Settings


class ExecutionAttemptLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-gamer-attempts-")
        self.root = Path(self.temporary.name)
        self.settings = Settings.for_test(self.root)
        self.settings.legacy_root.mkdir(parents=True)
        (self.settings.legacy_root / "daily-gui-config.json").write_text(
            json.dumps(
                {
                    "order": ["StarRail"],
                    "enabled": {"StarRail": True},
                    "dailyScheduleEnabled": False,
                    "dailyScheduleTime": "17:00",
                    "weeklyEnabled": True,
                    "weeklyDay": "Monday",
                    "dailyResetHour": 4,
                }
            ),
            encoding="utf-8",
        )
        (self.settings.legacy_root / "game-automation-policy.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "forbiddenActions": ["purchase", "draw", "account_settings"],
                    "games": {"StarRail": {}},
                }
            ),
            encoding="utf-8",
        )
        self.settings.web_dist.mkdir(parents=True)
        (self.settings.web_dist / "index.html").write_text(
            "<!doctype html><title>YeYu Gamer</title>", encoding="utf-8"
        )
        self.client_context = TestClient(create_app(self.settings))
        self.client = self.client_context.__enter__()
        self._bootstrap_webgui_session()
        self.manager = self.client.app.state.manager
        self.store = self.manager.store
        response = self.client.get(
            "/api/v1/todo-instances",
            params={"gameId": "StarRail", "cadence": "daily", "current": "true"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.todo = next(
            item
            for item in response.json()["items"]
            if item["required"]
            and item["risk"] == "routine_action"
            and item["status"] == "pending"
        )

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def _actor_headers(self, actor: str) -> dict[str, str]:
        token = (
            self.settings.tray_bootstrap_secret
            if actor == "tray"
            else (
                self.settings.actor_tokens_dir
                / ("agent.yeyu.token" if actor == "agent" else f"{actor}.token")
            )
            .read_text(encoding="ascii")
            .strip()
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-YeYu-Gamer-Actor": actor,
        }

    def _bootstrap_webgui_session(self) -> None:
        issued = self.client.post(
            "/api/v1/webgui/bootstrap-nonces",
            json={},
            headers=self._actor_headers("tray"),
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        exchanged = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": issued.json()["nonce"]},
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(exchanged.status_code, 200, exchanged.text)

    def _create_game_run(self) -> dict[str, Any]:
        return self.store.create_game_run(
            {
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "queued",
                "mode": "execute",
                "requested_by": "attempt-ledger-test",
                "message": "fenced execution fixture",
                "todo_instance_ids": [self.todo["todoInstanceId"]],
            }
        )

    def _plan(self, run: dict[str, Any], run_attempt_id: str) -> dict[str, Any]:
        return {
            "protocolVersion": "1.1",
            "runId": run["run_id"],
            "runAttemptId": run_attempt_id,
            "gameId": "StarRail",
            "cadence": "daily",
            "catalogVersion": self.todo["catalogVersion"],
            "executableTodoInstanceIds": [self.todo["todoInstanceId"]],
            "todos": [
                {
                    "todoInstanceId": self.todo["todoInstanceId"],
                    "todoDefinitionId": self.todo["todoDefinitionId"],
                    "operation": self.todo["operation"],
                    "risk": self.todo["risk"],
                }
            ],
        }

    def _create_run_attempt(
        self, run: dict[str, Any], *, raw_token: str
    ) -> tuple[dict[str, Any], str]:
        run_attempt_id = str(uuid.uuid4())
        fencing_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        attempt = self.store.create_run_attempt(
            {
                "run_attempt_id": run_attempt_id,
                "run_id": run["run_id"],
                "game_id": "StarRail",
                "cadence": "daily",
                "fencing_token_hash": fencing_hash,
                "cancel_authority_hash": "sha256:"
                + hashlib.sha256(
                    f"cancel::{raw_token}".encode("utf-8")
                ).hexdigest(),
                "plan": self._plan(run, run_attempt_id),
            }
        )
        return attempt, fencing_hash

    def _acquire_controller(
        self,
        run: dict[str, Any],
        attempt: dict[str, Any],
        *,
        raw_token: str,
        manager_id: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        return self.store.create_controller_lease(
            {
                "controller_lease_id": str(uuid.uuid4()),
                "desktop_id": "windows-interactive-desktop",
                "manager_id": manager_id or self.manager.manager_id,
                "game_id": "StarRail",
                "run_id": run["run_id"],
                "run_attempt_id": attempt["run_attempt_id"],
                "game_day_key": self.todo["periodKey"],
                "holder_principal_id": "manager-adapter",
                "fencing_hash": f"sha256:{digest}",
                "fencing_fingerprint": f"sha256:{digest[:16]}",
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
            }
        )

    def _start_todo_attempt(
        self, run: dict[str, Any], run_attempt_id: str, attempt_number: int
    ) -> dict[str, Any]:
        return self.store.start_todo_attempt(
            {
                "todo_attempt_id": str(uuid.uuid4()),
                "run_attempt_id": run_attempt_id,
                "run_id": run["run_id"],
                "todo_instance_id": self.todo["todoInstanceId"],
                "attempt_number": attempt_number,
                "operation": self.todo["operation"],
            }
        )

    def _artifact(
        self,
        *,
        run: dict[str, Any],
        run_attempt_id: str,
        todo_attempt_id: str,
        todo_instance_id: str | None = None,
    ) -> str:
        artifact_id = str(uuid.uuid4())
        self.store.create_resource(
            "artifact",
            resource_id=artifact_id,
            state="accepted",
            document={
                "kind": "screenshot",
                "contentType": "image/png",
                "source": "manager-adapter-v1.1-test",
                "raw": True,
                "gameId": "StarRail",
                "runId": run["run_id"],
                "runAttemptId": run_attempt_id,
                "todoAttemptId": todo_attempt_id,
                "todoInstanceId": todo_instance_id or self.todo["todoInstanceId"],
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "verdict": "accepted",
                "hash": hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
                "sizeBytes": 1,
                "fileName": f"artifact-{artifact_id}.png",
            },
        )
        return artifact_id

    def assert_no_fencing_material(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = "".join(
                    character.lower()
                    for character in str(key)
                    if character.isalnum()
                )
                self.assertNotIn(normalized, {"fencingtoken", "fencingtokenhash"})
                self.assert_no_fencing_material(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_no_fencing_material(child)

    def test_logs_rejoin_adapter_payload_with_its_run_scope(self) -> None:
        run = self._create_game_run()
        attempt, fencing_hash = self._create_run_attempt(
            run, raw_token="adapter-log-rejoin-private-token"
        )
        cursor = self.store.latest_event_sequence()
        self.store.append_adapter_event(
            run_attempt_id=attempt["run_attempt_id"],
            sequence=0,
            event_type="todo_terminal",
            payload={
                "eventType": "todo_terminal",
                "sequence": 0,
                "todoInstanceId": self.todo["todoInstanceId"],
                "todoAttemptId": "todo-attempt-log-rejoin",
                "status": "review_required",
                "observedState": "launcher-download-required",
                "reasonCode": "launcher_download_required",
                "reason": "launcher still requires a content download",
            },
            fencing_token_hash=fencing_hash,
        )

        response = self.client.get(
            "/api/v1/logs", params={"after": cursor, "limit": 10}
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["source"], "adapter:StarRail")
        self.assertEqual(item["gameId"], "StarRail")
        self.assertEqual(item["runId"], run["run_id"])
        self.assertEqual(item["runAttemptId"], attempt["run_attempt_id"])
        self.assertEqual(item["phase"], "todo_terminal")
        self.assertEqual(item["observedState"], "launcher-download-required")
        self.assertEqual(item["decision"], "review_required")
        self.assertEqual(item["reasonCode"], "launcher_download_required")
        self.assertEqual(item["level"], "warning")

    def test_safe_plan_and_adapter_event_fencing_sequence_and_terminal_state(self) -> None:
        run = self._create_game_run()
        raw_token = "raw-fencing-token-must-never-persist"
        raw_hash_attempt_id = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "must be a sha256 digest"):
            self.store.create_run_attempt(
                {
                    "run_attempt_id": raw_hash_attempt_id,
                    "run_id": run["run_id"],
                    "game_id": "StarRail",
                    "cadence": "daily",
                    "fencing_token_hash": raw_token,
                    "cancel_authority_hash": "sha256:" + "4" * 64,
                    "plan": self._plan(run, raw_hash_attempt_id),
                }
            )
        widened_attempt_id = str(uuid.uuid4())
        widened_plan = self._plan(run, widened_attempt_id)
        widened_plan["todos"] = []
        with self.assertRaisesRegex(ValueError, "differs from its GameRun Todo scope"):
            self.store.create_run_attempt(
                {
                    "run_attempt_id": widened_attempt_id,
                    "run_id": run["run_id"],
                    "game_id": "StarRail",
                    "cadence": "daily",
                    "fencing_token_hash": hashlib.sha256(
                        raw_token.encode("utf-8")
                    ).hexdigest(),
                    "cancel_authority_hash": "sha256:" + "4" * 64,
                    "plan": widened_plan,
                }
            )
        rejected_id = str(uuid.uuid4())
        unsafe_plan = self._plan(run, rejected_id)
        unsafe_plan["fencingToken"] = raw_token
        with self.assertRaisesRegex(ValueError, "must not persist fencing material"):
            self.store.create_run_attempt(
                {
                    "run_attempt_id": rejected_id,
                    "run_id": run["run_id"],
                    "game_id": "StarRail",
                    "cadence": "daily",
                    "fencing_token_hash": hashlib.sha256(
                        raw_token.encode("utf-8")
                    ).hexdigest(),
                    "cancel_authority_hash": "sha256:" + "4" * 64,
                    "plan": unsafe_plan,
                }
            )
        self.assertEqual(self.store.list_run_attempts(run_id=run["run_id"]), [])

        attempt, fencing_hash = self._create_run_attempt(run, raw_token=raw_token)
        persisted_plan = self.store.connection.execute(
            "SELECT plan_json FROM run_attempts WHERE run_attempt_id = ?",
            (attempt["run_attempt_id"],),
        ).fetchone()[0]
        self.assertNotIn(raw_token, persisted_plan)
        self.assertNotIn("fencingToken", persisted_plan)

        with self.assertRaisesRegex(ValueError, "fencing token is stale"):
            self.store.append_adapter_event(
                run_attempt_id=attempt["run_attempt_id"],
                sequence=0,
                event_type="hello",
                payload={"protocolVersion": "1.1"},
                fencing_token_hash="0" * 64,
            )
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            self.store.append_adapter_event(
                run_attempt_id=attempt["run_attempt_id"],
                sequence=1,
                event_type="hello",
                payload={"protocolVersion": "1.1"},
                fencing_token_hash=fencing_hash,
            )

        event, replayed = self.store.append_adapter_event(
            run_attempt_id=attempt["run_attempt_id"],
            sequence=0,
            event_type="hello",
            payload={"protocolVersion": "1.1"},
            fencing_token_hash=fencing_hash,
        )
        self.assertFalse(replayed)
        replay, replayed = self.store.append_adapter_event(
            run_attempt_id=attempt["run_attempt_id"],
            sequence=0,
            event_type="hello",
            payload={"protocolVersion": "1.1"},
            fencing_token_hash=fencing_hash,
        )
        self.assertTrue(replayed)
        self.assertEqual(replay["payload_hash"], event["payload_hash"])
        with self.assertRaisesRegex(ValueError, "reused with new content"):
            self.store.append_adapter_event(
                run_attempt_id=attempt["run_attempt_id"],
                sequence=0,
                event_type="hello",
                payload={"protocolVersion": "1.1", "changed": True},
                fencing_token_hash=fencing_hash,
            )
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            self.store.append_adapter_event(
                run_attempt_id=attempt["run_attempt_id"],
                sequence=2,
                event_type="todo_progress",
                payload={"message": "gap"},
                fencing_token_hash=fencing_hash,
            )
        self.store.append_adapter_event(
            run_attempt_id=attempt["run_attempt_id"],
            sequence=1,
            event_type="todo_progress",
            payload={"message": "contiguous"},
            fencing_token_hash=fencing_hash,
        )
        self.assertEqual(
            [item["sequence"] for item in self.store.list_adapter_events(attempt["run_attempt_id"])],
            [0, 1],
        )

        self.store.update_run_attempt(
            attempt["run_attempt_id"],
            state="completed",
            exit_code=0,
            result={"protocolValid": True},
            completed=True,
        )
        with self.assertRaisesRegex(ValueError, "terminal run attempt"):
            self.store.append_adapter_event(
                run_attempt_id=attempt["run_attempt_id"],
                sequence=2,
                event_type="run_terminal",
                payload={"status": "completed"},
                fencing_token_hash=fencing_hash,
            )
        with self.assertRaisesRegex(ValueError, "terminal run attempts are immutable"):
            self.store.update_run_attempt(
                attempt["run_attempt_id"],
                state="failed",
                result={"rewritten": True},
                completed=True,
            )

    def test_retryable_blocked_resume_requires_fresh_exact_attempt_artifact(self) -> None:
        run = self._create_game_run()
        first_run_attempt, _ = self._create_run_attempt(
            run, raw_token="first-run-fencing-token"
        )
        first_todo_attempt = self._start_todo_attempt(
            run, first_run_attempt["run_attempt_id"], 1
        )
        blocked = self.store.finish_todo_attempt(
            first_todo_attempt["todo_attempt_id"],
            status="blocked",
            reason_code="transient_window_state",
            reason="retry after a fresh observation",
            retryable=True,
            evidence_refs=[],
        )
        self.assertTrue(blocked["retryable"])
        self.assertEqual(
            self.store.get_todo_instance(self.todo["todoInstanceId"])["status"],
            "blocked",
        )
        self.store.update_run_attempt(
            first_run_attempt["run_attempt_id"],
            state="blocked",
            result={"retryable": True},
            completed=True,
        )

        second_run_attempt, _ = self._create_run_attempt(
            run, raw_token="second-run-fencing-token"
        )
        second_todo_attempt = self._start_todo_attempt(
            run, second_run_attempt["run_attempt_id"], 2
        )
        stale_artifact = self._artifact(
            run=run,
            run_attempt_id=first_run_attempt["run_attempt_id"],
            todo_attempt_id=first_todo_attempt["todo_attempt_id"],
        )
        with self.assertRaisesRegex(ValueError, "belongs to another attempt"):
            self.store.finish_todo_attempt(
                second_todo_attempt["todo_attempt_id"],
                status="completed",
                reason_code="stale_evidence_must_fail",
                reason="must use evidence created by this exact attempt",
                retryable=False,
                evidence_refs=[stale_artifact],
            )
        self.assertEqual(
            self.store.get_todo_attempt(second_todo_attempt["todo_attempt_id"])["state"],
            "running",
        )

        fresh_artifact = self._artifact(
            run=run,
            run_attempt_id=second_run_attempt["run_attempt_id"],
            todo_attempt_id=second_todo_attempt["todo_attempt_id"],
        )
        completed = self.store.finish_todo_attempt(
            second_todo_attempt["todo_attempt_id"],
            status="completed",
            reason_code="fresh_evidence_accepted",
            reason="fresh exact attempt evidence",
            retryable=False,
            evidence_refs=[fresh_artifact],
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["evidence_refs"], [fresh_artifact])
        todo = self.store.get_todo_instance(self.todo["todoInstanceId"])
        self.assertEqual(todo["status"], "completed")
        self.assertEqual(todo["attempts"], 2)
        self.assertEqual(todo["evidence_refs"], [fresh_artifact])

        with self.assertRaisesRegex(ValueError, "already terminal"):
            self.store.finish_todo_attempt(
                second_todo_attempt["todo_attempt_id"],
                status="completed",
                reason_code="rewrite",
                reason="must not rewrite",
                retryable=False,
                evidence_refs=[fresh_artifact],
            )
        with self.assertRaisesRegex(ValueError, "invalid Todo transition"):
            self.store.transition_todo_instance(
                self.todo["todoInstanceId"],
                status="pending",
                reason="must not reopen a completed period",
                evidence_refs=[],
                run_id=run["run_id"],
                increment_attempt=False,
                requested_by="attempt-ledger-test",
            )

    def test_fresh_batch_can_start_routine_review_from_a_prior_run(self) -> None:
        first_run = self._create_game_run()
        first_attempt, _ = self._create_run_attempt(
            first_run, raw_token="first-review-token"
        )
        first_todo_attempt = self._start_todo_attempt(
            first_run, first_attempt["run_attempt_id"], 1
        )
        self.store.finish_todo_attempt(
            first_todo_attempt["todo_attempt_id"],
            status="review_required",
            reason_code="prior_transport_failed",
            reason="prior fresh batch ended before a verified outcome",
            retryable=True,
            evidence_refs=[],
        )

        second_run = self._create_game_run()
        second_attempt, _ = self._create_run_attempt(
            second_run, raw_token="second-review-token"
        )
        restarted = self._start_todo_attempt(
            second_run, second_attempt["run_attempt_id"], 2
        )
        self.assertEqual(restarted["state"], "running")
        self.assertEqual(restarted["run_attempt_id"], second_attempt["run_attempt_id"])

    def test_attempt_read_apis_expose_history_without_fencing_material(self) -> None:
        run = self._create_game_run()
        raw_token = "api-read-must-not-return-this-token"
        run_attempt, fencing_hash = self._create_run_attempt(run, raw_token=raw_token)
        self.store.append_adapter_event(
            run_attempt_id=run_attempt["run_attempt_id"],
            sequence=0,
            event_type="hello",
            payload={"protocolVersion": "1.1", "packageId": "fixture"},
            fencing_token_hash=fencing_hash,
        )
        todo_attempt = self._start_todo_attempt(run, run_attempt["run_attempt_id"], 1)

        responses = [
            self.client.get(f"/api/v1/game-runs/{run['run_id']}/attempts"),
            self.client.get(f"/api/v1/run-attempts/{run_attempt['run_attempt_id']}"),
            self.client.get(
                f"/api/v1/run-attempts/{run_attempt['run_attempt_id']}/events"
            ),
            self.client.get(
                f"/api/v1/todo-instances/{self.todo['todoInstanceId']}/attempts"
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
            document = response.json()
            self.assert_no_fencing_material(document)
            serialized = json.dumps(document, sort_keys=True)
            self.assertNotIn(raw_token, serialized)
            self.assertNotIn(fencing_hash, serialized)

        run_attempt_document = responses[1].json()
        self.assertEqual(
            run_attempt_document["executableTodoInstanceIds"],
            [self.todo["todoInstanceId"]],
        )
        self.assertNotIn("plan", run_attempt_document)
        todo_attempt_page = responses[3].json()
        self.assertEqual(todo_attempt_page["items"][0]["todoAttemptId"], todo_attempt["todo_attempt_id"])

        schema = self.client.app.openapi()
        for path in (
            "/api/v1/game-runs/{run_id}/attempts",
            "/api/v1/run-attempts/{run_attempt_id}",
            "/api/v1/run-attempts/{run_attempt_id}/events",
            "/api/v1/todo-instances/{todo_instance_id}/attempts",
        ):
            self.assertIn(path, schema["paths"])
        for model in (
            "RunAttemptRecord",
            "RunAttemptPage",
            "TodoAttemptRecord",
            "TodoAttemptPage",
            "AdapterEventRecord",
            "AdapterEventPage",
        ):
            self.assertIn(model, schema["components"]["schemas"])

    def test_controller_lease_is_unique_explicitly_released_and_raw_free(self) -> None:
        first_run = self._create_game_run()
        first_raw = "desktop-controller-first-private-token"
        first_attempt, _ = self._create_run_attempt(first_run, raw_token=first_raw)
        first_lease = self._acquire_controller(
            first_run, first_attempt, raw_token=first_raw
        )

        second_run = self._create_game_run()
        second_raw = "desktop-controller-second-private-token"
        second_attempt, _ = self._create_run_attempt(second_run, raw_token=second_raw)
        with self.assertRaisesRegex(ValueError, "already has an active controller"):
            self._acquire_controller(
                second_run, second_attempt, raw_token=second_raw
            )

        self.store.transition_controller_lease(
            first_lease["controller_lease_id"],
            state="released",
            reason_code="attempt_terminal",
            reason="first fixture attempt ended",
        )
        second_lease = self._acquire_controller(
            second_run, second_attempt, raw_token=second_raw
        )
        self.assertEqual(second_lease["generation"], first_lease["generation"] + 1)

        response = self.client.get("/api/v1/execution-control/controller-leases")
        self.assertEqual(response.status_code, 200, response.text)
        public = response.json()["items"]
        self.assertTrue(public)
        self.assertIn("fencingFingerprint", public[0])
        self.assertNotIn("fencingHash", public[0])
        snapshot = self.client.get("/api/v1/snapshot").json()
        self.assertEqual(
            snapshot["executionControl"]["activeControllerLeaseCount"], 1
        )
        serialized = "\n".join(self.store.connection.iterdump())
        self.assertNotIn(first_raw, serialized)
        self.assertNotIn(second_raw, serialized)

    def test_expired_controller_does_not_implicitly_authorize_replacement(self) -> None:
        first_run = self._create_game_run()
        first_raw = "expired-controller-private-token"
        first_attempt, _ = self._create_run_attempt(first_run, raw_token=first_raw)
        first_lease = self._acquire_controller(
            first_run, first_attempt, raw_token=first_raw
        )
        self.store.connection.execute(
            "UPDATE controller_leases SET expires_at = ? WHERE controller_lease_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                first_lease["controller_lease_id"],
            ),
        )
        second_run = self._create_game_run()
        second_raw = "replacement-after-expiry-private-token"
        second_attempt, _ = self._create_run_attempt(second_run, raw_token=second_raw)
        with self.assertRaisesRegex(ValueError, "already has an active controller"):
            self._acquire_controller(
                second_run, second_attempt, raw_token=second_raw
            )

    def test_restart_revokes_old_process_lease_and_reconciles_attempt(self) -> None:
        run = self._create_game_run()
        raw_token = "old-manager-process-private-token"
        attempt, _ = self._create_run_attempt(run, raw_token=raw_token)
        lease = self._acquire_controller(
            run,
            attempt,
            raw_token=raw_token,
            manager_id="old-manager-process",
        )

        revoked = self.store.revoke_stale_controller_leases(
            self.manager.manager_id
        )

        self.assertEqual(revoked, 1)
        self.assertEqual(
            self.store.get_controller_lease_private(
                lease["controller_lease_id"]
            )["state"],
            "revoked",
        )
        self.assertEqual(
            self.store.get_run_attempt(attempt["run_attempt_id"])["state"],
            "review_required",
        )
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")

    def test_active_persistent_blocker_prevents_successor_completion(self) -> None:
        run = self._create_game_run()
        first_raw = "blocked-attempt-private-token"
        first_attempt, _ = self._create_run_attempt(run, raw_token=first_raw)
        first_lease = self._acquire_controller(
            run, first_attempt, raw_token=first_raw
        )
        first_todo_attempt = self._start_todo_attempt(
            run, first_attempt["run_attempt_id"], 1
        )
        blocker_artifact = self._artifact(
            run=run,
            run_attempt_id=first_attempt["run_attempt_id"],
            todo_attempt_id=first_todo_attempt["todo_attempt_id"],
        )
        first_todo_attempt = self.store.finish_todo_attempt(
            first_todo_attempt["todo_attempt_id"],
            status="blocked",
            reason_code="fixture_blocked",
            reason="fixture blocker with current evidence",
            retryable=True,
            evidence_refs=[blocker_artifact],
        )
        blocker, status = self.store.create_or_reuse_todo_blocker(
            {
                "manager_id": self.manager.manager_id,
                "game_id": "StarRail",
                "run_id": run["run_id"],
                "run_attempt_id": first_attempt["run_attempt_id"],
                "todo_instance_id": self.todo["todoInstanceId"],
                "todo_attempt_id": first_todo_attempt["todo_attempt_id"],
                "game_day_key": self.todo["periodKey"],
                "kind": "contract_invariant",
                "code": "fixture_blocked",
                "retryable": True,
                "reason": "fixture blocker with current evidence",
                "artifact_refs": [blocker_artifact],
            }
        )
        self.assertEqual(status, "created")
        self.assertEqual(blocker["state"], "active")
        blocker_page = self.client.get(
            "/api/v1/execution-control/todo-blockers",
            params={"runId": run["run_id"], "activeOnly": "true"},
        )
        self.assertEqual(blocker_page.status_code, 200, blocker_page.text)
        self.assertEqual(
            blocker_page.json()["items"][0]["blockerId"], blocker["blocker_id"]
        )
        self.store.update_run_attempt(
            first_attempt["run_attempt_id"],
            state="blocked",
            completed=True,
        )
        self.store.transition_controller_lease(
            first_lease["controller_lease_id"],
            state="released",
            reason_code="blocked_terminal",
            reason="fixture predecessor ended",
        )

        second_raw = "blocked-successor-private-token"
        second_attempt, _ = self._create_run_attempt(run, raw_token=second_raw)
        self._acquire_controller(run, second_attempt, raw_token=second_raw)
        second_todo_attempt = self._start_todo_attempt(
            run, second_attempt["run_attempt_id"], 2
        )
        completion_artifact = self._artifact(
            run=run,
            run_attempt_id=second_attempt["run_attempt_id"],
            todo_attempt_id=second_todo_attempt["todo_attempt_id"],
        )
        with self.assertRaisesRegex(ValueError, "active blocker"):
            self.store.finish_todo_attempt(
                second_todo_attempt["todo_attempt_id"],
                status="completed",
                reason_code="must_not_complete",
                reason="active blocker must win",
                retryable=False,
                evidence_refs=[completion_artifact],
            )

    def test_new_todo_period_retires_old_blocker_without_human_release(self) -> None:
        run = self._create_game_run()
        raw_token = "old-period-human-gate-private-token"
        attempt, _ = self._create_run_attempt(run, raw_token=raw_token)
        self._acquire_controller(run, attempt, raw_token=raw_token)
        todo_attempt = self._start_todo_attempt(
            run, attempt["run_attempt_id"], 1
        )
        artifact_id = self._artifact(
            run=run,
            run_attempt_id=attempt["run_attempt_id"],
            todo_attempt_id=todo_attempt["todo_attempt_id"],
        )
        terminal = self.store.finish_todo_attempt(
            todo_attempt["todo_attempt_id"],
            status="human_required",
            reason_code="login_required",
            reason="operator login is required",
            retryable=False,
            evidence_refs=[artifact_id],
        )
        blocker, _ = self.store.create_or_reuse_todo_blocker(
            {
                "manager_id": self.manager.manager_id,
                "game_id": "StarRail",
                "run_id": run["run_id"],
                "run_attempt_id": attempt["run_attempt_id"],
                "todo_instance_id": self.todo["todoInstanceId"],
                "todo_attempt_id": terminal["todo_attempt_id"],
                "game_day_key": self.todo["periodKey"],
                "kind": "human_required",
                "code": "login_required",
                "retryable": False,
                "reason": "operator login is required",
                "artifact_refs": [artifact_id],
            }
        )

        future_candidates = self.manager._todo_instance_candidates(
            ["StarRail"],
            "daily",
            at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        replacement = next(
            item
            for item in future_candidates
            if item["todo_definition_id"] == self.todo["todoDefinitionId"]
        )
        result = self.store.reconcile_todo_instances(
            [replacement],
            intent="reconcile",
            requested_by="period-retirement-test",
            reason="new-period",
        )

        retired = self.store.get_todo_blocker(blocker["blocker_id"])
        self.assertEqual(
            result["retired_todo_blocker_ids"], [blocker["blocker_id"]]
        )
        self.assertEqual(retired["state"], "resolved")
        self.assertEqual(retired["resolution_code"], "todo_period_expired")
        self.assertFalse(retired["release_explicit"])
        self.assertIsNone(retired["release_id"])
        self.assertEqual(
            self.store.execution_control_summary()["activeTodoBlockerCount"], 0
        )

    def test_fresh_rebatch_retires_only_retryable_prior_run_blocker(self) -> None:
        predecessor = self._create_game_run()
        predecessor_token = "fresh-rebatch-predecessor-private-token"
        predecessor_attempt, _ = self._create_run_attempt(
            predecessor, raw_token=predecessor_token
        )
        predecessor_lease = self._acquire_controller(
            predecessor, predecessor_attempt, raw_token=predecessor_token
        )
        predecessor_todo_attempt = self._start_todo_attempt(
            predecessor, predecessor_attempt["run_attempt_id"], 1
        )
        artifact_id = self._artifact(
            run=predecessor,
            run_attempt_id=predecessor_attempt["run_attempt_id"],
            todo_attempt_id=predecessor_todo_attempt["todo_attempt_id"],
        )
        terminal = self.store.finish_todo_attempt(
            predecessor_todo_attempt["todo_attempt_id"],
            status="blocked",
            reason_code="retryable_tool_timeout",
            reason="retryable tool timeout",
            retryable=True,
            evidence_refs=[artifact_id],
        )
        blocker, _ = self.store.create_or_reuse_todo_blocker(
            {
                "manager_id": self.manager.manager_id,
                "game_id": "StarRail",
                "run_id": predecessor["run_id"],
                "run_attempt_id": predecessor_attempt["run_attempt_id"],
                "todo_instance_id": self.todo["todoInstanceId"],
                "todo_attempt_id": terminal["todo_attempt_id"],
                "game_day_key": self.todo["periodKey"],
                "kind": "contract_invariant",
                "code": "retryable_tool_timeout",
                "retryable": True,
                "reason": "retryable tool timeout",
                "artifact_refs": [artifact_id],
            }
        )
        self.store.update_run_attempt(
            predecessor_attempt["run_attempt_id"],
            state="blocked",
            completed=True,
        )
        self.store.transition_controller_lease(
            predecessor_lease["controller_lease_id"],
            state="released",
            reason_code="predecessor_terminal",
            reason="predecessor ended before fresh batch",
        )

        successor = self._create_game_run()
        successor_token = "fresh-rebatch-successor-private-token"
        successor_attempt, _ = self._create_run_attempt(
            successor, raw_token=successor_token
        )
        self._acquire_controller(
            successor, successor_attempt, raw_token=successor_token
        )
        self._start_todo_attempt(
            successor, successor_attempt["run_attempt_id"], 2
        )

        retired = self.store.get_todo_blocker(blocker["blocker_id"])
        self.assertEqual(retired["state"], "resolved")
        self.assertEqual(
            retired["resolution_code"], "superseded_by_fresh_rebatch"
        )
        self.assertFalse(retired["release_explicit"])
        self.assertIn(
            successor["run_id"], retired["resolution_reason"]
        )


if __name__ == "__main__":
    unittest.main()
