import ast
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'integration-patches' / 'zzz-launcher-followup.patch'
SOURCE_FILES = {
    'hoyoplay_native_observation.py': 'E4042737D52AD2BE4FF2E0D52701CD8153320DF7DCF4776E8F156164F24E6162',
    'hoyoplay_native_execution.py': '574769488BE1DEE0E19AD7D69987170A54388AB2CB88F316A3A5CBA29C8AE79D',
}
SERVICE_PATH = Path('backend/yeyu_gamer_manager/services')


class Rejected(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


@unittest.skipUnless(os.environ.get('YEYU_QA_REFERENCE_SOURCE_ROOT'),
                     'REFERENCE SOURCE NOT SET: fixture validation NOT PASSED; set YEYU_QA_REFERENCE_SOURCE_ROOT')
class LauncherFollowupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reference = Path(os.environ['YEYU_QA_REFERENCE_SOURCE_ROOT'])
        if not reference.is_absolute():
            raise AssertionError('reference source root must be absolute')
        reference = reference.resolve(strict=True)
        sources = {}
        for filename, expected in SOURCE_FILES.items():
            source = (reference / SERVICE_PATH / filename).resolve(strict=True)
            if not source.is_relative_to(reference):
                raise AssertionError('reference source escapes explicit root')
            content = source.read_bytes()
            actual = hashlib.sha256(content).hexdigest().upper()
            if actual != expected:
                raise AssertionError(f'reference base SHA mismatch: {filename}; expected {expected}; actual {actual}')
            sources[filename] = content
        cls.temp = tempfile.TemporaryDirectory(prefix='zzz-followup-')
        cls.addClassCleanup(cls.temp.cleanup)
        stage_root = Path(cls.temp.name)
        cls.base = stage_root / 'base'
        cls.base.mkdir()
        cls.stage = stage_root / SERVICE_PATH
        cls.stage.mkdir(parents=True)
        for filename, content in sources.items():
            (cls.base / filename).write_bytes(content)
            (cls.stage / filename).write_bytes(content)
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        environment['GIT_CEILING_DIRECTORIES'] = str(stage_root.parent)
        subprocess.run(['git', 'apply', '--check', str(PATCH)], cwd=stage_root, check=True,
                       capture_output=True, env=environment)
        subprocess.run(['git', 'apply', str(PATCH)], cwd=stage_root, check=True,
                       capture_output=True, env=environment)

    def launch(self, directory, identity, expected=None):
        source = (directory / 'hoyoplay_native_observation.py').read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        sampler = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == 'NativeHoYoPlaySampler')
        method = next(node for node in sampler.body if isinstance(node, ast.FunctionDef)
                      and node.name == '_start_launcher')
        method.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        popen = Mock(return_value=SimpleNamespace(pid=71))
        query = Mock(return_value=identity)
        fixed = Path(r'D:\Game\Launchers\HoYoPlay\launcher.exe')
        namespace = dict(Path=Path, PureWindowsPath=PureWindowsPath, os=os, json=json,
                         _PinnedLauncher=object, _utc_now=lambda: 'fixture-time',
                         FIXED_LAUNCHER_EXECUTABLE=fixed, FIXED_LAUNCHER_ROOT=fixed.parent,
                         HYP_ACCESSIBILITY_ARGUMENTS=('--force-renderer-accessibility',),
                         subprocess=SimpleNamespace(Popen=popen, DEVNULL=-3),
                         GameLaunchService=SimpleNamespace(_queue_process_identity=query),
                         _failure=lambda reason, message: Rejected(reason, message))
        exec(compile(module, '<snapshot-only-launch-method>', 'exec'), namespace)
        owner = SimpleNamespace(_guard=Mock(), launch_guard=nullcontext,
                                task_id='fixture-task', manager_id='fixture-manager',
                                _launches=[], register_helper=Mock())
        with tempfile.TemporaryDirectory(dir=self.temp.name) as output:
            if expected:
                with self.assertRaises(Rejected) as caught:
                    namespace['_start_launcher'](owner, Path(output), Mock())
                self.assertEqual(expected, caught.exception.reason)
            else:
                namespace['_start_launcher'](owner, Path(output), Mock())
            self.assertTrue((Path(output) / 'launcher-start.json').is_file())
        self.assertEqual(1, popen.call_count)
        query.assert_called_once_with(71)
        self.assertEqual(1, len(owner._launches))
        self.assertEqual(2, owner.register_helper.call_count)

    def test_missing_identity_baseline_and_candidate_do_not_retry(self):
        for directory in (self.base, self.stage):
            self.launch(directory, None, 'zzz_observation_launch_identity_unavailable')

    def test_path_mismatch_is_independent_counterexample(self):
        wrong = SimpleNamespace(created_at=123, executable=r'D:\untrusted\launcher.exe')
        self.launch(self.base, wrong, 'zzz_observation_launch_identity_unavailable')
        self.launch(self.stage, wrong, 'zzz_observation_launch_identity_mismatch')

    def test_matching_identity_still_passes(self):
        valid = SimpleNamespace(created_at=123, executable=r'D:\Game\Launchers\HoYoPlay\launcher.exe')
        for directory in (self.base, self.stage):
            self.launch(directory, valid)

    def test_selection_counterexamples_execute_only_pure_predicate(self):
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        cases = [
            ('count-zero', 0, 'https://hyp-api.mihoyo.com', '/nap_cn', 'unqualified', 'ready', 'selection-document-count'),
            ('count-two', 2, 'https://hyp-api.mihoyo.com', '/nap_cn', 'unqualified', 'ready', 'selection-document-count'),
            ('wrong-origin', 1, 'https://example.invalid', '/nap_cn', 'unqualified', 'ready', 'selection-location-unqualified'),
            ('wrong-game', 1, 'https://hyp-api.mihoyo.com', '/hk4e_cn', 'unqualified', 'ready', 'selection-location-unqualified'),
            ('conflict', 1, 'https://hyp-api.mihoyo.com', '/nap_cn', 'conflicting', 'ready', 'selection-location-unqualified'),
            ('text-unavailable', 1, 'https://hyp-api.mihoyo.com', '/nap_cn', 'unqualified', 'unknown', 'selection-text-unavailable'),
            ('ready', 1, 'https://hyp-api.mihoyo.com', '/nap_cn', 'unqualified', 'ready', 'accepted'),
        ]
        for directory in (self.base, self.stage):
            source = (directory / 'hoyoplay_native_execution.py').read_text(encoding='utf-8-sig')
            start = source.index('    if($docs.Count -ne 1')
            end = source.index("    Session-Phase 'view-final-identity'", start)
            predicate = source[start:end]
            for label, count, origin, route, status, text, expected in cases:
                with self.subTest(version=directory.name, case=label):
                    script = "$ErrorActionPreference='Stop';$docs=@();"
                    script += f"1..{max(count, 1)} | ForEach-Object {{"
                    script += f"if($_ -le {count}) {{$docs+=@{{documentLocation=@{{origin='{origin}';path='/hypfe/index.html';route='{route}';status='{status}'}};documentText='{text}'}}}}}};"
                    script += "try {" + predicate + ";'accepted'} catch {$_.Exception.Message}"
                    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                                            capture_output=True, text=True, timeout=10,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.assertEqual(0, result.returncode)
                    wanted = expected if directory == self.stage or expected == 'accepted' else 'selection-unknown'
                    self.assertEqual(wanted, result.stdout.strip())


if __name__ == '__main__':
    unittest.main()
