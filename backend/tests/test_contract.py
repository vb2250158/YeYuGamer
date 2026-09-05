from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import MAX_MUTATION_BODY_BYTES, create_app
from yeyu_gamer_manager.security import WebGuiSessionBroker
from yeyu_gamer_manager.services.integration_catalog import registration_for
from yeyu_gamer_manager.settings import Settings
from yeyu_gamer_manager.store.sqlite_store import (
    PublicFencingMaterialRejected,
    SqliteStore,
)
from yeyu_gamer_manager.services.window_capture import (
    CapturedWindow,
    _encode_bgra_png,
)


_PRIVATE_STORE_TEXT_COLUMNS = {
    ("work_item_claims", "fencing_token"),
    ("work_item_fencing_token_history", "fencing_token"),
    ("run_attempts", "fencing_token_hash"),
    ("notification_deliveries", "lease_token"),
}


def public_text_columns(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    tables = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]
    result: dict[str, tuple[str, ...]] = {}
    for table in tables:
        quoted_table = '"' + table.replace('"', '""') + '"'
        columns: list[str] = []
        for column in connection.execute(f"PRAGMA table_info({quoted_table})"):
            column_name = str(column[1])
            if "TEXT" not in str(column[2]).upper() or (
                table,
                column_name,
            ) in _PRIVATE_STORE_TEXT_COLUMNS:
                continue
            columns.append(column_name)
        if columns:
            result[table] = tuple(columns)
    return result


def public_text_locations_containing(
    connection: sqlite3.Connection, needle: str
) -> list[str]:
    locations: list[str] = []
    for table, columns in public_text_columns(connection).items():
        quoted_table = '"' + table.replace('"', '""') + '"'
        for column_name in columns:
            quoted_column = '"' + column_name.replace('"', '""') + '"'
            found = connection.execute(
                f"""
                SELECT 1 FROM {quoted_table}
                WHERE instr(COALESCE(CAST({quoted_column} AS TEXT), ''), ?) > 0
                LIMIT 1
                """,
                (needle,),
            ).fetchone()
            if found is not None:
                locations.append(f"{table}.{column_name}")
    return locations


class ManagerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-gamer-manager-")
        self.root = Path(self.temporary.name)
        self.settings = Settings.for_test(self.root)
        self.settings.legacy_root.mkdir(parents=True)
        self.legacy_config_path = self.settings.legacy_root / "daily-gui-config.json"
        self.legacy_policy_path = (
            self.settings.legacy_root / "game-automation-policy.json"
        )
        self.legacy_config_path.write_text(
            json.dumps(
                {
                    "order": ["StarRail", "ZZZ"],
                    "enabled": {"StarRail": True, "ZZZ": False},
                    "dailyScheduleEnabled": False,
                    "dailyScheduleTime": "17:00",
                    "weeklyEnabled": True,
                    "weeklyDay": "Saturday",
                    "skipBlockedOnRunAll": True,
                    "executionStrategy": "continue",
                    "messageEndpointPort": 8877,
                    "emailNotifications": {"smtpPassword": "must-not-import"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.legacy_policy_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "forbiddenActions": ["purchase", "draw", "account_settings"],
                    "games": {
                        "StarRail": {
                            "tier": "stable",
                            "weeklyMode": "supported",
                            "paths": ["C:/must-not-leak"],
                        },
                        "ZZZ": {"tier": "stable", "weeklyMode": "supported"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.original_config = self.legacy_config_path.read_bytes()
        self.original_policy = self.legacy_policy_path.read_bytes()
        self.settings.web_dist.mkdir(parents=True)
        (self.settings.web_dist / "index.html").write_text(
            "<!doctype html><title>YeYu Gamer</title><div id='app'></div>",
            encoding="utf-8",
        )
        self.settings.actor_tokens_dir.mkdir(parents=True)
        (self.settings.actor_tokens_dir / "tray.token").write_text(
            "retired-persistent-tray-token-000000000000000000",
            encoding="ascii",
        )
        self.worker_tokens = {
            "alpha": "agent-alpha-worker-token-0123456789abcdef",
            "beta": "agent-beta-worker-token-0123456789abcdef0",
        }
        for worker, token in self.worker_tokens.items():
            (self.settings.actor_tokens_dir / f"agent.{worker}.token").write_text(
                token, encoding="ascii"
            )
        self.adapter_host_root = (
            self.settings.data_dir / "adapters" / "manager-adapter-host"
        )
        self.adapter_host_root.mkdir(parents=True)
        self.adapter_host_entrypoint = self.adapter_host_root / "host.exe"
        self.adapter_host_entrypoint.write_bytes(b"test-manager-adapter-host")
        self.adapter_host_hash = hashlib.sha256(
            self.adapter_host_entrypoint.read_bytes()
        ).hexdigest()
        (self.adapter_host_root / "install-manifest.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "packageId": "manager-adapter-host",
                    "hostVersion": "0.1.0",
                    "protocolVersion": "1.1",
                    "entryPoint": "host.exe",
                    "sha256": self.adapter_host_hash,
                    "sizeBytes": self.adapter_host_entrypoint.stat().st_size,
                    "hostReady": True,
                    "executionReady": False,
                    "supportedGameIds": ["StarRail", "ZZZ"],
                    "installedAt": "2026-08-27T00:00:00Z",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        self.lifecycle_actions: list[str] = []
        self.client_context = TestClient(
            create_app(self.settings, lifecycle_callback=self.lifecycle_actions.append)
        )
        self.client = self.client_context.__enter__()
        self.assertFalse((self.settings.actor_tokens_dir / "tray.token").exists())
        self.assertTrue(
            (self.settings.actor_tokens_dir / "agent.yeyu.token").is_file()
        )
        self.assertNotIn(str(self.settings.tray_bootstrap_secret), repr(self.settings))
        self.bootstrap_webgui_session()

    def bootstrap_webgui_session(self) -> str:
        issued = self.client.post(
            "/api/v1/webgui/bootstrap-nonces",
            json={},
            headers=self.actor_headers("tray"),
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        nonce = str(issued.json()["nonce"])
        self.assertRegex(nonce, r"^[A-Za-z0-9_-]{40,128}$")
        self.assertLessEqual(int(issued.json()["expiresInSeconds"]), 60)
        exchanged = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": nonce},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        self.assertEqual(exchanged.json(), {"authenticated": True, "actor": "webgui"})
        self.assertTrue(self.client.cookies.get("yeyu_session"))
        self.assertTrue(self.client.cookies.get("yeyu_csrf"))
        return nonce

    def adapter_host_protocol_run(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.assertEqual(Path(command[0]), self.adapter_host_entrypoint.resolve())
        self.assertEqual(command[1], "--operation")
        operation = command[2]
        self.assertIn(operation, {"canary", "execute"})
        self.assertEqual(command[3:5], ["--protocol-version", "1.1"])
        self.assertEqual(command[5], "--run-id")
        run_id = command[6]
        self.assertRegex(
            run_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        self.assertEqual(command[7], "--game-id")
        game_id = command[8]
        self.assertIn(game_id, {"StarRail", "ZZZ"})
        self.assertEqual(kwargs["cwd"], self.adapter_host_root)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["timeout"], 10)
        success = operation == "canary"
        payload = {
            "protocolVersion": "1.1",
            "hostVersion": "0.1.0",
            "operation": operation,
            "success": success,
            "code": "canary_passed" if success else "execution_package_unavailable",
            "message": "typed test response",
            "runId": run_id,
            "gameId": game_id,
            "hostReady": True,
            "executionReady": False,
            "adapterProcessStarted": False,
            "gameProcessStarted": False,
            "entryPointSha256": self.adapter_host_hash,
            "supportedGameIds": ["StarRail", "ZZZ"],
        }
        return subprocess.CompletedProcess(
            command,
            0 if success else 78,
            stdout=(json.dumps(payload, separators=(",", ":")) + "\n").encode(),
            stderr=b"",
        )

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def actor_headers(self, actor: str = "cli") -> dict[str, str]:
        if actor == "tray":
            token = self.settings.tray_bootstrap_secret
            self.assertIsNotNone(token)
        else:
            token_name = "agent.yeyu.token" if actor == "agent" else f"{actor}.token"
            token = (self.settings.actor_tokens_dir / token_name).read_text(
                encoding="ascii"
            ).strip()
        return {
            "Authorization": f"Bearer {str(token)}",
            "X-YeYu-Gamer-Actor": actor,
        }

    def mutation_headers(
        self,
        idempotency_key: str,
        *,
        request_id: str | None = None,
        actor: str = "cli",
    ) -> dict[str, str]:
        actor_auth = self.actor_headers(actor)
        version = self.client.get(
            "/api/v1/snapshot", headers=actor_auth
        ).json()["stateVersion"]
        headers = {
            **actor_auth,
            "Idempotency-Key": idempotency_key,
            "If-Match": f'"{version}"',
            "X-Expected-State-Version": str(version),
        }
        if request_id:
            headers["X-Request-Id"] = request_id
        return headers

    def worker_headers(self, worker: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.worker_tokens[worker]}",
            "X-YeYu-Gamer-Actor": "agent",
        }

    def worker_mutation_headers(self, worker: str, key: str) -> dict[str, str]:
        headers = self.worker_headers(worker)
        version = self.client.get(
            "/api/v1/snapshot", headers=headers
        ).json()["stateVersion"]
        return {
            **headers,
            "Idempotency-Key": key,
            "If-Match": str(version),
        }

    def create_diagnostic_artifact(self, key: str) -> str:
        response = self.client.post(
            "/api/v1/diagnostic-bundles",
            json={},
            headers=self.mutation_headers(key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["result"]["artifact"]["artifactId"])

    def test_meta_snapshot_and_all_webgui_read_pages_are_available(self) -> None:
        meta = self.client.get("/api/v1/meta")
        self.assertEqual(meta.status_code, 200)
        self.assertEqual(meta.json()["apiVersion"], "v1")
        self.assertFalse(meta.json()["legacyExecutionEnabled"])
        self.assertEqual(len(meta.json()["openapiSha256"]), 64)
        self.assertEqual(len(meta.json()["webAssetSha256"]), 64)
        expected_contract_hash = hashlib.sha256(
            json.dumps(
                self.client.app.openapi(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(meta.json()["openapiSha256"], expected_contract_hash)
        self.assertEqual(
            meta.headers["X-YeYu-Gamer-OpenAPI-SHA256"],
            meta.json()["openapiSha256"],
        )
        self.assertEqual(meta.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", meta.headers["Content-Security-Policy"])

        snapshot = self.client.get("/api/v1/snapshot")
        self.assertEqual(snapshot.status_code, 200)
        payload = snapshot.json()
        self.assertIsInstance(payload["stateVersion"], int)
        self.assertEqual(payload["eventCursor"], str(payload["stateVersion"]))
        self.assertEqual([game["gameId"] for game in payload["games"]], ["StarRail", "ZZZ"])
        self.assertIn("runtimeState", payload["games"][0])
        self.assertIn("acceptanceState", payload["games"][0])
        self.assertIn("reviewState", payload["games"][0])

        read_pages = [
            "/api/v1/games",
            "/api/v1/batches",
            "/api/v1/game-runs",
            "/api/v1/agent/work-items",
            "/api/v1/claims",
            "/api/v1/claims/decisions",
            "/api/v1/incidents",
            "/api/v1/artifacts",
            "/api/v1/weekly",
            "/api/v1/adapters",
            "/api/v1/adapter-diagnostic-canaries",
            "/api/v1/logs",
            "/api/v1/capabilities",
            "/api/v1/capability-invocations",
            "/api/v1/notifications",
        ]
        for path in read_pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("items", response.json())

        self.assertEqual(self.client.get("/api/v1/config").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/policy").status_code, 200)

    def test_logs_preserve_bounded_execution_semantics(self) -> None:
        manager = self.client.app.state.manager
        cursor = manager.store.latest_event_sequence()
        manager.store.append_event(
            "adapter-event.imported",
            "adapter-event",
            "adapter-event-log-test",
            {
                "event": {
                    "eventType": "todo_attempt_terminal",
                    "gameId": "StarRail",
                    "runId": "run-log-test",
                    "runAttemptId": "attempt-log-test",
                    "todoInstanceId": "todo-log-test",
                    "todoAttemptId": "todo-attempt-log-test",
                    "phase": "after",
                    "observedState": "monthly-card-popup",
                    "decision": "review_required",
                    "reasonCode": "home_scene_not_ready",
                    "reason": "The observed screen is not an interactive home scene.",
                }
            },
        )

        response = self.client.get(f"/api/v1/logs?after={cursor}&limit=10")
        self.assertEqual(response.status_code, 200, response.text)
        entry = response.json()["items"][0]
        self.assertEqual(entry["eventType"], "adapter-event.imported")
        self.assertEqual(entry["source"], "adapter:StarRail")
        self.assertEqual(entry["gameId"], "StarRail")
        self.assertEqual(entry["runId"], "run-log-test")
        self.assertEqual(entry["runAttemptId"], "attempt-log-test")
        self.assertEqual(entry["todoInstanceId"], "todo-log-test")
        self.assertEqual(entry["todoAttemptId"], "todo-attempt-log-test")
        self.assertEqual(entry["phase"], "after")
        self.assertEqual(entry["observedState"], "monthly-card-popup")
        self.assertEqual(entry["decision"], "review_required")
        self.assertEqual(entry["reasonCode"], "home_scene_not_ready")
        self.assertEqual(entry["level"], "warning")
        self.assertNotIn("{", entry["message"])
        self.assertEqual(response.json()["cursorMode"], "after")
        self.assertEqual(response.json()["nextCursor"], str(entry["sequence"]))

    def test_logs_recent_tail_and_before_paging_have_no_gap_or_overlap(self) -> None:
        manager = self.client.app.state.manager
        initial_sequence = manager.store.latest_event_sequence()
        appended: list[dict[str, object]] = []
        for index in range(263):
            appended.append(
                manager.store.append_event(
                    (
                        "run-attempt.terminal"
                        if index == 262
                        else "run-attempt.progress"
                    ),
                    "diagnostic-log-test",
                    f"tail-log-{index:03d}",
                    {
                        "phase": "terminal" if index == 262 else "running",
                        "decision": "completed" if index == 262 else "continue",
                    },
                )
            )

        response = self.client.get("/api/v1/logs?recent=true&limit=250")
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        items = page["items"]
        recent_sequences = [int(item["sequence"]) for item in items]
        expected_recent = [int(item["sequence"]) for item in appended[-250:]]
        self.assertEqual(recent_sequences, expected_recent)
        self.assertEqual(recent_sequences, sorted(recent_sequences))
        recent_timestamps = [str(item["timestamp"]) for item in items]
        self.assertEqual(recent_timestamps, sorted(recent_timestamps))
        self.assertEqual(page["cursorMode"], "recent")
        self.assertEqual(page["nextCursor"], str(recent_sequences[0]))
        self.assertEqual(items[-1]["eventType"], "run-attempt.terminal")
        self.assertEqual(items[-1]["decision"], "completed")

        older_response = self.client.get(
            "/api/v1/logs",
            params={"before": page["nextCursor"], "limit": 250},
        )
        self.assertEqual(older_response.status_code, 200, older_response.text)
        older_page = older_response.json()
        older_sequences = [
            int(item["sequence"])
            for item in older_page["items"]
            if int(item["sequence"]) > initial_sequence
        ]
        expected_older = [int(item["sequence"]) for item in appended[:-250]]
        self.assertEqual(older_sequences, expected_older)
        self.assertEqual(older_page["cursorMode"], "before")
        combined = older_sequences + recent_sequences
        self.assertEqual(
            combined,
            [int(item["sequence"]) for item in appended],
        )
        self.assertEqual(len(combined), len(set(combined)))

    def test_logs_reject_ambiguous_cursor_modes(self) -> None:
        for path in (
            "/api/v1/logs?after=1&recent=true",
            "/api/v1/logs?after=1&before=2",
            "/api/v1/logs?recent=true&before=2",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400, response.text)

    def test_sqlite_failure_returns_a_typed_recoverable_problem(self) -> None:
        manager = self.client.app.state.manager
        with mock.patch.object(
            manager,
            "snapshot",
            side_effect=sqlite3.InterfaceError("bad parameter or other API misuse"),
        ):
            response = self.client.get("/api/v1/snapshot")

        self.assertEqual(response.status_code, 503, response.text)
        payload = response.json()
        self.assertEqual(payload["code"], "manager_storage_unavailable")
        self.assertIn("local SQLite state store", payload["detail"])
        self.assertIn("requestId", payload)
        self.assertEqual(self.client.get("/api/v1/diagnostics").status_code, 200)
        self.assertIn("YeYu Gamer", self.client.get("/queue").text)

    def test_get_requests_are_pure(self) -> None:
        before = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        for path in [
            "/api/v1/meta",
            "/api/v1/health",
            "/api/v1/snapshot",
            "/api/v1/games",
            "/api/v1/config",
            "/api/v1/policy",
            "/health",
            "/status",
            "/logs",
            "/events",
        ]:
            self.assertEqual(self.client.get(path).status_code, 200)
        after = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        self.assertEqual(before, after)

    def test_write_requires_idempotency_and_replays_same_receipt(self) -> None:
        body = {
            "gameId": "StarRail",
            "cadence": "daily",
            "mode": "plan",
            "requestedBy": "contract-test",
        }
        missing = self.client.post(
            "/api/v1/game-runs",
            json=body,
            headers={
                **self.actor_headers(),
                "If-Match": str(
                    self.client.get("/api/v1/snapshot").json()["stateVersion"]
                ),
            },
        )
        self.assertEqual(missing.status_code, 422, missing.text)

        missing_precondition = self.client.post(
            "/api/v1/game-runs",
            json=body,
            headers={
                **self.actor_headers(),
                "Idempotency-Key": "run-plan-without-version",
            },
        )
        self.assertEqual(missing_precondition.status_code, 428)
        self.assertEqual(
            missing_precondition.json()["code"], "state_version_required"
        )

        headers = self.mutation_headers("run-plan-1", request_id="request-1")
        first = self.client.post("/api/v1/game-runs", json=body, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["state"], "accepted")
        self.assertEqual(first.json()["idempotencyKey"], "run-plan-1")
        self.assertIn("/api/v1/commands/", first.json()["statusUrl"])

        second = self.client.post("/api/v1/game-runs", json=body, headers=headers)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["commandId"], first.json()["commandId"])
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.headers["Idempotency-Replayed"], "true")
        self.assertEqual(
            self.client.get(first.json()["statusUrl"]).json()["commandId"],
            first.json()["commandId"],
        )

        conflicting_body = {**body, "cadence": "weekly"}
        conflict = self.client.post(
            "/api/v1/game-runs", json=conflicting_body, headers=headers
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "idempotency_conflict")

    def test_legacy_execution_is_off_and_allowlist_is_enforced(self) -> None:
        manager = self.client.app.state.manager
        connection = manager.store.connection

        def persisted_counts() -> dict[str, int]:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "batches",
                    "game_runs",
                    "batch_run_memberships",
                    "capability_invocations",
                    "notification_deliveries",
                    "command_receipts",
                    "idempotency",
                    "todo_instances",
                    "events",
                )
            }

        capability_by_id = {
            item["capabilityId"]: item
            for item in self.client.get("/api/v1/capabilities").json()["items"]
        }
        self.assertFalse(capability_by_id["game.daily.run"]["enabled"])
        self.assertFalse(capability_by_id["batch.daily.run"]["enabled"])
        counts_before = persisted_counts()
        with mock.patch.object(
            manager.adapter_host,
            "execute",
            side_effect=AssertionError("Host must not start without promoted Todo bindings"),
        ) as host_execute, mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.Popen",
            side_effect=AssertionError("no subprocess may start for a deferred-only request"),
        ) as popen:
            execute = self.client.post(
                "/api/v1/game-runs",
                json={"gameId": "StarRail", "mode": "execute"},
                headers=self.mutation_headers("must-not-execute"),
            )
            self.assertEqual(execute.status_code, 409, execute.text)
            execute_problem = execute.json()
            self.assertEqual(execute_problem["code"], "execution_unavailable")
            self.assertEqual(execute_problem["errors"]["candidateGameIds"], ["StarRail"])
            self.assertEqual(execute_problem["errors"]["deferredGameIds"], ["StarRail"])
            self.assertEqual(execute_problem["errors"]["games"][0]["gameId"], "StarRail")
            self.assertTrue(execute_problem["errors"]["games"][0]["reasonCode"])
            self.assertEqual(
                execute_problem["errors"]["games"][0]["runtimeBinding"]["bindingCount"],
                0,
            )

            batch = self.client.post(
                "/api/v1/batches",
                json={"gameIds": ["StarRail"], "mode": "execute"},
                headers=self.mutation_headers("must-reject-deferred-batch"),
            )
            self.assertEqual(batch.status_code, 409, batch.text)
            batch_problem = batch.json()
            self.assertEqual(batch_problem["code"], "execution_unavailable")
            self.assertEqual(batch_problem["errors"]["candidateGameIds"], ["StarRail"])
            self.assertEqual(batch_problem["errors"]["deferredGameIds"], ["StarRail"])

            # The capability is normally disabled because no enabled game is
            # execution-ready.  Enabling only its catalog row simulates another
            # globally-ready game and proves that the selected unready scope is
            # still rejected by the same final pre-persistence guard.
            original_batch_capability = manager.capabilities["batch.daily.run"]
            manager.capabilities["batch.daily.run"] = (
                original_batch_capability.model_copy(update={"enabled": True})
            )
            try:
                capability = self.client.post(
                    "/api/v1/capability-invocations",
                    json={
                        "capability": "batch.daily.run",
                        "arguments": {"gameIds": ["StarRail"]},
                    },
                    headers=self.mutation_headers("must-reject-deferred-capability"),
                )
            finally:
                manager.capabilities["batch.daily.run"] = original_batch_capability
            self.assertEqual(capability.status_code, 409, capability.text)
            self.assertEqual(capability.json()["code"], "execution_unavailable")
            self.assertEqual(
                capability.json()["errors"]["deferredGameIds"], ["StarRail"]
            )

            self.assertEqual(persisted_counts(), counts_before)
        host_execute.assert_not_called()
        popen.assert_not_called()

        plan = self.client.post(
            "/api/v1/batches",
            json={"gameIds": ["StarRail"], "mode": "plan"},
            headers=self.mutation_headers("deferred-batch-plan-remains-available"),
        )
        self.assertEqual(plan.status_code, 202, plan.text)
        plan_result = plan.json()["result"]
        self.assertEqual(plan_result["batch"]["state"], "planned")
        self.assertEqual(plan_result["gameRuns"], [])
        self.assertEqual(plan_result["executableGameIds"], [])
        self.assertEqual(plan_result["deferredGameIds"], ["StarRail"])

        unknown = self.client.post(
            "/api/v1/game-runs",
            json={"gameId": "NotARealGame", "mode": "plan"},
            headers=self.mutation_headers("unknown-game"),
        )
        self.assertEqual(unknown.status_code, 404)

        disabled = self.client.post(
            "/api/v1/game-runs",
            json={"gameId": "ZZZ", "mode": "execute"},
            headers=self.mutation_headers("disabled-game"),
        )
        self.assertIn(disabled.status_code, {409, 422})

        self.assertFalse((self.settings.data_dir / "legacy-logs").exists())
        adapter_entry = self.client.app.state.manager.adapter.script_path
        self.assertEqual(adapter_entry.name, "runner.exe")
        self.assertTrue(adapter_entry.is_relative_to(self.settings.data_dir.resolve()))
        self.assertFalse(adapter_entry.is_relative_to(self.settings.legacy_root.resolve()))

        # A batch without executableTodoInstanceIds must seal before either the
        # Host or a legacy runner can start. Deferred/blocked-only queues are not
        # valid execute requests in protocol v1.1.
        adapter = manager.adapter
        adapter.execution_enabled = True
        try:
            with mock.patch(
                "yeyu_gamer_manager.services.adapter_host.subprocess.run",
                side_effect=self.adapter_host_protocol_run,
            ) as host_run, mock.patch(
                "yeyu_gamer_manager.services.legacy_adapter.subprocess.Popen",
                side_effect=AssertionError("Host must not bypass to a legacy process"),
            ):
                with self.assertRaises(RuntimeError):
                    manager.adapter_host.execute(
                        "11111111-1111-4111-8111-111111111111",
                        "StarRail",
                        lambda *_: None,
                    )
            self.assertEqual(host_run.call_count, 0)
        finally:
            adapter.execution_enabled = False
        self.assertIsNone(adapter.active_execution())

    def test_partial_batch_keeps_deferred_games_visible_without_starting_them(self) -> None:
        manager = self.client.app.state.manager
        manager.store.update_config({"enabled": {"ZZZ": True}})
        candidate_game_ids = ["StarRail", "ZZZ"]
        manager.store.reconcile_todo_instances(
            manager._todo_instance_candidates(candidate_game_ids, "daily"),
            intent="reconcile",
            requested_by="contract-test",
            reason="prepare-partial-batch-contract",
        )
        plans = manager._todo_plans_for_games(candidate_game_ids, "daily")
        executable_todo_id = plans["StarRail"]["unresolvedRequiredTodoIds"][0]
        self.assertTrue(plans["ZZZ"]["unresolvedRequiredTodoIds"])
        plans["StarRail"]["executableTodoInstanceIds"] = [executable_todo_id]
        plans["StarRail"]["deferredTodoInstanceIds"] = [
            todo_id
            for todo_id in plans["StarRail"]["deferredTodoInstanceIds"]
            if todo_id != executable_todo_id
        ]
        plans["StarRail"]["runtimeBinding"] = {
            "status": "promoted",
            "manifestVerified": True,
            "bindings": [{"operation": "contract-test"}],
        }
        original_batch_capability = manager.capabilities["batch.daily.run"]
        manager.capabilities["batch.daily.run"] = (
            original_batch_capability.model_copy(update={"enabled": True})
        )

        try:
            with mock.patch.object(
                manager, "_todo_plans_for_games", return_value=plans
            ), mock.patch.object(
                manager, "_require_execution_ready"
            ) as require_ready, mock.patch(
                "yeyu_gamer_manager.services.manager.threading.Thread"
            ) as batch_thread:
                response = self.client.post(
                    "/api/v1/capability-invocations",
                    json={
                        "capability": "batch.daily.run",
                        "arguments": {"gameIds": candidate_game_ids},
                    },
                    headers=self.mutation_headers(
                        "partial-batch-capability-preserves-deferred"
                    ),
                )
        finally:
            manager.capabilities["batch.daily.run"] = original_batch_capability

        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        command_id = receipt["commandId"]
        batch_id = receipt["result"]["batchId"]
        self.assertEqual(receipt["result"], {"batchId": batch_id})
        self.assertLess(len(response.content), 2048)

        invocation = manager.store.get_capability_invocation(command_id)
        self.assertEqual(invocation["result"], {"batchId": batch_id})
        batch = self.client.get(f"/api/v1/batches/{batch_id}").json()
        self.assertEqual(batch["gameIds"], candidate_game_ids)
        self.assertEqual(batch["result"]["executableGameIds"], ["StarRail"])
        self.assertEqual(batch["result"]["deferredGameIds"], ["ZZZ"])
        memberships = self.client.get(
            f"/api/v1/batches/{batch_id}/run-memberships"
        ).json()
        self.assertEqual(len(memberships), 1)
        run = self.client.get(
            f"/api/v1/game-runs/{memberships[0]['runId']}"
        ).json()
        self.assertEqual(run["gameId"], "StarRail")
        require_ready.assert_called_once_with(["StarRail"])
        batch_thread.assert_called_once()
        batch_thread.return_value.start.assert_called_once_with()
        dispatched_runs = batch_thread.call_args.kwargs["args"][1]
        self.assertEqual(
            [item["runId"] for item in dispatched_runs], [run["runId"]]
        )

        manager._complete_command(command_id, "failed", "contract-test terminal")
        stored_receipt = self.client.get(f"/api/v1/commands/{command_id}").json()
        self.assertEqual(stored_receipt["result"], {"batchId": batch_id})
        receipt_events = [
            event
            for event in manager.store.list_events(0, 5000)
            if event["event_type"] == "command.receipt"
            and event["entity_id"] == command_id
        ]
        self.assertEqual(
            [event["payload"]["state"] for event in receipt_events],
            ["accepted", "failed"],
        )
        for event in receipt_events:
            self.assertEqual(event["payload"]["result"], {"batchId": batch_id})

    def test_execute_batch_receipt_is_compact_and_replay_dispatches_once(self) -> None:
        manager = self.client.app.state.manager
        candidate_game_ids = ["StarRail"]
        manager.store.reconcile_todo_instances(
            manager._todo_instance_candidates(candidate_game_ids, "daily"),
            intent="reconcile",
            requested_by="contract-test",
            reason="prepare-compact-execute-receipt",
        )
        plans = manager._todo_plans_for_games(candidate_game_ids, "daily")
        executable_todo_id = plans["StarRail"]["unresolvedRequiredTodoIds"][0]
        plans["StarRail"]["executableTodoInstanceIds"] = [executable_todo_id]
        plans["StarRail"]["deferredTodoInstanceIds"] = [
            todo_id
            for todo_id in plans["StarRail"]["deferredTodoInstanceIds"]
            if todo_id != executable_todo_id
        ]
        plans["StarRail"]["runtimeBinding"] = {
            "status": "promoted",
            "manifestVerified": True,
            "bindings": [{"operation": "contract-test"}],
        }
        headers = self.mutation_headers(
            "compact-execute-batch-replay", request_id="req-compact-execute"
        )
        counts_before = {
            table: int(
                manager.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in ("batches", "game_runs", "batch_run_memberships")
        }

        with mock.patch.object(
            manager, "_todo_plans_for_games", return_value=plans
        ) as todo_plans, mock.patch.object(
            manager, "_require_execution_ready"
        ) as require_ready, mock.patch(
            "yeyu_gamer_manager.services.manager.threading.Thread"
        ) as batch_thread:
            first = self.client.post(
                "/api/v1/batches",
                json={"gameIds": candidate_game_ids, "mode": "execute"},
                headers=headers,
            )
            replay = self.client.post(
                "/api/v1/batches",
                json={"gameIds": candidate_game_ids, "mode": "execute"},
                headers=headers,
            )

        self.assertEqual(first.status_code, 202, first.text)
        first_receipt = first.json()
        batch_id = first_receipt["commandId"]
        self.assertEqual(first_receipt["result"], {"batchId": batch_id})
        self.assertLess(len(first.content), 2048)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["commandId"], batch_id)
        self.assertEqual(replay.json()["result"], {"batchId": batch_id})
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        todo_plans.assert_called_once()
        require_ready.assert_called_once_with(candidate_game_ids)
        batch_thread.assert_called_once()
        batch_thread.return_value.start.assert_called_once_with()

        counts_after = {
            table: int(
                manager.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in counts_before
        }
        self.assertEqual(counts_after["batches"], counts_before["batches"] + 1)
        self.assertEqual(counts_after["game_runs"], counts_before["game_runs"] + 1)
        self.assertEqual(
            counts_after["batch_run_memberships"],
            counts_before["batch_run_memberships"] + 1,
        )

        batch = self.client.get(f"/api/v1/batches/{batch_id}").json()
        self.assertEqual(batch["result"]["candidateGameIds"], candidate_game_ids)
        self.assertEqual(batch["result"]["executableGameIds"], candidate_game_ids)
        memberships = self.client.get(
            f"/api/v1/batches/{batch_id}/run-memberships"
        ).json()
        self.assertEqual(len(memberships), 1)
        run = self.client.get(
            f"/api/v1/game-runs/{memberships[0]['runId']}"
        ).json()
        self.assertEqual(run["gameId"], "StarRail")
        dispatched_runs = batch_thread.call_args.kwargs["args"][1]
        self.assertEqual([item["runId"] for item in dispatched_runs], [run["runId"]])

        manager._complete_command(batch_id, "failed", "contract-test terminal")
        stored_receipt = self.client.get(f"/api/v1/commands/{batch_id}").json()
        self.assertEqual(stored_receipt["state"], "failed")
        self.assertEqual(stored_receipt["result"], {"batchId": batch_id})
        receipt_events = [
            event
            for event in manager.store.list_events(0, 5000)
            if event["event_type"] == "command.receipt"
            and event["entity_id"] == batch_id
        ]
        self.assertEqual(
            [event["payload"]["state"] for event in receipt_events],
            ["accepted", "failed"],
        )
        for event in receipt_events:
            self.assertEqual(event["payload"]["result"], {"batchId": batch_id})

    def test_manager_owned_adapter_host_canary_only_starts_fixed_host(self) -> None:
        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=AssertionError("GET must not start the Adapter Host"),
        ) as get_run:
            adapters = self.client.get("/api/v1/adapters").json()["items"]
            diagnostics = self.client.get("/api/v1/diagnostics")
        get_run.assert_not_called()
        self.assertEqual(diagnostics.status_code, 200, diagnostics.text)
        self.assertTrue(diagnostics.json()["adapterHost"]["hostManifestVerified"])
        self.assertEqual(len(adapters), 2)
        for adapter in adapters:
            self.assertTrue(adapter["hostHealthy"])
            self.assertEqual(adapter["hostStatus"], "ready")
            self.assertFalse(adapter["executionReady"])
            self.assertEqual(adapter["executionPackageStatus"], "missing")
            self.assertEqual(
                adapter["health"], "host-healthy-execution-package-missing"
            )

        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=self.adapter_host_protocol_run,
        ) as host_run, mock.patch(
            "yeyu_gamer_manager.services.legacy_adapter.subprocess.Popen",
            side_effect=AssertionError("diagnostic canary must not start an Adapter"),
        ):
            response = self.client.post(
                "/api/v1/adapters/legacy-starrail/diagnostic-canary-requests",
                json={"note": "contract canary"},
                headers=self.mutation_headers("adapter-host-canary-route"),
            )
        self.assertEqual(host_run.call_count, 1)
        self.assertEqual(response.status_code, 202, response.text)
        canary = response.json()["result"]["adapterDiagnosticCanary"]
        document = canary["document"]
        self.assertEqual(canary["state"], "passed")
        self.assertEqual(document["supportedOperations"], ["probe", "canary", "execute"])

        self.assertTrue(document["managerOwned"])
        self.assertTrue(document["hostHealthy"])
        self.assertEqual(document["hostStatus"], "ready")
        self.assertTrue(document["diagnosticOnly"])
        self.assertFalse(document["executionReady"])
        self.assertEqual(document["executionPackage"]["status"], "missing")
        self.assertFalse(document["adapterProcessStarted"])
        self.assertFalse(document["gameProcessStarted"])
        self.assertFalse((self.settings.data_dir / "legacy-logs").exists())
        self.assertIsNone(self.client.app.state.manager.adapter.active_execution())

        canary_id = canary["resourceId"]
        persisted = self.client.get(
            f"/api/v1/adapter-diagnostic-canaries/{canary_id}"
        )
        self.assertEqual(persisted.status_code, 200, persisted.text)
        self.assertEqual(persisted.json()["document"]["canaryStatus"], "passed")

        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=self.adapter_host_protocol_run,
        ) as capability_host_run, mock.patch(
            "yeyu_gamer_manager.services.legacy_adapter.subprocess.Popen",
            side_effect=AssertionError("capability canary must not start an Adapter"),
        ):
            capability = self.client.post(
                "/api/v1/capability-invocations",
                json={
                    "capability": "adapter.canary.request",
                    "arguments": {"adapterId": "legacy-starrail"},
                },
                headers=self.mutation_headers("adapter-host-canary-capability"),
            )
        self.assertEqual(capability_host_run.call_count, 1)
        self.assertEqual(capability.status_code, 202, capability.text)
        invocation = capability.json()["result"]["invocation"]
        self.assertEqual(invocation["state"], "done")
        capability_canary = capability.json()["result"]["adapterDiagnosticCanary"]
        self.assertEqual(capability_canary["document"]["canaryStatus"], "passed")
        self.assertFalse(capability_canary["document"]["gameProcessStarted"])

        missing = self.client.post(
            "/api/v1/adapters/legacy-unknown/diagnostic-canary-requests",
            json={},
            headers=self.mutation_headers("adapter-host-canary-unknown"),
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_scoped_adapter_probe_rejects_unrelated_fallback_package(self) -> None:
        manager = self.client.app.state.manager
        manifest = mock.Mock(
            package_version="test-v1",
            entry_point=self.adapter_host_entrypoint,
            package_digest="sha256:test",
            supported_game_ids=("StarRail",),
            operation_bindings={"StarRail": {"attach-home": "attach-home"}},
        )
        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.verify_execution_package",
            return_value=manifest,
        ):
            package = manager.adapter_host._execution_package_probe("ZZZ")
        self.assertEqual(package["status"], "missing")
        self.assertEqual(package["supportedGameIds"], [])

    def test_adapter_host_hash_tamper_fails_closed_without_process(self) -> None:
        original_size = self.adapter_host_entrypoint.stat().st_size
        self.adapter_host_entrypoint.write_bytes(b"x" * original_size)
        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=AssertionError("untrusted Host must never be started"),
        ) as host_run:
            adapters = self.client.get("/api/v1/adapters").json()["items"]
            response = self.client.post(
                "/api/v1/adapters/legacy-starrail/diagnostic-canary-requests",
                json={"note": "tamper contract"},
                headers=self.mutation_headers("adapter-host-tamper"),
            )
        host_run.assert_not_called()
        self.assertTrue(all(not item["hostHealthy"] for item in adapters))
        self.assertTrue(
            all(item["hostStatus"] == "host_hash_mismatch" for item in adapters)
        )
        self.assertEqual(response.status_code, 202, response.text)
        canary = response.json()["result"]["adapterDiagnosticCanary"]
        self.assertEqual(canary["state"], "failed")
        self.assertEqual(
            canary["document"]["diagnosticCode"], "host_hash_mismatch"
        )
        self.assertFalse(canary["document"]["adapterProcessStarted"])
        self.assertFalse(canary["document"]["gameProcessStarted"])

    def test_adapter_host_extras_and_reparse_root_fail_closed(self) -> None:
        (self.adapter_host_root / "unexpected.dll").write_bytes(b"not allowed")
        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=AssertionError("Host package with extras must not start"),
        ) as host_run:
            adapters = self.client.get("/api/v1/adapters").json()["items"]
        host_run.assert_not_called()
        self.assertTrue(all(not item["hostHealthy"] for item in adapters))
        self.assertTrue(
            all(item["hostStatus"] == "host_package_extras" for item in adapters)
        )

        (self.adapter_host_root / "unexpected.dll").unlink()
        host_class = type(self.client.app.state.manager.adapter_host)
        original = host_class._is_reparse_point

        def simulated_reparse(path: Path) -> bool:
            if path == self.adapter_host_root:
                return True
            return original(path)

        with mock.patch.object(
            host_class, "_is_reparse_point", side_effect=simulated_reparse
        ), mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=AssertionError("reparse Host package must not start"),
        ) as host_run:
            adapters = self.client.get("/api/v1/adapters").json()["items"]
        host_run.assert_not_called()
        self.assertTrue(
            all(item["hostStatus"] == "unsafe_host_path" for item in adapters)
        )

    def test_config_updates_manager_only_and_never_legacy_json(self) -> None:
        response = self.client.patch(
            "/api/v1/config",
            json={"dailyScheduleEnabled": True, "dailyScheduleTime": "18:30"},
            headers=self.mutation_headers("config-update-1"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        config = self.client.get("/api/v1/config").json()["config"]
        self.assertTrue(config["daily_schedule_enabled"])
        self.assertEqual(config["daily_schedule_time"], "18:30")
        self.assertEqual(self.legacy_config_path.read_bytes(), self.original_config)
        self.assertEqual(self.legacy_policy_path.read_bytes(), self.original_policy)

        queue_shape = self.client.patch(
            "/api/v1/config",
            json={
                "order": ["StarRail"],
                "enabled": {"StarRail": True, "ZZZ": False},
            },
            headers=self.mutation_headers("config-queue-shape"),
        )
        self.assertEqual(queue_shape.status_code, 202, queue_shape.text)
        self.assertEqual(
            self.client.get("/api/v1/config").json()["config"]["order"],
            ["StarRail", "ZZZ"],
        )

        timeout = self.client.patch(
            "/api/v1/config",
            json={"stepTimeoutSeconds": 3600},
            headers=self.mutation_headers("config-step-timeout"),
        )
        self.assertEqual(timeout.status_code, 202, timeout.text)
        self.assertEqual(
            self.client.get("/api/v1/config").json()["config"]["step_timeout_seconds"],
            3600,
        )
        rejected_timeout = self.client.patch(
            "/api/v1/config",
            json={"stepTimeoutSeconds": 300},
            headers=self.mutation_headers("config-step-timeout-too-short"),
        )
        self.assertEqual(rejected_timeout.status_code, 422, rejected_timeout.text)

    def test_game_paths_are_manager_owned_and_allowlisted(self) -> None:
        executable = str(Path(sys.executable))
        tool_root = self.root / "March7thAssistant"
        tool_root.mkdir()
        response = self.client.patch(
            "/api/v1/config",
            json={
                "gamePaths": {
                    "StarRail": {
                        "gamePath": executable,
                        "toolPath": str(tool_root),
                    }
                }
            },
            headers=self.mutation_headers("config-game-paths"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        paths = self.client.get("/api/v1/config").json()["config"]["game_paths"]
        self.assertEqual(
            paths["StarRail"],
            {
                "game_path": executable,
                "tool_path": str(tool_root),
            },
        )
        self.assertEqual(self.legacy_config_path.read_bytes(), self.original_config)

        unknown = self.client.patch(
            "/api/v1/config",
            json={"gamePaths": {"Unknown": {"gamePath": "C:\\Games\\Unknown"}}},
            headers=self.mutation_headers("config-game-paths-unknown"),
        )
        self.assertEqual(unknown.status_code, 422, unknown.text)
        self.assertEqual(unknown.json()["code"], "manager_validation")

        unsafe = self.client.patch(
            "/api/v1/config",
            json={"gamePaths": {"StarRail": {"toolPath": "powershell.exe -Command whoami"}}},
            headers=self.mutation_headers("config-game-paths-unsafe"),
        )
        self.assertEqual(unsafe.status_code, 422, unsafe.text)

    def test_game_paths_reject_missing_executable_when_enabled(self) -> None:
        missing = self.client.patch(
            "/api/v1/config",
            json={
                "gamePaths": {
                    "StarRail": {"gamePath": "C:\\Games\\Missing\\StarRail.exe"}
                }
            },
            headers=self.mutation_headers("config-game-paths-missing-exe"),
        )
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual(missing.json()["code"], "manager_validation")
        self.assertIn("configured game executable was not found", missing.json()["detail"])

        missing_tool = self.client.patch(
            "/api/v1/config",
            json={
                "gamePaths": {
                    "StarRail": {
                        "gamePath": str(Path(sys.executable)),
                        "toolPath": r"C:\Tools\Missing\March7thAssistant",
                    }
                }
            },
            headers=self.mutation_headers("config-game-paths-missing-tool"),
        )
        self.assertEqual(missing_tool.status_code, 422, missing_tool.text)
        self.assertIn("automation tool was not found", missing_tool.json()["detail"])

    def test_daily_tool_profiles_are_typed_and_manager_owned(self) -> None:
        configured = self.client.patch(
            "/api/v1/config",
            json={
                "dailyToolProfiles": {
                    "pgrMfw": {"profileId": "c_nightrain_pgr_daily_73912"},
                    "okWw": {
                        "whichToFarm": "Simulation Challenge",
                        "materialSelection": "Weapon EXP",
                        "farmNightmareNestForDailyEcho": False,
                    },
                    "zzzOneDragon": {
                        "dailyAppIds": ["coffee", "random_play"],
                        "weeklyAppIds": ["ridu_weekly"],
                    },
                    "endfield": {
                        "staminaStage": "干员经验",
                        "rewardTier": "高阶",
                        "staminaRotationStartDate": "2026-08-29",
                        "staminaRotation": ["干员经验", "超距辉映管"],
                        "teamSlot": "3",
                    },
                    "nte": {
                        "anomalyTaskType": "异能升级材料",
                        "materialIndex": 4,
                        "staminaTarget": 240,
                        "coffeeMode": "完整自动化",
                    },
                    "czn": {
                        "staminaCategory": "潜能",
                        "staminaTarget": "正义",
                        "battleEfficiency": 5,
                        "untilExhausted": True,
                    },
                }
            },
            headers=self.mutation_headers("config-daily-tool-profiles"),
        )
        self.assertEqual(configured.status_code, 202, configured.text)
        profiles = self.client.get("/api/v1/config").json()["config"][
            "daily_tool_profiles"
        ]
        self.assertEqual(
            profiles["pgr_mfw"]["profile_id"],
            "c_nightrain_pgr_daily_73912",
        )
        self.assertEqual(profiles["ok_ww"]["which_to_farm"], "Simulation Challenge")
        self.assertNotIn("additional_tasks", profiles["ok_ww"])
        self.assertEqual(profiles["zzz_one_dragon"]["weekly_app_ids"], ["ridu_weekly"])
        self.assertEqual(profiles["endfield"]["stamina_stage"], "干员经验")
        self.assertEqual(profiles["endfield"]["stamina_rotation"], ["干员经验", "超距辉映管"])
        self.assertEqual(profiles["nte"]["coffee_mode"], "完整自动化")
        self.assertEqual(profiles["czn"]["stamina_target"], "正义")
        self.assertEqual(profiles["czn"]["battle_efficiency"], 5)
        self.assertEqual(self.legacy_config_path.read_bytes(), self.original_config)

        unsafe_ok_ww = self.client.patch(
            "/api/v1/config",
            json={
                "dailyToolProfiles": {
                    "okWw": {"additionalTasks": ["Check Weekly Garden"]}
                }
            },
            headers=self.mutation_headers("config-daily-tool-profiles-unsafe-ww"),
        )
        self.assertEqual(unsafe_ok_ww.status_code, 422, unsafe_ok_ww.text)

        overlap = self.client.patch(
            "/api/v1/config",
            json={
                "dailyToolProfiles": {
                    "zzzOneDragon": {
                        "dailyAppIds": ["coffee"],
                        "weeklyAppIds": ["coffee"],
                    }
                }
            },
            headers=self.mutation_headers("config-daily-tool-profiles-overlap"),
        )
        self.assertEqual(overlap.status_code, 422, overlap.text)

    def test_daily_todo_selection_is_typed_manager_config(self) -> None:
        definitions = self.client.get(
            "/api/v1/todo-definitions?gameId=StarRail&cadence=daily"
        ).json()["items"]
        required_definition_ids = [
            str(item["todoDefinitionId"])
            for item in definitions
            if item["required"]
        ]
        self.assertGreaterEqual(len(required_definition_ids), 2)

        configured = self.client.patch(
            "/api/v1/config",
            json={
                "dailyTodoSelection": {
                    "StarRail": [required_definition_ids[0]],
                }
            },
            headers=self.mutation_headers("config-daily-todo-selection"),
        )
        self.assertEqual(configured.status_code, 202, configured.text)
        selection = self.client.get("/api/v1/config").json()["config"][
            "daily_todo_selection"
        ]
        self.assertEqual(selection["StarRail"], [required_definition_ids[0]])
        self.assertEqual(self.legacy_config_path.read_bytes(), self.original_config)

        planned = self.client.post(
            "/api/v1/batches",
            json={
                "cadence": "daily",
                "gameIds": ["StarRail"],
                "mode": "plan",
                "requestedBy": "contract-test",
            },
            headers=self.mutation_headers("plan-selected-daily-todo"),
        )
        self.assertEqual(planned.status_code, 202, planned.text)
        todo_plan = planned.json()["result"]["todoPlans"]["StarRail"]
        self.assertEqual(
            todo_plan["selectedTodoDefinitionIds"], [required_definition_ids[0]]
        )
        self.assertEqual(len(todo_plan["completionTodoInstanceIds"]), 1)

        invalid = self.client.patch(
            "/api/v1/config",
            json={"dailyTodoSelection": {"ZZZ": [required_definition_ids[0]]}},
            headers=self.mutation_headers("config-daily-todo-selection-invalid"),
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()["code"], "manager_validation")

    def test_integrations_expose_real_registered_stage_mappings(self) -> None:
        response = self.client.get("/api/v1/integrations")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        by_game = {item["gameId"]: item for item in items}
        self.assertEqual(
            [item["gameId"] for item in items[:2]], ["StarRail", "ZZZ"]
        )
        self.assertEqual(by_game["StarRail"]["mappingStatus"], "registered")
        self.assertTrue(by_game["StarRail"]["operations"])
        self.assertIn(
            "daily-training-objectives",
            {item["operation"] for item in by_game["StarRail"]["operations"]},
        )
        for game_id in ("WW", "Endfield", "GF2", "NTE", "PGR", "ZZZ", "NIKKE"):
            registration = registration_for(game_id)
            self.assertIsNotNone(registration)
            self.assertTrue(registration.daily_operations)
        self.assertIn(
            "battle-pass-free-track", registration_for("GF2").daily_operations
        )
        self.assertIn(
            "battle-pass-free-track", registration_for("PGR").daily_operations
        )
        for game_id in ("WW", "Endfield", "GF2", "NTE", "PGR", "ZZZ", "NIKKE"):
            self.assertEqual(by_game[game_id]["mappingStatus"], "registered")

    def test_state_version_compare_and_swap_rejects_stale_mutation(self) -> None:
        base_version = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        headers = {
            **self.actor_headers(),
            "Idempotency-Key": "cas-plan-1",
            "If-Match": f'"{base_version}"',
            "X-Expected-State-Version": str(base_version),
        }
        first = self.client.post(
            "/api/v1/game-runs",
            json={"gameId": "StarRail", "mode": "plan"},
            headers=headers,
        )
        self.assertEqual(first.status_code, 202, first.text)

        # A lost response can replay the original idempotency key even after
        # state advanced; a different command based on the stale version cannot.
        replay = self.client.post(
            "/api/v1/game-runs",
            json={"gameId": "StarRail", "mode": "plan"},
            headers=headers,
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])

        stale = self.client.patch(
            "/api/v1/config",
            json={"dailyScheduleTime": "18:45"},
            headers={
                **self.actor_headers(),
                "Idempotency-Key": "cas-config-stale",
                "If-Match": str(base_version),
            },
        )
        self.assertEqual(stale.status_code, 412, stale.text)
        self.assertEqual(stale.json()["code"], "state_version_conflict")
        self.assertGreater(
            stale.json()["errors"]["currentStateVersion"], base_version
        )

        disagree = self.client.patch(
            "/api/v1/config",
            json={"dailyScheduleTime": "19:00"},
            headers={
                **self.actor_headers(),
                "Idempotency-Key": "cas-config-disagree",
                "If-Match": "1",
                "X-Expected-State-Version": "2",
            },
        )
        self.assertEqual(disagree.status_code, 422, disagree.text)

    def test_agent_claim_decision_and_typed_capability(self) -> None:
        artifact_id = self.create_diagnostic_artifact("agent-evidence-bundle")
        starrail_todos = self.client.get(
            "/api/v1/todo-instances",
            params={"gameId": "StarRail", "cadence": "daily", "current": "true"},
        ).json()["items"]
        self.assertTrue(starrail_todos)
        diagnosed_todo_id = starrail_todos[0]["todoInstanceId"]
        second_diagnosed_todo_id = starrail_todos[1]["todoInstanceId"]
        work = self.client.post(
            "/api/v1/agent/work-items",
            json={
                "kind": "diagnose_game",
                "gameId": "StarRail",
                "artifactRefs": [artifact_id],
            },
            headers=self.mutation_headers("work-item-1", actor="agent"),
        )
        self.assertEqual(work.status_code, 202, work.text)
        work_id = work.json()["result"]["workItem"]["workItemId"]
        claim = self.client.post(
            f"/api/v1/agent/work-items/{work_id}/claims",
            json={"claimant": "agent-test", "leaseSeconds": 120},
            headers=self.mutation_headers("claim-1", actor="agent"),
        )
        self.assertEqual(claim.status_code, 202, claim.text)
        claim_id = claim.json()["result"]["claim"]["claimId"]
        fencing_token = claim.json()["result"]["claim"]["fencingToken"]

        capability = self.client.post(
            "/api/v1/capability-invocations",
            json={
                "capability": "game.daily.plan",
                "arguments": {"gameId": "StarRail"},
                "requestedBy": "agent-test",
                "workItemId": work_id,
                "claimId": claim_id,
                "fencingToken": fencing_token,
            },
            headers=self.mutation_headers("capability-1", actor="agent"),
        )
        self.assertEqual(capability.status_code, 202, capability.text)
        self.assertIn("gameRun", capability.json()["result"])

        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.run",
            side_effect=self.adapter_host_protocol_run,
        ) as host_run:
            canary = self.client.post(
                "/api/v1/capability-invocations",
                json={
                    "capability": "adapter.canary.request",
                    "arguments": {"adapterId": "legacy-starrail"},
                    "workItemId": work_id,
                    "claimId": claim_id,
                    "fencingToken": fencing_token,
                },
                headers=self.mutation_headers("capability-agent-canary", actor="agent"),
            )
        self.assertEqual(canary.status_code, 202, canary.text)
        self.assertEqual(host_run.call_count, 1)
        self.assertEqual(
            canary.json()["result"]["adapterDiagnosticCanary"]["state"], "passed"
        )

        wrong_adapter_scope = self.client.post(
            "/api/v1/capability-invocations",
            json={
                "capability": "adapter.canary.request",
                "arguments": {"adapterId": "legacy-zzz"},
                "workItemId": work_id,
                "claimId": claim_id,
                "fencingToken": fencing_token,
            },
            headers=self.mutation_headers(
                "capability-agent-canary-wrong-scope", actor="agent"
            ),
        )
        self.assertEqual(wrong_adapter_scope.status_code, 409, wrong_adapter_scope.text)

        wrong_scope = self.client.post(
            "/api/v1/capability-invocations",
            json={
                "capability": "game.daily.plan",
                "arguments": {"gameId": "ZZZ"},
                "requestedBy": "agent-test",
                "workItemId": work_id,
                "claimId": claim_id,
                "fencingToken": fencing_token,
            },
            headers=self.mutation_headers("capability-wrong-scope", actor="agent"),
        )
        self.assertEqual(wrong_scope.status_code, 409, wrong_scope.text)

        wrong_actor = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "fencingToken": fencing_token,
                "decision": "review_required",
                "reason": "must not accept another actor",
                "requestedBy": "agent-impostor",
            },
            headers=self.mutation_headers("decision-wrong-actor", actor="cli"),
        )
        self.assertEqual(wrong_actor.status_code, 409, wrong_actor.text)

        decision = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "fencingToken": fencing_token,
                "decision": "review_required",
                "reason": "evidence is not sufficient",
                "evidenceIds": [artifact_id],
                "requestedBy": "agent-test",
                "todoDiagnoses": [
                    {
                        "todoInstanceId": diagnosed_todo_id,
                        "difficulty": "hard",
                        "automatable": False,
                        "confidence": 0.86,
                        "basis": [
                            "two attempts stopped at the same visual stage",
                            "the current work-item artifact still lacks a completion marker",
                        ],
                        "failureStage": "daily-reward-verification",
                        "issue": "completion marker is not stable",
                        "recommendation": "add a stable five-reward-box verifier",
                    },
                    {
                        "todoInstanceId": second_diagnosed_todo_id,
                        "difficulty": "moderate",
                        "automatable": True,
                        "confidence": 0.72,
                        "basis": ["the operation is bound but needs a stronger verifier"],
                        "failureStage": "post-action-observation",
                        "issue": "post-action evidence is weak",
                        "recommendation": "capture a fresh post-action frame",
                    },
                ],
            },
            headers=self.mutation_headers("decision-1", actor="agent"),
        )
        self.assertEqual(decision.status_code, 202, decision.text)
        self.assertEqual(len(decision.json()["result"]["automationAssessments"]), 2)
        assessment = decision.json()["result"]["automationAssessment"]
        self.assertEqual(assessment["todoInstanceId"], diagnosed_todo_id)
        self.assertEqual(assessment["difficulty"], "hard")
        self.assertIs(assessment["automatable"], False)
        self.assertEqual(assessment["issue"], "completion marker is not stable")
        self.assertEqual(assessment["evidenceIds"], [])
        assessment_id = assessment["assessmentId"]
        assessment_read = self.client.get(
            f"/api/v1/automation-assessments/{assessment_id}"
        )
        self.assertEqual(assessment_read.status_code, 200, assessment_read.text)
        assessment_list = self.client.get(
            "/api/v1/automation-assessments",
            params={"todoInstanceId": diagnosed_todo_id},
        )
        self.assertEqual(assessment_list.status_code, 200, assessment_list.text)
        self.assertEqual(assessment_list.json()["total"], 1)
        work_after = self.client.get(f"/api/v1/agent/work-items/{work_id}").json()
        self.assertEqual(work_after["state"], "review_required")
        self.assertEqual(
            work_after["result"]["automationAssessmentId"], assessment_id
        )
        self.assertEqual(
            len(work_after["result"]["automationAssessmentIds"]), 2
        )

        unsafe = self.client.post(
            "/api/v1/capability-invocations",
            json={
                "capability": "game.daily.plan",
                "arguments": {"gameId": "StarRail", "shellCommand": "whoami"},
                "requestedBy": "agent-test",
                "workItemId": work_id,
                "claimId": claim_id,
                "fencingToken": fencing_token,
            },
            headers=self.mutation_headers("capability-unsafe", actor="agent"),
        )
        self.assertEqual(unsafe.status_code, 422)
        self.assertIn("shell/path/click", unsafe.json()["detail"])

    def test_manager_captures_registered_game_window_as_opaque_artifact(self) -> None:
        manager = self.client.app.state.manager
        todo = manager.list_todo_instances(
            game_id="StarRail", cadence="daily", current=True, limit=1
        )[0]
        run = manager.store.create_game_run(
            {
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "running",
                "mode": "execute",
                "requested_by": "test",
                "todo_instance_ids": [todo.todo_instance_id],
                "completion_todo_instance_ids": [todo.todo_instance_id],
            }
        )
        run_attempt_id = str(uuid.uuid4())
        manager.store.create_run_attempt(
            {
                "run_attempt_id": run_attempt_id,
                "run_id": run["run_id"],
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "starting",
                "fencing_token_hash": "sha256:" + "1" * 64,
                "cancel_authority_hash": "sha256:" + "2" * 64,
                "plan": {
                    "executableTodoInstanceIds": [todo.todo_instance_id],
                    "todos": [{"todoInstanceId": todo.todo_instance_id}],
                },
            }
        )
        png = _encode_bgra_png(2, 2, bytes.fromhex("0000ff00" * 4))
        capture = CapturedWindow(
            content=png,
            hwnd=123,
            pid=456,
            process_name="starrail.exe",
            title="崩坏：星穹铁道",
            width=2,
            height=2,
            method="PrintWindow",
        )
        with mock.patch.object(manager.window_capture, "capture", return_value=capture):
            response = self.client.post(
                "/api/v1/capability-invocations",
                json={
                    "capability": "observation.capture.request",
                    "arguments": {"gameId": "StarRail", "runId": run["run_id"]},
                    "requestedBy": "webgui",
                },
                headers=self.mutation_headers("capture-window"),
            )
        self.assertEqual(response.status_code, 202, response.text)
        artifact = response.json()["result"]["artifact"]
        self.assertEqual(artifact["kind"], "window-screenshot")
        self.assertEqual(artifact["runId"], run["run_id"])
        self.assertEqual(artifact["runAttemptId"], run_attempt_id)
        self.assertNotIn("relativePath", artifact)
        content = self.client.get(
            f"/api/v1/artifacts/{artifact['artifactId']}/content"
        )
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(content.content, png)
        detail = self.client.get("/api/v1/games/StarRail").json()
        self.assertEqual(detail["artifacts"][0]["artifactId"], artifact["artifactId"])

    def test_actor_credentials_browser_csrf_and_dispatch_boundaries(self) -> None:
        state_version = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        unauthenticated = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily"},
            headers={
                "Idempotency-Key": "unauthenticated-write",
                "If-Match": str(state_version),
            },
        )
        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)

        mismatch = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily"},
            headers={
                **self.actor_headers("cli"),
                "X-YeYu-Gamer-Actor": "agent",
                "Idempotency-Key": "mismatched-actor",
                "If-Match": str(state_version),
            },
        )
        self.assertEqual(mismatch.status_code, 403, mismatch.text)
        self.assertEqual(mismatch.json()["code"], "actor_identity_mismatch")

        dispatched = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "StarRail"},
            headers=self.mutation_headers("rabiroute-dispatch", actor="rabiroute"),
        )
        self.assertEqual(dispatched.status_code, 202, dispatched.text)
        work_id = dispatched.json()["result"]["workItem"]["workItemId"]
        forbidden_claim = self.client.post(
            f"/api/v1/agent/work-items/{work_id}/claims",
            json={"leaseSeconds": 120},
            headers=self.mutation_headers("rabiroute-cannot-claim", actor="rabiroute"),
        )
        self.assertEqual(forbidden_claim.status_code, 403, forbidden_claim.text)

        self.client.cookies.clear()
        anonymous_root = self.client.get("/")
        self.assertEqual(anonymous_root.status_code, 200)
        self.assertNotIn("yeyu_session", anonymous_root.headers.get("set-cookie", ""))
        self.assertNotIn("yeyu_csrf", anonymous_root.headers.get("set-cookie", ""))
        for public_path in (
            "/api/v1/meta",
            "/api/v1/health",
            "/api/v1/openapi.json",
        ):
            public_response = self.client.get(public_path)
            self.assertEqual(public_response.status_code, 200, public_response.text)
            self.assertNotIn(
                "yeyu_session", public_response.headers.get("set-cookie", "")
            )
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 401)

        recovered_local_session = self.client.post(
            "/api/v1/webgui/local-sessions",
            json={},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(recovered_local_session.status_code, 200, recovered_local_session.text)
        self.assertEqual(
            recovered_local_session.json(), {"authenticated": True, "actor": "webgui"}
        )
        self.assertTrue(self.client.cookies.get("yeyu_session"))
        self.assertTrue(self.client.cookies.get("yeyu_csrf"))
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 200)
        self.client.cookies.clear()

        self.client.cookies.set("yeyu_session", "forged-session")
        self.client.cookies.set("yeyu_csrf", "echoed-csrf")
        forged_browser = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily", "mode": "plan"},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": "echoed-csrf",
                "Idempotency-Key": "forged-browser",
                "If-Match": str(state_version),
            },
        )
        self.assertEqual(forged_browser.status_code, 403, forged_browser.text)
        self.assertEqual(forged_browser.json()["code"], "csrf_validation_failed")
        self.client.cookies.clear()

        for actor in ("agent", "rabiroute", "cli", "tray-lifecycle"):
            forbidden_nonce = self.client.post(
                "/api/v1/webgui/bootstrap-nonces",
                json={},
                headers=self.actor_headers(actor),
            )
            self.assertEqual(forbidden_nonce.status_code, 403, forbidden_nonce.text)

        issued = self.client.post(
            "/api/v1/webgui/bootstrap-nonces",
            json={},
            headers=self.actor_headers("tray"),
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        nonce = issued.json()["nonce"]
        persisted_bytes = b"".join(
            path.read_bytes()
            for path in self.settings.data_dir.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(str(nonce).encode("ascii"), persisted_bytes)
        self.assertNotIn(
            str(self.settings.tray_bootstrap_secret).encode("ascii"),
            persisted_bytes,
        )
        agent_scope_exchange = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": nonce},
            headers={
                **self.actor_headers("agent"),
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(agent_scope_exchange.status_code, 403, agent_scope_exchange.text)
        self.assertEqual(
            agent_scope_exchange.json()["code"],
            "webgui_bootstrap_context_rejected",
        )
        hostile_exchange = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": nonce},
            headers={
                "Origin": "http://evil.invalid",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": "echoed-csrf",
            },
        )
        self.assertEqual(hostile_exchange.status_code, 403, hostile_exchange.text)

        exchanged = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": nonce},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        csrf = self.client.cookies.get("yeyu_csrf")
        self.assertTrue(csrf)
        reused = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": nonce},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(reused.status_code, 401, reused.text)

        refreshed = self.client.get("/")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 200)
        browser_version = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        browser_write = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily", "mode": "plan", "gameIds": ["StarRail"]},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": str(csrf),
                "Idempotency-Key": "browser-csrf-plan",
                "If-Match": str(browser_version),
            },
        )
        self.assertEqual(browser_write.status_code, 202, browser_write.text)
        self.assertEqual(
            browser_write.json()["result"]["batch"]["requestedBy"], "webgui"
        )

        hostile_origin = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily"},
            headers={
                "Origin": "http://evil.invalid",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": str(csrf),
                "Idempotency-Key": "hostile-origin",
                "If-Match": str(
                    self.client.get("/api/v1/snapshot").json()["stateVersion"]
                ),
            },
        )
        self.assertEqual(hostile_origin.status_code, 403, hostile_origin.text)
        self.assertEqual(hostile_origin.json()["code"], "cross_origin_write")

    def test_webgui_bootstrap_ttl_one_shot_and_restart_invalidation(self) -> None:
        now = [100.0]
        broker = WebGuiSessionBroker(clock=lambda: now[0])
        expired_nonce, ttl = broker.issue_nonce()
        self.assertEqual(ttl, 60)
        now[0] += 61
        self.assertIsNone(broker.exchange_nonce(expired_nonce))

        live_nonce, _ = broker.issue_nonce()
        session = broker.exchange_nonce(live_nonce)
        self.assertIsNotNone(session)
        self.assertIsNone(broker.exchange_nonce(live_nonce))
        self.assertIsNotNone(broker.session(session.session_token if session else None))
        restarted = WebGuiSessionBroker(clock=lambda: now[0])
        self.assertIsNone(restarted.session(session.session_token if session else None))

    def test_webgui_session_idle_absolute_ttl_and_revocation(self) -> None:
        now = [100.0]
        broker = WebGuiSessionBroker(
            session_absolute_ttl_seconds=30,
            session_idle_ttl_seconds=10,
            clock=lambda: now[0],
        )
        nonce, _ = broker.issue_nonce()
        session = broker.exchange_nonce(nonce)
        self.assertIsNotNone(session)
        session_token = session.session_token if session else None
        now[0] += 9
        self.assertIsNotNone(broker.session(session_token))
        now[0] += 9
        self.assertIsNotNone(broker.session(session_token, touch=False))
        now[0] += 2
        self.assertIsNone(broker.session(session_token))

        nonce, _ = broker.issue_nonce()
        absolute = broker.exchange_nonce(nonce)
        self.assertIsNotNone(absolute)
        absolute_token = absolute.session_token if absolute else None
        now[0] += 9
        self.assertIsNotNone(broker.session(absolute_token))
        now[0] += 9
        self.assertIsNotNone(broker.session(absolute_token))
        now[0] += 13
        self.assertIsNone(broker.session(absolute_token))

        nonce, _ = broker.issue_nonce()
        revoked = broker.exchange_nonce(nonce)
        self.assertIsNotNone(revoked)
        revoked_token = revoked.session_token if revoked else None
        self.assertTrue(broker.revoke_session(revoked_token))
        self.assertFalse(broker.revoke_session(revoked_token))
        self.assertIsNone(broker.session(revoked_token))

    def test_expired_webgui_session_is_rejected_and_logout_revokes(self) -> None:
        now = [100.0]
        self.client.app.state.web_sessions = WebGuiSessionBroker(
            session_absolute_ttl_seconds=20,
            session_idle_ttl_seconds=10,
            clock=lambda: now[0],
        )
        self.client.cookies.clear()
        self.bootstrap_webgui_session()
        expired_session = str(self.client.cookies.get("yeyu_session"))
        expired_csrf = str(self.client.cookies.get("yeyu_csrf"))
        now[0] += 11
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 401)
        expired_write = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily", "mode": "plan"},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": expired_csrf,
                "Idempotency-Key": "expired-browser-session",
                "If-Match": "0",
            },
        )
        self.assertEqual(expired_write.status_code, 403, expired_write.text)

        self.client.cookies.clear()
        self.bootstrap_webgui_session()
        live_session = str(self.client.cookies.get("yeyu_session"))
        live_csrf = str(self.client.cookies.get("yeyu_csrf"))
        logout = self.client.post(
            "/api/v1/webgui/session-revocations",
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": live_csrf,
            },
        )
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertEqual(logout.json(), {"revoked": True})
        self.assertIsNone(self.client.cookies.get("yeyu_session"))
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 401)
        self.client.cookies.set("yeyu_session", live_session)
        self.client.cookies.set("yeyu_csrf", live_csrf)
        revoked_write = self.client.post(
            "/api/v1/batches",
            json={"kind": "daily", "mode": "plan"},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": live_csrf,
                "Idempotency-Key": "revoked-browser-session",
                "If-Match": "0",
            },
        )
        self.assertEqual(revoked_write.status_code, 403, revoked_write.text)
        self.assertNotEqual(expired_session, live_session)

    def test_manager_owned_incident_evidence_repair_and_governance_flows(self) -> None:
        planned = self.client.post(
            "/api/v1/game-runs",
            json={"gameId": "StarRail", "mode": "plan"},
            headers=self.mutation_headers("domain-plan-run"),
        )
        self.assertEqual(planned.status_code, 202, planned.text)
        run_id = planned.json()["result"]["gameRun"]["runId"]

        takeover = self.client.post(
            f"/api/v1/game-runs/{run_id}/takeover-requests",
            json={"reason": "operator verification"},
            headers=self.mutation_headers("domain-takeover"),
        )
        self.assertEqual(takeover.status_code, 202, takeover.text)
        self.assertEqual(takeover.json()["result"]["gameRun"]["state"], "human_required")
        released = self.client.post(
            f"/api/v1/game-runs/{run_id}/takeover-release-requests",
            json={"reason": "fresh observation required"},
            headers=self.mutation_headers("domain-release-takeover"),
        )
        self.assertEqual(released.status_code, 202, released.text)
        self.assertEqual(
            released.json()["result"]["gameRun"]["state"], "review_required"
        )
        resume = self.client.post(
            f"/api/v1/game-runs/{run_id}/resume-requests",
            json={"reason": "must remain disabled"},
            headers=self.mutation_headers("domain-resume-disabled"),
        )
        self.assertEqual(resume.status_code, 202, resume.text)
        self.assertFalse(
            resume.json()["result"]["todoPlan"]["executableTodoInstanceIds"]
        )
        self.assertTrue(
            resume.json()["result"]["todoPlan"]["unresolvedRequiredTodoIds"]
        )
        self.assertEqual(
            resume.json()["result"]["gameRun"]["runId"], run_id
        )

        diagnostic = self.client.post(
            "/api/v1/diagnostic-bundles",
            json={"runId": run_id},
            headers=self.mutation_headers("domain-diagnostic"),
        )
        self.assertEqual(diagnostic.status_code, 202, diagnostic.text)
        artifact_id = diagnostic.json()["result"]["artifact"]["artifactId"]
        artifact = self.client.get(f"/api/v1/artifacts/{artifact_id}")
        self.assertEqual(artifact.status_code, 200, artifact.text)
        self.assertEqual(artifact.json()["runId"], run_id)
        self.client.cookies.clear()
        anonymous_content = self.client.get(
            f"/api/v1/artifacts/{artifact_id}/content"
        )
        self.assertEqual(anonymous_content.status_code, 401, anonymous_content.text)
        agent_content = self.client.get(
            f"/api/v1/artifacts/{artifact_id}/content",
            headers=self.actor_headers("agent"),
        )
        self.assertEqual(agent_content.status_code, 403, agent_content.text)
        self.assertIn("claim", agent_content.json()["detail"])
        self.bootstrap_webgui_session()
        content = self.client.get(f"/api/v1/artifacts/{artifact_id}/content")
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(content.json()["schemaVersion"], 1)

        evidence_review = self.client.post(
            "/api/v1/evidence-reviews",
            json={"artifactId": artifact_id, "verdict": "accepted"},
            headers=self.mutation_headers("domain-evidence-review"),
        )
        self.assertEqual(evidence_review.status_code, 202, evidence_review.text)
        self.assertEqual(
            evidence_review.json()["result"]["artifact"]["verdict"], "accepted"
        )

        manager = self.client.app.state.manager
        failed = manager.store.create_game_run(
            {
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "failed",
                "mode": "plan",
                "requested_by": "contract-test",
                "message": "simulated adapter failure",
            }
        )
        incident_id = f"run-{failed['run_id']}"
        repair = self.client.post(
            f"/api/v1/incidents/{incident_id}/repair-sessions",
            json={"reason": "isolate the failing adapter"},
            headers=self.mutation_headers("domain-repair"),
        )
        self.assertEqual(repair.status_code, 202, repair.text)
        repair_id = repair.json()["result"]["repairSession"]["resourceId"]
        verification = self.client.post(
            f"/api/v1/repair-sessions/{repair_id}/verification-requests",
            json={
                "verdict": "passed",
                "note": "fixture replay passed",
                "evidenceIds": [artifact_id],
            },
            headers=self.mutation_headers("domain-repair-verify"),
        )
        self.assertEqual(verification.status_code, 202, verification.text)
        self.assertEqual(
            verification.json()["result"]["repairSession"]["state"], "verified"
        )

        adapter = self.client.get("/api/v1/adapters").json()["items"][0]
        if adapter["activeVersion"] is None:
            self.assertEqual(adapter["stage"], "compatibility")
            self.assertIsNone(adapter["activeVersionId"])
            self.assertIsNone(adapter["rollbackVersionId"])
        else:
            rollback = self.client.post(
                f"/api/v1/adapter-versions/{adapter['activeVersion']}/rollback-requests",
                json={
                    "adapterId": adapter["adapterId"],
                    "targetStage": "compatibility",
                    "reason": "contract verification only",
                },
                headers=self.mutation_headers("domain-adapter-rollback"),
            )
            self.assertEqual(rollback.status_code, 202, rollback.text)
            self.assertFalse(
                rollback.json()["result"]["governanceRequest"]["document"]["hotPromotion"]
            )

        capabilities = self.client.get("/api/v1/capabilities").json()["items"]
        self.assertGreaterEqual(len(capabilities), 20)
        for capability in capabilities:
            self.assertEqual(len(capability["implementationHash"]), 64)
            self.assertTrue(capability["outputSchema"])
            self.assertTrue(capability["policy"]["managerOnly"])
            if capability["enabled"]:
                self.assertEqual(
                    capability["policy"]["implementationStatus"], "implemented"
                )
        disabled_unbound = {
            "run.takeover.request",
            "run.takeover.release",
            "evidence.review.submit",
            "incident.repair.create",
            "repair.verification.request",
            "adapter.replay.request",
            "adapter.shadow.request",
            "adapter.promotion.request",
            "adapter.rollback.request",
            "diagnostics.bundle.create",
            "config.validate",
            "notification.draft",
        }
        capability_by_id = {
            item["capabilityId"]: item for item in capabilities
        }
        self.assertTrue(
            all(not capability_by_id[item]["enabled"] for item in disabled_unbound)
        )
        self.assertEqual(
            self.client.app.state.manager.store.list_resources("capability-request"),
            [],
        )
        weekly = self.client.get("/api/v1/weekly").json()["items"]
        self.assertTrue(weekly)
        self.assertTrue(all(item["capabilityRef"] == "game.weekly.plan@1.0" for item in weekly))
        disabled_weekly = self.client.post(
            "/api/v1/capability-invocations",
            json={
                "capability": "game.weekly.run",
                "arguments": {
                    "gameId": weekly[0]["gameId"],
                    "weeklyId": weekly[0]["weeklyId"],
                },
            },
            headers=self.mutation_headers("domain-weekly-disabled"),
        )
        self.assertEqual(disabled_weekly.status_code, 409, disabled_weekly.text)

        game_detail = self.client.get("/api/v1/games/StarRail").json()
        self.assertEqual(
            game_detail["managerOwnedFields"],
            [
                "attempts",
                "runAttempts",
                "todoAttempts",
                "attemptAnalysis",
                "artifacts",
                "todoInstances",
            ],
        )
        self.assertEqual(
            game_detail["unavailableFields"]["checkpoints"]["status"],
            "disabled-unimplemented",
        )

    def test_lifecycle_and_compatibility_contracts(self) -> None:
        batch = self.client.post(
            "/api/v1/batches",
            json={
                "kind": "daily",
                "requestedBy": "tray",
                "gameIds": ["StarRail"],
            },
            headers=self.mutation_headers("platform-batch-plan"),
        )
        self.assertEqual(batch.status_code, 202, batch.text)
        batch_id = batch.json()["result"]["batch"]["batchId"]
        cancelled = self.client.post(
            f"/api/v1/batches/{batch_id}/cancel-requests",
            json={"requestedBy": "tray", "reason": "contract-test"},
            headers=self.mutation_headers("platform-batch-cancel"),
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.text)

        response = self.client.post(
            "/api/v1/manager/stop-requests",
            json={"requestedBy": "tray", "reason": "test"},
            headers=self.mutation_headers("stop-1"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.lifecycle_actions, ["stop"])
        for path in ["/health", "/status", "/logs", "/events"]:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_event_stream_closes_when_lifecycle_shutdown_begins(self) -> None:
        manager = self.client.app.state.manager
        cursor = manager.store.latest_event_sequence()

        async def exercise() -> None:
            stream = manager.event_stream(cursor)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)
            manager.invoke_lifecycle_callback("stop")
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(pending, timeout=2.0)
            await stream.aclose()

        asyncio.run(exercise())
        self.assertEqual(self.lifecycle_actions, ["stop"])

    def test_fencing_grant_is_direct_only_and_reads_are_principal_scoped(self) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/health").status_code, 200)
        legacy_agent_token = (
            self.settings.actor_tokens_dir / "agent.token"
        ).read_text(encoding="ascii").strip()
        legacy_agent_headers = {
            "Authorization": f"Bearer {legacy_agent_token}",
            "X-YeYu-Gamer-Actor": "agent",
        }
        legacy_snapshot = self.client.get(
            "/api/v1/snapshot", headers=legacy_agent_headers
        )
        self.assertEqual(legacy_snapshot.status_code, 200, legacy_snapshot.text)
        legacy_mutation = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "StarRail"},
            headers={
                **legacy_agent_headers,
                "Idempotency-Key": "legacy-shared-agent-write",
                "If-Match": str(legacy_snapshot.json()["stateVersion"]),
            },
        )
        self.assertEqual(legacy_mutation.status_code, 403, legacy_mutation.text)
        self.assertIn("actor scope", legacy_mutation.json()["detail"])
        for path in ("/status", "/logs", "/events"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.get("/api/v1/events/stream").status_code, 401)

        lifecycle = self.actor_headers("tray-lifecycle")
        self.assertEqual(
            self.client.get("/api/v1/snapshot", headers=lifecycle).json()["games"],
            [],
        )
        for path in ("/status", "/api/v1/logs", "/api/v1/events"):
            self.assertEqual(
                self.client.get(path, headers=lifecycle).status_code, 403, path
            )
        self.assertEqual(
            self.client.get(
                "/api/v1/diagnostics", headers=self.worker_headers("alpha")
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/events/stream", headers=self.worker_headers("alpha")
            ).status_code,
            403,
        )

        dispatched = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "StarRail"},
            headers=self.mutation_headers("worker-dispatch", actor="rabiroute"),
        )
        self.assertEqual(dispatched.status_code, 202, dispatched.text)
        dispatch_projection = dispatched.json()["result"]["workItem"]
        self.assertNotIn("result", dispatch_projection)
        self.assertNotIn("artifactRefs", dispatch_projection)
        work_item_id = str(dispatch_projection["workItemId"])

        manager = self.client.app.state.manager
        # This contract test deliberately bypasses the store API below to prove
        # that SQLite triggers reject copied fencing material.  Quiesce the
        # Manager-owned workers first: a raw connection write can otherwise
        # enter a SQLite guard callback while a worker holds the store lock and
        # waits on that same connection, deadlocking only the test harness.
        manager.stop_todo_reset_scheduler()
        manager.stop_notification_worker()
        cursor = manager.store.latest_event_sequence()
        claimed = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=self.worker_mutation_headers("alpha", "worker-claim"),
        )
        self.assertEqual(claimed.status_code, 202, claimed.text)
        claim = claimed.json()["result"]["claim"]
        claim_id = str(claim["claimId"])
        fencing_token = str(claim["fencingToken"])
        self.assertGreaterEqual(len(fencing_token), 32)

        for path, headers in (
            ("/health", {}),
            ("/api/v1/claims", self.worker_headers("alpha")),
        ):
            copied_get_headers = {
                **headers,
                "X-Request-Id": f"get-trace::{fencing_token}",
            }
            copied_get = self.client.get(path, headers=copied_get_headers)
            self.assertEqual(copied_get.status_code, 422, copied_get.text)
            self.assertNotIn(fencing_token, copied_get.text)
            self.assertNotIn(
                fencing_token,
                "\n".join(f"{key}:{value}" for key, value in copied_get.headers.items()),
            )
            self.assertNotEqual(
                copied_get.headers.get("x-request-id"),
                copied_get_headers["X-Request-Id"],
            )

        for target in (
            f"/health?trace={fencing_token}",
            f"/api/v1/agent/work-items/{fencing_token}",
        ):
            copied_url = self.client.get(target, headers=self.worker_headers("alpha"))
            self.assertEqual(copied_url.status_code, 422, copied_url.text)
            self.assertNotIn(fencing_token, copied_url.text)
            self.assertNotIn(
                fencing_token,
                "\n".join(f"{key}:{value}" for key, value in copied_url.headers.items()),
            )

        copied_validation_input = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": f"invalid::{fencing_token}"},
            headers=self.worker_mutation_headers(
                "alpha", "worker-validation-secret-copy"
            ),
        )
        self.assertEqual(
            copied_validation_input.status_code, 422, copied_validation_input.text
        )
        self.assertNotIn(fencing_token, copied_validation_input.text)
        self.assertNotIn(
            fencing_token,
            "\n".join(
                f"{key}:{value}" for key, value in copied_validation_input.headers.items()
            ),
        )

        conflict_headers = self.worker_mutation_headers(
            "beta", "worker-conflict-before-public-sink"
        )
        conflict_headers["X-Request-Id"] = f"conflict-{fencing_token}"
        conflict_before_sink = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=conflict_headers,
        )
        self.assertEqual(
            conflict_before_sink.status_code, 422, conflict_before_sink.text
        )
        self.assertNotIn(fencing_token, conflict_before_sink.text)
        self.assertNotIn(
            fencing_token,
            "\n".join(
                f"{key}:{value}"
                for key, value in conflict_before_sink.headers.items()
            ),
        )

        connection = manager.store.connection
        public_columns = public_text_columns(connection)
        guard_triggers = connection.execute(
            """
            SELECT name FROM sqlite_temp_master
            WHERE type = 'trigger' AND name LIKE 'yeyu_public_text_%'
            """
        ).fetchall()
        self.assertEqual(len(guard_triggers), 2 * len(public_columns))

        original_note = manager.store.get_work_item(work_item_id)["note"]
        with self.assertRaises(sqlite3.IntegrityError) as raw_update:
            connection.execute(
                "UPDATE work_items SET note = ? WHERE work_item_id = ?",
                (f"unguarded-update::{fencing_token}", work_item_id),
            )
        self.assertNotIn(fencing_token, str(raw_update.exception))
        self.assertEqual(
            manager.store.get_work_item(work_item_id)["note"], original_note
        )
        with self.assertRaises(sqlite3.IntegrityError) as raw_insert:
            connection.execute(
                "INSERT INTO metadata(key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    "unguarded-public-insert",
                    json.dumps({"copied": fencing_token}),
                    "2026-08-28T00:00:00+00:00",
                ),
            )
        self.assertNotIn(fencing_token, str(raw_insert.exception))
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM metadata WHERE key = 'unguarded-public-insert'"
            ).fetchone()
        )
        escaped_fencing_token = "".join(
            f"\\u{ord(character):04x}" for character in fencing_token
        )
        with self.assertRaises(sqlite3.IntegrityError) as escaped_raw_insert:
            connection.execute(
                "INSERT INTO metadata(key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    "unguarded-escaped-public-insert",
                    '{"copied":"' + escaped_fencing_token + '"}',
                    "2026-08-28T00:00:00+00:00",
                ),
            )
        self.assertNotIn(fencing_token, str(escaped_raw_insert.exception))
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM metadata WHERE key = 'unguarded-escaped-public-insert'"
            ).fetchone()
        )
        with self.assertRaises(sqlite3.IntegrityError) as prefixed_escaped_insert:
            connection.execute(
                "INSERT INTO metadata(key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    "unguarded-prefixed-escaped-public-insert",
                    f"ordinary-prefix::{escaped_fencing_token}::suffix",
                    "2026-08-28T00:00:00+00:00",
                ),
            )
        self.assertNotIn(fencing_token, str(prefixed_escaped_insert.exception))
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM metadata "
                "WHERE key = 'unguarded-prefixed-escaped-public-insert'"
            ).fetchone()
        )
        with self.assertRaises(sqlite3.IntegrityError) as public_blob:
            connection.execute(
                "UPDATE work_items SET note = ? WHERE work_item_id = ?",
                (
                    sqlite3.Binary(
                        ('{"copied":"' + escaped_fencing_token + '"}').encode(
                            "utf-8"
                        )
                    ),
                    work_item_id,
                ),
            )
        self.assertNotIn(fencing_token, str(public_blob.exception))
        self.assertEqual(
            manager.store.get_work_item(work_item_id)["note"], original_note
        )

        candidate_token = "candidate-private-claim-token-0123456789abcdef"
        with self.assertRaises(sqlite3.IntegrityError) as candidate_same_row:
            connection.execute(
                """
                INSERT INTO work_item_claims(
                    claim_id, work_item_id, claimant, state,
                    claimed_at, expires_at, fencing_token
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    work_item_id,
                    f"copied::{candidate_token}",
                    "2026-08-28T00:00:00+00:00",
                    "2099-08-28T00:00:00+00:00",
                    candidate_token,
                ),
            )
        self.assertNotIn(candidate_token, str(candidate_same_row.exception))
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM work_item_claims WHERE fencing_token = ?",
                (candidate_token,),
            ).fetchone()
        )
        blob_candidate = "blob-private-claim-token-0123456789abcdef"
        with self.assertRaises(sqlite3.IntegrityError) as private_blob:
            connection.execute(
                """
                INSERT INTO work_item_claims(
                    claim_id, work_item_id, claimant, state,
                    claimed_at, expires_at, fencing_token
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    work_item_id,
                    f"copied::{blob_candidate}",
                    "2026-08-28T00:00:00+00:00",
                    "2099-08-28T00:00:00+00:00",
                    sqlite3.Binary(blob_candidate.encode("utf-8")),
                ),
            )
        self.assertNotIn(blob_candidate, str(private_blob.exception))
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM work_item_claims WHERE claim_id = ?",
                ("cccccccc-cccc-4ccc-8ccc-cccccccccccc",),
            ).fetchone()
        )

        candidate_work_item = manager.store.create_work_item(
            {
                "kind": "observation",
                "state": "planned",
                "requested_by": "candidate-scan-test",
                "result": {"ok": True},
            }
        )
        escaped_candidate = "".join(
            f"\\u{ord(character):04x}" for character in candidate_token
        )
        connection.execute(
            "UPDATE work_items SET note = ? WHERE work_item_id = ?",
            (
                f"ordinary-candidate-prefix::{escaped_candidate}::suffix",
                candidate_work_item["work_item_id"],
            ),
        )
        with self.assertRaises(PublicFencingMaterialRejected) as decoded_candidate:
            manager.store.create_work_item_claim(
                {
                    "work_item_id": candidate_work_item["work_item_id"],
                    "claimant": "candidate-worker",
                    "claimed_at": "2026-08-28T00:00:00+00:00",
                    "expires_at": "2099-08-28T00:00:00+00:00",
                    "fencing_token": candidate_token,
                }
            )
        self.assertNotIn(candidate_token, str(decoded_candidate.exception))
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM work_item_claims WHERE fencing_token = ?",
                (candidate_token,),
            ).fetchone()
        )
        connection.execute(
            "UPDATE work_items SET note = '' WHERE work_item_id = ?",
            (candidate_work_item["work_item_id"],),
        )

        copied_note = self.client.post(
            "/api/v1/agent/work-items",
            json={
                "kind": "diagnose_game",
                "gameId": "StarRail",
                "note": f"benign-note-prefix::{fencing_token}::suffix",
            },
            headers=self.worker_mutation_headers("alpha", "worker-note-secret-copy"),
        )
        self.assertEqual(copied_note.status_code, 422, copied_note.text)
        self.assertNotIn(fencing_token, copied_note.text)

        second_dispatch = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "StarRail"},
            headers=self.mutation_headers(
                "worker-secret-copy-second-dispatch", actor="rabiroute"
            ),
        )
        self.assertEqual(second_dispatch.status_code, 202, second_dispatch.text)
        second_work_item_id = str(
            second_dispatch.json()["result"]["workItem"]["workItemId"]
        )

        copied_idempotency = self.worker_mutation_headers(
            "alpha", "worker-idempotency-placeholder"
        )
        copied_idempotency["Idempotency-Key"] = f"copy-{fencing_token}"
        copied_key = self.client.post(
            f"/api/v1/agent/work-items/{second_work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=copied_idempotency,
        )
        self.assertEqual(copied_key.status_code, 422, copied_key.text)
        self.assertNotIn(fencing_token, copied_key.text)

        copied_request_id = self.worker_mutation_headers(
            "alpha", "worker-request-id-secret-copy"
        )
        copied_request_id["X-Request-Id"] = f"trace-{fencing_token}"
        copied_trace = self.client.post(
            f"/api/v1/agent/work-items/{second_work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=copied_request_id,
        )
        self.assertEqual(copied_trace.status_code, 422, copied_trace.text)
        self.assertNotIn(fencing_token, copied_trace.text)
        self.assertNotIn(
            fencing_token,
            "\n".join(f"{key}:{value}" for key, value in copied_trace.headers.items()),
        )
        self.assertNotEqual(
            copied_trace.headers.get("x-request-id"), copied_request_id["X-Request-Id"]
        )
        self.assertEqual(
            self.client.app.state.manager.store.get_work_item(second_work_item_id)[
                "state"
            ],
            "planned",
        )
        self.assertFalse(
            any(
                claim_record["work_item_id"] == second_work_item_id
                for claim_record in self.client.app.state.manager.store.list_work_item_claims(
                    500
                )
            )
        )
        self.assertEqual(
            public_text_locations_containing(
                self.client.app.state.manager.store.connection, fencing_token
            ),
            [],
        )

        alpha_command = self.client.get(
            f"/api/v1/commands/{claim_id}", headers=self.worker_headers("alpha")
        )
        self.assertEqual(alpha_command.status_code, 200, alpha_command.text)
        self.assertNotIn("fencingToken", alpha_command.text)
        self.assertEqual(
            self.client.get(
                f"/api/v1/commands/{claim_id}", headers=self.worker_headers("beta")
            ).status_code,
            404,
        )

        replayed = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=self.worker_mutation_headers("alpha", "worker-claim"),
        )
        self.assertEqual(replayed.status_code, 202, replayed.text)
        self.assertEqual(replayed.headers["Idempotency-Replayed"], "true")
        self.assertEqual(
            replayed.json()["result"]["claim"]["fencingToken"], fencing_token
        )

        alpha_claims = self.client.get(
            "/api/v1/claims", headers=self.worker_headers("alpha")
        )
        self.assertEqual(alpha_claims.status_code, 200, alpha_claims.text)
        self.assertEqual(alpha_claims.json()["total"], 1)
        self.assertNotIn("fencingToken", alpha_claims.text)
        self.assertEqual(
            self.client.get(
                "/api/v1/claims", headers=self.worker_headers("beta")
            ).json()["total"],
            0,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/agent/work-items/{work_item_id}",
                headers=self.worker_headers("beta"),
            ).status_code,
            404,
        )

        copied_secret_headers = self.worker_mutation_headers(
            "alpha", "worker-secret-copy"
        )
        copied_secret_headers["X-Request-Id"] = f"decision-{fencing_token}"
        copied_secret = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "fencingToken": fencing_token,
                "decision": "review_required",
                "reason": f"must not persist {fencing_token}",
            },
            headers=copied_secret_headers,
        )
        self.assertEqual(copied_secret.status_code, 422, copied_secret.text)
        self.assertNotIn(fencing_token, copied_secret.text)
        self.assertNotIn(
            fencing_token,
            "\n".join(f"{key}:{value}" for key, value in copied_secret.headers.items()),
        )
        self.assertNotEqual(
            copied_secret.headers.get("x-request-id"),
            copied_secret_headers["X-Request-Id"],
        )

        cross_worker = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "fencingToken": fencing_token,
                "decision": "review_required",
                "reason": "a different worker must not resolve this claim",
            },
            headers=self.worker_mutation_headers("beta", "worker-cross-decision"),
        )
        self.assertEqual(cross_worker.status_code, 409, cross_worker.text)

        privileged_headers = self.actor_headers("cli")
        public_surfaces = [
            self.client.get(
                f"/api/v1/agent/work-items/{work_item_id}",
                headers=privileged_headers,
            ),
            self.client.get("/api/v1/claims", headers=privileged_headers),
            self.client.get(
                f"/api/v1/commands/{claim_id}", headers=privileged_headers
            ),
            self.client.get(
                f"/api/v1/events?after={cursor}", headers=privileged_headers
            ),
            self.client.get("/api/v1/diagnostics", headers=privileged_headers),
            self.client.get("/api/v1/logs", headers=privileged_headers),
        ]
        for response in public_surfaces:
            self.assertEqual(response.status_code, 200, response.text)
            lowered = response.text.lower()
            self.assertNotIn(fencing_token.lower(), lowered)
            self.assertNotIn("fencingtoken", lowered)

        async def collect_claim_sse() -> str:
            stream = manager.event_stream(cursor)
            try:
                return "".join([await anext(stream) for _ in range(3)])
            finally:
                await stream.aclose()

        stream_payload = asyncio.run(collect_claim_sse()).lower()
        self.assertNotIn(fencing_token.lower(), stream_payload)
        self.assertNotIn("fencingtoken", stream_payload)

        connection = manager.store.connection
        public_columns = (
            ("events", "payload_json"),
            ("idempotency", "response_json"),
            ("command_receipts", "receipt_json"),
            ("work_items", "result_json"),
        )
        for table, column in public_columns:
            values = "\n".join(
                str(row[0])
                for row in connection.execute(f"SELECT {column} FROM {table}")
            ).lower()
            self.assertNotIn(fencing_token.lower(), values)
            self.assertNotIn("fencingtoken", values)
        self.assertEqual(
            public_text_locations_containing(connection, fencing_token),
            [],
        )
        private_claim = connection.execute(
            "SELECT fencing_token FROM work_item_claims WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        self.assertEqual(private_claim[0], fencing_token)

    def test_running_claim_renews_for_same_principal_and_reclaims_after_expiry(
        self,
    ) -> None:
        dispatched = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "StarRail"},
            headers=self.mutation_headers("claim-lifecycle-dispatch", actor="rabiroute"),
        )
        self.assertEqual(dispatched.status_code, 202, dispatched.text)
        work_item_id = str(
            dispatched.json()["result"]["workItem"]["workItemId"]
        )

        first = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=self.worker_mutation_headers("alpha", "claim-lifecycle-first"),
        )
        self.assertEqual(first.status_code, 202, first.text)
        first_claim = first.json()["result"]["claim"]
        first_claim_id = str(first_claim["claimId"])
        first_token = str(first_claim["fencingToken"])
        first_expiry = str(first_claim["expiresAt"])

        renewed = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 600},
            headers=self.worker_mutation_headers("alpha", "claim-lifecycle-renew"),
        )
        self.assertEqual(renewed.status_code, 202, renewed.text)
        self.assertEqual(renewed.headers["Idempotency-Replayed"], "false")
        renewed_claim = renewed.json()["result"]["claim"]
        self.assertEqual(renewed_claim["claimId"], first_claim_id)
        self.assertEqual(renewed_claim["fencingToken"], first_token)
        self.assertGreater(str(renewed_claim["expiresAt"]), first_expiry)

        cross_principal = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 600},
            headers=self.worker_mutation_headers("beta", "claim-lifecycle-cross"),
        )
        self.assertEqual(cross_principal.status_code, 409, cross_principal.text)

        manager = self.client.app.state.manager
        with manager.store.atomic():
            manager.store.connection.execute(
                "UPDATE work_item_claims SET expires_at = ? WHERE claim_id = ?",
                ("2000-01-01T00:00:00+00:00", first_claim_id),
            )

        reclaimed = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 600},
            headers=self.worker_mutation_headers("beta", "claim-lifecycle-reclaim"),
        )
        self.assertEqual(reclaimed.status_code, 202, reclaimed.text)
        reclaimed_claim = reclaimed.json()["result"]["claim"]
        reclaimed_claim_id = str(reclaimed_claim["claimId"])
        reclaimed_token = str(reclaimed_claim["fencingToken"])
        self.assertNotEqual(reclaimed_claim_id, first_claim_id)
        self.assertNotEqual(reclaimed_token, first_token)
        self.assertEqual(
            manager.store.get_work_item_claim_private(first_claim_id)["state"],
            "expired",
        )
        self.assertEqual(
            manager.store.get_work_item(work_item_id)["result"]["activeClaimId"],
            reclaimed_claim_id,
        )

        stale = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": first_claim_id,
                "fencingToken": first_token,
                "decision": "review_required",
                "reason": "expired claim must remain fenced",
            },
            headers=self.worker_mutation_headers("alpha", "claim-lifecycle-stale"),
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertNotIn(first_token, stale.text)

        public_claims = self.client.get(
            "/api/v1/claims", headers=self.actor_headers("cli")
        )
        public_item = self.client.get(
            f"/api/v1/agent/work-items/{work_item_id}",
            headers=self.actor_headers("cli"),
        )
        for response in (public_claims, public_item):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(first_token, response.text)
            self.assertNotIn(reclaimed_token, response.text)
        self.assertEqual(
            public_text_locations_containing(manager.store.connection, first_token),
            [],
        )
        self.assertEqual(
            public_text_locations_containing(manager.store.connection, reclaimed_token),
            [],
        )

    def test_legacy_public_fencing_material_is_scrubbed_on_initialize(self) -> None:
        database_path = self.root / "legacy-fencing.sqlite3"
        store = SqliteStore(database_path)
        store.initialize()
        event = store.append_event("legacy.event", "legacy", "event-1", {"ok": True})
        work_item = store.create_work_item(
            {
                "kind": "observation",
                "state": "planned",
                "requested_by": "legacy",
                "result": {"ok": True},
            }
        )
        legacy_token = "legacy-private-claim-token-0123456789abcdef"
        claim = store.create_work_item_claim(
            {
                "work_item_id": work_item["work_item_id"],
                "claimant": "legacy-worker",
                "claimed_at": "2026-08-28T00:00:00+00:00",
                "expires_at": "2099-08-28T00:00:00+00:00",
                "fencing_token": legacy_token,
            }
        )
        store.run_idempotent(
            key="legacy-idempotency",
            method="POST",
            path="/legacy",
            request={"ok": True},
            status_code=202,
            expected_state_version=None,
            operation=lambda: {"commandId": "legacy-command", "ok": True},
        )
        store.save_command_receipt("legacy-command", {"ok": True})
        store.append_event(
            "command.receipt", "command", "legacy-command", {"ok": True}
        )
        resource = store.create_resource(
            "diagnostic-bundle",
            state="ready",
            document={"benign": True},
        )
        # Model a pre-v6 database.  TEMP guards disappear with this connection;
        # the next initialize must perform the one-time historical scrub.
        store.connection.execute("DELETE FROM schema_version WHERE version = 6")
        store.close()

        leaked = json.dumps(
            {
                "nested": {
                    "benign": f"prefix::{legacy_token}::suffix",
                    "fencingToken": "legacy-keyed-secret-value",
                }
            },
            separators=(",", ":"),
        )
        escaped_token = "".join(
            f"\\u{ord(character):04x}" for character in legacy_token
        )
        escaped_leaked = (
            '{"nested":"'
            + escaped_token
            + '","fencingToken":"legacy-keyed-secret-value"}'
        )
        legacy_connection = sqlite3.connect(database_path)
        try:
            legacy_connection.execute(
                "UPDATE events SET payload_json = ? WHERE sequence = ?",
                (leaked, event["sequence"]),
            )
            legacy_connection.execute(
                """
                UPDATE events SET event_type = ?, entity_id = ?
                WHERE sequence = ?
                """,
                (
                    f"legacy.{legacy_token}",
                    f"event::{legacy_token}",
                    event["sequence"],
                ),
            )
            legacy_connection.execute(
                """
                UPDATE idempotency SET idempotency_key = ?, response_json = ?
                WHERE idempotency_key = ?
                """,
                (
                    f"legacy-key::{legacy_token}",
                    json.dumps(
                        {
                            "commandId": "legacy-command",
                            "benign": f"request::{legacy_token}",
                        },
                        separators=(",", ":"),
                    ),
                    "legacy-idempotency",
                ),
            )
            legacy_connection.execute(
                "UPDATE work_items SET note = ?, result_json = ? WHERE work_item_id = ?",
                (
                    f"legacy note {legacy_token}",
                    escaped_leaked,
                    work_item["work_item_id"],
                ),
            )
            legacy_connection.execute(
                "UPDATE manager_resources SET document_json = ? WHERE resource_id = ?",
                (leaked, resource["resource_id"]),
            )
            legacy_connection.commit()
        finally:
            legacy_connection.close()

        migrated = SqliteStore(database_path)
        migrated.initialize()
        try:
            self.assertEqual(
                public_text_locations_containing(migrated.connection, legacy_token),
                [],
            )
            private_claim = migrated.connection.execute(
                "SELECT fencing_token FROM work_item_claims WHERE claim_id = ?",
                (claim["claim_id"],),
            ).fetchone()
            self.assertEqual(private_claim[0], legacy_token)
            self.assertIsNone(
                migrated.connection.execute(
                    "SELECT 1 FROM idempotency WHERE idempotency_key LIKE 'legacy-key::%'"
                ).fetchone()
            )
            self.assertIsNone(
                migrated.connection.execute(
                    "SELECT 1 FROM command_receipts WHERE command_id = 'legacy-command'"
                ).fetchone()
            )
            self.assertIsNone(
                migrated.connection.execute(
                    """
                    SELECT 1 FROM events
                    WHERE entity_type = 'command' AND entity_id = 'legacy-command'
                    """
                ).fetchone()
            )
            migrated_event = migrated.connection.execute(
                "SELECT * FROM events WHERE sequence = ?", (event["sequence"],)
            ).fetchone()
            self.assertIsNotNone(migrated_event)
            self.assertEqual(migrated_event["event_id"], event["event_id"])
            self.assertNotIn(legacy_token, migrated_event["event_type"])
            self.assertNotIn(legacy_token, migrated_event["entity_id"])
            self.assertNotIn(legacy_token, migrated_event["payload_json"])
            self.assertNotIn("fencingToken", migrated_event["payload_json"])
            migrated_work_item = migrated.get_work_item(work_item["work_item_id"])
            self.assertNotIn(legacy_token, migrated_work_item["note"])
            self.assertNotIn(
                "fencingToken",
                json.dumps(migrated_work_item["result"], ensure_ascii=False),
            )
            migrated_resource = migrated.get_resource(
                "diagnostic-bundle", resource["resource_id"]
            )
            self.assertNotIn(
                legacy_token,
                json.dumps(migrated_resource["document"], ensure_ascii=False),
            )
            self.assertNotIn(
                "fencingToken",
                json.dumps(migrated_resource["document"], ensure_ascii=False),
            )
            schema_versions = {
                int(row[0])
                for row in migrated.connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchall()
            }
            self.assertIn(6, schema_versions)
        finally:
            migrated.close()

    def test_v6_database_is_rechecked_and_repairs_later_public_contamination(
        self,
    ) -> None:
        database_path = self.root / "v6-fencing-recheck.sqlite3"
        store = SqliteStore(database_path)
        store.initialize()
        work_item = store.create_work_item(
            {
                "kind": "observation",
                "state": "planned",
                "requested_by": "legacy-v6-writer",
                "result": {"ok": True},
            }
        )
        token = "v6-private-claim-token-0123456789abcdef"
        claim = store.create_work_item_claim(
            {
                "work_item_id": work_item["work_item_id"],
                "claimant": "legacy-v6-worker",
                "claimed_at": "2026-08-28T00:00:00+00:00",
                "expires_at": "2099-08-28T00:00:00+00:00",
                "fencing_token": token,
            }
        )
        self.assertIsNotNone(
            store.connection.execute(
                "SELECT 1 FROM schema_version WHERE version = 6"
            ).fetchone()
        )
        store.close()

        escaped_token = "".join(f"\\u{ord(character):04x}" for character in token)
        contaminated_note = f"ordinary-prefix::{escaped_token}::suffix"
        legacy_connection = sqlite3.connect(database_path)
        try:
            legacy_connection.execute(
                "UPDATE work_items SET note = ? WHERE work_item_id = ?",
                (
                    contaminated_note,
                    work_item["work_item_id"],
                ),
            )
            legacy_connection.commit()
        finally:
            legacy_connection.close()

        reopened = SqliteStore(database_path)
        reopened.initialize()
        try:
            self.assertEqual(
                public_text_locations_containing(reopened.connection, token), []
            )
            repaired_work_item = reopened.get_work_item(work_item["work_item_id"])
            repaired_note = str(repaired_work_item["note"])
            self.assertNotEqual(repaired_note, contaminated_note)
            self.assertEqual(
                SqliteStore._text_values_are_safe_for_tokens((token,), repaired_note),
                1,
            )
            private_claim = reopened.connection.execute(
                "SELECT fencing_token FROM work_item_claims WHERE claim_id = ?",
                (claim["claim_id"],),
            ).fetchone()
            self.assertEqual(private_claim[0], token)
            self.assertIsNotNone(
                reopened.connection.execute(
                    "SELECT 1 FROM schema_version WHERE version = 6"
                ).fetchone()
            )
            self.assertIsNotNone(
                reopened.connection.execute(
                    "SELECT 1 FROM schema_version WHERE version = 7"
                ).fetchone()
            )
        finally:
            reopened.close()

    def test_v6_startup_scrubs_public_blobs_and_normalizes_private_blob_token(
        self,
    ) -> None:
        database_path = self.root / "v6-fencing-blob.sqlite3"
        store = SqliteStore(database_path)
        store.initialize()
        work_item = store.create_work_item(
            {
                "kind": "observation",
                "state": "planned",
                "requested_by": "legacy-blob-writer",
                "result": {"ok": True},
            }
        )
        token = "blob-migration-private-token-0123456789abcdef"
        claim = store.create_work_item_claim(
            {
                "work_item_id": work_item["work_item_id"],
                "claimant": "legacy-blob-worker",
                "claimed_at": "2026-08-28T00:00:00+00:00",
                "expires_at": "2099-08-28T00:00:00+00:00",
                "fencing_token": token,
            }
        )
        store.close()

        escaped_token = "".join(f"\\u{ord(character):04x}" for character in token)
        legacy_connection = sqlite3.connect(database_path)
        try:
            legacy_connection.execute(
                "UPDATE work_item_claims SET fencing_token = ? WHERE claim_id = ?",
                (sqlite3.Binary(token.encode("utf-8")), claim["claim_id"]),
            )
            legacy_connection.execute(
                "UPDATE work_items SET note = ? WHERE work_item_id = ?",
                (
                    sqlite3.Binary(
                        ('{"blob":"' + escaped_token + '"}').encode("utf-8")
                    ),
                    work_item["work_item_id"],
                ),
            )
            legacy_connection.commit()
        finally:
            legacy_connection.close()

        reopened = SqliteStore(database_path)
        reopened.initialize()
        try:
            private_claim = reopened.connection.execute(
                "SELECT typeof(fencing_token), fencing_token FROM work_item_claims "
                "WHERE claim_id = ?",
                (claim["claim_id"],),
            ).fetchone()
            self.assertEqual(tuple(private_claim), ("text", token))
            public_note = reopened.connection.execute(
                "SELECT typeof(note), note FROM work_items WHERE work_item_id = ?",
                (work_item["work_item_id"],),
            ).fetchone()
            self.assertEqual(public_note[0], "text")
            self.assertNotIn(
                token,
                json.dumps(json.loads(public_note[1]), ensure_ascii=False),
            )
            self.assertEqual(
                public_text_locations_containing(reopened.connection, token), []
            )
        finally:
            reopened.close()

    def test_private_token_history_survives_claim_rewrite_delete_and_restart(
        self,
    ) -> None:
        database_path = self.root / "fencing-history.sqlite3"
        first = SqliteStore(database_path)
        first.initialize()
        work_item = first.create_work_item(
            {
                "kind": "observation",
                "state": "planned",
                "requested_by": "history-test",
                "result": {"ok": True},
            }
        )
        token_a = "historical-private-token-a-0123456789abcdef"
        token_b = "historical-private-token-b-0123456789abcdef"
        token_c = "historical-private-token-c-0123456789abcdef"
        claim = first.create_work_item_claim(
            {
                "work_item_id": work_item["work_item_id"],
                "claimant": "history-worker",
                "claimed_at": "2026-08-28T00:00:00+00:00",
                "expires_at": "2099-08-28T00:00:00+00:00",
                "fencing_token": token_a,
            }
        )
        first.close()

        legacy_connection = sqlite3.connect(database_path)
        legacy_connection.execute(
            "UPDATE work_item_claims SET fencing_token = ? WHERE claim_id = ?",
            (token_b, claim["claim_id"]),
        )
        legacy_connection.execute(
            """
            INSERT INTO work_item_claims(
                claim_id, work_item_id, claimant, state,
                claimed_at, expires_at, fencing_token
            ) VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                work_item["work_item_id"],
                "legacy-direct-writer",
                "2026-08-28T00:00:00+00:00",
                "2099-08-28T00:00:00+00:00",
                sqlite3.Binary(token_c.encode("utf-8")),
            ),
        )
        legacy_connection.execute(
            "DELETE FROM work_item_claims WHERE claim_id IN (?, ?)",
            (claim["claim_id"], "dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        )
        legacy_connection.commit()
        legacy_connection.close()

        final = SqliteStore(database_path)
        final.initialize()
        try:
            history = {
                str(row[0])
                for row in final.connection.execute(
                    "SELECT fencing_token FROM work_item_fencing_token_history"
                ).fetchall()
            }
            self.assertTrue({token_a, token_b, token_c}.issubset(history))
            trigger_sql = "\n".join(
                str(row[0])
                for row in final.connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'trigger'
                      AND name LIKE 'trg_work_item_claim_fencing_history_%'
                    ORDER BY name
                    """
                ).fetchall()
            )
            self.assertEqual(trigger_sql.count("CREATE TRIGGER"), 2)
            self.assertNotIn("yeyu_", trigger_sql.lower())
            for token in (token_a, token_b, token_c):
                with self.assertRaises(sqlite3.IntegrityError) as blocked:
                    final.connection.execute(
                        "UPDATE work_items SET note = ? WHERE work_item_id = ?",
                        (f"copied::{token}", work_item["work_item_id"]),
                    )
                self.assertNotIn(token, str(blocked.exception))
        finally:
            final.close()

    def test_initialized_stores_refresh_external_token_history_without_restart(
        self,
    ) -> None:
        database_path = self.root / "fencing-history-live-refresh.sqlite3"
        first = SqliteStore(database_path)
        second = SqliteStore(database_path)
        first.initialize()
        second.initialize()
        try:
            work_item = first.create_work_item(
                {
                    "kind": "observation",
                    "state": "planned",
                    "requested_by": "multi-store-history-test",
                    "result": {"ok": True},
                }
            )
            token_a = "live-private-token-a-0123456789abcdef"
            token_b = "live-private-token-b-0123456789abcdef"
            claim = second.create_work_item_claim(
                {
                    "work_item_id": work_item["work_item_id"],
                    "claimant": "multi-store-worker",
                    "claimed_at": "2026-08-28T00:00:00+00:00",
                    "expires_at": "2099-08-28T00:00:00+00:00",
                    "fencing_token": token_a,
                }
            )

            with self.assertRaises(PublicFencingMaterialRejected) as official_sink:
                first.set_metadata("external-token-copy", {"copied": token_a})
            self.assertNotIn(token_a, str(official_sink.exception))

            second.connection.execute(
                "UPDATE work_item_claims SET fencing_token = ? WHERE claim_id = ?",
                (token_b, claim["claim_id"]),
            )
            with self.assertRaises(sqlite3.IntegrityError) as raw_sink:
                first.connection.execute(
                    "UPDATE work_items SET note = ? WHERE work_item_id = ?",
                    (f"copied::{token_b}", work_item["work_item_id"]),
                )
            self.assertNotIn(token_b, str(raw_sink.exception))
            history = {
                str(row[0])
                for row in first.connection.execute(
                    "SELECT fencing_token FROM work_item_fencing_token_history"
                ).fetchall()
            }
            self.assertTrue({token_a, token_b}.issubset(history))
        finally:
            second.close()
            first.close()

    def test_public_text_guard_normalizes_nested_json_escapes_and_surrogates(
        self,
    ) -> None:
        ascii_token = "alpha012345"
        escaped_ascii = "".join(
            f"\\u{ord(character):04x}" for character in ascii_token
        )
        double_escaped_ascii = escaped_ascii.replace("\\", "\\\\")
        self.assertEqual(
            SqliteStore._text_values_are_safe_for_tokens(
                (ascii_token,), f"prefix::{escaped_ascii}::suffix"
            ),
            0,
        )
        self.assertEqual(
            SqliteStore._text_values_are_safe_for_tokens(
                (ascii_token,), f"prefix::{double_escaped_ascii}::suffix"
            ),
            0,
        )
        self.assertEqual(
            SqliteStore._text_values_are_safe_for_tokens(
                ("token-\U0001f600-end",), r"prefix::token-\ud83d\ude00-end"
            ),
            0,
        )

        too_deep = escaped_ascii
        for _ in range(5):
            too_deep = too_deep.replace("\\", "\\\\")
        self.assertEqual(
            SqliteStore._text_values_are_safe_for_tokens(
                (ascii_token,), f"prefix::{too_deep}::suffix"
            ),
            0,
        )

    def test_commit_failure_rolls_back_and_reloads_private_token_cache(self) -> None:
        store = self.client.app.state.manager.store
        connection = store.connection
        rolled_back_token = "rolled-back-private-token-0123456789abcdef"
        with self.assertRaises(sqlite3.IntegrityError):
            with store.atomic():
                connection.execute("PRAGMA defer_foreign_keys=ON")
                connection.execute(
                    """
                    INSERT INTO work_item_claims(
                        claim_id, work_item_id, claimant, state,
                        claimed_at, expires_at, fencing_token
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "missing-work-item",
                        "rollback-test-worker",
                        "2026-08-28T00:00:00+00:00",
                        "2099-08-28T00:00:00+00:00",
                        rolled_back_token,
                    ),
                )
        self.assertFalse(connection.in_transaction)
        self.assertEqual(store._transaction_depth, 0)
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM work_item_claims WHERE fencing_token = ?",
                (rolled_back_token,),
            ).fetchone()
        )
        store.set_metadata(
            "transaction-recovered", {"rolledBackValue": rolled_back_token}
        )
        self.assertEqual(
            store.get_metadata("transaction-recovered")["rolledBackValue"],
            rolled_back_token,
        )

    def test_every_external_manager_mutation_uses_the_idempotent_boundary(self) -> None:
        package_root = Path(__file__).parents[1] / "yeyu_gamer_manager"
        routes_tree = ast.parse(
            (package_root / "api" / "routes.py").read_text(encoding="utf-8")
        )
        route_functions = {
            node.name: node
            for node in routes_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def manager_calls(function_name: str, seen: frozenset[str]) -> set[str]:
            if function_name in seen:
                return set()
            node = route_functions[function_name]
            result: set[str] = set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if (
                    isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "manager"
                ):
                    result.add(child.func.attr)
                elif (
                    isinstance(child.func, ast.Name)
                    and child.func.id in route_functions
                ):
                    result.update(
                        manager_calls(child.func.id, seen | {function_name})
                    )
            return result

        manager_tree = ast.parse(
            (package_root / "services" / "manager.py").read_text(encoding="utf-8")
        )
        manager_class = next(
            node
            for node in manager_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ManagerService"
        )
        manager_methods = {
            node.name: node
            for node in manager_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        checked_routes: list[str] = []
        for name, node in route_functions.items():
            mutating = False
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"api", "compat"}
                    and target.attr in {"post", "put", "patch", "delete"}
                ):
                    mutating = True
                    break
            if not mutating:
                continue
            checked_routes.append(name)
            self.assertIn("mutation", {argument.arg for argument in node.args.args}, name)
            calls = manager_calls(name, frozenset()) - {"work_item_dispatch_projection"}
            self.assertTrue(calls, name)
            for method_name in calls:
                method = manager_methods[method_name]
                uses_idempotency = any(
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "self"
                    and child.func.attr in {"_idempotent", "_idempotent_with_replay"}
                    for child in ast.walk(method)
                )
                self.assertTrue(uses_idempotency, f"{name} -> {method_name}")
        self.assertGreaterEqual(len(checked_routes), 25)

    def test_production_uvicorn_configuration_disables_raw_access_logs(self) -> None:
        from yeyu_gamer_manager import __main__ as manager_main
        from yeyu_gamer_manager.logging_setup import shutdown_manager_logging

        listener = mock.Mock(name="listener")
        try:
            with (
                mock.patch.object(
                    manager_main.Settings, "from_env", return_value=self.settings
                ),
                mock.patch.object(manager_main, "create_app", return_value=object()),
                mock.patch.object(
                    manager_main, "bind_manager_listener", return_value=listener
                ) as bind,
                mock.patch.object(manager_main.uvicorn, "Server") as server_type,
            ):
                server = server_type.return_value
                self.assertEqual(manager_main.run(), 0)
        finally:
            shutdown_manager_logging()
        # The socket is bound before create_app/lifespan so a busy port can
        # never reach state recovery.
        bind.assert_called_once_with(self.settings.host, self.settings.port)
        server.run.assert_called_once_with(sockets=[listener])
        listener.close.assert_called_once_with()
        config = server_type.call_args.args[0]
        self.assertFalse(config.access_log)
        self.assertEqual(config.limit_concurrency, 64)
        log_path = self.settings.data_dir / "logs" / "manager" / "manager.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("manager.start.listener_bound", log_path.read_text(encoding="utf-8"))

    def test_production_entry_refuses_a_busy_port_before_touching_state(self) -> None:
        from yeyu_gamer_manager import __main__ as manager_main
        from yeyu_gamer_manager.listener import ManagerPortUnavailable
        from yeyu_gamer_manager.logging_setup import shutdown_manager_logging

        busy = ManagerPortUnavailable("127.0.0.1", 8877, OSError(10048, "in use"))
        with (
            mock.patch.object(
                manager_main.Settings, "from_env", return_value=self.settings
            ),
            mock.patch.object(manager_main, "create_app") as create_app,
            mock.patch.object(
                manager_main, "bind_manager_listener", side_effect=busy
            ),
            mock.patch.object(manager_main.uvicorn, "Server") as server_type,
        ):
            self.assertEqual(manager_main.run(), 3)
        shutdown_manager_logging()
        create_app.assert_not_called()
        server_type.assert_not_called()

    def test_listener_bind_detects_an_occupied_loopback_port(self) -> None:
        import socket

        from yeyu_gamer_manager.listener import (
            ManagerPortUnavailable,
            bind_manager_listener,
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
            occupant.bind(("127.0.0.1", 0))
            occupant.listen(1)
            port = occupant.getsockname()[1]
            with self.assertRaises(ManagerPortUnavailable):
                bind_manager_listener("127.0.0.1", port)
        listener = bind_manager_listener("127.0.0.1", port)
        try:
            self.assertEqual(listener.getsockname()[1], port)
        finally:
            listener.close()

    def test_mutation_body_limit_covers_content_length_and_chunked_streams(
        self,
    ) -> None:
        prefix = b'{"nonce":"'
        suffix = b'"}'
        oversized_value = b"x" * (MAX_MUTATION_BODY_BYTES + 1)

        declared = self.client.post(
            "/api/v1/webgui/session-exchanges",
            content=prefix + oversized_value + suffix,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(declared.status_code, 413, declared.text)
        self.assertNotIn("x" * 128, declared.text)

        def chunks():
            yield prefix
            yield oversized_value[: MAX_MUTATION_BODY_BYTES // 2]
            yield oversized_value[MAX_MUTATION_BODY_BYTES // 2 :]
            yield suffix

        streamed = self.client.post(
            "/api/v1/webgui/session-exchanges",
            content=chunks(),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "Transfer-Encoding": "chunked",
            },
        )
        self.assertEqual(streamed.status_code, 413, streamed.text)
        self.assertNotIn("x" * 128, streamed.text)

    def test_manager_id_changes_for_a_new_process_instance(self) -> None:
        first_id = self.client.get("/api/v1/meta").json()["managerId"]
        replacement_app = create_app(self.settings)
        with TestClient(replacement_app) as replacement:
            second_id = replacement.get("/api/v1/meta").json()["managerId"]
        self.assertNotEqual(first_id, second_id)

    def test_lifecycle_rejects_while_execution_is_active(self) -> None:
        manager = self.client.app.state.manager
        manager.store.create_game_run(
            {
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "queued",
                "mode": "execute",
                "requested_by": "contract-test",
                "message": "simulated durable active execution",
            }
        )
        response = self.client.post(
            "/api/v1/manager/restart-requests",
            json={"requestedBy": "tray", "reason": "must-drain-first"},
            headers=self.mutation_headers("restart-active-1"),
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("execution is active", response.json()["detail"])
        self.assertEqual(self.lifecycle_actions, [])

    def test_sqlite_is_wal_and_has_event_and_idempotency_ledgers(self) -> None:
        connection = sqlite3.connect(self.settings.database_path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertTrue(
                {"events", "idempotency", "games", "command_receipts"}.issubset(tables)
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
