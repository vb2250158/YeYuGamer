"""Replay official MultiAccount identity observation. No compose login."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openkuro-runner"))
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


OBSERVER = Path(__file__).parents[1] / "openkuro-runner" / "WwYeYuObserver.py"
OFFICIAL_MULTI = Path(__file__).parents[2] / "vendor-baselines" / "ok-wuthering-waves" / "src" / "task" / "MultiAccountDailyTask.py"


class OfficialMultiAccountObservationReplay(unittest.TestCase):
    def test_observer_does_not_call_login_helpers_or_mutate_done_set(self) -> None:
        events: list[str] = []

        class BaseWWTask:
            def ensure_main(self, **_):
                events.append("ensure")

        class Multi:
            def __init__(self):
                self.done_set = {"keep-me"}

            def _detect_current_account_from_login(self):
                events.append("detect")
                return "195****2369"

            def _select_and_login_account(self):
                events.append("select")
                return "176****5513"

            def run(self):
                events.append("multi-run")
                self._detect_current_account_from_login()
                self._select_and_login_account()

        class Daily(BaseWWTask):
            def run(self):
                events.append("daily")

            def open_daily(self):
                return (0, True)

            def claim_daily(self):
                return "claimed"

            def claim_mail(self):
                return "mail"

            def claim_battle_pass(self):
                return "bp"

        class Nightmare:
            def run(self):
                return None

            def run_capture_mode(self):
                return None

        modules = {
            "src": type(sys)("src"),
            "src.task": type(sys)("src.task"),
            "src.task.DailyTask": SimpleNamespace(DailyTask=Daily),
            "src.task.BaseWWTask": SimpleNamespace(BaseWWTask=BaseWWTask),
            "src.task.NightmareNestTask": SimpleNamespace(NightmareNestTask=Nightmare),
            "src.task.TacetTask": SimpleNamespace(TacetTask=type("T", (), {"farm_tacet": lambda self, **_: None})),
            "src.task.ForgeryTask": SimpleNamespace(ForgeryTask=type("F", (), {"farm_forgery": lambda self, **_: None})),
            "src.task.SimulationTask": SimpleNamespace(SimulationTask=type("S", (), {"farm_simulation": lambda self, **_: None})),
            "src.task.MultiAccountDailyTask": SimpleNamespace(MultiAccountDailyTask=Multi),
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules):
            stage = Path(directory) / "stage.jsonl"
            with patch.dict(os.environ, {"YEYU_GAMER_STAGE_FILE": str(stage)}, clear=False):
                spec = importlib.util.spec_from_file_location("ww_observer_account", OBSERVER)
                observer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(observer)
                observer.install()
                multi = Multi()
                original_done = set(multi.done_set)
                multi.run()
                Daily().claim_daily()
                self.assertEqual(multi.done_set, original_done)
                self.assertEqual(events, ["multi-run", "detect", "select"])
                self.assertNotIn("switch", events)
            rows = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
            completed = [row for row in rows if row["operation"] == "upstream-multi-account-task" and row["state"] == "completed"]
            self.assertEqual(len(completed), 1)
            self.assertIn("returned:None", completed[0]["detail"])
            detected = [row for row in rows if row["operation"] == "official-account" and row["state"] == "detected"]
            self.assertEqual([row["account"] for row in detected], ["195****2369", "176****5513"])
            claimed = [row for row in rows if row["operation"] == "claim-daily-reward"]
            # This fixture has no official executor. Missing capture is explicit
            # and must not alter the official task's completion or identity.
            self.assertTrue(any(row["detail"].startswith("step_capture_unavailable:recognition:") for row in claimed))
            self.assertFalse(any(row["state"] == "step_artifact" for row in claimed))
            self.assertTrue(any(row["state"] == "completed" for row in claimed))

    def test_official_multi_account_source_still_owns_switching(self) -> None:
        text = OFFICIAL_MULTI.read_text(encoding="utf-8")
        self.assertIn("def _switch_to_login(self):", text)
        self.assertIn("def _select_and_login_account(self):", text)
        self.assertIn("self.run_task_by_class(DailyTask)", text)
        self.assertNotIn("YEYU_GAMER", text)
        self.assertNotIn("yeyu_gamer", text.lower())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OfficialMultiAccountObservationReplay)
    )
    print(json.dumps({
        "status": "passed" if result.wasSuccessful() else "failed",
        "cases": result.testsRun,
        "gameStarted": False,
        "toolStarted": False,
    }))
    raise SystemExit(0 if result.wasSuccessful() else 1)
