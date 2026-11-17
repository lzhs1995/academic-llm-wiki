"""Isolated repository tests: never commit the live research vault."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'tools/research-commit.py'
spec = importlib.util.spec_from_file_location('research_commit', SOURCE)
commit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commit)


class ResearchCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='research-commit-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Isolated Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('owned.md', 'owned old\n')
        self.write('other.md', 'other old\n')
        self.write('tools/wiki-lint.py', (
            'from pathlib import Path\n'
            'root = Path(__file__).resolve().parent.parent\n'
            'assert (root / "owned.md").read_text() == "owned new\\n"\n'
            'assert (root / "other.md").read_text() == "other old\\n"\n'
        ))
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.index = self.root / '.git/index'
        self.lock = self.root / '.git/index.lock'

    def git(self, *args):
        env = dict(os.environ)
        for k in ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
            env.pop(k, None)
        return subprocess.run(['git', '-C', str(self.root), *args], env=env,
                              check=True, capture_output=True, text=True).stdout

    def write(self, name, text):
        p = self.root/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def run_commit(self, paths=None, apply=True):
        return commit.prepare_commit(self.root, paths or ['owned.md'], self.head,
                                     'reviewed owned change', apply=apply)

    def with_lint_mutation(self, mutate, operation):
        original = subprocess.run
        def runner(args, *a, **kw):
            result = original(args, *a, **kw)
            if len(args) > 2 and args[2].endswith('/tools/wiki-lint.py'):
                mutate()
            return result
        with patch.object(commit.subprocess, 'run', side_effect=runner):
            return operation()

    def test_exact_commit_preserves_unrelated_unstaged(self):
        self.write('owned.md', 'owned new\n')
        self.write('other.md', 'someone else work\n')
        self.write('untracked.md', 'untracked work\n')
        result = self.run_commit()
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD').strip(), 'owned.md')
        self.assertEqual(self.git('show', 'HEAD:other.md'), 'other old\n')
        self.assertEqual((self.root/'other.md').read_text(), 'someone else work\n')
        self.assertEqual((self.root/'untracked.md').read_text(), 'untracked work\n')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertFalse(self.lock.exists())

    def test_existing_staged_refused_and_exact_index_preserved(self):
        self.write('other.md', 'someone staged\n')
        self.git('add', 'other.md')
        self.write('other.md', 'someone additional unstaged\n')
        self.write('owned.md', 'owned new\n')
        before = self.index.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Existing staged'):
            self.run_commit()
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.git('show', ':other.md'), 'someone staged\n')
        self.assertEqual((self.root/'other.md').read_text(), 'someone additional unstaged\n')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertFalse(self.lock.exists())

    def test_dry_run_validates_export_without_mutating_index_or_head(self):
        self.write('owned.md', 'owned new\n')
        before = self.index.read_bytes()
        result = self.run_commit(apply=False)
        self.assertEqual(result['status'], 'validated_dry_run')
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertFalse(self.lock.exists())

    def test_owned_content_change_during_lint_refused(self):
        self.write('owned.md', 'owned new\n')
        with self.assertRaisesRegex(RuntimeError, 'Owned files changed'):
            self.with_lint_mutation(lambda: self.write('owned.md', 'unreviewed\n'), self.run_commit)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual((self.root/'owned.md').read_text(), 'unreviewed\n')
        self.assertFalse(self.lock.exists())

    def test_owned_mode_change_even_when_git_ignores_mode_refused(self):
        self.git('config', 'core.filemode', 'false')
        self.write('owned.md', 'owned new\n')
        with self.assertRaisesRegex(RuntimeError, 'Owned files changed'):
            self.with_lint_mutation(lambda: (self.root/'owned.md').chmod(0o755), self.run_commit)
        self.assertFalse(self.lock.exists())

    def test_stale_expected_head_refused(self):
        self.write('owned.md', 'owned new\n')
        with self.assertRaisesRegex(RuntimeError, 'HEAD changed'):
            commit.prepare_commit(self.root, ['owned.md'], '0'*40, 'no', apply=True)
        self.assertFalse(self.lock.exists())

    def test_head_change_during_lint_refused(self):
        self.write('owned.md', 'owned new\n')
        def advance():
            tree = self.git('rev-parse', 'HEAD^{tree}').strip()
            new = self.git('commit-tree', tree, '-p', self.head, '-m', 'concurrent').strip()
            self.git('update-ref', 'HEAD', new, self.head)
        with self.assertRaisesRegex(RuntimeError, 'HEAD changed'):
            self.with_lint_mutation(advance, self.run_commit)
        self.assertEqual(self.git('show', 'HEAD:owned.md'), 'owned old\n')
        self.assertFalse(self.lock.exists())

    def test_branch_change_at_same_head_refused(self):
        self.write('owned.md', 'owned new\n')
        self.git('branch', 'other-branch')
        with self.assertRaisesRegex(RuntimeError, 'HEAD changed'):
            self.with_lint_mutation(lambda: self.git('symbolic-ref', 'HEAD', 'refs/heads/other-branch'), self.run_commit)
        self.assertFalse(self.lock.exists())

    def test_lint_failure_removes_only_own_lock(self):
        self.write('owned.md', 'invalid candidate\n')
        before = self.index.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_commit()
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertFalse(self.lock.exists())

    def test_existing_lock_is_never_removed(self):
        self.lock.write_text('other owner')
        with self.assertRaises(FileExistsError):
            self.run_commit()
        self.assertEqual(self.lock.read_text(), 'other owner')

    def test_success_does_not_delete_next_owners_lock(self):
        self.write('owned.md', 'owned new\n')
        replace = os.replace
        def install(src, dst):
            replace(src, dst)
            self.lock.write_text('next owner')
        with patch.object(commit.os, 'replace', side_effect=install):
            self.run_commit()
        self.assertEqual(self.lock.read_text(), 'next owner')

    def test_index_install_failure_rolls_back_branch(self):
        self.write('owned.md', 'owned new\n')
        before = self.index.read_bytes()
        with patch.object(commit.os, 'replace', side_effect=OSError('fixture install failure')):
            with self.assertRaises(OSError):
                self.run_commit()
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertFalse(self.lock.exists())

    def test_literal_filename_does_not_expand_wildcard(self):
        self.write('literal[1]*.md', 'literal new\n')
        self.write('literal1-other.md', 'someone other\n')
        self.write('owned.md', 'owned new\n')
        result = self.run_commit(['owned.md', 'literal[1]*.md'])
        self.assertEqual(set(result['paths']), {'owned.md', 'literal[1]*.md'})
        self.assertIn('?? literal1-other.md', self.git('status', '--short'))

    def test_deleted_file_and_new_file_exact_paths(self):
        self.write('owned.md', 'owned new\n')
        self.write('new.md', 'new\n')
        (self.root/'other.md').unlink()
        # Fixture expects other.md, so validate only owned+new; deletion remains unstaged.
        result = self.run_commit(['owned.md', 'new.md'])
        self.assertEqual(set(result['paths']), {'owned.md', 'new.md'})
        self.assertEqual(self.git('show', 'HEAD:other.md'), 'other old\n')
        self.assertFalse((self.root/'other.md').exists())

    def test_owned_deletion_is_committed(self):
        self.write('owned.md', 'owned new\n')
        (self.root/'other.md').unlink()
        self.write('tools/wiki-lint.py', 'from pathlib import Path\n'
                   'assert not (Path(__file__).resolve().parent.parent / "other.md").exists()\n')
        result = self.run_commit(['owned.md', 'other.md', 'tools/wiki-lint.py'])
        self.assertEqual(set(result['paths']), {'owned.md', 'other.md', 'tools/wiki-lint.py'})
        self.assertNotIn('other.md', self.git('ls-tree', '--name-only', 'HEAD').splitlines())
        self.assertFalse(self.lock.exists())

    def test_no_changes_cleans_lock_and_preserves_index(self):
        before = self.index.read_bytes()
        self.assertEqual(self.run_commit()['status'], 'no_changes')
        self.assertEqual(self.index.read_bytes(), before)
        self.assertFalse(self.lock.exists())

    def test_external_index_environment_cannot_hide_real_staged_changes(self):
        self.write('other.md', 'someone staged\n')
        self.git('add', 'other.md')
        external = self.root/'alternate-index'
        external.write_bytes(self.index.read_bytes())
        with patch.dict(os.environ, {'GIT_INDEX_FILE': str(external)}):
            with self.assertRaisesRegex(RuntimeError, 'Existing staged'):
                self.run_commit()
        self.assertEqual(self.git('show', ':other.md'), 'someone staged\n')

    def test_invalid_whitelist_rejected(self):
        for paths in ('owned.md', [1], ['../escape'], ['/tmp/x'], ['.git/config'], ['tools'], ['./owned.md']):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.run_commit(paths)
        self.assertFalse(self.lock.exists())

    def install_source_fixture(self, external=False, alias=False):
        tools = SOURCE.parent
        sys_path = str(tools)
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from source_registry import SourceSpec, publish_batch
        self.write('tools/source_registry.py', (tools / 'source_registry.py').read_text())
        if external:
            external_dir = tempfile.TemporaryDirectory(prefix='external-origin-')
            self.addCleanup(external_dir.cleanup)
            origin = Path(external_dir.name).resolve() / 'source.md'
            origin.write_text('Original evidence\n')
        else:
            origin = self.root.resolve() / 'inbox/source.md'
            origin.parent.mkdir(exist_ok=True)
            origin.write_text('Original evidence\n')
        result = publish_batch([SourceSpec('test-source', origin, 'fixture', aliases=[str(origin)] if alias else [])], vault=self.root, write=True)
        record = next(iter(result['manifest']['sources'].values()))['versions'][0]
        reference = str(origin) if alias else record['snapshot_path']
        self.write('tools/wiki-lint.py', (
            'from pathlib import Path\n'
            'from source_registry import validate_evidence\n'
            'root = Path(__file__).resolve().parent.parent\n'
            f'result = validate_evidence({reference!r}, "Original evidence", vault=root)\n'
            'assert result["ok"], result\n'
            f'assert result["record"]["source_path"] == {str(origin)!r}\n'
        ))
        self.git('add', '.')
        self.git('commit', '-qm', 'source baseline')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.write('owned.md', 'owned new\n')
        return origin

    def test_candidate_source_changed_or_deleted_rejects_without_worktree_fallback(self):
        origin = self.install_source_fixture()
        for content in ('Changed candidate source', None):
            if content is None:
                origin.unlink()
            else:
                origin.write_text(content)
            before = self.index.read_bytes()
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_commit(['owned.md', 'inbox/source.md'])
            self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
            self.assertEqual(self.index.read_bytes(), before)
            self.assertFalse(self.lock.exists())

    def test_absolute_alias_maps_export_and_preserves_original_provenance(self):
        self.install_source_fixture(alias=True)
        self.assertEqual(self.run_commit(apply=False)['status'], 'validated_dry_run')

    def test_unowned_worktree_source_change_does_not_replace_candidate_source(self):
        origin = self.install_source_fixture()
        origin.write_text('Unowned source change')
        self.assertEqual(self.run_commit(apply=False)['status'], 'validated_dry_run')
        self.assertEqual(origin.read_text(), 'Unowned source change')

    def test_external_origin_is_checked_at_real_path(self):
        origin = self.install_source_fixture(external=True)
        origin.write_text('External source changed before validation')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_commit()
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertFalse(self.lock.exists())

    def test_external_origin_change_after_lint_refuses_publication(self):
        origin = self.install_source_fixture(external=True)
        before = self.index.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'External sources changed'):
            self.with_lint_mutation(lambda: origin.write_text('Changed after lint'), self.run_commit)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertFalse(self.lock.exists())

    def test_external_origin_change_after_commit_object_before_ref_refused(self):
        origin = self.install_source_fixture(external=True)
        before = self.index.read_bytes()
        original_git = commit.git
        def mutate_after_object(root, *args, **kwargs):
            result = original_git(root, *args, **kwargs)
            if args and args[0] == 'commit-tree':
                origin.write_text('Changed immediately before ref publication')
            return result
        with patch.object(commit, 'git', side_effect=mutate_after_object):
            with self.assertRaisesRegex(RuntimeError, 'External sources changed before commit publication'):
                self.run_commit()
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertFalse(self.lock.exists())

    def test_external_origin_parent_symlink_is_rejected(self):
        origin = self.install_source_fixture(external=True)
        real = origin.parent.with_name(origin.parent.name + '-moved')
        origin.parent.rename(real)
        origin.parent.symlink_to(real, target_is_directory=True)
        import shutil
        self.addCleanup(shutil.rmtree, real)
        self.addCleanup(origin.parent.unlink)
        with self.assertRaisesRegex(RuntimeError, 'External source became a symlink'):
            self.run_commit()
        self.assertFalse(self.lock.exists())

    def test_inherited_source_mapping_cannot_override_actual_export(self):
        self.install_source_fixture(alias=True)
        with patch.dict(os.environ, {commit.VALIDATION_CONTEXT_ENV: json.dumps({'original_root': '/wrong', 'export_root': '/wrong'})}):
            self.assertEqual(self.run_commit(apply=False)['status'], 'validated_dry_run')


if __name__ == '__main__':
    unittest.main()
