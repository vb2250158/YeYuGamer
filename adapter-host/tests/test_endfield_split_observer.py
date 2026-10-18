"""Replay the pinned official DailyFeature without launching its GUI or game."""
from __future__ import annotations

from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "adapter-host/openkuro-runner"))
import EndfieldYeYuBridge as bridge


def load_feature():
    source = Path(os.environ.get("YEYU_GAMER_ENDFIELD_SOURCE", str(
        ROOT / ".cache/upstream-checkouts/endfield-v1.1.8-20261002")))
    path = source / "src/tasks/daily/daily_feature.py"
    spec = importlib.util.spec_from_file_location("src.tasks.daily.daily_feature", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DailyFeature


Feature = load_feature()
CASES = (
    ("MailTask", "run_mail", "⭐收邮件", "mail"),
    ("DailyBattleTask", "run_battle", "⭐刷体力", "spend-sanity"),
    ("DeliverySendTask", "delivery_send_others", "⭐转交运送委托", "delivery-commission"),
    ("CreditCollectTask", "run_credit_collect", "⭐收信用", "collect-credit"),
    ("BoatHarvestTask", "boat_claim_rewards", "⭐帝江号收菜", "dijiang-harvest"),
    ("DailyRewardTask", "claim_daily_rewards", "⭐日常奖励", "claim-daily-reward"),
)


class SplitObserverTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache/verification').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".cache/verification")
        self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name) / "stage.jsonl"
        self.stage.write_text("", encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "YEYU_GAMER_STAGE_FILE": str(self.stage),
            "YEYU_GAMER_SELECTED_OPERATIONS": json.dumps([c[3] for c in CASES]),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        # Recognition capture has its own real-writer replay; isolate this
        # boundary's exact callbacks, account context, results and exceptions.
        for name, value in (("recognition_scope", lambda *a, **kw: nullcontext()),
                            ("_install_upstream_logging", lambda: None)):
            stub = patch.object(bridge, name, value)
            stub.start()
            self.addCleanup(stub.stop)

    def make_host(self, result=None, error=None):
        calls = []
        class Host:
            def ensure_main(self): return None
            def build_task_plan(self): return self.original_plan
            def _is_account_override_enabled(self): return False
        host = Host()
        host.current_user, host.current_account_id = "fixture-user", "fixture-account"
        host.daily_runner = object()
        host.executor = SimpleNamespace(onetime_tasks=[])
        host.original_plan = []
        for class_name, method_name, switch, operation in CASES:
            def execute(impl, operation=operation):
                calls.append((operation, impl.running, impl.current_user,
                              impl.current_account_id, impl.daily_runner,
                              impl._daily_host_account_overrides_enabled))
                impl._daily_boat_state_confirmed = True
                if error is not None:
                    raise error
                return result
            def account(impl, user, account_id):
                impl.current_user, impl.current_account_id = user, account_id
            cls = type(class_name, (), {
                "__module__": "src.tasks.onetime." + class_name,
                method_name: execute, "set_current_account": account,
            })
            impl = cls()
            impl.running, impl.current_user, impl.current_account_id = False, "old", "old-account"
            impl.config = {switch: ["fixture-option"]}
            host.executor.onetime_tasks.append(impl)
            feature = Feature(host, cls, switch, method_name)
            # The third tuple element must remain the actual upstream object.
            predicate = lambda feature=feature, switch=switch: bool(feature.impl_config(switch))
            feature.predicate = predicate
            host.original_plan.append(feature.plan_item())
        bridge._install_methods(Host)
        return host, calls

    def events(self):
        return [json.loads(line) for line in self.stage.read_text(encoding="utf-8").splitlines()]

    def test_all_six_real_feature_callbacks_keep_order_and_context(self):
        token = object()
        host, calls = self.make_host(result=token)
        plan = host.build_task_plan()
        for original, observed in zip(host.original_plan, plan):
            self.assertEqual(original[0], observed[0])
            self.assertIs(original[2], observed[2])
            self.assertTrue(observed[2]())
            self.assertIs(observed[1](), token)
        self.assertEqual([c[0] for c in calls], [c[3] for c in CASES])
        for call in calls:
            self.assertEqual(call[1:4], (True, "fixture-user", "fixture-account"))
            self.assertIs(call[4], host.daily_runner)
            self.assertFalse(call[5])
        self.assertEqual([(e["operation"], e["state"]) for e in self.events()],
                         [(c[3], state) for c in CASES for state in ("started", "completed")])
        for impl in host.executor.onetime_tasks:
            self.assertEqual((impl.running, impl.current_user, impl.current_account_id),
                             (False, "old", "old-account"))
            self.assertFalse(hasattr(impl, "daily_runner"))
            self.assertFalse(hasattr(impl, "_daily_host_account_overrides_enabled"))

    def test_false_and_exception_propagate_and_context_is_restored(self):
        host, _ = self.make_host(result=False)
        self.assertIs(host.build_task_plan()[0][1](), False)
        self.assertEqual([e["state"] for e in self.events()], ["started", "failed"])
        self.stage.write_text("", encoding="utf-8")
        error = RuntimeError("fixture official failure")
        host, _ = self.make_host(error=error)
        with self.assertRaises(RuntimeError) as caught:
            host.build_task_plan()[0][1]()
        self.assertIs(caught.exception, error)
        self.assertEqual([e["state"] for e in self.events()], ["started", "failed"])
        self.assertFalse(host.executor.onetime_tasks[0].running)

    def test_unselected_callback_still_runs_without_inventing_events(self):
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = "[]"
        host, calls = self.make_host()
        self.assertIsNone(host.build_task_plan()[0][1]())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.events(), [])

    def test_mismatched_feature_host_or_label_is_not_attributed(self):
        host, _ = self.make_host()
        original = host.original_plan[0]
        host.original_plan[0] = ("unexpected-label", *original[1:])
        self.assertIs(host.build_task_plan()[0], host.original_plan[0])
        host.original_plan[0] = original
        original[1].__self__.host = object()
        self.assertIs(host.build_task_plan()[0], original)

    def test_attribution_failure_keeps_official_entry_callable(self):
        host, calls = self.make_host()
        with patch.object(bridge, '_plan_operation', side_effect=RuntimeError('fixture telemetry failure')):
            plan = host.build_task_plan()
        self.assertIs(plan[0], host.original_plan[0])
        self.assertIsNone(plan[0][1]())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
