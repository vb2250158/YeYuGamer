"""Official uncertain results must remain uncertain without changing returns."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'openkuro-runner'))
import Gf2YeYuObserver as observer

class PendingOutcomeTests(unittest.TestCase):
    def test_missing_supply_reward_choice_is_human_gate_and_preserves_false_return(self):
        class Task:
            def log_error(self, message):
                self.message = message
                return self.logger_result
        observer._install_choice_observer(Task)
        with tempfile.TemporaryDirectory() as raw:
            stage=Path(raw)/'stage.jsonl'; task=Task(); task.logger_result=object()
            def original(self):
                self.forwarded_result=self.log_error('未配置拂晓之光补给包奖励，请手动选择')
                return False
            with patch.dict(os.environ,{'YEYU_GAMER_STAGE_FILE':str(stage),'YEYU_GAMER_SELECTED_OPERATIONS':'["battle-pass-free-track"]'}):
                self.assertIs(False,observer._observe('battle-pass-free-track',original)(task))
            self.assertIs(task.logger_result,task.forwarded_result)
            events=[json.loads(line) for line in stage.read_text(encoding='utf-8').splitlines()]
            states=[row['state'] for row in events if row['state']!='progress']
            self.assertEqual(['started','human_required'],states)
            self.assertTrue(next(row for row in events if row['state']=='human_required')['detail'].startswith('gf2_supply_reward_choice_required:'))

    def test_choice_log_outside_selected_step_does_not_create_human_gate(self):
        class Task:
            def log_error(self, message): return 'logged'
        observer._install_choice_observer(Task)
        with tempfile.TemporaryDirectory() as raw:
            stage=Path(raw)/'stage.jsonl'
            with patch.dict(os.environ,{'YEYU_GAMER_STAGE_FILE':str(stage),'YEYU_GAMER_SELECTED_OPERATIONS':'[]'}):
                def original(self):
                    self.log_error('未配置拂晓之光补给包奖励，请手动选择'); return False
                self.assertIs(False,observer._observe('battle-pass-free-track',original)(Task()))
                self.assertEqual('logged',Task().log_error('未配置拂晓之光补给包奖励，请手动选择'))
            self.assertFalse(stage.exists())

    def test_pending_result_is_not_completion(self):
        for result,expected in (('待核查','uncertain'),(False,'failed'),(True,'completed'),(None,'completed')):
            with self.subTest(result=result),tempfile.TemporaryDirectory() as raw:
                stage=Path(raw)/'stage.jsonl'
                with patch.dict(os.environ,{'YEYU_GAMER_STAGE_FILE':str(stage),
                                          'YEYU_GAMER_SELECTED_OPERATIONS':'["battle-pass-free-track"]'}):
                    original=lambda self:result
                    wrapped=observer._observe('battle-pass-free-track',original)
                    self.assertIs(result,wrapped(SimpleNamespace()))
                events=[json.loads(line) for line in stage.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(['started',expected],[item['state'] for item in events if item['state']!='progress'])

if __name__=='__main__':
    unittest.main()
