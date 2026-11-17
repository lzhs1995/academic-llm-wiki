#!/usr/bin/env python3
"""Commit only named task files after validating the exact candidate tree.

Uses an isolated Git index, takes the real index lock, refuses existing staged
changes, verifies the expected HEAD and preserves unrelated unstaged files.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import hashlib
import json
import stat
import subprocess
import sys
import tempfile

VALIDATION_CONTEXT_ENV = 'RESEARCH_SOURCE_VALIDATION_CONTEXT'


def external_source_state(export, original_root):
    """Fingerprint real external origins named by the candidate manifest, not the worktree manifest."""
    manifest_path = export / 'sources/manifest.json'
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 'research-source-registry/v1':
        raise ValueError('Candidate has an unsupported source manifest')
    paths = set()
    for source in manifest['sources'].values():
        for record in source['versions']:
            for origin in record['origins']:
                path = Path(origin['path'])
                if '..' in path.parts:
                    raise ValueError('Source provenance paths must not contain traversal')
                if path.is_absolute() and not path.is_relative_to(original_root):
                    paths.add(path)
    result = {}
    for path in sorted(paths):
        if path != path.resolve():
            raise RuntimeError(f'External source became a symlink: {path}')
        try:
            before = path.stat()
        except FileNotFoundError:
            result[str(path)] = None  # Historical unreferenced missing origins can remain recorded.
            continue
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f'External source is not a regular file: {path}')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        after = path.stat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or path != path.resolve():
            raise RuntimeError(f'External source changed while fingerprinting: {path}')
        result[str(path)] = (*identity(after), digest)
    return result

def git(root, *args, env=None, **kwargs):
    # A caller's alternate index/worktree must not masquerade as the real index.
    clean = dict(os.environ)
    for key in ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_PREFIX'):
        clean.pop(key, None)
    clean.pop(VALIDATION_CONTEXT_ENV, None)
    if env:
        clean.update(env)
    clean['GIT_LITERAL_PATHSPECS'] = '1'
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, env=clean, **kwargs).stdout

def head_ref(root):
    try:
        return git(root, 'symbolic-ref', '-q', 'HEAD').decode().strip()
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        return 'HEAD'  # Detached HEAD: update this direct ref, not a guessed branch.

def file_state(root, names):
    result = {}
    for name in names:
        path = root/name
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[name] = None
            continue
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
        else:
            raise ValueError('Owned paths must be regular files or contained symlinks')
        result[name] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), hashlib.sha256(data).hexdigest())
    return result

def prepare_commit(root, paths, expected_head, message, apply=False, check=True):
    root = root.resolve()
    if Path(git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != root:
        raise ValueError('Root must be the repository worktree root')
    head = git(root, 'rev-parse', 'HEAD').decode().strip()
    branch = head_ref(root)
    if head != expected_head:
        raise RuntimeError('HEAD changed; rebase task evidence before committing')
    if git(root, 'diff', '--cached', '--name-only'):
        raise RuntimeError('Existing staged changes; preserved, not consumed')
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ValueError('Paths must be a JSON list of exact relative file names')
    safe = []
    for raw in paths:
        p = Path(raw)
        if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0].lower() == '.git' or p.as_posix() != raw:
            raise ValueError('Only exact vault-relative file paths are allowed')
        target = root / p
        if any((root/Path(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts))):
            raise ValueError('A parent directory may not be a symlink')
        if target.is_dir():
            raise ValueError('Directories are not an owned-file whitelist')
        if target.is_symlink() and not target.resolve().is_relative_to(root):
            raise ValueError('Outside symlink cannot be included')
        safe.append(p.as_posix())
    if not safe:
        raise ValueError('No paths')
    index_path = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
    lock = Path(str(index_path)+'.lock')
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    lock_identity = os.fstat(fd)
    installed = False
    try:
        # Close the gap between initial checks and acquisition of the actual index lock.
        if git(root, 'diff', '--cached', '--name-only'):
            raise RuntimeError('Existing staged changes; preserved, not consumed')
        initial_files = file_state(root, safe)
        with tempfile.TemporaryDirectory(prefix='research-commit-') as tmp:
            stage = Path(tmp)/'index'
            env = {'GIT_INDEX_FILE': str(stage)}
            git(root, 'read-tree', head, env=env)
            git(root, '-c', 'core.excludesFile=/dev/null', 'add', '--', *safe, env=env)
            names = set(git(root, 'diff', '--cached', '--name-only', '-z', env=env).decode().rstrip('\0').split('\0')) - {''}
            if not names <= set(safe):
                raise RuntimeError('Candidate includes unowned files')
            tree = git(root, 'write-tree', env=env).decode().strip()
            if not names:
                return {'status':'no_changes', 'head':head}
            if check:
                export = Path(tmp).resolve()/'tree'
                export.mkdir()
                git(root, 'checkout-index', '--all', '--prefix='+str(export)+'/', env=env)
                lint = export/'tools/wiki-lint.py'
                if not lint.exists():
                    raise RuntimeError('Candidate tree lacks validator')
                external_before = external_source_state(export, root)
                validation_env = dict(os.environ)
                validation_env.pop(VALIDATION_CONTEXT_ENV, None)
                validation_env[VALIDATION_CONTEXT_ENV] = json.dumps({'original_root': str(root), 'export_root': str(export)})
                subprocess.run([sys.executable, '-B', str(lint), '--root', str(export), '--strict'],
                               check=True, env=validation_env)
            # Re-stage the owned paths in a second index to detect changes during validation.
            second = Path(tmp)/'check-index'
            env2 = {'GIT_INDEX_FILE': str(second)}
            git(root, 'read-tree', head, env=env2)
            git(root, '-c', 'core.excludesFile=/dev/null', 'add', '--', *safe, env=env2)
            if git(root, 'write-tree', env=env2).decode().strip() != tree or file_state(root, safe) != initial_files:
                raise RuntimeError('Owned files changed during validation; no commit')
            if git(root, 'rev-parse', 'HEAD').decode().strip() != head or head_ref(root) != branch:
                raise RuntimeError('HEAD changed during validation; no commit')
            if git(root, 'diff', '--cached', '--name-only'):
                raise RuntimeError('Real staged tree changed; preserved')
            if check and external_source_state(export, root) != external_before:
                raise RuntimeError('External sources changed during validation; no commit')
            if not apply:
                return {'status':'validated_dry_run', 'head':head, 'tree':tree, 'paths':sorted(names)}
            commit = git(root, 'commit-tree', tree, '-p', head, '-m', message).decode().strip()
            # Prepare the future real index before the compare-and-swap branch update.
            payload = stage.read_bytes()
            with os.fdopen(fd, 'wb', closefd=False) as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            if check and external_source_state(export, root) != external_before:
                raise RuntimeError('External sources changed before commit publication; no commit')
            # Bind to the original branch so a concurrent checkout cannot redirect it.
            git(root, 'update-ref', branch, commit, head)
            os.close(fd)
            fd = None
            try:
                os.replace(lock, index_path)
                installed = True
            except OSError:
                # Keep HEAD and the original index coherent if index installation fails.
                git(root, 'update-ref', branch, head, commit)
                raise
            return {'status':'committed', 'head':commit, 'parent':head, 'paths':sorted(names)}
    finally:
        if fd is not None:
            os.close(fd)
        if not installed:
            try:
                current = lock.stat()
                if (current.st_dev, current.st_ino) == (lock_identity.st_dev, lock_identity.st_ino):
                    lock.unlink()
            except FileNotFoundError:
                pass

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent.parent)
    p.add_argument('--expected-head',required=True)
    p.add_argument('--message',required=True)
    p.add_argument('--paths-file',type=Path,required=True,help='JSON list of exact relative files')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    import json
    try:
        result=prepare_commit(a.root,json.loads(a.paths_file.read_text()),a.expected_head,a.message,a.apply)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (RuntimeError,ValueError,OSError,subprocess.CalledProcessError) as exc:
        print(str(exc),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
