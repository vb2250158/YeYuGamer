"""Capture optional native inputs without changing task semantics or claiming rewards."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'openkuro-runner'))
import Gf2YeYuObserver as observer


class Gf2OptionalReadonlyObserverTests(unittest.TestCase):
    def inspect(self, *, missing_supply=False, broken_export=False, raised=None):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'reward-status/input.png'
            source.parent.mkdir();source.write_bytes(b'unchanged-native-pixels')
            calls=[]
            class Task:
                def inspect_page(self,page):
                    calls.append(page)
                    return {'page':page,'imagePath':str(source),'imageSize':[3840,2160]}
                def inspect_activity_supply(self):
                    self.inspect_page('supply-intelligence-menu-not-found' if missing_supply else 'supply-intelligence')
                    self.inspect_page('supply-prebattle-menu-not-found' if missing_supply else 'supply-prebattle')
                    return not missing_supply
                def inspect_exploration(self):
                    self.inspect_page('exploration-status')
                    if raised is not None:raise raised
                    return True
                def run(self):
                    self.inspect_page('account-before')
                    supply=self.inspect_activity_supply()
                    self.inspect_exploration()
                    self.inspect_page('unrecognized-page')
                    self.inspect_page('account-after')
                    return supply
            observer._install_inspection(Task);observer._install_inspection(Task)
            config=ModuleType('src.config');config.config={'screenshots_folder':str(root)}
            stage=root/'stage.jsonl'
            with patch.dict(sys.modules,{'src':ModuleType('src'),'src.config':config}),patch.dict(os.environ,{
                    'YEYU_GAMER_STAGE_FILE':str(stage),'YEYU_GAMER_SELECTED_OPERATIONS':'["activity-supply","exploration-claim"]'}):
                if broken_export:source.unlink()
                if raised is None:self.assertIs(Task().run(),not missing_supply)
                else:
                    with self.assertRaises(type(raised)) as caught:Task().run()
                    self.assertIs(raised,caught.exception)
            rows=[json.loads(line) for line in stage.read_text(encoding='utf8').splitlines()]
            metadata=[json.loads(path.read_text(encoding='utf8')) for path in root.glob('gf2-reward-status-*.json')]
            for path in root.glob('gf2-reward-status-*.png'):self.assertEqual(b'unchanged-native-pixels',path.read_bytes())
            return rows,metadata,calls

    def test_optional_pages_and_identity_keep_exact_separate_operation_scopes(self):
        rows,metadata,calls=self.inspect()
        self.assertEqual(6,len(calls))
        self.assertEqual(7,len(metadata))  # supply4 + exploration3
        self.assertEqual({'activity-supply':4,'exploration-claim':3},{op:sum(m['operation']==op for m in metadata) for op in ('activity-supply','exploration-claim')})
        self.assertEqual(14,sum(row['state']=='step_artifact' for row in rows))
        self.assertNotIn('unrecognized-page',{m['page'] for m in metadata})
        self.assertTrue(all(m['readonlyInspection'] and m['diagnosticOnly'] for m in metadata))

    def test_missing_supply_entry_keeps_native_false_and_failed_terminal(self):
        rows,metadata,_=self.inspect(missing_supply=True)
        terminals={r['operation']:r['state'] for r in rows if r['state'] in ('completed','failed')}
        self.assertEqual({'activity-supply':'failed','exploration-claim':'completed'},terminals)
        self.assertTrue(any(m['page'].endswith('-menu-not-found') for m in metadata))

    def test_missing_export_does_not_change_native_results_or_call_order(self):
        rows,metadata,calls=self.inspect(broken_export=True)
        self.assertEqual([],metadata)
        self.assertEqual(6,len(calls))
        self.assertEqual(4,len(rows))

    def test_original_exception_is_preserved_and_no_success_is_fabricated(self):
        error=RuntimeError('native exploration navigation failed')
        rows,_,_=self.inspect(raised=error)
        self.assertFalse(any(r['state']=='completed' for r in rows))
        self.assertEqual(2,sum(r['state']=='failed' for r in rows))


if __name__=='__main__':unittest.main()
