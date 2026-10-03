"""Exercise the real WeGame response parser and action budget without input."""
import subprocess
import unittest
from unittest.mock import patch

from yeyu_gamer_manager.services.game_launcher import GameLaunchService


class WeGameProbeTests(unittest.TestCase):
    def test_no_effect_result_is_preserved(self):
        outcome = 'no-effect:wegame-primary:audited-primary-action'
        with patch.object(GameLaunchService, '_run_launcher_probe',
                          return_value=subprocess.CompletedProcess([], 4, outcome, '')):
            self.assertEqual(GameLaunchService._probe_nikke_wegame_primary_action(
                allow_action=True, cancel_requested=None), outcome)

    def test_no_effect_requires_expected_exit_and_action_permission(self):
        for code, allow, outcome in [
            (0, True, 'no-effect:wegame-primary:启动'),
            (4, False, 'no-effect:wegame-primary:启动'),
            (4, True, 'no-effect:unknown-action'),
        ]:
            with self.subTest(code=code, allow=allow), patch.object(
                GameLaunchService, '_run_launcher_probe',
                return_value=subprocess.CompletedProcess([], code, outcome, ''),
            ):
                self.assertEqual(GameLaunchService._probe_nikke_wegame_primary_action(
                    allow_action=allow, cancel_requested=None), 'error:wegame-invalid-probe-result')

    def test_no_effect_actions_consume_budget_through_real_parser(self):
        permissions = []
        def shell_result(*args, environment, **kwargs):
            allowed = environment['YEYU_NIKKE_WEGAME_ALLOW_ACTION'] == '1'
            permissions.append(allowed)
            prefix = 'no-effect' if allowed else 'ready'
            return subprocess.CompletedProcess([], 4 if allowed else 3,
                                               prefix + ':wegame-primary:audited-primary-action', '')
        service = GameLaunchService()
        with patch.object(GameLaunchService, '_run_launcher_probe', side_effect=shell_result), \
             patch.object(service, '_list_running', return_value={77: 'wegame.exe'}), \
             patch.object(service, '_notify') as notify, \
             patch('yeyu_gamer_manager.services.game_launcher.time.monotonic', return_value=100):
            actions = 0
            for _ in range(4):
                actions, _ = service._drive_nikke_wegame_surface(
                    observer=None, game_id='NIKKE', started_at=0,
                    expected_names={'nikke.exe'}, actions=actions,
                    wait_started_at=99, cancel_requested=None)
        self.assertEqual(actions, service.NIKKE_WEGAME_MAX_ACTION_ACTIONS)
        self.assertEqual(permissions, [True, True, True, False])
        self.assertEqual(notify.call_count, 3)
        self.assertTrue(all(call.args[5]['gameReady'] is False for call in notify.call_args_list))
