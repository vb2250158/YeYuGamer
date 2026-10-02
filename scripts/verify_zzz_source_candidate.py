"""Verify a catalogued isolated ZZZ source patch before binding it."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *arguments], capture_output=True,
                            text=True, encoding='utf-8', timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('zzz_source_git_validation_failed: ' + ' '.join(arguments[:2]))
    return result.stdout.strip()


def verify(root: Path, catalog: Path) -> dict:
    root = root.resolve()
    commit = git(root, 'rev-parse', 'HEAD')
    entries = json.loads(catalog.read_text(encoding='utf-8-sig'))['candidates']
    matches = [entry for entry in entries if entry['sourceCommit'] == commit]
    if len(matches) != 1:
        raise ValueError('zzz_source_candidate_not_catalogued')
    entry = matches[0]
    if entry['kind'] != 'isolated-local-source-patch-with-formal-runtime-binding':
        raise ValueError('zzz_source_candidate_kind_invalid')
    if git(root, 'remote', 'get-url', 'origin') != entry['officialRepository']:
        raise ValueError('zzz_source_repository_mismatch')
    git(root, 'merge-base', '--is-ancestor', entry['officialBaseCommit'], commit)
    git(root, 'diff', '--quiet', 'HEAD', '--', 'src')
    untracked = git(root, 'ls-files', '--others', '--exclude-standard', '--', 'src')
    if untracked:
        raise ValueError('zzz_source_untracked_code')
    verified = []
    for expected in entry['verifiedFiles']:
        path = (root/expected['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('zzz_source_file_missing_or_outside_root')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected['sha256']:
            raise ValueError('zzz_source_file_changed')
        verified.append({'path': str(path), 'sha256': digest})
    return {'candidateId': entry['candidateId'], 'sourceCommit': commit,
            'officialBaseCommit': entry['officialBaseCommit'], 'kind': entry['kind'],
            'verifiedFiles': verified}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.catalog), ensure_ascii=False))
