"""Login evidence gates input; it never becomes a successful game step."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'adapter-host/openkuro-runner'))
spec=importlib.util.spec_from_file_location('ef_credential_fixture',ROOT/'adapter-host/openkuro-runner/EndfieldYeYuBridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)


class EndfieldCredentialGateTests(unittest.TestCase):
    def task(self,texts):
        frame=object()
        class Task:
            original_result=False
            clicks=0
            executor=SimpleNamespace(nullable_frame=lambda:frame)
            def wait_login(self):return self.original_result
            def ensure_main(self):return None
            def ocr(self,*,frame,match):return [SimpleNamespace(name=t) for t in texts if match.search(t)]
            def click(self,*args):self.clicks+=1
            def log_info(self,*args):pass
        bridge._install_login_handoff(Task)
        bridge._install_login_handoff(Task,'title_login')
        return Task()

    def test_nested_handoffs_report_gate_once_and_preserve_official_false_result(self):
        task=self.task(['请输入手机号','请输入验证码','鹰角网络','点击任意位置继续'])
        with patch.object(bridge,'_context',return_value=('stage',{'attach-world'})), \
             patch.object(bridge,'_emit') as emit,patch.object(bridge,'_login_capture') as capture:
            self.assertIs(task.wait_login(),False)
            self.assertIs(task.wait_login(),False)
            self.assertEqual(task.clicks,0)
            self.assertEqual(emit.call_count,1)
            self.assertEqual(emit.call_args.args[1:3],('attach-world','human_required'))
            capture.assert_called_once()
            self.assertEqual(capture.call_args.args[1:],('before','credential'))

    def test_partial_unknown_and_duplicate_fields_are_not_credential_proof(self):
        for texts in (['请输入手机号','鹰角网络'],['请输入验证码','鹰角网络'],
                      ['请输入手机号','请输入验证码'],['请输入手机号','请输入手机号','请输入验证码','鹰角网络'],
                      ['请输入手机号','请输入验证码','鹰角网络','鹰角网络'],
                      ['普通文字请输入手机号','请输入验证码','鹰角网络'],
                      ['请输入手机号','请输入验证码','未知网络'],['登录','鹰角网络']):
            with self.subTest(texts=texts),patch.object(bridge,'_context',return_value=('stage',{'attach-world'})), \
                 patch.object(bridge,'_emit') as emit,patch.object(bridge,'_login_capture'):
                task=self.task(texts)
                self.assertIs(task.wait_login(),False)
                emit.assert_not_called();self.assertEqual(task.clicks,0)

    def test_sealed_native_recording_ocr_confusions_still_require_three_unique_fields(self):
        task=self.task(['品请输入手机号','请输入验证码','应角网络'])
        with patch.object(bridge,'_context',return_value=('stage',{'attach-world'})), \
             patch.object(bridge,'_emit') as emit,patch.object(bridge,'_login_capture'):
            self.assertIs(task.wait_login(),False)
            self.assertEqual(task.clicks,0)
            self.assertEqual(emit.call_count,1)

    def test_no_source_frame_scope_or_official_success_never_creates_gate(self):
        for case in ('scope','frame','success'):
            task=self.task(['请输入手机号','请输入验证码','鹰角网络'])
            if case=='frame':task.executor=SimpleNamespace(nullable_frame=lambda:None)
            if case=='success':task.original_result=True
            with patch.object(bridge,'_context',return_value=('stage',set() if case=='scope' else {'attach-world'})), \
                 patch.object(bridge,'_emit') as emit,patch.object(bridge,'_login_capture'):
                self.assertIs(task.wait_login(),case=='success')
                emit.assert_not_called();self.assertEqual(task.clicks,0)


if __name__=='__main__':unittest.main()
