from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from yeyu_gamer_manager.domain.todos import TODO_INSTANCE_NAMESPACE, todo_instance_id
from yeyu_gamer_manager.services.completion_contract import adjudicate_completion
from yeyu_gamer_manager.services.current_completion import project_current_game_completion
from yeyu_gamer_manager.services.manager_todos import ManagerTodosService
from yeyu_gamer_manager.services.todo_catalog import catalog
from yeyu_gamer_manager.store.sqlite_store import SqliteStore
import test_completion_contract_adjudicator as adjudicator_fixture
import test_current_completion as current_completion_fixture


ACCOUNT_B = "00000000-0000-4000-8000-000000000002"


class AccountFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yeyu-account-foundation-")
        self.path = Path(self.temp.name) / "manager.sqlite3"
        self.store = SqliteStore(self.path)
        self.store.initialize()
        self.store.import_legacy(config_values={}, games=[{
            "game_id": "WW", "display_name": "WW", "order_index": 0, "enabled": True,
        }], source_info={}, policy_projection={})
        self.store.sync_todo_definitions([item for item in catalog() if item["game_id"] == "WW"])
        self.accounts = [{"account_id": "default", "label": "A", "enabled": True},
                         {"account_id": ACCOUNT_B, "label": "B", "enabled": True}]
        self.store.update_config({"game_accounts": {"WW": self.accounts}})
        host = Mock()
        host.execution_bindings.return_value = {"available": False, "bindings": []}
        self.service = ManagerTodosService(store=self.store, adapter_host=host,
            projection_history=lambda: ([], []), automation_assessment_record=lambda item: item)
        self.candidates = self.service._todo_instance_candidates(["WW"], "daily")
        self.store.reconcile_todo_instances(self.candidates, intent="reconcile", requested_by="test", reason="test")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_for(self, account_id, todo_ids):
        return self.store.create_game_run({"game_id": "WW", "account_id": account_id,
            "account_snapshot": {"label": "frozen", "saved_account_label": "saved-label"},
            "cadence": "daily", "state": "planned", "mode": "execute", "requested_by": "test",
            "todo_instance_ids": todo_ids})

    def test_default_identity_unchanged_and_accounts_separate(self):
        definition, period = "todo.v1.ww.daily.attach-home", "daily:2026-09-05"
        self.assertEqual(todo_instance_id(definition, period), todo_instance_id(definition, period, account_id="default"))
        self.assertEqual(todo_instance_id(definition, period), "todo-instance-" + str(uuid.uuid5(TODO_INSTANCE_NAMESPACE, f"yeyu-gamer/todo-instance/v1/{definition}/{period}")))
        self.assertNotEqual(todo_instance_id(definition, period), todo_instance_id(definition, period, account_id=ACCOUNT_B))
        a = self.store.list_todo_instances(game_id="WW", account_id="default")
        b = self.store.list_todo_instances(game_id="WW", account_id=ACCOUNT_B)
        self.assertEqual(len(a), len(b))
        self.assertTrue(a)
        self.assertFalse({item["todo_instance_id"] for item in a} & {item["todo_instance_id"] for item in b})

    def test_completed_a_does_not_complete_b_or_reuse_a_plan(self):
        for todo in self.store.list_todo_instances(game_id="WW", account_id="default"):
            if todo["required"]:
                self.store.transition_todo_instance(todo["todo_instance_id"], status="in_progress", reason="test fact",
                    evidence_refs=[], run_id=None, increment_attempt=True, requested_by="test")
                self.store.transition_todo_instance(todo["todo_instance_id"], status="completed", reason="test fact",
                    evidence_refs=["test-proof-a"], run_id=None, increment_attempt=False, requested_by="test")
        plans = self.service._todo_plans_for_targets([
            {"gameId": "WW", "accountId": "default"}, {"gameId": "WW", "accountId": ACCOUNT_B}], "daily")
        self.assertEqual([p["accountId"] for p in plans], ["default", ACCOUNT_B])
        self.assertEqual(plans[0]["requiredRemaining"], 0)
        self.assertGreater(plans[1]["requiredRemaining"], 0)
        self.assertFalse(set(plans[0]["completionTodoInstanceIds"]) & set(plans[1]["completionTodoInstanceIds"]))
        self.assertEqual(self.service._todo_plans_for_games(["WW"], "daily")["WW"], plans[0])
        scope = self.service._batch_todo_scope({"WW": plans[0], f"WW::{ACCOUNT_B}": plans[1]}, ["WW", f"WW::{ACCOUNT_B}"])
        self.assertEqual([item["gameId"] for item in scope["games"]], ["WW", "WW"])
        self.assertEqual(scope["games"][1]["accountId"], ACCOUNT_B)

    def test_disabled_account_hidden_by_default_but_explicit_history_available(self):
        self.accounts[1]["enabled"] = False
        self.store.update_config({"game_accounts": {"WW": self.accounts}})
        self.assertEqual({item.account_id for item in self.service.list_todo_instances(game_id="WW")}, {"default"})
        self.assertEqual({item.account_id for item in self.service.list_todo_instances(game_id="WW", account_id=ACCOUNT_B, current=True)}, {ACCOUNT_B})

    def test_reset_of_a_leaves_b_completed_fact_unchanged(self):
        b = next(item for item in self.store.list_todo_instances(account_id=ACCOUNT_B) if item["required"])
        self.store.transition_todo_instance(b["todo_instance_id"], status="in_progress", reason="test",
            evidence_refs=[], run_id=None, increment_attempt=True, requested_by="test")
        self.store.transition_todo_instance(b["todo_instance_id"], status="completed", reason="test",
            evidence_refs=["test-proof-b"], run_id=None, increment_attempt=False, requested_by="test")
        before = self.store.get_todo_instance(b["todo_instance_id"])
        a_candidates = self.service._todo_instance_candidates(["WW"], "daily", account_id="default")
        self.store.reconcile_todo_instances(a_candidates, intent="reset", requested_by="test", reason="reset A")
        self.assertEqual(self.store.get_todo_instance(b["todo_instance_id"]), before)

    def test_continuation_cannot_switch_account_and_keeps_frozen_scope(self):
        b = self.store.list_todo_instances(account_id=ACCOUNT_B)[0]
        run = self.run_for(ACCOUNT_B, [b["todo_instance_id"]])
        predecessor = self.store.create_batch({"cadence": "daily", "state": "running", "game_ids": ["WW"], "requested_by": "test", "result": {}})
        self.store.add_batch_run_membership(predecessor["batch_id"], run["run_id"], ordinal=0, role="initial")
        targets = [{"gameId": "WW", "accountId": ACCOUNT_B}]
        self.store.seal_batch(predecessor["batch_id"], state="review_required", result={"accountTargets": targets})
        scope = {"games": [{"gameId": "WW", "accountId": ACCOUNT_B, "scopeFingerprint": "frozen-B"}]}
        attempt = self.store.create_run_attempt({"run_attempt_id": str(uuid.uuid4()), "run_id": run["run_id"],
            "game_id": "WW", "account_id": ACCOUNT_B, "cadence": "daily", "fencing_token_hash": "a" * 64,
            "cancel_authority_hash": "sha256:" + "b" * 64, "plan": {"accountId": ACCOUNT_B,
            "executableTodoInstanceIds": [b["todo_instance_id"]], "todos": [{"todoInstanceId": b["todo_instance_id"]}]}})
        self.store.create_or_get_resume_intent({"resume_intent_id": "resume-B", "run_id": run["run_id"],
            "predecessor_run_attempt_id": attempt["run_attempt_id"], "source_run_revision": 1,
            "source_attempt_revision": 1, "decision": {}, "state": "requested"})
        intent = {"resume_intent_id": "resume-B", "predecessor_batch_id": predecessor["batch_id"],
                  "run_id": run["run_id"], "requested_by": "test", "result": {"accountTargets": targets, "todoScope": scope}}
        child, replay = self.store.create_or_get_continuation_batch(intent)
        self.assertFalse(replay)
        self.assertEqual(child["result"]["todoScope"], scope)
        self.assertTrue(self.store.create_or_get_continuation_batch(intent)[1])
        foreign = self.run_for("default", [])
        with self.assertRaisesRegex(ValueError, "account is outside"):
            self.store.create_or_get_continuation_batch({**intent, "resume_intent_id": "wrong-account", "run_id": foreign["run_id"]})

    def test_game_run_and_transition_reject_cross_account_scope(self):
        a = self.store.list_todo_instances(game_id="WW", account_id="default")[0]
        b = self.store.list_todo_instances(game_id="WW", account_id=ACCOUNT_B)[0]
        with self.assertRaisesRegex(ValueError, "account scope"):
            self.run_for(ACCOUNT_B, [a["todo_instance_id"]])
        run = self.run_for(ACCOUNT_B, [b["todo_instance_id"]])
        self.assertTrue(self.store.has_account_execution("WW", ACCOUNT_B))
        with self.assertRaisesRegex(ValueError, "account scope"):
            self.store.transition_todo_instance(a["todo_instance_id"], status="in_progress", reason="test",
                evidence_refs=[], run_id=run["run_id"], increment_attempt=True, requested_by="test")
        self.store.update_config({"game_accounts": {"WW": [{**self.accounts[1], "label": "new"}]}})
        self.assertEqual(self.store.get_game_run(run["run_id"])["account_snapshot"]["label"], "frozen")

    def test_attempt_account_must_equal_run_and_plan(self):
        b = self.store.list_todo_instances(game_id="WW", account_id=ACCOUNT_B)[0]
        self.store.create_game_run({"game_id": "WW", "account_id": ACCOUNT_B,
            "cadence": "daily", "state": "planned", "mode": "plan", "requested_by": "test"})
        self.assertFalse(self.store.has_account_execution("WW", ACCOUNT_B))
        run = self.run_for(ACCOUNT_B, [b["todo_instance_id"]])
        self.assertTrue(self.store.has_account_execution("WW", ACCOUNT_B))
        data = {"run_attempt_id": str(uuid.uuid4()), "run_id": run["run_id"], "game_id": "WW",
            "account_id": ACCOUNT_B, "cadence": "daily", "fencing_token_hash": "a" * 64,
            "cancel_authority_hash": "sha256:" + "b" * 64,
            "plan": {"accountId": ACCOUNT_B, "executableTodoInstanceIds": [b["todo_instance_id"]],
                "todos": [{"todoInstanceId": b["todo_instance_id"]}]}}
        with self.assertRaisesRegex(ValueError, "scope differs"):
            self.store.create_run_attempt({**data, "account_id": "default"})
        with self.assertRaisesRegex(ValueError, "scope differs"):
            self.store.create_run_attempt({**data, "plan": {**data["plan"], "accountId": "default"}})
        attempt = self.store.create_run_attempt(data)
        self.assertEqual(self.store.get_run_attempt(attempt["run_attempt_id"])["account_id"], ACCOUNT_B)
        self.assertTrue(self.store.has_account_execution("WW", ACCOUNT_B))
        self.assertFalse(self.store.has_account_execution("WW", "default"))

    def test_legacy_unique_migration_keeps_ids_seal_and_foreign_keys(self):
        # Recreate the actual v13 unique constraint in a private fixture database.
        self.store.connection.execute("DELETE FROM todo_instances WHERE account_id != 'default'")
        old_ids = [item["todo_instance_id"] for item in self.store.list_todo_instances()]
        run = self.run_for("default", old_ids[:1])
        batch = self.store.create_batch({"cadence": "daily", "state": "running", "game_ids": ["WW"], "requested_by": "test", "result": {}})
        self.store.add_batch_run_membership(batch["batch_id"], run["run_id"], ordinal=0, role="initial")
        sealed = self.store.seal_batch(batch["batch_id"], state="review_required", result={"acceptedDone": False, "marker": "historical"})
        self.store.close()
        connection = sqlite3.connect(self.path)
        try:
            objects = list(connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='todo_instances' AND type IN ('index','trigger') AND sql IS NOT NULL"))
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='todo_instances'").fetchone()[0]
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(sql.replace("todo_instances (", "todo_instances_legacy (", 1).replace("todo_instances (", "todo_instances_legacy (", 1)
                .replace("todo_instances(", "todo_instances_legacy(", 1).replace("UNIQUE(account_id, todo_definition_id, period_key)", "UNIQUE(todo_definition_id, period_key)"))
            connection.execute("INSERT INTO todo_instances_legacy SELECT * FROM todo_instances")
            connection.execute("DROP TABLE todo_instances")
            connection.execute("ALTER TABLE todo_instances_legacy RENAME TO todo_instances")
            for (object_sql,) in objects:
                connection.execute(object_sql)
            connection.execute("DELETE FROM schema_version WHERE version=14")
            connection.commit()
        finally:
            connection.close()
        self.store = SqliteStore(self.path)
        self.store.initialize()
        self.assertEqual([item["todo_instance_id"] for item in self.store.list_todo_instances()], old_ids)
        self.assertEqual(self.store.get_game_run(run["run_id"])["account_id"], "default")
        self.assertEqual(self.store.get_batch(batch["batch_id"])["result"], sealed["result"])
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.store.reconcile_todo_instances(self.candidates, intent="reconcile", requested_by="test", reason="post-migration")
        self.assertTrue(self.store.list_todo_instances(account_id=ACCOUNT_B))
        self.store.close()
        self.store = SqliteStore(self.path)
        self.store.initialize()
        self.assertEqual(self.store.get_batch(batch["batch_id"])["result"], sealed["result"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM schema_version WHERE version=14").fetchone()[0], 1)


class AccountCompletionTests(unittest.TestCase):
    def test_cross_account_evidence_todo_review_or_attempt_cannot_be_accepted(self):
        snapshot = adjudicator_fixture.CompletionContractAdjudicatorTests().snapshot()
        self.assertTrue(adjudicate_completion(snapshot).accepted_done)
        mutations = {
            "evidence": (snapshot.evidence[0].model_copy(update={"account_id": ACCOUNT_B}), *snapshot.evidence[1:]),
            "todos": (snapshot.todos[0].model_copy(update={"account_id": ACCOUNT_B}), *snapshot.todos[1:]),
            "agent_review": snapshot.agent_review.model_copy(update={"account_id": ACCOUNT_B}),
            "current_attempt": snapshot.current_attempt.model_copy(update={"account_id": ACCOUNT_B}),
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                self.assertFalse(adjudicate_completion(snapshot.model_copy(update={key: value})).accepted_done)

    def test_seal_for_other_account_is_not_current_acceptance_even_with_same_fingerprint(self):
        fixture = current_completion_fixture.CurrentCompletionProjectionTests()
        seal = fixture.seal(seal_version=1, contracts=[fixture.contract()])
        self.assertEqual(project_current_game_completion(game_id="StarRail", current_scope=fixture.scope, sealed_batches=[seal]).status, "accepted_done")
        self.assertEqual(project_current_game_completion(game_id="StarRail", account_id=ACCOUNT_B, current_scope=fixture.scope, sealed_batches=[seal]).status, "none")
        seal["result"]["todoScope"]["games"][0]["accountId"] = ACCOUNT_B
        self.assertEqual(project_current_game_completion(game_id="StarRail", account_id=ACCOUNT_B, current_scope=fixture.scope, sealed_batches=[seal]).status, "none")
        seal["result"]["completionContracts"][0]["accountId"] = ACCOUNT_B
        self.assertEqual(project_current_game_completion(game_id="StarRail", account_id=ACCOUNT_B, current_scope=fixture.scope, sealed_batches=[seal]).status, "accepted_done")


if __name__ == "__main__":
    unittest.main()
