from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.domain.todos import todo_instance_id, todo_period
from yeyu_gamer_manager.services.integration_catalog import registration_for
from yeyu_gamer_manager.services.current_completion import CurrentCompletionStatus
from yeyu_gamer_manager.services.todo_catalog import (
    CANONICAL_DEFINITION_SCHEMA,
    CATALOG_VERSION,
    DAILY_CAPABILITY,
    DEFINITION_VERSION,
    canonical_definition_document,
    catalog,
    definition_source_hash,
)
from yeyu_gamer_manager.settings import Settings
from yeyu_gamer_manager.store.sqlite_store import SqliteStore


GAME_IDS = [
    "WW",
    "PGR",
    "StarRail",
    "ZZZ",
    "Endfield",
    "GF2",
    "NTE",
    "FGO",
    "NIKKE",
    "BD2",
    "CZN",
]


class TodoCatalogTests(unittest.TestCase):
    def test_registered_daily_operations_include_optional_selectable_todos(self) -> None:
        catalog_items = catalog()
        definitions = {
            (item["game_id"], item["operation"])
            for item in catalog_items
            if item["cadence"] == "daily"
        }
        for game_id in (
            "StarRail",
            "WW",
            "Endfield",
            "GF2",
            "NTE",
            "PGR",
            "ZZZ",
            "NIKKE",
        ):
            registration = registration_for(game_id)
            self.assertIsNotNone(registration)
            self.assertFalse(
                {
                    operation
                    for operation in registration.daily_operations
                    if (game_id, operation) not in definitions
                }
            )
            registered = set(registration.daily_operations)
            self.assertFalse(
                {
                    item["operation"]
                    for item in catalog_items
                    if item["game_id"] == game_id
                    and item["cadence"] == "daily"
                    and item["automation_state"] == "source-tool-declared-unbound"
                    and item["adapter_capability_ref"] is not None
                    and item["operation"] not in registered
                },
                f"{game_id} has tool-backed daily Todos omitted by its integration",
            )

    def test_catalog_is_stable_versioned_and_covers_daily_and_weekly(self) -> None:
        definitions = catalog()
        self.assertEqual({item["game_id"] for item in definitions}, set(GAME_IDS))
        self.assertEqual(
            {(item["game_id"], item["cadence"]) for item in definitions},
            {(game_id, cadence) for game_id in GAME_IDS for cadence in ("daily", "weekly")},
        )
        self.assertEqual(
            len(definitions), len({item["todo_definition_id"] for item in definitions})
        )
        self.assertEqual(
            len(definitions),
            len(
                {
                    (item["game_id"], item["cadence"], item["operation"])
                    for item in definitions
                }
            ),
        )
        for item in definitions:
            self.assertRegex(
                item["todo_definition_id"],
                r"^todo\.v1\.[a-z0-9_-]+\.(?:daily|weekly)\.[a-z0-9-]+$",
            )
            self.assertEqual(item["definition_version"], DEFINITION_VERSION)
            self.assertEqual(item["catalog_version"], CATALOG_VERSION)
            self.assertRegex(item["source_hash"], r"^[0-9a-f]{64}$")
            self.assertTrue(item["source_refs"])
            self.assertTrue(item["operation"])
            self.assertTrue(item["title"])
            self.assertEqual(item["reset_rule"]["timezone"], "Asia/Shanghai")
            self.assertEqual(item["reset_rule"]["cadence"], item["cadence"])
            if item["cadence"] == "weekly":
                self.assertEqual(item["reset_rule"]["weekStartDay"], "Monday")
            if item["risk"] == "forbidden":
                self.assertIsNone(item["adapter_capability_ref"])
                self.assertIn(item["initial_status"], {"skipped", "review_required"})
                self.assertEqual(item["automation_state"], "forbidden-by-policy")
            self.assertNotEqual(item["automation_state"], "implemented")

    def test_source_hash_covers_the_complete_canonical_definition(self) -> None:
        definition = catalog()[0]
        canonical = canonical_definition_document(definition)

        self.assertEqual(
            set(canonical),
            {
                "schemaVersion",
                "todoDefinitionId",
                "definitionVersion",
                "gameId",
                "cadence",
                "operation",
                "title",
                "category",
                "orderIndex",
                "required",
                "risk",
                "automationDifficulty",
                "adapterCapabilityRef",
                "automationState",
                "initialStatus",
                "initialReason",
                "resetRule",
                "sourceRefs",
            },
        )
        self.assertEqual(canonical["schemaVersion"], CANONICAL_DEFINITION_SCHEMA)
        self.assertEqual(canonical["todoDefinitionId"], definition["todo_definition_id"])
        self.assertEqual(canonical["definitionVersion"], DEFINITION_VERSION)
        self.assertEqual(definition_source_hash(definition), definition["source_hash"])

        def changed_definition(**overrides: Any) -> dict[str, Any]:
            changed = json.loads(json.dumps(definition, ensure_ascii=False))
            changed.update(overrides)
            return changed

        mutations = (
            ("title", changed_definition(title=f"{definition['title']}（修订）")),
            ("category", changed_definition(category="changed-category")),
            ("required", changed_definition(required=not definition["required"])),
            ("risk", changed_definition(risk="observe_only")),
            (
                "capability",
                changed_definition(adapter_capability_ref="game.changed.run@1.0"),
            ),
            ("state", changed_definition(automation_state="changed-state")),
            (
                "reset",
                changed_definition(
                    reset_rule={**definition["reset_rule"], "time": "05:00"}
                ),
            ),
            (
                "sourceRefs",
                changed_definition(
                    source_refs=[*definition["source_refs"], "new-source.md#changed"]
                ),
            ),
        )
        for field, changed in mutations:
            with self.subTest(field=field):
                self.assertNotEqual(
                    definition_source_hash(changed), definition["source_hash"]
                )

    def test_sensitive_risks_never_expose_adapter_capabilities(self) -> None:
        sensitive_definitions = [
            item
            for item in catalog()
            if item["risk"] in {"approval_required", "forbidden"}
        ]
        self.assertTrue(sensitive_definitions)
        for item in sensitive_definitions:
            with self.subTest(todo_definition_id=item["todo_definition_id"]):
                self.assertIsNone(item["adapter_capability_ref"])
                self.assertNotEqual(item["automation_state"], "implemented")

    def test_endfield_crafting_requires_review_until_costs_are_modelled(self) -> None:
        definitions_by_operation = {
            item["operation"]: item
            for item in catalog()
            if item["game_id"] == "Endfield"
        }
        for operation in ("craft-equipment", "simple-craft"):
            item = definitions_by_operation[operation]
            with self.subTest(operation=operation):
                self.assertEqual(item["risk"], "approval_required")
                self.assertIsNone(item["adapter_capability_ref"])
                self.assertEqual(item["automation_state"], "disabled-unimplemented")
                self.assertEqual(item["initial_status"], "review_required")
                self.assertIn("资源", item["initial_reason"])
                self.assertIn("配方", item["initial_reason"])

    def test_zzz_city_fund_is_bound_to_free_claim_only(self) -> None:
        definition = next(
            item
            for item in catalog()
            if item["game_id"] == "ZZZ"
            and item["operation"] == "city-fund-free-claim"
        )
        self.assertEqual(definition["risk"], "routine_action")
        self.assertEqual(definition["adapter_capability_ref"], DAILY_CAPABILITY)
        self.assertEqual(definition["automation_state"], "source-tool-declared-unbound")
        self.assertEqual(definition["initial_status"], "pending")
        self.assertTrue(any("丽都城募.md" in ref for ref in definition["source_refs"]))

    def test_starrail_final_verifier_is_bound_as_safe_observation(self) -> None:
        definition = next(
            item
            for item in catalog()
            if item["game_id"] == "StarRail"
            and item["operation"] == "verify-daily-task-list"
        )
        self.assertEqual(definition["risk"], "observe_only")
        self.assertEqual(definition["adapter_capability_ref"], "game.daily.run@1.0")
        self.assertEqual(definition["automation_state"], "source-tool-declared-unbound")

    def test_period_and_instance_ids_are_deterministic_at_the_four_am_boundary(self) -> None:
        rule = {"timezone": "Asia/Shanghai", "time": "04:00", "cadence": "daily"}
        before = todo_period(rule, datetime(2026, 8, 27, 19, 59, tzinfo=timezone.utc))
        after = todo_period(rule, datetime(2026, 8, 27, 20, 1, tzinfo=timezone.utc))
        self.assertEqual(before["periodKey"], "2026-08-27")
        self.assertEqual(after["periodKey"], "2026-08-28")
        first = todo_instance_id("todo.v1.starrail.daily.attach-home", after["periodKey"])
        self.assertEqual(
            first,
            todo_instance_id("todo.v1.starrail.daily.attach-home", after["periodKey"]),
        )
        self.assertRegex(first, r"^todo-instance-[0-9a-f-]{36}$")


class TodoApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-gamer-todo-")
        self.root = Path(self.temporary.name)
        self.settings = Settings.for_test(self.root)
        self.settings.legacy_root.mkdir(parents=True)
        (self.settings.legacy_root / "daily-gui-config.json").write_text(
            json.dumps(
                {
                    "order": GAME_IDS,
                    "enabled": {game_id: True for game_id in GAME_IDS},
                    "dailyScheduleEnabled": False,
                    "dailyScheduleTime": "17:00",
                    "weeklyEnabled": True,
                    "weeklyDay": "Monday",
                    "dailyResetHour": 4,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.settings.legacy_root / "game-automation-policy.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "forbiddenActions": ["purchase", "draw", "account_settings"],
                    "games": {game_id: {} for game_id in GAME_IDS},
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
        self.bootstrap_webgui_session()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def actor_headers(self, actor: str = "cli") -> dict[str, str]:
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

    def bootstrap_webgui_session(self) -> None:
        issued = self.client.post(
            "/api/v1/webgui/bootstrap-nonces",
            json={},
            headers=self.actor_headers("tray"),
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        exchanged = self.client.post(
            "/api/v1/webgui/session-exchanges",
            json={"nonce": issued.json()["nonce"]},
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(exchanged.status_code, 200, exchanged.text)

    def mutation_headers(self, key: str, actor: str = "cli") -> dict[str, str]:
        version = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        return {
            **self.actor_headers(actor),
            "Idempotency-Key": key,
            "If-Match": f'"{version}"',
            "X-Expected-State-Version": str(version),
        }

    def create_artifact(self, key: str) -> str:
        response = self.client.post(
            "/api/v1/diagnostic-bundles",
            json={},
            headers=self.mutation_headers(key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["result"]["artifact"]["artifactId"])

    def transition(
        self, todo_id: str, key: str, status: str, **values: object
    ) -> dict[str, object]:
        response = self.client.post(
            f"/api/v1/todo-instances/{todo_id}/transitions",
            json={"status": status, **values},
            headers=self.mutation_headers(key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def current_items(self, game_id: str, cadence: str = "daily") -> list[dict[str, object]]:
        response = self.client.get(
            "/api/v1/todo-instances",
            params={"gameId": game_id, "cadence": cadence, "current": "true"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def complete_todos_through_fenced_attempt(
        self, items: list[dict[str, object]], label: str
    ) -> tuple[str, str, list[str]]:
        """Seed accepted Todo facts through the Manager-owned attempt ledger.

        Public Todo transitions intentionally cannot manufacture execution state.
        Tests that need a completed period therefore use the same fenced Store
        boundary that Adapter events drive, with one fresh owned artifact per
        Todo attempt.
        """

        self.assertTrue(items)
        manager = self.client.app.state.manager
        store = manager.store
        game_id = str(items[0]["gameId"])
        cadence = str(items[0]["cadence"])
        todo_ids = [str(item["todoInstanceId"]) for item in items]
        run = store.create_game_run(
            {
                "game_id": game_id,
                "cadence": cadence,
                "state": "queued",
                "mode": "execute",
                "requested_by": "todo-contract-test",
                "message": "fenced test attempt",
                "todo_instance_ids": todo_ids,
                "completion_todo_instance_ids": todo_ids,
            }
        )
        run_attempt_id = str(uuid.uuid4())
        fencing_hash = hashlib.sha256(
            f"{label}-fencing-token".encode("utf-8")
        ).hexdigest()
        plan = {
            "protocolVersion": "1.1",
            "runId": run["run_id"],
            "runAttemptId": run_attempt_id,
            "gameId": game_id,
            "cadence": cadence,
            "executableTodoInstanceIds": todo_ids,
            "todos": [
                {
                    "todoInstanceId": str(item["todoInstanceId"]),
                    "todoDefinitionId": str(item["todoDefinitionId"]),
                    "operation": str(item["operation"]),
                    "risk": str(item["risk"]),
                }
                for item in items
            ],
        }
        store.create_run_attempt(
            {
                "run_attempt_id": run_attempt_id,
                "run_id": run["run_id"],
                "game_id": game_id,
                "cadence": cadence,
                "fencing_token_hash": fencing_hash,
                "cancel_authority_hash": "sha256:" + "3" * 64,
                "plan": plan,
            }
        )
        artifact_ids: list[str] = []
        for index, item in enumerate(items, start=1):
            todo_id = str(item["todoInstanceId"])
            current = store.get_todo_instance(todo_id)
            todo_attempt_id = str(uuid.uuid4())
            store.start_todo_attempt(
                {
                    "todo_attempt_id": todo_attempt_id,
                    "run_attempt_id": run_attempt_id,
                    "run_id": run["run_id"],
                    "todo_instance_id": todo_id,
                    "attempt_number": int(current["attempts"]) + 1,
                    "operation": str(item["operation"]),
                }
            )
            artifact_id = str(uuid.uuid4())
            store.create_resource(
                "artifact",
                resource_id=artifact_id,
                state="accepted",
                document={
                    "kind": "screenshot",
                    "contentType": "image/png",
                    "source": "manager-adapter-v1.1-test",
                    "raw": True,
                    "gameId": game_id,
                    "runId": run["run_id"],
                    "runAttemptId": run_attempt_id,
                    "todoAttemptId": todo_attempt_id,
                    "todoInstanceId": todo_id,
                    "gameDayKey": current["period_key"],
                    "capturedAt": datetime.now(timezone.utc).isoformat(),
                    "verdict": "accepted",
                    "hash": hashlib.sha256(
                        f"{label}-{index}".encode("utf-8")
                    ).hexdigest(),
                    "sizeBytes": 1,
                    "fileName": f"artifact-{artifact_id}.png",
                    "testOrdinal": index,
                },
            )
            store.finish_todo_attempt(
                todo_attempt_id,
                status="completed",
                reason_code="test_evidence_accepted",
                reason="fresh attempt-owned evidence accepted",
                retryable=False,
                evidence_refs=[artifact_id],
            )
            artifact_ids.append(artifact_id)
        store.update_run_attempt(
            run_attempt_id,
            state="completed",
            exit_code=0,
            result={"completedTodoInstanceIds": todo_ids},
            completed=True,
        )
        return str(run["run_id"]), run_attempt_id, artifact_ids

    def test_read_contract_is_typed_complete_and_pure(self) -> None:
        before = self.client.get("/api/v1/snapshot").json()["stateVersion"]
        definitions = self.client.get("/api/v1/todo-definitions").json()
        instances = self.client.get(
            "/api/v1/todo-instances", params={"current": "true", "limit": 5000}
        ).json()
        preview = self.client.get(
            "/api/v1/todo-reset-preview",
            params=[("action", "reconcile"), ("gameId", "StarRail"), ("cadence", "daily")],
        ).json()
        detail = self.client.get("/api/v1/games/StarRail").json()
        diagnostics = self.client.get("/api/v1/diagnostics").json()
        after = self.client.get("/api/v1/snapshot").json()["stateVersion"]

        self.assertEqual(before, after)
        self.assertGreater(definitions["total"], 100)
        self.assertEqual(instances["total"], definitions["total"])
        sample_definition = definitions["items"][0]
        self.assertEqual(sample_definition["definitionVersion"], DEFINITION_VERSION)
        self.assertEqual(sample_definition["catalogVersion"], CATALOG_VERSION)
        self.assertRegex(sample_definition["sourceHash"], r"^[0-9a-f]{64}$")
        sample_instance = instances["items"][0]
        self.assertIn("periodStartsAt", sample_instance)
        self.assertIn("periodEndsAt", sample_instance)
        self.assertIn(sample_instance["status"], {
            "pending", "in_progress", "completed", "skipped", "blocked", "review_required"
        })
        self.assertEqual(preview["wouldCreateCount"], 0)
        self.assertTrue(preview["items"])
        self.assertIn("effectiveResetRule", preview["items"][0])
        self.assertIn("nextPeriodResetRule", preview["items"][0])
        self.assertTrue(detail["todoDefinitions"])
        self.assertTrue(detail["todoInstances"])
        self.assertIn("progress", detail)
        self.assertRegex(detail["nextResetAt"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(
            detail["unresolvedRequiredTodoIds"],
            detail["todoSummary"]["daily"]["unresolvedRequiredTodoIds"],
        )
        self.assertIn("difficultOrFailedOperations", diagnostics["todo"])

        schema = self.client.app.openapi()
        for path in (
            "/api/v1/todo-definitions",
            "/api/v1/todo-instances",
            "/api/v1/todo-instances/{todo_instance_id}/transitions",
            "/api/v1/todo-reset-preview",
            "/api/v1/todo-reconcile-requests",
            "/api/v1/todo-reset-requests",
        ):
            self.assertIn(path, schema["paths"])
        for model in (
            "TodoDefinitionRecord",
            "TodoDefinitionPage",
            "TodoInstanceRecord",
            "TodoInstancePage",
            "TodoTransitionRequest",
            "TodoResetPreviewResponse",
            "TodoResetPolicyConfig",
        ):
            self.assertIn(model, schema["components"]["schemas"])

    def test_scheduled_boundary_reconciles_without_resetting_same_week(self) -> None:
        at = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
        with mock.patch.object(
            self.client.app.state.manager.store,
            "reconcile_todo_instances",
            return_value={
                "created_todo_instance_ids": [],
                "retired_todo_blocker_ids": [],
            },
        ) as reconcile:
            self.client.app.state.manager._reconcile_todo_reset_boundary(at)

        self.assertEqual(reconcile.call_args.kwargs["intent"], "reconcile")
        self.assertEqual(
            reconcile.call_args.kwargs["reason"], "configured-period-boundary"
        )

    def test_registered_integrations_expose_but_do_not_force_their_entry_stage(self) -> None:
        response = self.client.get("/api/v1/integrations")
        self.assertEqual(response.status_code, 200, response.text)
        by_game = {item["gameId"]: item for item in response.json()["items"]}
        starrail = by_game["StarRail"]
        self.assertEqual(starrail["mappingStatus"], "registered")
        self.assertEqual(starrail["integrationId"], "starrail.march7th-daily")
        self.assertEqual(starrail["entryOperation"], "attach-home")
        self.assertEqual(
            [item["operation"] for item in starrail["operations"]],
            [
                "attach-home",
                "spend-trailblaze-power",
                "daily-training-objectives",
                "claim-daily-training-rewards",
                "verify-daily-task-list",
            ],
        )
        ww = by_game["WW"]
        self.assertEqual(ww["mappingStatus"], "registered")
        self.assertEqual(ww["integrationId"], "ok-ww.daily-task")
        self.assertEqual(ww["entryOperation"], "attach-world")
        self.assertEqual(
            [item["operation"] for item in ww["operations"]],
            [
                "attach-world",
                "inspect-daily-progress",
                "farm-nightmare-daily-echo",
                "spend-waveplates",
                "claim-daily-reward",
                "claim-mail",
                "claim-battle-pass",
            ],
        )
        ww_parameters = {item["key"]: item for item in ww["parameters"]}
        self.assertEqual(ww_parameters["which_to_farm"]["dispatchStatus"], "active")
        self.assertEqual(
            ww_parameters["which_to_farm"]["options"], ["无音区", "锻造挑战", "模拟领域"]
        )
        self.assertEqual(by_game["ZZZ"]["mappingStatus"], "registered")
        self.assertEqual(
            [item["operation"] for item in by_game["ZZZ"]["operations"]],
            [
                "attach-home",
                "coffee",
                "scratch-card",
                "trigrams-collection",
                "suibian-temple",
                "random-play",
                "charge-plan",
                "city-fund-free-claim",
                "engagement-reward",
            ],
        )
        self.assertEqual(
            by_game["ZZZ"]["parameters"][0]["dispatchStatus"], "requires_adapter"
        )
        self.assertEqual(by_game["CZN"]["mappingStatus"], "registered")
        self.assertEqual(
            [item["operation"] for item in by_game["CZN"]["operations"]],
            ["login-bonus", "achievement-schedule", "arkhianon-supply", "simulation-stamina"],
        )
        self.assertEqual(by_game["FGO"]["mappingStatus"], "registered")
        self.assertEqual(by_game["BD2"]["mappingStatus"], "registered")

        definition_id_by_operation = {
            item["operation"]: item["todoDefinitionId"]
            for item in self.client.get(
                "/api/v1/todo-definitions?gameId=WW&cadence=daily"
            ).json()["items"]
        }
        independently_selected = self.client.patch(
            "/api/v1/config",
            json={
                "dailyTodoSelection": {
                    "WW": [definition_id_by_operation["claim-daily-reward"]]
                }
            },
            headers=self.mutation_headers("ww-mapping-missing-entry"),
        )
        self.assertEqual(independently_selected.status_code, 202, independently_selected.text)
        selection = self.client.get("/api/v1/config").json()["config"]["daily_todo_selection"]
        self.assertEqual(
            selection["WW"], [definition_id_by_operation["claim-daily-reward"]]
        )

    def test_game_card_and_batch_plan_use_the_same_selected_completion_scope(self) -> None:
        manager = self.client.app.state.manager
        captured: dict[str, Any] = {}
        from yeyu_gamer_manager.services import manager as manager_module

        original = manager_module.project_current_game_completion

        def capture_scope(**kwargs: Any):
            captured["scope"] = kwargs["current_scope"]
            captured["invalidatedAt"] = kwargs.get("invalidated_at")
            return original(**kwargs)

        with mock.patch.object(
            manager_module,
            "project_current_game_completion",
            side_effect=capture_scope,
        ):
            response = self.client.get("/api/v1/games/WW")
        self.assertEqual(response.status_code, 200, response.text)

        plan = manager._todo_plans_for_games(["WW"], "daily")["WW"]
        self.assertEqual(captured["scope"]["scopeKey"], plan["scopeKey"])
        self.assertEqual(
            captured["scope"]["scopeFingerprint"],
            plan["scopeFingerprint"],
        )
        self.assertEqual(
            captured["scope"]["definitionCount"],
            len(plan["completionTodoInstanceIds"]),
        )
        self.assertIsNone(captured["invalidatedAt"])

    def test_today_overview_uses_enabled_selected_execution_scope(self) -> None:
        ww_definitions = self.client.get(
            "/api/v1/todo-definitions",
            params={"gameId": "WW", "cadence": "daily"},
        ).json()["items"]
        selected_definition_id = next(
            item["todoDefinitionId"]
            for item in ww_definitions
            if item["operation"] == "claim-daily-reward"
        )
        selected = self.client.patch(
            "/api/v1/config",
            json={"dailyTodoSelection": {"WW": [selected_definition_id]}},
            headers=self.mutation_headers("today-selected-scope"),
        )
        self.assertEqual(selected.status_code, 202, selected.text)

        manager = self.client.app.state.manager
        with manager.store.atomic():
            manager.store.connection.execute(
                "UPDATE games SET enabled = 0 WHERE game_id = 'CZN'"
            )
            manager.store.append_event(
                "game.updated",
                "game",
                "CZN",
                {"enabled": False, "reason": "today-scope-test"},
            )

        overview = self.client.get("/api/v1/snapshot").json()["todo"]
        self.assertNotIn("CZN", overview["scopeGameIds"])
        self.assertEqual(overview["games"]["WW"]["definitionCount"], 1)
        scoped = [overview["games"][game_id] for game_id in overview["scopeGameIds"]]
        self.assertEqual(
            overview["requiredTotal"],
            sum(item["requiredTotal"] for item in scoped),
        )
        self.assertEqual(
            overview["requiredRemaining"],
            sum(item["requiredRemaining"] for item in scoped),
        )

    def test_accepted_current_seal_overrides_stale_run_review_state_on_game_card(self) -> None:
        accepted = SimpleNamespace(
            status=CurrentCompletionStatus.ACCEPTED_DONE,
            batch_id="accepted-batch",
            seal_version=42,
            game_day_key="2026-09-01",
            decision_id="accepted-decision",
            review_id="accepted-review",
            decision="accepted_done",
            evidence_ids=("accepted-evidence",),
            screenshot_evidence_ids=("accepted-screenshot",),
            reason_code="current_completion_contract_accepted",
        )
        with mock.patch(
            "yeyu_gamer_manager.services.manager.project_current_game_completion",
            return_value=accepted,
        ):
            response = self.client.get("/api/v1/games/WW")
        self.assertEqual(response.status_code, 200, response.text)
        card = response.json()
        self.assertEqual(card["runtimeState"], "completed")
        self.assertEqual(card["acceptanceState"], "accepted_done")
        self.assertEqual(card["reviewState"], "none")
        self.assertTrue(card["rewardClaimed"])

    def test_transitions_batch_planning_resume_and_work_item_snapshot(self) -> None:
        artifact_id = self.create_artifact("todo-artifact")
        starrail_required = [
            item for item in self.current_items("StarRail") if item["required"]
        ]
        first_id = str(starrail_required[0]["todoInstanceId"])
        in_progress_headers = self.mutation_headers("todo-in-progress")
        in_progress = self.client.post(
            f"/api/v1/todo-instances/{first_id}/transitions",
            json={"status": "in_progress"},
            headers=in_progress_headers,
        )
        self.assertEqual(in_progress.status_code, 409, in_progress.text)
        self.assertIn("fenced Manager Adapter attempt", in_progress.json()["detail"])
        replay = self.client.post(
            f"/api/v1/todo-instances/{first_id}/transitions",
            json={"status": "in_progress"},
            headers=in_progress_headers,
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(self.client.get(f"/api/v1/todo-instances/{first_id}").json()["attempts"], 0)

        missing_evidence = self.client.post(
            f"/api/v1/todo-instances/{first_id}/transitions",
            json={"status": "completed"},
            headers=self.mutation_headers("todo-complete-no-evidence"),
        )
        self.assertEqual(missing_evidence.status_code, 409, missing_evidence.text)
        foreign_evidence = self.client.post(
            f"/api/v1/todo-instances/{first_id}/transitions",
            json={"status": "completed", "evidenceRefs": [artifact_id]},
            headers=self.mutation_headers("todo-complete-foreign-evidence"),
        )
        self.assertEqual(foreign_evidence.status_code, 409, foreign_evidence.text)

        self.complete_todos_through_fenced_attempt(
            starrail_required, "todo-complete-starrail"
        )
        terminal_rollback = self.client.post(
            f"/api/v1/todo-instances/{first_id}/transitions",
            json={"status": "pending"},
            headers=self.mutation_headers("todo-terminal-rollback"),
        )
        self.assertNotEqual(terminal_rollback.status_code, 202)

        game_detail = self.client.get("/api/v1/games/StarRail").json()
        self.assertEqual(game_detail["progress"]["percent"], 100.0)
        self.assertNotEqual(game_detail["acceptanceState"], "accepted_done")
        self.assertTrue(game_detail["runAttempts"])
        self.assertTrue(game_detail["todoAttempts"])
        self.assertGreater(
            game_detail["attemptAnalysis"]["summary"]["todoAttemptCount"], 0
        )

        zzz_required = [item for item in self.current_items("ZZZ") if item["required"]]
        self.transition(
            str(zzz_required[0]["todoInstanceId"]),
            "todo-skip-zzz",
            "skipped",
            reason="operator deferred this required item",
        )
        batch = self.client.post(
            "/api/v1/batches",
            json={"gameIds": ["StarRail", "ZZZ"], "mode": "plan"},
            headers=self.mutation_headers("todo-batch-plan"),
        )
        self.assertEqual(batch.status_code, 202, batch.text)
        batch_result = batch.json()["result"]
        self.assertEqual(batch_result["batch"]["gameIds"], ["ZZZ"])
        self.assertEqual(batch_result["skippedCompletedGameIds"], ["StarRail"])
        self.assertIn(
            str(zzz_required[0]["todoInstanceId"]),
            batch_result["todoPlans"]["ZZZ"]["unresolvedRequiredTodoIds"],
        )
        for game_id in ("StarRail", "ZZZ"):
            frozen_plan = batch_result["todoPlans"][game_id]
            current_ids = {
                str(item["todoInstanceId"])
                for item in self.current_items(game_id)
            }
            completion_ids = set(frozen_plan["completionTodoInstanceIds"])
            self.assertEqual(
                {
                    str(item["todoInstanceId"])
                    for item in frozen_plan["completionTodoScope"]["items"]
                },
                completion_ids,
            )
            self.assertTrue(completion_ids.issubset(current_ids))
            self.assertFalse(
                any(
                    item["risk"] == "forbidden"
                    for item in frozen_plan["completionTodoScope"]["items"]
                ),
                "the frozen completion scope must contain only the selected safe Todos",
            )

        run = self.client.post(
            "/api/v1/game-runs",
            json={"gameId": "ZZZ", "mode": "plan"},
            headers=self.mutation_headers("todo-run-plan"),
        )
        self.assertEqual(run.status_code, 202, run.text)
        run_id = run.json()["result"]["gameRun"]["runId"]
        resume = self.client.post(
            f"/api/v1/game-runs/{run_id}/resume-requests",
            json={"reason": "resume from unfinished required items"},
            headers=self.mutation_headers("todo-run-resume"),
        )
        self.assertEqual(resume.status_code, 202, resume.text)
        self.assertEqual(resume.json()["result"]["gameRun"]["runId"], run_id)
        self.assertTrue(
            resume.json()["result"]["todoPlan"]["unresolvedRequiredTodoIds"]
        )

        work_item = self.client.post(
            "/api/v1/agent/work-items",
            json={"kind": "diagnose_game", "gameId": "ZZZ", "cadence": "daily"},
            headers=self.mutation_headers("todo-agent-work-item", actor="agent"),
        )
        self.assertEqual(work_item.status_code, 202, work_item.text)
        work_result = work_item.json()["result"]["workItem"]["result"]
        todo_snapshot = work_result["todoSnapshot"]
        self.assertEqual(todo_snapshot["schemaVersion"], 1)
        self.assertEqual(
            todo_snapshot["targetTodoInstanceIds"],
            [item["todoInstanceId"] for item in todo_snapshot["items"]],
        )
        self.assertEqual(
            set(todo_snapshot["targetTodoInstanceIds"]),
            {
                str(item["todoInstanceId"])
                for item in self.current_items("ZZZ")
            },
        )
        self.assertIn("todoDifficulty", work_result)
        self.assertIn("attemptAnalysis", work_result)
        self.assertIn("problemSignals", work_result["attemptAnalysis"])

    def test_reset_policy_is_validated_and_deferred_until_next_period(self) -> None:
        before_items = self.current_items("StarRail")
        target = before_items[0]
        before_next_reset = self.client.get("/api/v1/games/StarRail").json()[
            "nextResetAt"
        ]
        definition_id = str(target["todoDefinitionId"])
        patch = self.client.patch(
            "/api/v1/config",
            json={
                "todoResetPolicy": {
                    "timezone": "Asia/Shanghai",
                    "time": "06:00",
                    "weekStartDay": "Monday",
                    "perGame": {"StarRail": {"time": "07:00"}},
                    "perDefinition": {definition_id: {"time": "08:00"}},
                }
            },
            headers=self.mutation_headers("todo-reset-policy"),
        )
        self.assertEqual(patch.status_code, 202, patch.text)
        preview = self.client.get(
            "/api/v1/todo-reset-preview",
            params=[("gameId", "StarRail"), ("cadence", "daily")],
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_by_definition = {
            item["todoDefinitionId"]: item for item in preview.json()["items"]
        }
        target_preview = preview_by_definition[definition_id]
        self.assertTrue(target_preview["policyChangeDeferred"])
        self.assertEqual(target_preview["effectiveResetRule"]["time"], "04:00")
        self.assertEqual(target_preview["nextPeriodResetRule"]["time"], "08:00")
        self.assertEqual(
            self.client.get("/api/v1/games/StarRail").json()["nextResetAt"],
            before_next_reset,
        )

        headers = self.mutation_headers("todo-reconcile-idempotent")
        body = {"gameIds": ["StarRail"], "cadence": "daily", "reason": "test"}
        first = self.client.post(
            "/api/v1/todo-reconcile-requests", json=body, headers=headers
        )
        second = self.client.post(
            "/api/v1/todo-reconcile-requests", json=body, headers=headers
        )
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertTrue(second.json()["replayed"])
        self.assertFalse(first.json()["result"]["createdTodoInstanceIds"])
        self.assertEqual(
            [item["todoInstanceId"] for item in self.current_items("StarRail")],
            [item["todoInstanceId"] for item in before_items],
        )

        invalid = self.client.patch(
            "/api/v1/config",
            json={
                "todoResetPolicy": {
                    "timezone": "Unknown/Nowhere",
                    "time": "04:00",
                }
            },
            headers=self.mutation_headers("todo-reset-invalid-zone"),
        )
        self.assertNotEqual(invalid.status_code, 202)

    def test_forbidden_todo_cannot_be_started_or_completed(self) -> None:
        artifact_id = self.create_artifact("todo-forbidden-artifact")
        forbidden = next(
            item
            for item in self.current_items("StarRail")
            if item["risk"] == "forbidden"
        )
        todo_id = str(forbidden["todoInstanceId"])

        started = self.client.post(
            f"/api/v1/todo-instances/{todo_id}/transitions",
            json={"status": "in_progress", "reason": "must remain forbidden"},
            headers=self.mutation_headers("todo-forbidden-start"),
        )
        self.assertNotEqual(started.status_code, 202, started.text)

        completed = self.client.post(
            f"/api/v1/todo-instances/{todo_id}/transitions",
            json={
                "status": "completed",
                "reason": "an artifact must not bypass the risk gate",
                "evidenceRefs": [artifact_id],
            },
            headers=self.mutation_headers("todo-forbidden-complete"),
        )
        self.assertNotEqual(completed.status_code, 202, completed.text)
        current = self.client.get(f"/api/v1/todo-instances/{todo_id}").json()
        self.assertEqual(current["status"], forbidden["status"])
        self.assertEqual(current["attempts"], forbidden["attempts"])
        self.assertEqual(current["evidenceRefs"], forbidden["evidenceRefs"])

    def test_completed_todo_is_an_immutable_period_fact(self) -> None:
        artifact_id = self.create_artifact("todo-terminal-artifact")
        target = next(
            item
            for item in self.current_items("StarRail")
            if item["required"] and item["risk"] != "forbidden"
        )
        todo_id = str(target["todoInstanceId"])
        self.complete_todos_through_fenced_attempt(
            [target], "todo-terminal-complete"
        )
        before = self.client.get(f"/api/v1/todo-instances/{todo_id}").json()
        self.assertEqual(before["status"], "completed")
        self.assertNotEqual(before["evidenceRefs"], [artifact_id])

        repeated = self.client.post(
            f"/api/v1/todo-instances/{todo_id}/transitions",
            json={
                "status": "completed",
                "reason": "must not rewrite a completed period fact",
                "evidenceRefs": [artifact_id],
                "incrementAttempt": True,
            },
            headers=self.mutation_headers("todo-terminal-rewrite"),
        )
        self.assertNotEqual(repeated.status_code, 202, repeated.text)
        after = self.client.get(f"/api/v1/todo-instances/{todo_id}").json()
        for field in (
            "status",
            "attempts",
            "reason",
            "evidenceRefs",
            "runId",
            "completedAt",
            "updatedAt",
        ):
            self.assertEqual(after[field], before[field], field)


class TodoMigrationAndSealTests(unittest.TestCase):
    def test_old_game_run_schema_migrates_and_batch_seal_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yeyu-gamer-old-db-") as folder:
            path = Path(folder) / "manager.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE game_runs(
                    run_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    cadence TEXT NOT NULL,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    exit_code INTEGER,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()

            store = SqliteStore(path)
            store.initialize()
            try:
                game_run_columns = {
                    row["name"]
                    for row in store.connection.execute(
                        "PRAGMA table_info(game_runs)"
                    ).fetchall()
                }
                self.assertIn("todo_instance_ids_json", game_run_columns)
                self.assertIn("completed_todo_instance_ids_json", game_run_columns)
                self.assertIn("completion_todo_instance_ids_json", game_run_columns)
                self.assertIn("completion_scope_version", game_run_columns)
                schema_versions = {
                    row["version"]
                    for row in store.connection.execute(
                        "SELECT version FROM schema_version"
                    ).fetchall()
                }
                self.assertTrue(set(range(1, 12)).issubset(schema_versions))

                now = datetime.now(timezone.utc).isoformat()
                store.connection.execute(
                    """
                    INSERT INTO games(
                        game_id, display_name, order_index, enabled, state,
                        reward_claimed, message, policy_json, updated_at
                    ) VALUES ('StarRail', 'StarRail', 0, 1, 'unknown', 0, '', '{}', ?)
                    """,
                    (now,),
                )
                run = store.create_game_run(
                    {
                        "game_id": "StarRail",
                        "cadence": "daily",
                        "state": "planned",
                        "mode": "plan",
                        "requested_by": "migration-test",
                    }
                )
                self.assertEqual(run["todo_instance_ids"], [])
                self.assertEqual(run["completion_todo_instance_ids"], [])
                self.assertEqual(run["completion_scope_version"], 1)

                batch = store.create_batch(
                    {
                        "cadence": "daily",
                        "state": "running",
                        "game_ids": ["StarRail"],
                        "requested_by": "seal-test",
                        "result": {},
                    }
                )
                sealed = store.seal_batch(
                    batch["batch_id"],
                    state="review_required",
                    result={
                        "finalGameRunIds": [run["run_id"]],
                        "todoSnapshot": {},
                        "unresolvedRequiredTodoIds": [],
                        "acceptedDone": False,
                    },
                )
                event_count = len(
                    [
                        event
                        for event in store.list_events(0, 1000)
                        if event["event_type"] == "batch.sealed"
                    ]
                )
                replay = store.seal_batch(
                    batch["batch_id"], state="review_required", result={}
                )
                self.assertEqual(replay["result"], sealed["result"])
                self.assertEqual(
                    len(
                        [
                            event
                            for event in store.list_events(0, 1000)
                            if event["event_type"] == "batch.sealed"
                        ]
                    ),
                    event_count,
                )
                self.assertEqual(
                    sealed["result"]["sealVersion"],
                    next(
                        event["sequence"]
                        for event in store.list_events(0, 1000)
                        if event["event_type"] == "batch.sealed"
                    ),
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
