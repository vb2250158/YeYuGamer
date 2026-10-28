from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import mock_open, patch

module_path = Path(__file__).resolve().parents[1] / "yeyu_gamer_manager" / "services" / "daily_queue_priority.py"
module_spec = importlib.util.spec_from_file_location("daily_queue_priority", module_path)
daily_queue_priority = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(daily_queue_priority)
order_daily_queue = daily_queue_priority.order_daily_queue


class DailyQueuePriorityTests(unittest.TestCase):
    def tearDown(self) -> None:
        daily_queue_priority._load_default_priority.cache_clear()

    def test_default_resource_order(self) -> None:
        expected = ["WW", "ZZZ", "NTE", "Endfield", "StarRail", "GF2", "PGR", "FGO"]
        self.assertEqual(order_daily_queue(reversed(expected), {}), expected)

    def test_missing_and_none_use_defaults_and_ignore_legacy_order(self) -> None:
        for config in ({}, {"daily_queue_priority": None}, {"order": ["FGO", "WW"]}):
            with self.subTest(config=config):
                self.assertEqual(order_daily_queue(["FGO", "WW"], config), ["WW", "FGO"])

    def test_override_replaces_defaults_and_unknowns_remain_stable(self) -> None:
        items = ["unknown-b", "WW", "unknown-a", "ZZZ", "FGO"]
        self.assertEqual(
            order_daily_queue(items, {"daily_queue_priority": ["FGO", "ZZZ"]}),
            ["FGO", "ZZZ", "unknown-b", "WW", "unknown-a"],
        )

    def test_empty_override_preserves_order_without_loading_resource(self) -> None:
        items = ["FGO", "WW", "unknown", "WW"]
        with patch.object(daily_queue_priority, "_load_default_priority") as loader:
            result = order_daily_queue(items, {"daily_queue_priority": []})
        loader.assert_not_called()
        self.assertEqual(result, items)
        self.assertIsNot(result, items)

    def test_absent_games_are_not_created_and_empty_input_stays_empty(self) -> None:
        self.assertEqual(order_daily_queue(["FGO", "WW"], {}), ["WW", "FGO"])
        self.assertEqual(order_daily_queue([], {}), [])

    def test_account_order_identity_and_inputs_are_preserved(self) -> None:
        items = [
            {"game": "WW", "account": "second"},
            {"game": "FGO", "account": "main"},
            {"game": "WW", "account": "first"},
            {"game": "ZZZ", "account": "main"},
        ]
        config = {"daily_queue_priority": ["WW", "ZZZ"], "order": ["FGO"]}
        original_items, original_config = deepcopy(items), deepcopy(config)
        result = order_daily_queue(iter(items), config, game_id=lambda item: item["game"])
        self.assertEqual(result, [items[0], items[2], items[3], items[1]])
        for actual, expected in zip(result, [items[0], items[2], items[3], items[1]]):
            self.assertIs(actual, expected)
        self.assertEqual(items, original_items)
        self.assertEqual(config, original_config)

    def test_invalid_config_shapes(self) -> None:
        for config in (None, [], True, ""):
            with self.subTest(config=config), self.assertRaises(ValueError):
                order_daily_queue([], config)

    def test_invalid_priority(self) -> None:
        for priority in ("WW", {}, True, ("WW",), [1], [None], [True],
                         [""], ["  "], ["WW", "WW"], [["WW"]]):
            with self.subTest(priority=priority), self.assertRaises(ValueError):
                order_daily_queue([], {"daily_queue_priority": priority})

    def test_invalid_item_ids(self) -> None:
        for identifier in (None, 1, True, "", "  ", []):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                order_daily_queue([identifier], {})

    def test_only_resource_is_cached_and_config_mutations_take_effect(self) -> None:
        daily_queue_priority._load_default_priority.cache_clear()
        original_open = Path.open
        with patch.object(Path, "open", autospec=True, side_effect=original_open) as opened:
            self.assertEqual(order_daily_queue(["FGO", "WW"], {}), ["WW", "FGO"])
            config = {"daily_queue_priority": ["FGO", "WW"]}
            self.assertEqual(order_daily_queue(["WW", "FGO"], config), ["FGO", "WW"])
            config["daily_queue_priority"].reverse()
            self.assertEqual(order_daily_queue(["FGO", "WW"], config), ["WW", "FGO"])
            config["daily_queue_priority"].append("WW")
            with self.assertRaises(ValueError):
                order_daily_queue([], config)
            self.assertEqual(order_daily_queue(["FGO", "WW"], {}), ["WW", "FGO"])
            self.assertEqual(opened.call_count, 1)

    def test_invalid_resource_is_validated(self) -> None:
        for raw in ('{}', '["WW", "WW"]', '[""]', '[null]'):
            daily_queue_priority._load_default_priority.cache_clear()
            with self.subTest(raw=raw), patch.object(Path, "open", mock_open(read_data=raw)):
                with self.assertRaises(ValueError):
                    order_daily_queue([], {})


if __name__ == "__main__":
    unittest.main()
