"""Account boundaries exercised through the same Manager used by Start Daily."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.domain.models import Cadence, ConfigPatchRequest, RequestMode, GameRunCreateRequest
from yeyu_gamer_manager.services.account_scopes import (
    daily_account_targets, frozen_run_tool_profiles, initialize_ww_account_configs,
)
from yeyu_gamer_manager.services.manager_errors import ManagerValidation
from yeyu_gamer_manager.services.adapter_protocol import AdapterExecutionPlan
from yeyu_gamer_manager.services.game_launcher import GameLaunchReceipt
from yeyu_gamer_manager.settings import Settings


class AccountOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yeyu-accounts-")
        self.settings = Settings.for_test(Path(self.temp.name))
        self.settings.legacy_root.mkdir(parents=True)
        (self.settings.legacy_root / "daily-gui-config.json").write_text(json.dumps({
            "enabled": {"WW": True}, "order": ["WW"], "dailyScheduleEnabled": False,
        }), encoding="utf-8")
        (self.settings.legacy_root / "game-automation-policy.json").write_text(json.dumps({
            "schemaVersion": 2, "forbiddenActions": ["purchase", "draw", "account_settings"], "games": {"WW": {}},
        }), encoding="utf-8")
        self.settings.web_dist.mkdir(parents=True)
        (self.settings.web_dist / "index.html").write_text("<title>test</title>", encoding="utf-8")
        self.context = TestClient(create_app(self.settings))
        self.client = self.context.__enter__()
        self.manager = self.client.app.state.manager
        self.store = self.manager.store
        self.store.update_notification_policy({"enabled": False, "automatic_dispatch": False}, requested_by="test")

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def save(self, accounts, key=None):
        return self.manager.patch_config(
            ConfigPatchRequest(game_accounts={"WW": accounts}), idempotency_key=key or str(uuid.uuid4()),
            request_id=None, path="/api/v1/config", expected_state_version=None,
        )

    def register(self):
        self.save([
            {"account_id": "default", "label": "主号", "enabled": True, "saved_account_label": "100****0001"},
            {"label": "小号", "enabled": True, "saved_account_label": "100****0002"},
        ])
        return self.store.get_config()["values"]["game_accounts"]["WW"]

    def make_batch(self):
        original = self.manager._account_todo_plans
        def executable(targets, cadence):
            plans = original(targets, cadence)
            for plan in plans.values():
                plan["executableTodoInstanceIds"] = list(plan["completionTodoInstanceIds"])
            return plans
        # Only the package promotion preflight is stubbed; the Manager persists
        # real frozen scopes/memberships. No coordinator or process is started.
        with patch.object(self.manager, "_require_execution_ready"), patch.object(self.manager, "_account_todo_plans", executable):
            return self.manager._create_batch_resources(candidate_game_ids=["WW"], cadence=Cadence.DAILY,
                mode=RequestMode.EXECUTE, requested_by="test", planning_reason="account-contract-test")

    def test_new_ids_are_manager_generated_and_idempotent_and_cannot_be_reassigned(self):
        request = [
            {"account_id": "default", "label": "当前账号", "enabled": True},
            {"label": "新增", "enabled": True},
        ]
        first = self.save(request, "register-once")
        second = self.save(request, "register-once")
        self.assertEqual(first.command_id, second.command_id)
        accounts = self.store.get_config()["values"]["game_accounts"]["WW"]
        self.assertEqual(len(accounts), 2)
        uuid.UUID(accounts[1]["account_id"])
        with self.assertRaises(ManagerValidation):
            self.save([{**accounts[0]}, {**accounts[1], "account_id": str(uuid.uuid4())}])
        with self.assertRaises(ManagerValidation):
            self.save([accounts[0]])

    def test_legacy_settings_initialize_once_and_omitted_account_values_stay_independent(self):
        legacy = {"daily_todo_selection": {"WW": ["daily-one"]},
                  "daily_tool_profiles": {"ok_ww": {"tacet_suppression_number": 4}}}
        first = initialize_ww_account_configs(legacy, ["required"])
        legacy["daily_todo_selection"]["WW"].append("daily-two")
        legacy["daily_tool_profiles"]["ok_ww"]["tacet_suppression_number"] = 9
        second = initialize_ww_account_configs({**legacy, "game_accounts": first}, ["required", "new"])
        self.assertEqual(first, second)
        self.assertEqual(second["WW"][0]["daily_todo_selection"], ["daily-one"])
        self.assertEqual(second["WW"][0]["daily_tool_profiles"]["ok_ww"]["tacet_suppression_number"], 4)
        self.assertEqual(initialize_ww_account_configs({"daily_todo_selection": {"WW": []}}, ["required"])["WW"][0]["daily_todo_selection"], [])
        accounts = self.register()
        initial = accounts[1]
        self.save([accounts[0], {"account_id": initial["account_id"], "label": "改名", "enabled": True,
                              "saved_account_label": initial["saved_account_label"]}])
        saved = self.store.get_config()["values"]["game_accounts"]["WW"][1]
        self.assertEqual(saved["daily_todo_selection"], initial["daily_todo_selection"])
        self.assertEqual(saved["daily_tool_profiles"], initial["daily_tool_profiles"])

    def test_accounts_freeze_their_own_steps_profiles_and_resume_scope(self):
        accounts = self.register()
        definitions = accounts[0]["daily_todo_selection"]
        accounts[0]["daily_todo_selection"] = definitions[:2]
        accounts[1]["daily_todo_selection"] = definitions[2:4]
        accounts[0]["daily_tool_profiles"] = {"ok_ww": {"tacetSuppressionNumber": 2}}
        accounts[1]["daily_tool_profiles"] = {"ok_ww": {"whichToFarm": "Simulation Challenge", "materialSelection": "Weapon EXP"}}
        self.save(accounts)
        batch, runs, _, plans = self.make_batch()
        for index, run in enumerate(runs):
            frozen = run["account_snapshot"]
            expected = definitions[index * 2:index * 2 + 2]
            self.assertEqual(frozen["daily_todo_selection"], expected)
            self.assertEqual({item.todo_definition_id for item in self.manager._frozen_todos_for_run(run)}, set(expected))
            self.assertEqual(frozen_run_tool_profiles(run, batch, {"ok_ww": {"tacet_suppression_number": 99}}), frozen["daily_tool_profiles"])
        saved = self.store.get_config()["values"]["game_accounts"]["WW"]
        saved[1]["daily_todo_selection"] = definitions[-1:]
        saved[1]["daily_tool_profiles"]["ok_ww"]["material_selection"] = "Shell Credit"
        self.save(saved)
        frozen_plan = self.manager._frozen_run_todo_plan(self.store.get_game_run(runs[1]["run_id"]))
        self.assertEqual(set(frozen_plan["selectedTodoDefinitionIds"]), set(definitions[2:4]))
        self.assertEqual(set(frozen_plan["completionTodoInstanceIds"]), set(runs[1]["completion_todo_instance_ids"]))
        self.assertEqual(self.store.get_game_run(runs[1]["run_id"])["account_snapshot"]["daily_tool_profiles"]["ok_ww"]["material_selection"], "Weapon EXP")
        selected = self.manager._selected_todo_scope_items(game_id="WW", cadence="daily",
            all_items=self.manager.list_todo_instances(game_id="WW", cadence="daily", current=True, limit=1000))
        self.assertEqual({item.todo_definition_id for item in selected if item.account_id == saved[1]["account_id"]}, set(definitions[-1:]))

    def test_empty_account_steps_skip_queue_and_new_standalone_plan_freezes_config(self):
        accounts = self.register()
        accounts[0]["daily_todo_selection"] = []
        self.save(accounts)
        batch, runs, _, _ = self.make_batch()
        self.assertEqual([run["account_id"] for run in runs], [accounts[1]["account_id"]])
        self.assertEqual([target["accountId"] for target in batch["result"]["accountTargets"]], [accounts[1]["account_id"]])
        receipt = self.manager.create_game_run(GameRunCreateRequest(game_id="WW", account_id=accounts[1]["account_id"], mode="plan"),
            idempotency_key=str(uuid.uuid4()), request_id=None, path="/api/v1/game-runs", expected_state_version=None)
        snapshot = self.store.get_game_run(receipt.command_id)["account_snapshot"]
        self.assertEqual(snapshot["daily_todo_selection"], accounts[1]["daily_todo_selection"])
        self.assertEqual(snapshot["daily_tool_profiles"], accounts[1]["daily_tool_profiles"])

    def test_account_rejects_foreign_steps_profiles_and_missing_legacy_run_profile(self):
        accounts = self.register()
        for selection in (["foreign.todo"], [accounts[0]["daily_todo_selection"][0]] * 2):
            with self.assertRaises(ManagerValidation):
                self.save([{**accounts[0], "daily_todo_selection": selection}, accounts[1]])
        with self.assertRaises(ValueError):
            ConfigPatchRequest(game_accounts={"WW": [{**accounts[0], "daily_tool_profiles": {"nte": {}}}]})
        old_run = {"game_id": "WW", "account_snapshot": {}}
        with self.assertRaises(ManagerValidation):
            frozen_run_tool_profiles(old_run, None, {"ok_ww": {"tacet_suppression_number": 9}})
        with self.assertRaises(ManagerValidation):
            frozen_run_tool_profiles(old_run, {"result": {}}, {})
        self.assertEqual(frozen_run_tool_profiles(old_run, {"result": {"dailyToolProfiles": {}}}, {"ok_ww": {}}), {})

    def test_adapter_receives_original_account_profile_and_only_protocol_identity_fields(self):
        accounts = self.register()
        accounts[1]["daily_tool_profiles"] = {"ok_ww": {"whichToFarm": "Simulation Challenge", "materialSelection": "Weapon EXP"}}
        self.save(accounts)
        _, runs, _, _ = self.make_batch()
        run = runs[1]
        todos = self.manager._frozen_todos_for_run(run)
        runtime = {"manifestVerified": True, "packageDigest": "sha256:" + "a" * 64, "packageVersion": "test",
            "bindings": [{"operation": item.operation, "todoDefinitionIds": [item.todo_definition_id],
                "adapterCapabilityRefs": [item.adapter_capability_ref], "risk": item.risk,
                "timeoutSeconds": 60, "supportsResume": True} for item in todos]}
        accounts = self.store.get_config()["values"]["game_accounts"]["WW"]
        accounts[1]["daily_tool_profiles"]["ok_ww"]["material_selection"] = "Shell Credit"
        self.save(accounts)
        # All process, capture and Host boundaries are mocks. The real Manager
        # constructs and persists its fenced attempt in this test's temp Store.
        self.store.update_config({"game_paths": {"WW": {"game_path": r"C:\Games\WW\launcher.exe"}}})
        with patch.object(self.manager.adapter_host, "execution_bindings", return_value=runtime), \
             patch.object(self.manager.adapter_host, "validate_execution_request", side_effect=lambda plan: AdapterExecutionPlan.from_document(plan.to_document())), \
             patch.object(self.manager.adapter_host, "execute", return_value=1234) as host, \
             patch.object(self.manager.game_launcher, "ensure_started", return_value=GameLaunchReceipt("already-running", 123, "WutheringWaves.exe")), \
             patch.object(self.manager, "_record_zombie_game_processes"), \
             patch.object(self.manager, "_close_other_batch_games"), \
             patch.object(self.manager, "_open_attempt_log", return_value=None):
            plan, pid = self.manager._start_game_run(run["run_id"])
        self.assertEqual(pid, 1234)
        self.assertEqual(set(plan.account_snapshot), {"label", "saved_account_label"})
        self.assertEqual(plan.account_id, run["account_id"])
        profile = host.call_args.kwargs["installation_binding"]["dailyTaskProfile"]
        self.assertEqual(profile["whichToFarm"], "Simulation Challenge")
        self.assertEqual(profile["materialSelection"], "Weapon EXP")
        self.assertEqual(set(plan.executable_todo_instance_ids), set(run["todo_instance_ids"]))

    def test_start_daily_freezes_two_ww_runs_with_disjoint_todos_and_account_order(self):
        accounts = self.register()
        batch, runs, _, _ = self.make_batch()
        self.assertEqual([run["game_id"] for run in runs], ["WW", "WW"])
        self.assertEqual([run["account_id"] for run in runs], [item["account_id"] for item in accounts])
        self.assertFalse(set(runs[0]["todo_instance_ids"]) & set(runs[1]["todo_instance_ids"]))
        self.save([{**item, "label": "改名"} for item in reversed(accounts)])
        frozen = self.store.get_batch(batch["batch_id"])["result"]["accountTargets"]
        self.assertEqual([target["accountId"] for target in frozen], [item["account_id"] for item in accounts])
        self.assertEqual([target["accountLabel"] for target in frozen], ["主号", "小号"])
        self.assertEqual([target["runId"] for target in frozen], [run["run_id"] for run in runs])
        changed = [dict(item) for item in accounts]
        changed[1]["saved_account_label"] = "100****0003"
        with self.assertRaises(ManagerValidation):
            self.save(changed)
        for run in runs:
            loaded = self.manager.get_game_run(run["run_id"])
            self.assertEqual(loaded.completion_contract["accountId"], run["account_id"])
            self.assertFalse(loaded.completion_contract["acceptedDone"])

    def test_seal_requires_contract_for_each_account_of_the_same_game(self):
        self.register()
        batch, runs, _, _ = self.make_batch()
        sealed = self.manager._seal_batch_from_todos(batch_id=batch["batch_id"], initial_result=batch["result"],
            game_ids=["WW"], cadence="daily", state="blocked", final_run_ids=[run["run_id"] for run in runs],
            reason="test incomplete accounts")
        self.assertFalse(sealed["result"]["acceptedDone"])
        coverage = sealed["result"]["completionCoverage"]
        self.assertEqual(len(coverage), 2)
        self.assertEqual({item["accountId"] for item in coverage}, {run["account_id"] for run in runs})
        for run in runs:
            entry = next(item for item in coverage if item["accountId"] == run["account_id"])
            self.assertEqual(entry["runState"], run["state"])
            blocker = next(item for item in sealed["result"]["notificationBlockers"] if item["accountId"] == run["account_id"])
            self.assertEqual(blocker["runId"], run["run_id"])
            self.assertEqual(blocker["targetId"], "WW" if run["account_id"] == "default" else f'WW::{run["account_id"]}')

    def test_mail_frame_metadata_uses_the_frozen_account_and_run(self):
        self.register()
        _, runs, _, _ = self.make_batch()
        frames = []
        for run in runs:
            artifact_id = str(uuid.uuid4())
            self.store.create_resource("artifact", resource_id=artifact_id, state="captured", document={
                "gameId": "WW", "runId": run["run_id"], "kind": "game-ui-daily-reward-watermarked",
                "operation": "claim-daily-reward", "todoInstanceId": run["todo_instance_ids"][0],
                "capturedAt": run["created_at"],
            })
            decision = SimpleNamespace(game_id="WW", account_id=run["account_id"], run_id=run["run_id"],
                                       accepted_evidence_refs=(artifact_id,))
            frame = self.manager._mail_screenshot_caption(decision, artifact_id)
            frames.append(frame)
            self.assertEqual((frame["gameId"], frame["accountId"], frame["runId"]),
                             ("WW", run["account_id"], run["run_id"]))
            self.assertEqual(frame["kind"], "game-ui-daily-reward-watermarked")
            self.assertEqual(frame["todoInstanceId"], run["todo_instance_ids"][0])
            self.assertEqual(frame["targetId"], "WW" if run["account_id"] == "default" else f'WW::{run["account_id"]}')
        self.assertNotEqual(frames[0]["targetId"], frames[1]["targetId"])
        self.assertNotEqual(frames[0]["runId"], frames[1]["runId"])

    def test_typed_attempt_reads_preserve_the_nondefault_run_account(self):
        self.register()
        _, runs, _, _ = self.make_batch()
        run = runs[1]
        todo_id = run["todo_instance_ids"][0]
        attempt_id = str(uuid.uuid4())
        self.store.create_run_attempt({
            "run_attempt_id": attempt_id, "run_id": run["run_id"],
            "game_id": run["game_id"], "account_id": run["account_id"], "cadence": "daily",
            "fencing_token_hash": "a" * 64, "cancel_authority_hash": "sha256:" + "b" * 64,
            "plan": {"accountId": run["account_id"], "executableTodoInstanceIds": [todo_id],
                     "todos": [{"todoInstanceId": todo_id}]},
        })
        self.store.update_run_attempt(attempt_id, state="human_required", completed=True)
        token = (self.settings.actor_tokens_dir / "cli.token").read_text(encoding="ascii").strip()
        headers = {"Authorization": f"Bearer {token}", "X-YeYu-Gamer-Actor": "cli"}

        run_response = self.client.get(f'/api/v1/game-runs/{run["run_id"]}', headers=headers)
        self.assertEqual(run_response.status_code, 200, run_response.text)
        self.assertEqual(run_response.json()["accountId"], run["account_id"])
        responses = [
            (f'/api/v1/game-runs/{run["run_id"]}/attempts', lambda body: body["items"]),
            (f'/api/v1/run-attempts/{attempt_id}', lambda body: [body]),
            ('/api/v1/games/WW', lambda body: body["runAttempts"]),
        ]
        for path, records in responses:
            with self.subTest(path=path):
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                attempt = next(item for item in records(response.json()) if item["runAttemptId"] == attempt_id)
                self.assertEqual(attempt["accountId"], run["account_id"])
                self.assertEqual(attempt["runId"], run["run_id"])
                self.assertEqual(attempt["state"], "human_required")

    def test_mail_selection_keeps_the_completed_daily_claim_after_frame(self):
        self.register()
        _, runs, _, _ = self.make_batch()
        run = runs[1]
        todos = self.manager._frozen_todos_for_run(run)
        claim = next(item for item in todos if item.operation == "claim-daily-reward")
        screenshot_ids = []
        for index in range(5):
            artifact_id = f"mail-selection-{index}"
            screenshot_ids.append(artifact_id)
            self.store.create_resource("artifact", resource_id=artifact_id, state="captured", document={
                "gameId": "WW", "runId": run["run_id"], "kind": "game-ui-step-after-watermarked",
                "operation": claim.operation if index == 0 else "attach-world",
                "todoInstanceId": claim.todo_instance_id if index == 0 else todos[0].todo_instance_id,
                "capturedAt": f"2026-09-05T12:00:0{index}+00:00",
            })
        decision = SimpleNamespace(game_id="WW", account_id=run["account_id"], run_id=run["run_id"],
                                   run_attempt_id=None, screenshot_artifact_refs=screenshot_ids,
                                   accepted_evidence_refs=(), accepted_done=False)
        completed = claim.model_copy(update={"status": "completed", "evidence_refs": [screenshot_ids[0]]})
        with patch.object(self.manager, "_frozen_todos_for_run", return_value=[completed]):
            selected = self.manager._mail_screenshot_selection(decision)
        self.assertEqual(selected[0], screenshot_ids[0])
        self.assertEqual(len(selected), 3)
        with patch.object(self.manager, "_frozen_todos_for_run", return_value=[claim]):
            self.assertNotIn(screenshot_ids[0], self.manager._mail_screenshot_selection(decision))

    def test_missing_saved_label_blocks_before_runs_are_created(self):
        accounts = self.register()
        accounts[0]["saved_account_label"] = ""
        self.save(accounts)
        before = len(self.store.list_game_runs(100))
        with self.assertRaises(ManagerValidation):
            self.make_batch()
        self.assertEqual(len(self.store.list_game_runs(100)), before)

    def test_account_scoped_reset_preview_does_not_include_the_other_account(self):
        accounts = self.register()
        account_id = accounts[1]["account_id"]
        preview = self.manager.preview_todo_reset(action="reset", game_ids=["WW"], cadence="daily", account_id=account_id)
        self.assertTrue(preview.items)
        self.assertEqual({item.account_id for item in preview.items}, {account_id})

    def test_legacy_unbound_run_cannot_resume_into_another_selected_account(self):
        todos = self.manager.list_todo_instances(game_id="WW", account_id="default", cadence="daily", current=True)
        run = self.store.create_game_run({"game_id": "WW", "cadence": "daily", "state": "queued", "mode": "execute",
            "requested_by": "test", "todo_instance_ids": [todos[0].todo_instance_id],
            "completion_todo_instance_ids": [todos[0].todo_instance_id]})
        self.save([{"account_id": "default", "label": "旧账号", "enabled": False},
                   {"label": "小号", "enabled": True, "saved_account_label": "100****0002"}])
        with patch.object(self.manager, "_prepare_run_attempt") as prepare:
            with self.assertRaises(ManagerValidation):
                self.manager._start_game_run(run["run_id"])
            prepare.assert_not_called()
