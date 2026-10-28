from types import SimpleNamespace
import unittest
from unittest import mock

import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.domain.models import BatchCreateRequest, ConfigPatchRequest


class BeforeAccountFreeze(Exception):
    pass


class DailyQueuePriorityManagerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.store = self.fixture.store

    def test_daily_game_projection_uses_priority_without_rewriting_game_configuration(self):
        games = [SimpleNamespace(game_id=game_id) for game_id in ("PGR", "StarRail", "ZZZ", "GF2", "NTE", "Endfield", "FGO", "WW")]
        before = self.store.get_config()
        sequence = self.store.latest_event_sequence()
        with mock.patch.object(self.manager, "list_games", return_value=games):
            result = self.manager.daily_execution_games()
        self.assertEqual([game.game_id for game in result], ["WW", "ZZZ", "NTE", "Endfield", "StarRail", "GF2", "PGR", "FGO"])
        self.assertEqual(self.store.get_config(), before)
        self.assertEqual(self.store.latest_event_sequence(), sequence)

    def test_automatic_batch_orders_games_and_accounts_before_freezing(self):
        targets = [SimpleNamespace(game_id=game_id, account_id=account) for game_id, account in
                   (("PGR", "default"), ("WW", "account-two"), ("ZZZ", "default"), ("WW", "account-one"))]
        with mock.patch.object(self.manager, "_freeze_account_targets", side_effect=BeforeAccountFreeze) as freeze:
            with self.assertRaises(BeforeAccountFreeze):
                self.manager._create_batch_resources(candidate_game_ids=["PGR", "WW", "ZZZ"], cadence="daily", mode="plan",
                    requested_by="manager-daily", planning_reason="offline order", requested_targets=targets)
        self.assertEqual(freeze.call_args.args[0], ["WW", "ZZZ", "PGR"])
        self.assertEqual([(item.game_id, item.account_id) for item in freeze.call_args.args[2]],
                         [("WW", "account-two"), ("WW", "account-one"), ("ZZZ", "default"), ("PGR", "default")])

    def test_explicit_manual_batch_order_is_not_changed(self):
        with mock.patch.object(self.manager, "_freeze_account_targets", side_effect=BeforeAccountFreeze) as freeze:
            with self.assertRaises(BeforeAccountFreeze):
                self.manager._create_batch_resources(candidate_game_ids=["PGR", "WW", "ZZZ"], cadence="daily", mode="plan",
                    requested_by="operator", planning_reason="offline explicit order")
        self.assertEqual(freeze.call_args.args[0], ["PGR", "WW", "ZZZ"])

    def test_typed_priority_configuration_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            ConfigPatchRequest(daily_queue_priority=["WW", "WW"])
        self.assertEqual(ConfigPatchRequest(daily_queue_priority=["GF2", "WW"]).daily_queue_priority, ["GF2", "WW"])


if __name__ == "__main__":
    unittest.main()
