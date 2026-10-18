from __future__ import annotations

import base64
import hashlib
import io
import unittest
from unittest import mock

from PIL import Image

import test_contract as contract_support


class OperatorEvidenceUploadTests(unittest.TestCase):
    def test_game_detail_aggregates_accounts_without_requiring_default(self):
        import json
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient
        from yeyu_gamer_manager.app import create_app
        from yeyu_gamer_manager.domain.models import ConfigPatchRequest
        from yeyu_gamer_manager.settings import Settings

        with tempfile.TemporaryDirectory(prefix="yeyu-detail-") as directory:
            settings = Settings.for_test(Path(directory))
            settings.legacy_root.mkdir(parents=True)
            (settings.legacy_root / "daily-gui-config.json").write_text(json.dumps({
                "enabled": {"WW": True}, "order": ["WW"], "dailyScheduleEnabled": False,
            }), encoding="utf-8")
            (settings.legacy_root / "game-automation-policy.json").write_text(json.dumps({
                "schemaVersion": 2, "forbiddenActions": [], "games": {"WW": {}},
            }), encoding="utf-8")
            with TestClient(create_app(settings)) as client:
                manager = client.app.state.manager
                manager.patch_config(
                    ConfigPatchRequest(game_accounts={"WW": [
                        {"label": "first", "enabled": True},
                        {"label": "second", "enabled": True},
                    ]}), idempotency_key="two-accounts", request_id=None,
                    path="/api/v1/config", expected_state_version=None,
                )
                todos = manager.list_todo_instances(game_id="WW", current=True, limit=1000)
                self.assertEqual(len({todo.account_id for todo in todos}), 2)
                self.assertNotIn("default", {todo.account_id for todo in todos})
                detail = manager.get_game_detail("WW")
                self.assertEqual(len(detail["todoInstances"]), len(todos))
                for cadence in ("daily", "weekly"):
                    scoped = [todo for todo in todos if todo.cadence == cadence]
                    self.assertEqual(detail["todoSummary"][cadence], manager._todo_summary_from_items(scoped, cadence))

    def setUp(self):
        self.fixture = contract_support.ManagerContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.todo = self.client.get("/api/v1/todo-instances", headers=self.fixture.actor_headers()).json()["items"][0]
        self.path = f"/api/v1/todo-instances/{self.todo['todoInstanceId']}/evidence-uploads"
        stream = io.BytesIO()
        Image.new("RGB", (64, 32), "green").save(stream, format="PNG")
        self.content = stream.getvalue()

    def upload(self, key="upload", actor="agent-launch", **changes):
        body = {"fileName": "evidence.png", "contentBase64": base64.b64encode(self.content).decode(), "note": "待人工核对；不代表官方验收"}
        body.update(changes)
        return self.client.post(self.path, json=body, headers=self.fixture.mutation_headers(key, actor=actor))

    def test_upload_links_current_todo_without_fabricating_attempt(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]
        artifact = result["artifact"]
        self.assertEqual(artifact["hash"], hashlib.sha256(self.content).hexdigest())
        self.assertTrue(artifact["diagnosticOnly"])
        self.assertFalse(artifact["frameCaptureTimeKnown"])
        self.assertFalse(artifact["accountIdentityConfirmed"])
        self.assertIsNone(artifact["runAttemptId"])
        self.assertEqual(artifact["accountId"], self.todo["accountId"])
        todo = result["todoInstance"]
        self.assertEqual(todo["status"], "review_required")
        self.assertEqual(todo["attempts"], self.todo["attempts"])
        self.assertIn(result["evidenceId"], todo["evidenceRefs"])
        content = self.client.get(f"/api/v1/artifacts/{result['evidenceId']}/content", headers=self.fixture.actor_headers("agent-launch"))
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(content.content, self.content)

    def test_replay_and_conflict(self):
        first = self.upload()
        second = self.upload()
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()["result"]["evidenceId"], second.json()["result"]["evidenceId"])
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(self.upload(note="different").status_code, 409)
        self.assertEqual(len(list((self.fixture.settings.data_dir / "artifacts").glob("operator-upload-*"))), 1)

    def test_invalid_image_base64_and_paths(self):
        for changes in ({"contentBase64": "!!!!"}, {"contentBase64": base64.b64encode(b"not an image").decode()}, {"fileName": "../image.png"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.upload(**changes).status_code, 422)

    def test_large_screenshot_and_other_endpoint_body_limit(self):
        self.content += b"\x00" * (1024 * 1024)
        self.assertEqual(self.upload().status_code, 202)
        rejected = self.client.post("/api/v1/evidence-reviews", content=b" " * (1024 * 1024 + 1), headers=self.fixture.mutation_headers("large-other"))
        self.assertEqual(rejected.status_code, 413)

    def test_missing_cas_and_stale_cas(self):
        body = {"fileName": "evidence.png", "contentBase64": base64.b64encode(self.content).decode(), "note": "review"}
        headers = {**self.fixture.actor_headers("agent-launch"), "Idempotency-Key": "no-cas"}
        self.assertEqual(self.client.post(self.path, json=body, headers=headers).status_code, 428)
        headers["If-Match"] = '"0"'
        self.assertEqual(self.client.post(self.path, json=body, headers=headers).status_code, 412)

    def test_role_boundaries(self):
        for actor in ("scheduler", "agent", "rabiroute"):
            self.assertEqual(self.upload(actor=actor).status_code, 403)
        headers = self.fixture.actor_headers("agent-launch")
        for path in ("/api/v1/todo-instances", "/api/v1/game-runs"):
            self.assertEqual(self.client.get(path, headers=headers).status_code, 200)
        for path in ("/api/v1/config", "/api/v1/agent/work-items", "/api/v1/manager/anything"):
            self.assertEqual(self.client.get(path, headers=headers).status_code, 403)
        denied = self.client.patch("/api/v1/config", json={}, headers=self.fixture.mutation_headers("config", actor="agent-launch"))
        self.assertEqual(denied.status_code, 403)

    def test_transition_cannot_complete_or_release_blocker(self):
        path = f"/api/v1/todo-instances/{self.todo['todoInstanceId']}/transitions"
        for body in ({"status": "completed"}, {"status": "pending"}, {"status": "review_required", "release_technical_blocker": True}, {"status": "review_required", "incrementAttempt": True}):
            response = self.client.post(path, json=body, headers=self.fixture.mutation_headers("denied", actor="agent-launch"))
            self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post(path, json={"status": "review_required", "reason": "需要核实"}, headers=self.fixture.mutation_headers("review", actor="agent-launch"))
        self.assertEqual(response.status_code, 202, response.text)

    def test_run_control_passes_scope_but_scheduler_does_not(self):
        for action in ("cancel", "resume", "takeover", "takeover-release"):
            path = f"/api/v1/game-runs/missing/{action}-requests"
            response = self.client.post(path, json={"reason": "scope test"}, headers=self.fixture.mutation_headers(action, actor="agent-launch"))
            self.assertEqual(response.status_code, 404, response.text)
            response = self.client.post(path, json={"reason": "scope test"}, headers=self.fixture.mutation_headers(action, actor="scheduler"))
            self.assertEqual(response.status_code, 404 if action == "resume" else 403, response.text)

    def test_file_is_removed_if_transaction_fails(self):
        with mock.patch.object(self.client.app.state.manager.store, "transition_todo_instance", side_effect=ValueError("forced rollback")):
            self.assertEqual(self.upload().status_code, 422)
        root = self.fixture.settings.data_dir / "artifacts"
        self.assertEqual(list(root.glob("operator-upload-*")), [])


if __name__ == "__main__":
    unittest.main()
