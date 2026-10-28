from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from yeyu_gamer_manager.services import queue_timeout_budget
from yeyu_gamer_manager.services.queue_timeout_budget import load_queue_timeout_budget


class QueueTimeoutBudgetTests(unittest.TestCase):
    def tearDown(self) -> None:
        queue_timeout_budget._load_default_budget.cache_clear()

    def test_defaults_match_resource_and_required_values(self) -> None:
        resource_path = (
            Path(queue_timeout_budget.__file__).parent
            / "resources" / "queue_timeout_budgets.json"
        )
        expected = {
            "human_takeover_seconds": 300,
            "completion_review_seconds": 900,
            "callback_overhead_seconds": 40,
            "callback_cancel_grace_seconds": 15,
            "poll_seconds": 1,
            "orphan_pending_seconds": 120,
        }
        self.assertEqual(json.loads(resource_path.read_text(encoding="utf-8")), expected)
        self.assertEqual(asdict(load_queue_timeout_budget({})), expected)

    def test_partial_and_full_overrides(self) -> None:
        defaults = asdict(load_queue_timeout_budget({}))
        for name in defaults:
            with self.subTest(name=name):
                overrides = {name: 2.5}
                budget = load_queue_timeout_budget({"queue_timeout_budget": overrides})
                self.assertEqual(asdict(budget), {**defaults, **overrides})
        overrides = dict.fromkeys(defaults, 3)
        self.assertEqual(
            asdict(load_queue_timeout_budget({"queue_timeout_budget": overrides})), overrides
        )

    def test_only_human_and_review_allow_zero(self) -> None:
        budget = load_queue_timeout_budget({"queue_timeout_budget": {
            "human_takeover_seconds": 0, "completion_review_seconds": 0,
        }})
        self.assertEqual(budget.human_takeover_seconds, 0)
        self.assertEqual(budget.completion_review_seconds, 0)
        for name in ("callback_overhead_seconds", "callback_cancel_grace_seconds", "poll_seconds"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_queue_timeout_budget({"queue_timeout_budget": {name: 0}})

    def test_invalid_field_values(self) -> None:
        for name in asdict(load_queue_timeout_budget({})):
            for value in (True, False, None, "1", [], {}, -1, float("nan"),
                          float("inf"), float("-inf"), 10 ** 1000):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    load_queue_timeout_budget({"queue_timeout_budget": {name: value}})

    def test_invalid_shapes_and_unknown_fields(self) -> None:
        for config in (None, [], True):
            with self.subTest(config=config), self.assertRaises(ValueError):
                load_queue_timeout_budget(config)
        for overrides in (None, [], True, "", {"unknown": 1}, {1: 1}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                load_queue_timeout_budget({"queue_timeout_budget": overrides})

    def test_results_are_immutable_and_do_not_contaminate_each_other(self) -> None:
        overrides = {"poll_seconds": 2}
        config = {"queue_timeout_budget": overrides, "unrelated": True}
        first = load_queue_timeout_budget(config)
        self.assertEqual(overrides, {"poll_seconds": 2})
        overrides["poll_seconds"] = 3
        second = load_queue_timeout_budget(config)
        self.assertEqual(first.poll_seconds, 2)
        self.assertEqual(second.poll_seconds, 3)
        with self.assertRaises(FrozenInstanceError):
            first.poll_seconds = 4
        defaults = load_queue_timeout_budget({})
        self.assertEqual(defaults.poll_seconds, 1)
        self.assertIsNot(defaults, load_queue_timeout_budget({}))

    def test_cache_reads_only_file_defaults(self) -> None:
        queue_timeout_budget._load_default_budget.cache_clear()
        original_open = Path.open
        with patch.object(Path, "open", autospec=True, side_effect=original_open) as mocked_open:
            load_queue_timeout_budget({"queue_timeout_budget": {"poll_seconds": 2}})
            load_queue_timeout_budget({"queue_timeout_budget": {"poll_seconds": 3}})
            self.assertEqual(load_queue_timeout_budget({}).poll_seconds, 1)
            self.assertEqual(mocked_open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
