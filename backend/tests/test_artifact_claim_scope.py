from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.settings import Settings


class ArtifactClaimScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-artifact-scope-")
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
                    "weeklyDay": "Saturday",
                    "skipBlockedOnRunAll": True,
                    "executionStrategy": "continue",
                    "messageEndpointPort": 8877,
                }
            ),
            encoding="utf-8",
        )
        (self.settings.legacy_root / "game-automation-policy.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "forbiddenActions": ["purchase", "draw", "account_settings"],
                    "games": {
                        "StarRail": {
                            "tier": "stable",
                            "weeklyMode": "supported",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.settings.web_dist.mkdir(parents=True)
        (self.settings.web_dist / "index.html").write_text(
            "<!doctype html><div id='app'></div>", encoding="utf-8"
        )
        self.context = TestClient(create_app(self.settings))
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()

    def actor_headers(self, actor: str) -> dict[str, str]:
        if actor == "tray":
            token = str(self.settings.tray_bootstrap_secret)
        else:
            token_name = "agent.yeyu.token" if actor == "agent" else f"{actor}.token"
            token = (self.settings.actor_tokens_dir / token_name).read_text(
                encoding="ascii"
            ).strip()
        return {
            "Authorization": f"Bearer {token}",
            "X-YeYu-Gamer-Actor": actor,
        }

    def mutation_headers(self, actor: str, key: str) -> dict[str, str]:
        actor_headers = self.actor_headers(actor)
        version = self.client.get(
            "/api/v1/snapshot", headers=actor_headers
        ).json()["stateVersion"]
        return {
            **actor_headers,
            "Idempotency-Key": key,
            "If-Match": str(version),
        }

    def create_artifact(self, key: str) -> str:
        response = self.client.post(
            "/api/v1/diagnostic-bundles",
            json={},
            headers=self.mutation_headers("cli", key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["result"]["artifact"]["artifactId"])

    def create_work_item(self, artifact_refs: list[str], key: str) -> str:
        response = self.client.post(
            "/api/v1/agent/work-items",
            json={
                "kind": "diagnose_game",
                "gameId": "StarRail",
                "artifactRefs": artifact_refs,
            },
            headers=self.mutation_headers("agent", key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["result"]["workItem"]["workItemId"])

    def claim_work_item(self, work_item_id: str, key: str) -> tuple[str, str]:
        response = self.client.post(
            f"/api/v1/agent/work-items/{work_item_id}/claims",
            json={"leaseSeconds": 120},
            headers=self.mutation_headers("agent", key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        claim = response.json()["result"]["claim"]
        return str(claim["claimId"]), str(claim["fencingToken"])

    def claim_headers(
        self, work_item_id: str, claim_id: str, fencing_token: str
    ) -> dict[str, str]:
        return {
            **self.actor_headers("agent"),
            "X-YeYu-Gamer-Work-Item-Id": work_item_id,
            "Claim-Id": claim_id,
            "Fencing-Token": fencing_token,
        }

    def test_artifact_reads_are_actor_and_active_claim_scoped(self) -> None:
        allowed_artifact = self.create_artifact("artifact-allowed")
        other_artifact = self.create_artifact("artifact-other")
        work_item_id = self.create_work_item(
            [allowed_artifact], "artifact-work-item"
        )
        claim_id, fencing_token = self.claim_work_item(
            work_item_id, "artifact-claim"
        )
        scoped = self.claim_headers(work_item_id, claim_id, fencing_token)

        for path in (
            "/api/v1/artifacts",
            f"/api/v1/artifacts/{allowed_artifact}",
            f"/api/v1/artifacts/{allowed_artifact}/content",
        ):
            with self.subTest(actor="anonymous", path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
            for actor in ("tray", "tray-lifecycle", "rabiroute"):
                with self.subTest(actor=actor, path=path):
                    self.assertEqual(
                        self.client.get(path, headers=self.actor_headers(actor)).status_code,
                        403,
                    )
            with self.subTest(actor="agent-without-claim", path=path):
                self.assertEqual(
                    self.client.get(
                        path, headers=self.actor_headers("agent")
                    ).status_code,
                    403,
                )

        listed = self.client.get("/api/v1/artifacts", headers=scoped)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["artifactId"] for item in listed.json()["items"]],
            [allowed_artifact],
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/artifacts/{allowed_artifact}", headers=scoped
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/artifacts/{allowed_artifact}/content", headers=scoped
            ).status_code,
            200,
        )
        for suffix in ("", "/content"):
            denied = self.client.get(
                f"/api/v1/artifacts/{other_artifact}{suffix}", headers=scoped
            )
            self.assertEqual(denied.status_code, 404, denied.text)

        stale = {
            **scoped,
            "Fencing-Token": "x" * 32,
        }
        self.assertEqual(
            self.client.get(
                f"/api/v1/artifacts/{allowed_artifact}", headers=stale
            ).status_code,
            409,
        )
        wrong_work_item = {
            **scoped,
            "X-YeYu-Gamer-Work-Item-Id": "work-item-other",
        }
        self.assertEqual(
            self.client.get(
                f"/api/v1/artifacts/{allowed_artifact}", headers=wrong_work_item
            ).status_code,
            409,
        )

        cli_headers = self.actor_headers("cli")
        self.assertGreaterEqual(
            self.client.get("/api/v1/artifacts", headers=cli_headers).json()["total"],
            2,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/artifacts/{other_artifact}", headers=cli_headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/artifacts/{other_artifact}/content", headers=cli_headers
            ).status_code,
            200,
        )

        manager = self.client.app.state.manager
        manager.store.connection.execute(
            "UPDATE work_item_claims SET expires_at = ? WHERE claim_id = ?",
            ("2000-01-01T00:00:00Z", claim_id),
        )
        expired = self.client.get(
            f"/api/v1/artifacts/{allowed_artifact}", headers=scoped
        )
        self.assertEqual(expired.status_code, 409, expired.text)

        parameters = self.client.app.openapi()["paths"][
            "/api/v1/artifacts/{artifact_id}"
        ]["get"]["parameters"]
        header_names = {
            parameter["name"].lower()
            for parameter in parameters
            if parameter["in"] == "header"
        }
        self.assertTrue(
            {
                "x-yeyu-gamer-work-item-id",
                "claim-id",
                "fencing-token",
            }.issubset(header_names)
        )

    def test_diagnose_work_item_includes_same_scope_todo_evidence_once(self) -> None:
        evidence_id = self.create_artifact("todo-evidence")
        self.create_work_item([], "todo-reconcile-work-item")
        manager = self.client.app.state.manager
        todo = next(
            item
            for item in manager.list_todo_instances(
                game_id="StarRail", cadence="daily", current=True, limit=1000
            )
            if item.status != "completed"
        )
        manager.store.transition_todo_instance(
            todo.todo_instance_id,
            status="blocked",
            reason="test evidence projection",
            evidence_refs=[evidence_id],
            run_id=None,
            increment_attempt=False,
            requested_by="test",
        )

        response = self.client.post(
            "/api/v1/agent/work-items",
            json={
                "kind": "diagnose_game",
                "gameId": "StarRail",
                "artifactRefs": [evidence_id],
            },
            headers=self.mutation_headers("agent", "todo-evidence-work-item"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        refs = response.json()["result"]["workItem"]["artifactRefs"]
        self.assertEqual(refs, [evidence_id])

        unsafe = self.client.post(
            "/api/v1/agent/work-items",
            json={
                "kind": "diagnose_game",
                "gameId": "StarRail",
                "artifactRefs": ["../artifact.json"],
            },
            headers=self.mutation_headers("agent", "unsafe-artifact-ref"),
        )
        self.assertEqual(unsafe.status_code, 422, unsafe.text)
        self.assertIn("opaque IDs", unsafe.text)

    def test_content_serves_the_verified_bytes_and_rejects_oversize_or_reparse(self) -> None:
        artifact_id = self.create_artifact("verified-content")
        manager = self.client.app.state.manager
        resource = manager.store.get_resource("artifact", artifact_id)
        artifact_path = (
            self.settings.data_dir
            / "artifacts"
            / resource["document"]["relativePath"]
        )
        original_bytes = artifact_path.read_bytes()
        original_reader = manager.artifact_content

        def read_then_replace(*args: object, **kwargs: object):
            verified = original_reader(*args, **kwargs)
            artifact_path.write_bytes(b"replacement-after-verification")
            return verified

        with mock.patch.object(
            manager, "artifact_content", side_effect=read_then_replace
        ):
            response = self.client.get(
                f"/api/v1/artifacts/{artifact_id}/content",
                headers=self.actor_headers("cli"),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, original_bytes)
        self.assertNotEqual(response.content, artifact_path.read_bytes())

        oversized_path = self.settings.data_dir / "artifacts" / "oversized.bin"
        oversized_path.parent.mkdir(parents=True, exist_ok=True)
        oversized_path.write_bytes(b"")
        with oversized_path.open("r+b") as stream:
            stream.truncate(25 * 1024 * 1024 + 1)
        oversized = manager.store.create_resource(
            "artifact",
            state="captured",
            document={
                "kind": "diagnostic",
                "capturedAt": "2026-08-28T00:00:00Z",
                "source": "security-test",
                "raw": True,
                "contentType": "application/octet-stream",
                "hash": "0" * 64,
                "sizeBytes": oversized_path.stat().st_size,
                "fileName": "oversized.bin",
                "relativePath": "oversized.bin",
            },
        )
        too_large = self.client.get(
            f"/api/v1/artifacts/{oversized['resource_id']}/content",
            headers=self.actor_headers("cli"),
        )
        self.assertEqual(too_large.status_code, 409, too_large.text)

        artifact_path.write_bytes(original_bytes)
        with mock.patch(
            "yeyu_gamer_manager.services.manager._has_reparse_point",
            side_effect=lambda path: Path(path) == artifact_path,
        ):
            reparse = self.client.get(
                f"/api/v1/artifacts/{artifact_id}/content",
                headers=self.actor_headers("cli"),
            )
        self.assertEqual(reparse.status_code, 404, reparse.text)

    def test_automatic_todo_evidence_projection_is_capped_at_fifty(self) -> None:
        self.create_work_item([], "todo-cap-reconcile")
        manager = self.client.app.state.manager
        evidence_ids = []
        for index in range(55):
            resource = manager.store.create_resource(
                "artifact",
                state="captured",
                document={
                    "kind": "diagnostic",
                    "capturedAt": "2026-08-28T00:00:00Z",
                    "source": "test",
                    "raw": True,
                    "contentType": "application/json",
                    "hash": f"test-{index}",
                    "sizeBytes": 0,
                    "fileName": f"test-{index}.json",
                },
            )
            evidence_ids.append(str(resource["resource_id"]))
        todo = next(
            item
            for item in manager.list_todo_instances(
                game_id="StarRail", cadence="daily", current=True, limit=1000
            )
            if item.status != "completed"
        )
        manager.store.transition_todo_instance(
            todo.todo_instance_id,
            status="blocked",
            reason="test fifty artifact cap",
            evidence_refs=evidence_ids,
            run_id=None,
            increment_attempt=False,
            requested_by="test",
        )

        response = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "StarRail"},
            headers=self.mutation_headers("agent", "todo-cap-work-item"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        refs = response.json()["result"]["workItem"]["artifactRefs"]
        self.assertEqual(refs, evidence_ids[:50])
        self.assertEqual(len(refs), 50)


if __name__ == "__main__":
    unittest.main()
