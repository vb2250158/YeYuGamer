import ast
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from yeyu_gamer_manager.services.daily_queue_priority import order_daily_queue


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "backend/yeyu_gamer_manager/services/autonomous_daily.py"
BASE_SHA = "f571e8a699a48e26204e8d86612334938abd2389baebee3a6e19a5afae2dc5c9"


class AutonomousQueueFollowupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reference = os.environ.get("YEYU_QA_REFERENCE_SOURCE_ROOT")
        if not reference:
            raise unittest.SkipTest("Explicit read-only reference root required")
        payload = (Path(reference) / SOURCE).read_bytes()
        if hashlib.sha256(payload).hexdigest() != BASE_SHA:
            raise AssertionError("Autonomous source SHA drift; re-review before repinning")
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            target = stage / SOURCE
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            result = subprocess.run(["git", "apply", "--unidiff-zero", str(ROOT / "integration-patches/autonomous-queue-followup.patch")],
                                    cwd=stage, capture_output=True, text=True)
            if result.returncode:
                raise AssertionError("Autonomous integration patch no longer applies")
            cls.tree = ast.parse(target.read_text(encoding="utf-8-sig"))

    def test_every_scheduler_occupancy_query_uses_host_aware_manager(self):
        calls = [node for node in ast.walk(self.tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)]
        self.assertFalse(any(node.func.attr == "active_execution_summary" for node in calls))
        self.assertEqual(sum(node.func.attr == "execution_occupancy" for node in calls), 7)

    def prefix(self, frozen):
        function = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "_scheduled_prefix")
        namespace = {"datetime": datetime, "ManagerConflict": RuntimeError,
                     "__package__": "yeyu_gamer_manager.services",
                     "__name__": "yeyu_gamer_manager.services.autonomous_daily"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), SOURCE, "exec"), namespace)
        selected = ["PGR", "StarRail", "ZZZ", "NTE", "Endfield", "WW"]
        config = {"daily_todo_selection": {identifier: ["daily"] for identifier in selected}}
        state = {"cycleTargets": frozen, "dispatchedTargets": []}
        manager = SimpleNamespace(
            daily_execution_games=lambda: [SimpleNamespace(game_id=identifier, enabled=True)
                                           for identifier in order_daily_queue(selected, config)],
            _todo_instance_candidates=lambda **kwargs: [])
        coordinator = SimpleNamespace(manager=manager, store=SimpleNamespace(
            get_config=lambda: {"values": config}), read=lambda: state,
            write=lambda **kwargs: state.update(kwargs))
        scopes = ModuleType("yeyu_gamer_manager.services.account_scopes")
        scopes.daily_account_targets = lambda config, identifiers: [
            {"gameId": identifier, "accountId": account, "accountSnapshot": {}}
            for identifier in identifiers for account in (["one", "two"] if identifier == "WW" else ["main"])]
        with mock.patch.dict(sys.modules, {scopes.__name__: scopes}):
            ready, boundary = namespace["_scheduled_prefix"](coordinator, datetime.now(timezone.utc), "2026-10-28")
        self.assertIsNone(boundary)
        return ready

    def test_new_cycle_freezes_ranked_games_and_preserves_account_order(self):
        ready = self.prefix([])
        self.assertEqual([row["gameId"] for row in ready], ["WW", "WW", "ZZZ", "NTE", "Endfield", "StarRail", "PGR"])
        self.assertEqual([row["accountId"] for row in ready[:2]], ["one", "two"])

    def test_existing_cycle_is_not_reordered(self):
        frozen = [{"gameId": "PGR", "accountId": "main"}, {"gameId": "WW", "accountId": "two"}]
        self.assertEqual(self.prefix(frozen), frozen)
