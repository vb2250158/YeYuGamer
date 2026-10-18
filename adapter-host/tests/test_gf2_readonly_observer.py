"""GF2 inspection observation must preserve source calls and bounded raw inputs."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'openkuro-runner'))
import Gf2YeYuObserver as observer


class Gf2ReadonlyObserverTests(unittest.TestCase):
    def run_source(self, *, broken_export=False, raised=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'reward-status' / 'input.png'
            source.parent.mkdir()
            source.write_bytes(b'actual-native-input')
            calls = []

            class Task:
                def inspect_page(self, page):
                    calls.append(page)
                    return {'page': page, 'imagePath': str(source), 'imageSize': [3840, 2160]}

                def inspect_daily_missions(self):
                    self.inspect_page('daily-missions')
                    if raised is not None:
                        raise raised
                    return True

                def inspect_battle_pass(self):
                    self.inspect_page('battle-pass-actions')
                    for index in range(12):
                        self.inspect_page('battle-pass-rewards-' + str(index))
                    return False

                def run(self):
                    self.inspect_page('account-before')
                    self.inspect_daily_missions()
                    self.inspect_battle_pass()
                    self.inspect_page('account-after')
                    return False

            observer._install_inspection(Task)
            # Idempotent installation cannot duplicate calls or emitters.
            observer._install_inspection(Task)
            config_module = ModuleType('src.config')
            config_module.config = {'screenshots_folder': str(root)}
            stage = root / 'stage.jsonl'
            with patch.dict(sys.modules, {'src': ModuleType('src'), 'src.config': config_module}):
                with patch.dict(os.environ, {'YEYU_GAMER_STAGE_FILE': str(stage),
                    'YEYU_GAMER_SELECTED_OPERATIONS': '["claim-daily-missions","battle-pass-free-track"]'}):
                    if broken_export:
                        source.unlink()
                    if raised is None:
                        result = Task().run()
                        self.assertIs(result, False)
                    else:
                        with self.assertRaises(type(raised)) as caught:
                            Task().run()
                        self.assertIs(caught.exception, raised)
            events = [json.loads(line) for line in stage.read_text().splitlines()]
            metadata = [json.loads(file.read_text()) for file in root.glob('gf2-reward-status-*.json')]
            for file in root.glob('gf2-reward-status-*.png'):
                self.assertEqual(b'actual-native-input', file.read_bytes())
            return events, metadata, calls

    def test_exact_inputs_are_bounded_and_shared_identity_has_both_todo_scopes(self):
        events, metadata, calls = self.run_source()
        self.assertEqual(16, len(calls))
        self.assertEqual(14, len(metadata))  # daily 3 + pass 11, 28 artifacts overall
        self.assertEqual(2, sum(item['page'] == 'account-before' for item in metadata))
        self.assertEqual(2, sum(item['page'] == 'account-after' for item in metadata))
        self.assertTrue(all(item['readonlyInspection'] and item['diagnosticOnly'] for item in metadata))
        self.assertTrue(all(item['frameCapturedAt'] is None for item in metadata))
        self.assertEqual({'claim-daily-missions': 'completed', 'battle-pass-free-track': 'failed'},
                         {event['operation']: event['state'] for event in events
                          if event['state'] in {'completed', 'failed'}})
        self.assertEqual(2, sum(event['state'] == 'started' for event in events))
        artifacts = [event for event in events if event['state'] == 'step_artifact']
        self.assertEqual(28, len(artifacts))
        for event in artifacts:
            fields = dict(field.strip().split('=', 1) for field in event['detail'].split(';'))
            self.assertTrue(fields['file'].startswith('gf2-reward-status-'))
            self.assertIn(fields['kind'], {'game-ui-recognition-raw', 'game-ui-recognition-meta'})
            self.assertTrue(fields['capturedAt'])
        self.assertLessEqual(max(sum(item['operation'] == op for item in metadata) for op in
                            ('claim-daily-missions', 'battle-pass-free-track')), 11)

    def test_export_failure_does_not_change_return_or_source_calls(self):
        events, metadata, calls = self.run_source(broken_export=True)
        self.assertEqual(16, len(calls))
        self.assertEqual([], metadata)
        self.assertEqual(4, len(events))

    def test_original_exception_object_is_preserved(self):
        error = RuntimeError('source navigation failed')
        events, metadata, calls = self.run_source(raised=error)
        self.assertEqual(['account-before', 'daily-missions'], calls)
        self.assertEqual(2, sum(event['state'] == 'failed' for event in events))


if __name__ == '__main__':
    unittest.main()
