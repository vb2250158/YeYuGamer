"""Check real Git identity and source integrity without launching ZZZ."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('zzz_source_validator', ROOT/'scripts/verify_zzz_source_candidate.py')
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class ZzzSourceCandidateTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT/'.cache/verification'
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='zzz-source-', dir=cache)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/'tool'
        self.root.mkdir()
        self.command('init', '-q')
        self.command('config', 'user.name', 'Source fixture')
        self.command('config', 'user.email', 'fixture@example.invalid')
        self.origin = 'https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon.git'
        self.command('remote', 'add', 'origin', self.origin)
        self.source = self.root/'src/controller.py'
        self.source.parent.mkdir()
        self.source.write_text('official = True\n', encoding='utf-8')
        self.command('add', 'src')
        self.command('commit', '-qm', 'official fixture')
        base = self.command('rev-parse', 'HEAD')
        self.source.write_text('official = True\nowned_click = True\n', encoding='utf-8')
        self.command('add', 'src')
        self.command('commit', '-qm', 'patch fixture')
        self.entry = {'candidateId': 'fixture', 'sourceCommit': self.command('rev-parse', 'HEAD'),
                      'officialBaseCommit': base, 'officialRepository': self.origin,
                      'kind': 'isolated-local-source-patch-with-formal-runtime-binding',
                      'verifiedFiles': [{'path': 'src/controller.py',
                                         'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest()}]}
        self.catalog = Path(self.temporary.name)/'catalog.json'

    def command(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], encoding='utf-8',
                                       stderr=subprocess.STDOUT, timeout=15,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()

    def verify(self):
        self.catalog.write_text(json.dumps({'candidates': [self.entry]}), encoding='utf-8')
        return VALIDATOR.verify(self.root, self.catalog)

    def test_valid_fixed_source_and_official_ancestor(self):
        result = self.verify()
        self.assertEqual(self.entry['sourceCommit'], result['sourceCommit'])
        self.assertEqual(str(self.source.resolve()), result['verifiedFiles'][0]['path'])

    def test_modified_tracked_code_is_rejected(self):
        self.source.write_text('changed = True\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'git_validation_failed'):
            self.verify()

    def test_extra_untracked_code_is_rejected(self):
        (self.source.parent/'injected.py').write_text('changed = True\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'untracked_code'):
            self.verify()

    def test_wrong_repository_is_rejected(self):
        self.command('remote', 'set-url', 'origin', 'https://example.invalid/other.git')
        with self.assertRaisesRegex(ValueError, 'repository_mismatch'):
            self.verify()

    def test_wrong_official_ancestor_is_rejected(self):
        self.entry['officialBaseCommit'] = '0'*40
        with self.assertRaisesRegex(ValueError, 'git_validation_failed'):
            self.verify()

    def test_catalogue_hash_mismatch_is_rejected(self):
        self.entry['verifiedFiles'][0]['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'file_changed'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
