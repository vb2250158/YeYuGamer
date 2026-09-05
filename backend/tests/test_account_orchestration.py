"""Account boundaries exercised through the same Manager used by Start Daily."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.domain.models import Cadence, ConfigPatchRequest, RequestMode
from yeyu_gamer_manager.services.manager_errors import ManagerValidation
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
