import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
from PIL import Image


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'screenshots/reward-status'
        self.folder.mkdir(parents=True);self.stage=self.root/'stage.jsonl';self.events=[]
        config_module=ModuleType('src.config');config_module.config={'screenshots_folder':str(self.folder.parent)}
        self.modules=patch.dict('sys.modules',{'src.config':config_module});self.modules.start();self.addCleanup(self.modules.stop)
        self.env=patch.dict(os.environ,{'YEYU_GAMER_ENDFIELD_REWARD_INSPECTION_ONLY':'1',
            'YEYU_GAMER_SELECTED_OPERATIONS':'["claim-daily-reward"]','YEYU_GAMER_STAGE_FILE':str(self.stage)})
        self.env.start();self.addCleanup(self.env.stop)
        path=Path(__file__).resolve().parents[1]/'openkuro-runner/EndfieldRewardStatusObserver.py'
        spec=importlib.util.spec_from_file_location('endfield_reward_status_observer',path)
        self.observer=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.observer)
        outer=self
        class Task:
            def inspect_page(self,name):
                path=outer.folder/(name+'.png')
                image=Image.new('RGB',(3840,2160));image.putpixel((240,2120),(25,130,240))
                image.save(path)
                self.result={'page':name,'imagePath':str(path),'visibleLevels':[1]}
                return self.result
            def run(self):
                result=self.inspect_page('daily');self.inspect_page('rewards');return result
        self.Task=Task
    def emit(self,*args):self.events.append(args)
    def test_same_native_pixels_and_return_are_preserved_with_bounded_two_file_inputs(self):
        self.assertTrue(self.observer.install(self.Task,self.emit))
        task=self.Task();result=task.run()
        artifacts=[json.loads(e[-1])for e in self.events if e[2]=='step_artifact']
        self.assertEqual(len(artifacts),4)
        self.assertEqual(result['page'],'daily');self.assertEqual(task.result['page'],'rewards')
        with Image.open(self.root/artifacts[0]['file'])as image:
            self.assertEqual(image.getpixel((240,2120)),(25,130,240))
        metas=[json.loads((self.root/e['file']).read_text(encoding='utf-8'))for e in artifacts if e['kind']=='official-recognition-metadata']
        self.assertEqual([m['node']for m in metas],['daily-status','reward-status-0'])
        self.assertTrue(all(m['frameCaptureTimeKnown']is False for m in metas))
    def test_inactive_normal_run_has_no_observer_or_side_effect(self):
        os.environ['YEYU_GAMER_ENDFIELD_REWARD_INSPECTION_ONLY']='0'
        original=self.Task.run
        self.assertFalse(self.observer.install(self.Task,self.emit));self.assertIs(self.Task.run,original)
        self.Task().run();self.assertEqual(self.events,[])
    def test_observation_failure_does_not_change_original_exception(self):
        error=RuntimeError('original')
        def fail(task):raise error
        self.Task.run=fail;self.observer.install(self.Task,self.emit)
        with self.assertRaises(RuntimeError)as caught:self.Task().run()
        self.assertIs(caught.exception,error);self.assertEqual(self.events[-1][2],'failed')
    def test_foreign_source_image_is_not_exported_but_official_result_survives(self):
        path=self.root/'foreign.png';Image.new('RGB',(3840,2160)).save(path)
        original={'page':'daily','imagePath':str(path)}
        self.Task.inspect_page=lambda *args:original
        self.Task.run=lambda task:task.inspect_page('daily')
        self.observer.install(self.Task,self.emit)
        self.assertIs(self.Task().run(),original)
        self.assertFalse(any(e[2]=='step_artifact'for e in self.events))
    def test_false_official_terminal_stays_failed(self):
        self.Task.run=lambda task:False;self.observer.install(self.Task,self.emit)
        self.assertIs(self.Task().run(),False);self.assertEqual(self.events[-1][2],'failed')

    def test_broken_emitter_preserves_original_return_and_exception(self):
        def emit_failure(*args):raise OSError('observer transport failed')
        self.observer.install(self.Task,emit_failure)
        self.assertEqual(self.Task().run()['page'],'daily')
        original_error=RuntimeError('official error')
        class FailedTask(self.Task):
            def run(self):raise original_error
        FailedTask._yeyu_reward_status_observer=False
        self.observer.install(FailedTask,emit_failure)
        with self.assertRaises(RuntimeError)as caught:FailedTask().run()
        self.assertIs(caught.exception,original_error)

    def test_unknown_return_is_reviewed_without_rewriting_result(self):
        self.Task.run=lambda task:None;self.observer.install(self.Task,self.emit)
        self.assertIsNone(self.Task().run());self.assertEqual(self.events[-1][2],'review_required')

    def test_invalid_selected_operations_do_not_observe_or_replace_official_return(self):
        expected = object()
        for selected in (None, 7, 'claim-daily-reward', {'claim-daily-reward': True},
                         ['claim-daily-reward', 7]):
            with self.subTest(selected=selected):
                class Task(self.Task):
                    def run(self):
                        return expected
                self.events.clear()
                os.environ['YEYU_GAMER_SELECTED_OPERATIONS'] = json.dumps(selected)
                self.observer.install(Task, self.emit)
                self.assertIs(Task().run(), expected)
                self.assertEqual(self.events, [])

if __name__=='__main__':unittest.main()
