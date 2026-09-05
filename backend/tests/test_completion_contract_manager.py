from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.settings import Settings
from yeyu_gamer_manager.services.artifact_integrity import (
    ArtifactIntegrityResult,
)
from yeyu_gamer_manager.store.sqlite_store import RecordNotFound


def _unique_png_bytes(fixture_id: str) -> bytes:
    """Build a valid one-pixel PNG whose bytes are unique to this fixture."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", checksum)
        )

    pixel = hashlib.sha256(fixture_id.encode("utf-8")).digest()[:3]
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
            chunk(b"tEXt", b"fixtureArtifact\x00" + fixture_id.encode("ascii")),
            chunk(b"IDAT", zlib.compress(b"\x00" + pixel)),
            chunk(b"IEND", b""),
        )
    )


class CompletionContractManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="yeyu-gamer-completion-contract-"
        )
        self.root = Path(self.temporary.name)
        self.settings = Settings.for_test(self.root)
        self.settings.legacy_root.mkdir(parents=True)
        (self.settings.legacy_root / "daily-gui-config.json").write_text(
            json.dumps(
                {
                    "order": ["StarRail", "ZZZ"],
                    "enabled": {"StarRail": True, "ZZZ": True},
                    "dailyScheduleEnabled": False,
                    "weeklyEnabled": False,
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
                    "games": {"StarRail": {}, "ZZZ": {}},
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
        self.manager = self.client.app.state.manager
        self.store = self.manager.store
        self.store.update_notification_policy(
            {"enabled": False, "automatic_dispatch": False},
            requested_by="completion-contract-test",
        )
        self.all_todos = [
            item.model_dump(mode="json", by_alias=True)
            for item in self.manager.list_todo_instances(
                game_id="StarRail",
                cadence="daily",
                current=True,
                limit=1000,
            )
        ]
        self.required_todos = [item for item in self.all_todos if item["required"]]
        self.assertEqual(len(self.required_todos), 5)
        self.game_day_key = self.required_todos[0]["periodKey"]
        self.todo_plans = self.manager._todo_plans_for_games(["StarRail"], "daily")
        todo_scope = self.manager._batch_todo_scope(
            self.todo_plans, ["StarRail"]
        )
        self.batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["StarRail"],
                "requested_by": "completion-contract-test",
                "result": {
                    "gameDay": self.game_day_key,
                    "candidateGameIds": ["StarRail"],
                    "todoScope": todo_scope,
                    "todoPlans": self.todo_plans,
                    "deferredGameIds": [],
                },
            }
        )
        self.run, self.run_attempt, self.screenshot_id = self._completed_run()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def _actor_headers(self, actor: str = "agent") -> dict[str, str]:
        token = (
            self.settings.actor_tokens_dir
            / ("agent.yeyu.token" if actor == "agent" else f"{actor}.token")
        ).read_text(encoding="ascii").strip()
        return {
            "Authorization": f"Bearer {token}",
            "X-YeYu-Gamer-Actor": actor,
        }

    def test_legacy_ww_machine_review_is_downgraded_not_accepted(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        record = self.manager._completion_review_record(
            {
                "resource_id": str(uuid.uuid4()),
                "created_at": now,
                "document": {
                    "workItemId": str(uuid.uuid4()),
                    "claimId": str(uuid.uuid4()),
                    "decisionId": str(uuid.uuid4()),
                    "reviewerPrincipalId": "adapter:openkuro",
                    "decision": "accepted",
                    "gameId": "WW",
                    "runId": str(uuid.uuid4()),
                    "runAttemptId": str(uuid.uuid4()),
                    "gameDayKey": "daily:legacy-ww",
                    "predicates": [
                        {
                            "type": "ww-reward-claim-screenshot-pair",
                            "rawArtifactRef": "legacy-raw-artifact",
                            "watermarkedArtifactRef": "legacy-watermark-artifact",
                        }
                    ],
                    "artifactRefs": [
                        "legacy-raw-artifact",
                        "legacy-watermark-artifact",
                    ],
                    "reviewedAt": now,
                },
            }
        )

        self.assertEqual(record.decision, "review_required")
        self.assertEqual(len(record.predicates), 1)
        self.assertEqual(
            record.predicates[0].predicate_id,
            "legacy-invalid-ww-reward-claim-screenshot-pair",
        )
        self.assertTrue(record.predicates[0].metrics["legacySchemaInvalid"])

    def _mutation_headers(self, key: str) -> dict[str, str]:
        actor = self._actor_headers()
        version = self.client.get(
            "/api/v1/snapshot", headers=actor
        ).json()["stateVersion"]
        return {
            **actor,
            "Idempotency-Key": key,
            "If-Match": str(version),
            "X-Expected-State-Version": str(version),
        }

    def _completed_run(self) -> tuple[dict[str, Any], dict[str, Any], str]:
        todo_ids = [item["todoInstanceId"] for item in self.required_todos]
        completion_todo_ids = list(
            self.todo_plans["StarRail"]["completionTodoInstanceIds"]
        )
        run = self.store.create_game_run(
            {
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "running",
                "mode": "execute",
                "requested_by": "completion-contract-test",
                "message": "typed completion fixture",
                "todo_instance_ids": todo_ids,
                "completion_todo_instance_ids": completion_todo_ids,
            }
        )
        run_attempt_id = str(uuid.uuid4())
        attempt = self.store.create_run_attempt(
            {
                "run_attempt_id": run_attempt_id,
                "run_id": run["run_id"],
                "game_id": "StarRail",
                "cadence": "daily",
                "fencing_token_hash": hashlib.sha256(
                    b"completion-contract-fixture-token"
                ).hexdigest(),
                "cancel_authority_hash": "sha256:"
                + hashlib.sha256(b"completion-contract-cancel-authority").hexdigest(),
                "plan": {
                    "protocolVersion": "1.1",
                    "runId": run["run_id"],
                    "runAttemptId": run_attempt_id,
                    "gameId": "StarRail",
                    "cadence": "daily",
                    "executableTodoInstanceIds": todo_ids,
                    "todos": [
                        {
                            "todoInstanceId": item["todoInstanceId"],
                            "todoDefinitionId": item["todoDefinitionId"],
                            "operation": item["operation"],
                            "risk": item["risk"],
                        }
                        for item in self.required_todos
                    ],
                },
            }
        )
        self.store.update_run_attempt(
            run_attempt_id, state="running", completed=False
        )
        screenshot_id = ""
        screenshot_ids: list[str] = []
        screenshot_by_todo: dict[str, str] = {}
        for index, todo in enumerate(self.required_todos, start=1):
            todo_attempt = self.store.start_todo_attempt(
                {
                    "todo_attempt_id": str(uuid.uuid4()),
                    "run_attempt_id": run_attempt_id,
                    "run_id": run["run_id"],
                    "todo_instance_id": todo["todoInstanceId"],
                    "attempt_number": 1,
                    "operation": todo["operation"],
                }
            )
            artifact_id = str(uuid.uuid4())
            is_final_panel = todo["operation"] == "verify-daily-task-list"
            artifact_bytes = _unique_png_bytes(artifact_id)
            relative_path = f"{artifact_id}.png"
            artifact_path = self.settings.data_dir / "artifacts" / relative_path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(artifact_bytes)
            self.store.create_resource(
                "artifact",
                resource_id=artifact_id,
                state="captured",
                document={
                    "schemaVersion": 1,
                    "kind": (
                        "game-ui-daily-training-panel"
                        if is_final_panel
                        else "game-ui-step-after-raw"
                    ),
                    "contentType": "image/png",
                    "gameId": "StarRail",
                    "runId": run["run_id"],
                    "runAttemptId": run_attempt_id,
                    "todoAttemptId": todo_attempt["todo_attempt_id"],
                    "todoInstanceId": todo["todoInstanceId"],
                    "gameDayKey": todo["periodKey"],
                    "capturedAt": datetime.now(timezone.utc).isoformat(),
                    "hash": hashlib.sha256(artifact_bytes).hexdigest(),
                    "sizeBytes": len(artifact_bytes),
                    "source": "manager-adapter-v1.1-test",
                    "raw": True,
                    "verdict": "unreviewed",
                    "fileName": relative_path or f"artifact-{artifact_id}",
                    "relativePath": relative_path,
                },
            )
            self.store.finish_todo_attempt(
                todo_attempt["todo_attempt_id"],
                status="completed",
                reason_code="fixture_completed",
                reason="fixture Todo completed with owned evidence",
                retryable=False,
                evidence_refs=[artifact_id],
            )
            if is_final_panel:
                screenshot_id = artifact_id
            screenshot_ids.append(artifact_id)
            screenshot_by_todo[todo["todoInstanceId"]] = artifact_id
        self.assertTrue(screenshot_id)
        self.screenshot_ids = screenshot_ids
        self.screenshot_by_todo = screenshot_by_todo
        attempt = self.store.update_run_attempt(
            run_attempt_id,
            state="completed",
            exit_code=0,
            result={"fixture": True},
            completed=True,
        )
        self.store.update_game_run(
            run["run_id"],
            state="review_required",
            exit_code=0,
            message="awaiting typed Agent completion review",
            completed_todo_instance_ids=todo_ids,
        )
        return run, attempt, screenshot_id

    def _claim_review_work_item(self) -> tuple[str, str, str]:
        batch = self.store.get_batch(self.batch["batch_id"])
        work_id = batch["result"]["completionReviewWorkItemIds"][
            self.run["run_id"]
        ]
        claim = self.client.post(
            f"/api/v1/agent/work-items/{work_id}/claims",
            json={"claimant": "ignored-by-route-binding", "leaseSeconds": 120},
            headers=self._mutation_headers(f"completion-claim-{uuid.uuid4()}"),
        )
        self.assertEqual(claim.status_code, 202, claim.text)
        claim_record = claim.json()["result"]["claim"]
        return work_id, claim_record["claimId"], claim_record["fencingToken"]

    def _completion_review(self, *, run_attempt_id: str, game_day_key: str):
        return {
            "gameId": "StarRail",
            "runId": self.run["run_id"],
            "runAttemptId": run_attempt_id,
            "gameDayKey": game_day_key,
            "predicates": [
                {
                    "predicateId": "starrail.daily_training.points",
                    "metrics": {"current": 500, "target": 500},
                    "artifactRefs": [self.screenshot_id],
                },
                {
                    "predicateId": "starrail.daily_training.reward_tiers",
                    "metrics": {"claimed": 5, "total": 5},
                    "artifactRefs": [self.screenshot_id],
                },
            ],
            "todoReviews": [
                {
                    "todoInstanceId": todo["todoInstanceId"],
                    "verdict": "confirmed",
                    "reasonCode": "visual_completion_confirmed",
                    "artifactRefs": [
                        self.screenshot_by_todo[todo["todoInstanceId"]]
                    ],
                }
                for todo in self.required_todos
            ],
        }

    def _begin_review_phase(self) -> dict[str, Any]:
        return self.manager._begin_completion_review_phase(
            batch_id=self.batch["batch_id"],
            initial_result=dict(self.store.get_batch(self.batch["batch_id"])["result"]),
            game_ids=["StarRail"],
            cadence="daily",
            state="review_required",
            completed_run_ids=[self.run["run_id"]],
            failed_run_ids=[],
            final_run_ids=[self.run["run_id"]],
            reason="fixture seal",
            timed_out=False,
        )

    def test_ww_promoted_review_requires_raw_and_watermarked_reward_pair(self) -> None:
        plan = SimpleNamespace(
            run_id="ww-run",
            run_attempt_id="ww-attempt",
            game_id="WW",
            executable_todo_instance_ids=("ww-claim",),
            todos=(SimpleNamespace(todo_instance_id="ww-claim", operation="claim-daily-reward"),),
        )
        attempt = {
            "todo_instance_id": "ww-claim",
            "state": "completed",
            "evidence_refs": ["ww-raw"],
        }
        raw_resource = {
            "document": {
                "kind": "game-ui-daily-reward-raw",
                "contentType": "image/png",
                "gameId": "WW",
                "runId": "ww-run",
                "runAttemptId": "ww-attempt",
                "raw": True,
            }
        }
        with (
            mock.patch.object(self.store, "list_todo_attempts", return_value=[attempt]),
            mock.patch.object(self.store, "get_resource", return_value=raw_resource),
        ):
            review = self.manager._ensure_promoted_adapter_completion_review(plan)

        self.assertIsNone(review)

    def test_ww_generic_before_after_pair_cannot_auto_accept_completion(self) -> None:
        plan = SimpleNamespace(
            run_id="ww-run",
            run_attempt_id="ww-attempt",
            game_id="WW",
            executable_todo_instance_ids=("ww-claim",),
            todos=(SimpleNamespace(todo_instance_id="ww-claim", operation="claim-daily-reward"),),
        )
        evidence_refs = [
            "ww-before",
            "ww-after",
            "ww-watermarked",
            "ww-activity-100",
            "ww-log",
        ]
        attempt = {
            "todo_instance_id": "ww-claim",
            "state": "completed",
            "evidence_refs": evidence_refs,
        }
        kinds = {
            "ww-before": ("game-ui-daily-reward-before", True, "image/png"),
            "ww-after": ("game-ui-daily-reward-raw", True, "image/png"),
            "ww-watermarked": ("game-ui-daily-reward-watermarked", False, "image/png"),
            "ww-activity-100": ("game-ui-daily-activity-100", True, "text/plain"),
            "ww-log": ("tool-log-outcome", True, "text/plain"),
        }
        resources = {
            artifact_id: {
                "resource_id": artifact_id,
                "created_at": datetime.now(timezone.utc),
                "document": {
                    "kind": kind,
                    "contentType": content_type,
                    "gameId": "WW",
                    "runId": "ww-run",
                    "runAttemptId": "ww-attempt",
                    "raw": raw,
                },
            }
            for artifact_id, (kind, raw, content_type) in kinds.items()
        }

        def get_resource(resource_type: str, resource_id: str):
            if resource_type == "artifact":
                return resources[resource_id]
            raise RecordNotFound(resource_id)

        def create_resource(resource_type: str, *, resource_id: str, state: str, document: dict[str, Any]):
            self.assertEqual(resource_type, "completion-review")
            return {
                "resource_id": resource_id,
                "state": state,
                "document": document,
                "created_at": datetime.now(timezone.utc),
            }

        with (
            mock.patch.object(self.store, "list_todo_attempts", return_value=[attempt]),
            mock.patch.object(self.store, "get_resource", side_effect=get_resource),
            mock.patch.object(self.store, "create_resource", side_effect=create_resource),
            mock.patch.object(self.store, "get_game_run", return_value={"game_id": "WW"}),
            mock.patch.object(
                self.manager,
                "_frozen_todos_for_run",
                return_value=[SimpleNamespace(todo_instance_id="ww-claim", period_key="2026-08-31")],
            ),
            mock.patch.object(
                self.manager,
                "_completion_artifact_integrity",
                side_effect=lambda artifact_id, _document: ArtifactIntegrityResult(
                    valid=True,
                    reason_code="verified",
                    content_hash=hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
                    content=(
                        b"dailyActivityPoints=100"
                        if artifact_id == "ww-activity-100"
                        else b"\x89PNG\r\n\x1a\n"
                    ),
                ),
            ),
        ):
            review = self.manager._ensure_promoted_adapter_completion_review(plan)

        self.assertIsNone(review)

    def _promoted_lineage_fixture(self):
        now = datetime.now(timezone.utc)
        prior_attempt_id = "starrail-prior-attempt"
        current_attempt_id = "starrail-current-attempt"
        prior_run_id = "starrail-prior-run"
        current_run_id = "starrail-current-run"
        definitions = [
            ("starrail-attach", "attach-home", current_attempt_id, current_run_id),
            (
                "starrail-objectives",
                "daily-training-objectives",
                prior_attempt_id,
                prior_run_id,
            ),
            (
                "starrail-claim",
                "claim-daily-training-rewards",
                prior_attempt_id,
                prior_run_id,
            ),
        ]
        frozen_todos = [
            SimpleNamespace(
                todo_instance_id=todo_id,
                operation=operation,
                required=True,
                period_key="2026-09-01",
                status="completed",
            )
            for todo_id, operation, _, _ in definitions
        ]
        evidence = []
        facts = []
        for todo_id, operation, attempt_id, owner_run_id in definitions:
            refs = []
            kinds = [
                ("game-ui-step-before-raw", True, "image/png"),
                ("game-ui-step-after-watermarked", False, "image/png"),
            ]
            if operation == "claim-daily-training-rewards":
                kinds.extend(
                    [
                        ("game-ui-daily-reward-raw", True, "image/png"),
                        (
                            "game-ui-daily-reward-watermarked",
                            False,
                            "image/png",
                        ),
                    ]
                )
            for index, (kind, raw, content_type) in enumerate(kinds):
                artifact_id = f"{todo_id}-{index}"
                refs.append(artifact_id)
                evidence.append(
                    SimpleNamespace(
                        artifact_id=artifact_id,
                        kind=kind,
                        content_type=content_type,
                        captured_at=now,
                        raw=raw,
                        game_id="StarRail",
                        run_id=owner_run_id,
                        run_attempt_id=attempt_id,
                        todo_instance_id=todo_id,
                        game_day_key="2026-09-01",
                        is_screenshot=content_type.startswith("image/"),
                    )
                )
            facts.append(
                SimpleNamespace(
                    todo_instance_id=todo_id,
                    required=True,
                    status="completed",
                    run_id=owner_run_id,
                    run_attempt_id=attempt_id,
                    evidence_refs=tuple(refs),
                )
            )
        current_attempt = SimpleNamespace(
            run_attempt_id=current_attempt_id,
            run_id=current_run_id,
            state="completed",
        )
        prior_attempt = SimpleNamespace(
            run_attempt_id=prior_attempt_id,
            run_id=prior_run_id,
            state="completed",
        )
        snapshot = SimpleNamespace(
            game_id="StarRail",
            run_id=current_run_id,
            current_attempt=current_attempt,
            attempt_lineage=(current_attempt, prior_attempt),
            todos=tuple(facts),
            evidence=tuple(evidence),
            game_day=SimpleNamespace(
                period_key="2026-09-01",
                starts_at=now - timedelta(hours=1),
                ends_at=now + timedelta(hours=1),
            ),
        )
        return current_run_id, current_attempt_id, frozen_todos, snapshot

    def test_starrail_promoted_lineage_waits_for_visual_agent_review(self) -> None:
        run_id, attempt_id, frozen_todos, snapshot = self._promoted_lineage_fixture()
        created: dict[str, Any] = {}

        def get_resource(resource_type: str, resource_id: str):
            raise RecordNotFound(resource_id)

        def create_resource(
            resource_type: str,
            *,
            resource_id: str,
            state: str,
            document: dict[str, Any],
        ):
            created.update(document)
            return {
                "resource_id": resource_id,
                "state": state,
                "document": document,
                "created_at": datetime.now(timezone.utc),
            }

        with (
            mock.patch.object(
                self.store, "get_game_run", return_value={"game_id": "StarRail"}
            ),
            mock.patch.object(
                self.manager, "_completion_contract_snapshot", return_value=snapshot
            ),
            mock.patch.object(
                self.manager, "_frozen_todos_for_run", return_value=frozen_todos
            ),
            mock.patch.object(self.store, "get_resource", side_effect=get_resource),
            mock.patch.object(
                self.store, "create_resource", side_effect=create_resource
            ),
        ):
            review = self.manager._ensure_promoted_adapter_lineage_completion_review(
                run_id, expected_run_attempt_id=attempt_id
            )

        self.assertIsNone(review)
        self.assertEqual(created, {})

    def test_completion_review_scope_includes_cross_run_lineage_screenshots(self) -> None:
        run_id, _, frozen_todos, snapshot = self._promoted_lineage_fixture()
        with (
            mock.patch.object(
                self.store, "get_game_run", return_value={"game_id": "StarRail"}
            ),
            mock.patch.object(
                self.manager, "_completion_contract_snapshot", return_value=snapshot
            ),
            mock.patch.object(
                self.manager, "_frozen_todos_for_run", return_value=frozen_todos
            ),
            mock.patch.object(
                self.manager,
                "_completion_policy_context",
                return_value=(SimpleNamespace(), SimpleNamespace(), "supported", None),
            ),
            mock.patch(
                "yeyu_gamer_manager.services.manager.policy_review_contract",
                return_value={},
            ),
        ):
            _, screenshots, _ = self.manager._completion_review_scope(
                batch_id="lineage-batch", run_id=run_id
            )

        self.assertEqual(len(screenshots), 8)
        self.assertTrue(any(value.startswith("starrail-objectives") for value in screenshots))
        self.assertTrue(any(value.startswith("starrail-attach") for value in screenshots))

    def test_missing_review_cannot_seal_accepted_done(self) -> None:
        awaiting = self._begin_review_phase()
        self.assertEqual(awaiting["state"], "review_required")
        self.assertNotIn("sealVersion", awaiting["result"])
        self.assertEqual(
            awaiting["result"]["awaitingCompletionReviewRunIds"],
            [self.run["run_id"]],
        )
        self.assertFalse(awaiting["result"]["acceptedDone"])
        self.assertEqual(self.store.list_notification_deliveries(), [])
        work_item_id = awaiting["result"]["completionReviewWorkItemIds"][
            self.run["run_id"]
        ]
        self.assertEqual(
            self.store.get_work_item(work_item_id)["artifact_refs"],
            self.screenshot_ids,
        )
        self.manager._recover_completion_review_phases()
        self.manager._recover_completion_review_phases()
        self.assertEqual(
            len(
                [
                    item
                    for item in self.store.list_work_items(100)
                    if item["run_id"] == self.run["run_id"]
                    and item["kind"] == "evidence_review"
                ]
            ),
            1,
        )
        self.assertNotIn(
            "sealVersion", self.store.get_batch(self.batch["batch_id"])["result"]
        )
        self.assertEqual(self.store.list_notification_deliveries(), [])

    def test_review_work_item_exposes_structured_required_todo_scope(self) -> None:
        awaiting = self._begin_review_phase()
        work_item_id = awaiting["result"]["completionReviewWorkItemIds"][
            self.run["run_id"]
        ]
        work_item = self.store.get_work_item(work_item_id)
        scope = work_item["result"]["completionReviewScope"]
        contract = work_item["result"]["completionReviewContract"]

        self.assertEqual(scope["schemaVersion"], 3)
        self.assertEqual(
            set(scope["requiredTodoInstanceIds"]),
            {item["todoInstanceId"] for item in self.required_todos},
        )
        self.assertEqual(len(scope["requiredTodos"]), len(self.required_todos))
        for item in scope["requiredTodos"]:
            self.assertTrue(item["operation"])
            self.assertEqual(item["status"], "completed")
            self.assertEqual(
                item["artifactRefs"],
                [self.screenshot_by_todo[item["todoInstanceId"]]],
            )
        self.assertEqual(
            contract["todoReviewContract"],
            {
                "requiredForAccepted": True,
                "acceptedVerdict": "confirmed",
                "reasonCodeRequired": True,
                "artifactRefsRequired": True,
                "allowedEvidenceContentTypes": ["image/jpeg", "image/png"],
            },
        )

    def test_openapi_describes_structured_todo_review_input(self) -> None:
        schemas = self.client.app.openapi()["components"]["schemas"]
        submission = schemas["CompletionReviewSubmission"]
        todo_reviews = submission["properties"]["todoReviews"]
        self.assertEqual(
            todo_reviews["items"]["$ref"],
            "#/components/schemas/AgentTodoReview",
        )
        todo_review = schemas["AgentTodoReview"]
        self.assertEqual(
            set(todo_review["required"]),
            {"todoInstanceId", "verdict", "reasonCode", "artifactRefs"},
        )

    def test_generic_accepted_review_without_todo_verdicts_is_rejected(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        review = self._completion_review(
            run_attempt_id=self.run_attempt["run_attempt_id"],
            game_day_key=self.game_day_key,
        )
        review.pop("todoReviews")

        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "generic game-level review has no per-Todo semantics",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": review,
            },
            headers=self._mutation_headers(
                f"completion-generic-review-{uuid.uuid4()}"
            ),
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("cover every frozen required Todo", response.text)
        self.assertEqual(
            self.store.list_resources("completion-review", 100), []
        )
        self.assertNotIn(
            "sealVersion", self.store.get_batch(self.batch["batch_id"])["result"]
        )

    def test_accepted_review_with_unconfirmed_todo_is_rejected(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        review = self._completion_review(
            run_attempt_id=self.run_attempt["run_attempt_id"],
            game_day_key=self.game_day_key,
        )
        review["todoReviews"][0] = {
            **review["todoReviews"][0],
            "verdict": "review_required",
            "reasonCode": "daily_visual_evidence_missing",
        }

        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "one Todo still lacks semantic visual proof",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": review,
            },
            headers=self._mutation_headers(
                f"completion-unconfirmed-todo-{uuid.uuid4()}"
            ),
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("every Todo verdict confirmed", response.text)
        self.assertEqual(
            self.store.list_resources("completion-review", 100), []
        )

    def test_byte_identical_screenshots_cannot_claim_two_todo_semantics(self) -> None:
        first_id, second_id = self.screenshot_ids[:2]
        first = self.store.get_resource("artifact", first_id)
        second = self.store.get_resource("artifact", second_id)
        first_bytes = (
            self.settings.data_dir
            / "artifacts"
            / first["document"]["relativePath"]
        ).read_bytes()
        second_path = (
            self.settings.data_dir
            / "artifacts"
            / second["document"]["relativePath"]
        )
        second_path.write_bytes(first_bytes)
        self.store.update_resource(
            "artifact",
            second_id,
            state=second["state"],
            document={
                **second["document"],
                "hash": hashlib.sha256(first_bytes).hexdigest(),
                "sizeBytes": len(first_bytes),
            },
        )

        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "identical frame bytes are presented as two operations",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": self._completion_review(
                    run_attempt_id=self.run_attempt["run_attempt_id"],
                    game_day_key=self.game_day_key,
                ),
            },
            headers=self._mutation_headers(
                f"completion-duplicate-semantics-{uuid.uuid4()}"
            ),
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("byte-identical screenshots", response.text)
        self.assertEqual(
            self.store.list_resources("completion-review", 100), []
        )

    def test_failed_attempt_does_not_hold_the_review_barrier(self) -> None:
        # A run whose Adapter attempt failed can never be accepted by an Agent
        # review, so the batch (and its round mail) seals immediately while the
        # evidence_review work item is still created for diagnosis.
        with mock.patch.object(
            type(self.manager),
            "_completion_review_can_change_outcome",
            return_value=False,
        ):
            sealed = self._begin_review_phase()
        self.assertIsNotNone(sealed["result"].get("sealVersion"))
        self.assertEqual(sealed["result"]["awaitingCompletionReviewRunIds"], [])
        self.assertFalse(sealed["result"]["acceptedDone"])
        self.assertEqual(sealed["result"]["notificationOutcome"], "blocked")
        self.assertEqual(
            sealed["result"]["completionReviewPhase"]["status"], "completed"
        )
        self.assertIn(
            self.run["run_id"], sealed["result"]["completionReviewWorkItemIds"]
        )
        self.assertEqual(len(self.store.list_notification_deliveries()), 1)

    def test_review_barrier_expires_into_machine_seal(self) -> None:
        awaiting = self._begin_review_phase()
        self.assertNotIn("sealVersion", awaiting["result"])
        phase = awaiting["result"]["completionReviewPhase"]
        self.assertEqual(phase["status"], "awaiting")
        self.assertIn("barrierStartedAt", phase)
        self.assertEqual(
            phase["barrierSeconds"], self.manager.COMPLETION_REVIEW_BARRIER_SECONDS
        )
        self.manager._recover_completion_review_phases()
        self.assertNotIn(
            "sealVersion", self.store.get_batch(self.batch["batch_id"])["result"]
        )
        expired_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.manager.COMPLETION_REVIEW_BARRIER_SECONDS + 5
        )
        with mock.patch(
            "yeyu_gamer_manager.services.manager.utc_now", return_value=expired_at
        ):
            self.manager._recover_completion_review_phases()
        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertIsNotNone(sealed["result"].get("sealVersion"))
        self.assertFalse(sealed["result"]["acceptedDone"])
        self.assertTrue(sealed["result"]["completionReviewPhase"]["barrierExpired"])
        self.assertEqual(len(self.store.list_notification_deliveries()), 1)

    def test_current_accepted_review_is_the_only_done_authority(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        payload = {
            "claimId": claim_id,
            "decision": "accepted",
            "reason": "current raw panel confirms 500/500 and all five tiers",
            "evidenceIds": self.screenshot_ids,
            "fencingToken": token,
            "completionReview": self._completion_review(
                run_attempt_id=self.run_attempt["run_attempt_id"],
                game_day_key=self.game_day_key,
            ),
        }
        decision_headers = self._mutation_headers(
            f"completion-decision-{uuid.uuid4()}"
        )
        response = self.client.post(
            "/api/v1/claims/decisions",
            json=payload,
            headers=decision_headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        review = response.json()["result"]["completionReview"]
        read = self.client.get(
            f"/api/v1/completion-reviews/{review['completionReviewId']}",
            headers=self._actor_headers(),
        )
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["runAttemptId"], self.run_attempt["run_attempt_id"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.update_resource(
                "completion-review",
                review["completionReviewId"],
                state="accepted",
                document={},
            )

        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertTrue(
            sealed["result"]["acceptedDone"],
            json.dumps(
                {
                    "completionCoverage": sealed["result"].get(
                        "completionCoverage"
                    ),
                    "unresolvedRequiredTodoIds": sealed["result"].get(
                        "unresolvedRequiredTodoIds"
                    ),
                    "completionContracts": sealed["result"].get(
                        "completionContracts"
                    ),
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        self.assertEqual(sealed["state"], "done")
        self.assertEqual(sealed["result"]["candidateGameIds"], ["StarRail"])
        self.assertEqual(sealed["result"]["unresolvedRequiredTodoIds"], [])
        completion_coverage = sealed["result"]["completionCoverage"]
        self.assertEqual(
            {item["gameId"] for item in completion_coverage},
            set(sealed["result"]["candidateGameIds"]),
        )
        self.assertEqual(len(completion_coverage), 1)
        coverage = completion_coverage[0]
        self.assertEqual(coverage["runId"], self.run["run_id"])
        self.assertEqual(coverage["scopeIntegrity"], "frozen_batch_scope")
        self.assertEqual(coverage["unresolvedRequiredTodoIds"], [])
        self.assertTrue(coverage["acceptedDone"])
        self.assertEqual(coverage["issues"], [])
        self.assertTrue(coverage["completionAdjudicationId"])
        frozen_scope = sealed["result"]["todoScope"]["games"][0]
        self.assertEqual(frozen_scope["gameId"], "StarRail")
        self.assertEqual(
            set(frozen_scope["completionTodoInstanceIds"]),
            set(self.todo_plans["StarRail"]["completionTodoInstanceIds"]),
        )
        contract = sealed["result"]["completionContracts"][0]
        self.assertTrue(contract["acceptedDone"])
        self.assertEqual(contract["outcome"], "accepted_done")
        self.assertEqual(contract["reviewId"], review["completionReviewId"])
        self.assertEqual(
            contract["runAttemptId"], self.run_attempt["run_attempt_id"]
        )
        self.assertEqual(contract["gameDayKey"], self.game_day_key)
        self.assertEqual(contract["blockingPredicates"], [])
        self.assertEqual(contract["missingPredicates"], [])
        self.assertIn(
            self.screenshot_id, sealed["result"]["sealEvidenceArtifactIds"]
        )
        seal_version = sealed["result"]["sealVersion"]
        replay = self.client.post(
            "/api/v1/claims/decisions", json=payload, headers=decision_headers
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.manager._recover_completion_review_phases()
        self.manager._recover_completion_review_phases()
        after = self.store.get_batch(self.batch["batch_id"])
        self.assertEqual(after["result"]["sealVersion"], seal_version)
        self.assertEqual(
            len(self.store.list_resources("completion-review", 100)), 1
        )
        self.assertEqual(
            len(self.store.list_resources("completion-adjudication", 100)), 1
        )
        self.assertEqual(len(self.store.list_notification_deliveries()), 1)

    def test_accepted_review_reopens_and_rehashes_the_screenshot(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        screenshot_path = (
            self.settings.data_dir
            / "artifacts"
            / f"{self.screenshot_id}.png"
        )
        original = screenshot_path.read_bytes()
        screenshot_path.write_bytes(b"x" * len(original))

        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "the screenshot claims completion",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": self._completion_review(
                    run_attempt_id=self.run_attempt["run_attempt_id"],
                    game_day_key=self.game_day_key,
                ),
            },
            headers=self._mutation_headers(
                f"completion-tampered-{uuid.uuid4()}"
            ),
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("artifact_hash_mismatch", response.text)
        batch = self.store.get_batch(self.batch["batch_id"])
        self.assertNotIn("sealVersion", batch["result"])
        self.assertFalse(batch["result"]["acceptedDone"])

    def test_seal_rehashes_every_todo_artifact_after_review(self) -> None:
        attempts = self.store.list_todo_attempts(
            run_attempt_id=self.run_attempt["run_attempt_id"], limit=100
        )
        non_screenshot_ref = next(
            artifact_id
            for attempt in attempts
            for artifact_id in attempt["evidence_refs"]
            if artifact_id != self.screenshot_id
        )
        document = self.store.get_resource(
            "artifact", non_screenshot_ref
        )["document"]
        artifact_path = (
            self.settings.data_dir
            / "artifacts"
            / document["relativePath"]
        )
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        with mock.patch.object(
            self.manager, "_resume_completion_review_batches_for_run"
        ):
            response = self.client.post(
                "/api/v1/claims/decisions",
                json={
                    "claimId": claim_id,
                    "decision": "accepted",
                    "reason": "the final panel itself is valid",
                    "evidenceIds": self.screenshot_ids,
                    "fencingToken": token,
                    "completionReview": self._completion_review(
                        run_attempt_id=self.run_attempt["run_attempt_id"],
                        game_day_key=self.game_day_key,
                    ),
                },
                headers=self._mutation_headers(
                    f"completion-seal-rehash-{uuid.uuid4()}"
                ),
            )

        self.assertEqual(response.status_code, 202, response.text)
        original = artifact_path.read_bytes()
        artifact_path.write_bytes(b"x" * len(original))
        self.manager._recover_completion_review_phases()
        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertFalse(sealed["result"]["acceptedDone"])
        contract = sealed["result"]["completionContracts"][0]
        self.assertIn(
            "evidence_entity_integrity", contract["blockingPredicates"]
        )
        excluded = next(
            item
            for item in contract["excludedEvidence"]
            if item["artifactId"] == non_screenshot_ref
        )
        self.assertEqual(
            excluded["integrityReasonCode"], "artifact_hash_mismatch"
        )

    def test_explicit_visual_verifier_failure_never_becomes_done(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "review_required",
                "reason": (
                    "daily_visual_evidence_missing; "
                    "activity500Confirmed=false; "
                    "allRewardTiersClaimed=false"
                ),
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": {
                    **self._completion_review(
                        run_attempt_id=self.run_attempt["run_attempt_id"],
                        game_day_key=self.game_day_key,
                    ),
                    "predicates": [
                        {
                            "predicateId": "starrail.daily_training.points",
                            "metrics": {"current": 0, "target": 500},
                            "artifactRefs": [self.screenshot_id],
                        },
                        {
                            "predicateId": "starrail.daily_training.reward_tiers",
                            "metrics": {"claimed": 0, "total": 5},
                            "artifactRefs": [self.screenshot_id],
                        },
                    ],
                },
            },
            headers=self._mutation_headers(
                f"completion-verifier-false-{uuid.uuid4()}"
            ),
        )

        self.assertEqual(response.status_code, 202, response.text)
        review = response.json()["result"]["completionReview"]
        self.assertEqual(review["decision"], "review_required")
        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertFalse(sealed["result"]["acceptedDone"])
        contract = sealed["result"]["completionContracts"][0]
        self.assertEqual(contract["outcome"], "review_required")
        self.assertIn(
            "agent_review_accepted", contract["missingPredicates"]
        )

    def test_rejected_current_review_seals_blocked(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        screenshot_path = self.settings.data_dir / "artifacts" / f"{self.screenshot_id}.png"
        screenshot_path.unlink()
        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "rejected",
                "reason": "the current panel does not establish accepted completion",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": self._completion_review(
                    run_attempt_id=self.run_attempt["run_attempt_id"],
                    game_day_key=self.game_day_key,
                ),
            },
            headers=self._mutation_headers(f"completion-rejected-{uuid.uuid4()}"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertEqual(sealed["state"], "blocked")
        self.assertFalse(sealed["result"]["acceptedDone"])
        self.assertEqual(
            sealed["result"]["completionContracts"][0]["outcome"], "blocked"
        )
        self.assertNotIn(
            self.screenshot_id, sealed["result"]["sealEvidenceArtifactIds"]
        )
        self.assertFalse(
            any(
                item["artifactId"] == self.screenshot_id
                for item in sealed["result"]["notificationScreenshotDecisions"]
            )
        )
        excluded = next(
            item
            for item in sealed["result"]["completionContracts"][0][
                "excludedEvidence"
            ]
            if item["artifactId"] == self.screenshot_id
        )
        self.assertEqual(
            excluded["integrityReasonCode"], "artifact_file_unavailable"
        )
        blocker = sealed["result"]["notificationBlockers"][0]
        self.assertEqual(
            blocker["screenshotUnavailableReasonCode"],
            "accepted_screenshot_excluded",
        )
        self.assertIn("artifact_kind_rejected", blocker["screenshotUnavailableReason"])
        self.assertEqual(len(self.store.list_notification_deliveries()), 1)

    def test_partial_candidate_contract_set_cannot_seal_done(self) -> None:
        candidate_game_ids = ["StarRail", "ZZZ"]
        todo_plans = self.manager._todo_plans_for_games(candidate_game_ids, "daily")
        todo_scope = self.manager._batch_todo_scope(todo_plans, candidate_game_ids)
        self.batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": candidate_game_ids,
                "requested_by": "partial-contract-test",
                "result": {
                    "gameDay": todo_scope["scopeKey"],
                    "candidateGameIds": candidate_game_ids,
                    "todoScope": todo_scope,
                    "deferredGameIds": [],
                },
            }
        )
        self.manager._begin_completion_review_phase(
            batch_id=self.batch["batch_id"],
            initial_result=dict(self.batch["result"]),
            game_ids=candidate_game_ids,
            cadence="daily",
            state="review_required",
            completed_run_ids=[self.run["run_id"]],
            failed_run_ids=[],
            final_run_ids=[self.run["run_id"]],
            reason="one frozen candidate never produced a completion contract",
            timed_out=False,
        )
        _, claim_id, token = self._claim_review_work_item()
        response = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "StarRail alone satisfies its current contract",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": self._completion_review(
                    run_attempt_id=self.run_attempt["run_attempt_id"],
                    game_day_key=self.game_day_key,
                ),
            },
            headers=self._mutation_headers(f"partial-contract-{uuid.uuid4()}"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertFalse(sealed["result"]["acceptedDone"])
        self.assertNotEqual(sealed["state"], "done")
        self.assertEqual(
            {item["gameId"] for item in sealed["result"]["completionContracts"]},
            {"StarRail"},
        )
        coverage = {
            item["gameId"]: item for item in sealed["result"]["completionCoverage"]
        }
        self.assertIn("missing_completion_contract", coverage["ZZZ"]["issues"])
        self.assertEqual(
            next(
                item
                for item in sealed["result"]["notificationBlockers"]
                if item["gameId"] == "ZZZ"
            )["screenshotUnavailableReasonCode"],
            "no_completion_contract",
        )

    def test_restart_recovers_persisted_review_phase_without_duplicates(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        with mock.patch.object(
            self.manager, "_resume_completion_review_batches_for_run"
        ):
            response = self.client.post(
                "/api/v1/claims/decisions",
                json={
                    "claimId": claim_id,
                    "decision": "accepted",
                    "reason": "persist review before simulated Manager restart",
                    "evidenceIds": self.screenshot_ids,
                    "fencingToken": token,
                    "completionReview": self._completion_review(
                        run_attempt_id=self.run_attempt["run_attempt_id"],
                        game_day_key=self.game_day_key,
                    ),
                },
                headers=self._mutation_headers(
                    f"completion-before-restart-{uuid.uuid4()}"
                ),
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotIn(
            "sealVersion", self.store.get_batch(self.batch["batch_id"])["result"]
        )
        self.assertEqual(self.store.list_notification_deliveries(), [])

        recovered = type(self.manager)(
            settings=self.settings,
            store=self.store,
            legacy_report=self.manager.legacy_report,
            adapter=self.manager.adapter,
        )
        sealed = self.store.get_batch(self.batch["batch_id"])
        self.assertEqual(sealed["state"], "done")
        self.assertTrue(sealed["result"]["acceptedDone"])
        recovered._recover_completion_review_phases()
        self.assertEqual(
            len(
                [
                    item
                    for item in self.store.list_work_items(100)
                    if item["run_id"] == self.run["run_id"]
                    and item["kind"] == "evidence_review"
                ]
            ),
            1,
        )
        self.assertEqual(
            len(self.store.list_resources("completion-review", 100)), 1
        )
        self.assertEqual(
            len(self.store.list_resources("completion-adjudication", 100)), 1
        )
        self.assertEqual(len(self.store.list_notification_deliveries()), 1)

    def test_old_attempt_and_wrong_game_day_reviews_are_rejected(self) -> None:
        self._begin_review_phase()
        _, claim_id, token = self._claim_review_work_item()
        wrong_day = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "wrong GameDay must fail closed",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": self._completion_review(
                    run_attempt_id=self.run_attempt["run_attempt_id"],
                    game_day_key="1999-01-01",
                ),
            },
            headers=self._mutation_headers(f"completion-wrong-day-{uuid.uuid4()}"),
        )
        self.assertEqual(wrong_day.status_code, 422, wrong_day.text)

        newer_id = str(uuid.uuid4())
        todo_ids = [item["todoInstanceId"] for item in self.required_todos]
        self.store.create_run_attempt(
            {
                "run_attempt_id": newer_id,
                "run_id": self.run["run_id"],
                "game_id": "StarRail",
                "cadence": "daily",
                "fencing_token_hash": "0" * 64,
                "cancel_authority_hash": "sha256:" + "1" * 64,
                "plan": {
                    "executableTodoInstanceIds": todo_ids,
                    "todos": [
                        {
                            "todoInstanceId": item["todoInstanceId"],
                            "todoDefinitionId": item["todoDefinitionId"],
                            "operation": item["operation"],
                            "risk": item["risk"],
                        }
                        for item in self.required_todos
                    ],
                },
            }
        )
        stale = self.client.post(
            "/api/v1/claims/decisions",
            json={
                "claimId": claim_id,
                "decision": "accepted",
                "reason": "old attempt must fail closed",
                "evidenceIds": self.screenshot_ids,
                "fencingToken": token,
                "completionReview": self._completion_review(
                    run_attempt_id=self.run_attempt["run_attempt_id"],
                    game_day_key=self.game_day_key,
                ),
            },
            headers=self._mutation_headers(f"completion-stale-{uuid.uuid4()}"),
        )
        self.assertEqual(stale.status_code, 422, stale.text)
        reviews = self.client.get(
            "/api/v1/completion-reviews",
            params={"runId": self.run["run_id"]},
            headers=self._actor_headers(),
        )
        self.assertEqual(reviews.status_code, 200, reviews.text)
        self.assertEqual(reviews.json()["total"], 0)


if __name__ == "__main__":
    unittest.main()
