"""Boundary regressions: temporary local bare Git and isolated HOME only.

Targeted run:
    python -m unittest discover -s tests -p test_bridge_boundaries.py -v
"""
import os
from pathlib import Path
import unittest
from unittest import mock

from agent_sync import core, gitops, private_remote
from agent_sync import local_bridge as fast

try:
    from . import test_local_bridge as fixtures
except ImportError:
    import test_local_bridge as fixtures


class BridgeBoundaryTests(unittest.TestCase):
    # Reuse fixture methods, not its TestCase inheritance or a module-level
    # imported TestCase class: discover must not collect its tests again here.
    git = fixtures.FastSyncTests.git
    configure = fixtures.FastSyncTests.configure
    invoke = fixtures.FastSyncTests.invoke
    prime = fixtures.FastSyncTests.prime
    snapshot = fixtures.FastSyncTests.snapshot
    bare_head = fixtures.FastSyncTests.bare_head
    bare_bytes = fixtures.FastSyncTests.bare_bytes

    def setUp(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        environment = mock.patch.dict(os.environ, clean, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        fixtures.FastSyncTests.setUp(self)

    def assert_blocked(self, status='error', before_refresh=True):
        before = self.snapshot()
        with mock.patch.object(core, 'apply', side_effect=AssertionError('blocked apply')), \
             mock.patch.object(fast.collector, 'collect', side_effect=AssertionError('blocked collect')), \
             mock.patch.object(gitops, 'git_sync', side_effect=AssertionError('blocked upload')):
            for check in (True, False, False, True):
                with self.subTest(check=check):
                    if before_refresh:
                        with mock.patch.object(fast.sources, 'refresh', side_effect=AssertionError('blocked refresh')):
                            result, code = self.invoke(check)
                    else:
                        result, code = self.invoke(check)
                    self.assertEqual((result['status'], code), (status, 2), result)
                    self.assertFalse(result['uploaded'])
                    self.assertEqual(self.snapshot(), before)

    def publish_manifest(self):
        self.git(self.repo, 'add', '--', 'manifest.json')
        self.git(self.repo, 'commit', '-m', 'boundary fixture manifest')
        self.git(self.repo, 'push', 'origin', 'main')

    def test_info_exclude_new_binding_and_legacy_baseline_repeat_then_recover(self):
        local = self.root / 'settings.yaml'
        local.write_bytes(b'fixture_setting: true\n')
        core.add_group(self.repo, 'settings', 'rules', 'file', 'data/settings.yaml')
        core.bind(self.repo, 'settings', local)
        self.publish_manifest()
        exclude = self.repo / '.git/info/exclude'
        original = exclude.read_bytes()
        exclude.write_bytes(original + b'\n*.yaml\n')
        remote = self.bare_head()

        # The first prospective push must fail before data/baseline are written.
        self.assert_blocked(before_refresh=False)
        self.assertFalse((self.repo / 'data/settings.yaml').exists())
        self.assertEqual(self.bare_head(), remote)

        # Recreate the old failure's persisted data + baseline using real core.
        # Git status now conceals the untracked managed file completely.
        core.apply(self.repo, 'push')
        with fast.safe_git():
            self.assertEqual(self.git(self.repo, 'status', '--porcelain', '--untracked-files=all'), b'')
        self.assert_blocked()
        self.assertEqual(self.bare_head(), remote)
        self.assertNotIn(b'data/settings.yaml\n', self.git(self.bare, 'ls-tree', '-r', '--name-only', 'main'))

        exclude.write_bytes(original)
        result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(self.bare_bytes('data/settings.yaml'), local.read_bytes())
        self.assertIn(b'data/settings.yaml\0', self.git(self.repo, 'ls-files', '-z'))

        # Matching ignore rules are harmless once the exact file is tracked.
        exclude.write_bytes(original + b'\n*.yaml\n')
        local.write_bytes(b'fixture_setting: false\n')
        result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(self.bare_bytes('data/settings.yaml'), local.read_bytes())

    def test_primed_fast_path_rechecks_ignored_unbound_data(self):
        exclude = self.repo / '.git/info/exclude'
        exclude.write_bytes(exclude.read_bytes() + b'\n*.yaml\n')
        self.prime()
        checkpoint = (self.root / 'fast-sync-state.json').read_bytes()
        (self.repo / 'data/unbound.yaml').write_bytes(b'unbound: fixture\n')
        self.assert_blocked()
        self.assertEqual((self.root / 'fast-sync-state.json').read_bytes(), checkpoint)
        self.assertEqual(self.bare_head(), self.initial_head)

    def test_global_excludes_content_change_without_config_change(self):
        excludes = self.base / 'home/global-ignore'
        excludes.write_bytes(b'# initially empty\n')
        self.git(self.repo, 'config', '--global', 'core.excludesFile', str(excludes))
        self.prime()
        config = self.git(self.repo, 'config', '--null', '--list', '--show-origin', '--show-scope')
        checkpoint = (self.root / 'fast-sync-state.json').read_bytes()
        excludes.write_bytes(b'*.yaml\n')
        (self.repo / 'data/global-unbound.yaml').write_bytes(b'global: fixture\n')
        self.assertEqual(self.git(self.repo, 'config', '--null', '--list', '--show-origin', '--show-scope'), config)
        self.assert_blocked()
        self.assertEqual((self.root / 'fast-sync-state.json').read_bytes(), checkpoint)
        excludes.write_bytes(b'# fixed\n')
        result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(self.bare_bytes('data/global-unbound.yaml'), b'global: fixture\n')

    def test_nested_ignore_is_checked_for_prospective_literal_file(self):
        nested = self.repo / 'data/nested'
        nested.mkdir()
        (nested / '.gitignore').write_bytes(b'*.yaml\n')
        before = self.snapshot()
        with fast.safe_git():
            with self.assertRaises(core.Error):
                gitops._stageable(self.repo, ['data/nested/settings.yaml'])
            gitops._stageable(self.repo, ['data/nested/ordinary.md'])
        self.assertEqual(self.snapshot(), before)

    def test_git_sync_rejects_all_candidates_before_any_partial_stage(self):
        exclude = self.repo / '.git/info/exclude'
        original = exclude.read_bytes()
        exclude.write_bytes(original + b'\n*.yaml\n')
        # More than one staging batch: a late ignored candidate must not leave
        # earlier allowed files staged, committed, or pushed.
        for number in range(70):
            (self.repo / ('data/pending-%03d.md' % number)).write_bytes(b'fixture\n')
        (self.repo / 'data/zz-settings.yaml').write_bytes(b'late: fixture\n')
        before = self.snapshot()
        for attempt in range(2):
            with self.subTest(attempt=attempt), fast.safe_git():
                with mock.patch.object(gitops, '_git', wraps=gitops._git) as commands:
                    with self.assertRaises(core.Error):
                        gitops.git_sync(self.repo)
                    self.assertFalse(any('add' in call.args[1] for call in commands.call_args_list))
            self.assertEqual(self.snapshot(), before)
        exclude.write_bytes(original)
        with fast.safe_git():
            self.assertEqual(gitops.git_sync(self.repo)['status'], 'pushed')
        self.assertEqual(self.bare_bytes('data/zz-settings.yaml'), b'late: fixture\n')
        self.assertEqual(self.bare_bytes('data/pending-000.md'), b'fixture\n')

    def test_ordinary_caches_remain_ignored_and_never_force_added(self):
        self.prime()
        cache = self.repo / 'data/__pycache__'
        cache.mkdir()
        (cache / 'fixture.pyc').write_bytes(b'cache fixture')
        (self.repo / 'data/.DS_Store').write_bytes(b'finder fixture')
        (self.repo / 'data/fixture.pyc').write_bytes(b'cache fixture')
        result, code = self.invoke()
        self.assertEqual((result['status'], code), ('up_to_date', 0), result)
        self.rule.write_bytes(b'ordinary external edit\n')
        with mock.patch.object(gitops, '_git', wraps=gitops._git) as commands:
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        for call in commands.call_args_list:
            args = call.args[1]
            if 'add' in args:
                self.assertNotIn('-f', args)
                self.assertNotIn('--force', args)
        tree = self.git(self.bare, 'ls-tree', '-r', '--name-only', 'main')
        for name in (b'__pycache__', b'.pyc', b'.DS_Store'):
            self.assertNotIn(name, tree)
        self.assertEqual((cache / 'fixture.pyc').read_bytes(), b'cache fixture')

    def test_direct_file_inside_source_rejected_before_any_write(self):
        source = self.base / 'program'
        source.mkdir()
        (source / 'sentinel.md').write_bytes(b'read-only source\n')
        # Existing private repository rule would otherwise be pulled here.
        target = source / 'private-rule.md'
        core.bind(self.repo, 'rules', target)
        with mock.patch.object(private_remote, 'SOURCE_ROOT', source):
            self.assert_blocked('unsafe_path')
        self.assertFalse(target.exists())
        self.assertEqual(core._load(self.repo)[1]['rules'], str(target))

    def test_direct_tree_inside_or_ancestor_of_source_rejected(self):
        parent = self.base / 'program-parent'
        source = parent / 'installed'
        source.mkdir(parents=True)
        (source / 'sentinel.md').write_bytes(b'read-only source\n')
        core.add_group(self.repo, 'boundary', 'rules', 'tree', 'data/boundary')
        for target in (source / 'nested', source, parent):
            with self.subTest(target=target):
                core.bind(self.repo, 'boundary', target)
                with mock.patch.object(private_remote, 'SOURCE_ROOT', source):
                    self.assert_blocked('unsafe_path')
                self.assertEqual(core._load(self.repo)[1]['boundary'], str(target))

    def test_file_binding_ancestor_is_rejected_before_materialization(self):
        # A missing file target can lexically enclose an installed source root.
        # No such file or source directory is materialized: overlap rejection
        # must not depend on the target's current filesystem kind/existence.
        target = self.base / 'missing-program-container.md'
        core.bind(self.repo, 'rules', target)
        with mock.patch.object(private_remote, 'SOURCE_ROOT', target / 'installed'):
            self.assert_blocked('unsafe_path')
        self.assertFalse(target.exists())

    def test_enclosing_source_git_worktree_is_also_protected(self):
        worktree = self.base / 'program-worktree'
        worktree.mkdir()
        self.git(worktree, 'init', '--initial-branch=main')
        installed = worktree / 'package'
        installed.mkdir()
        target = worktree / 'sibling-rule.md'
        core.bind(self.repo, 'rules', target)
        with mock.patch.object(private_remote, 'SOURCE_ROOT', installed):
            self.assertIn(worktree, private_remote.source_roots())
            self.assert_blocked('unsafe_path')
        self.assertFalse(target.exists())

    def test_normal_external_direct_binding_can_pull_without_source_writes(self):
        source = self.base / 'program'
        source.mkdir()
        sentinel = source / 'sentinel.md'
        sentinel.write_bytes(b'read-only program fixture\n')
        target = self.base / 'external-rule.md'
        core.bind(self.repo, 'rules', target)
        before = self.snapshot()
        with mock.patch.object(private_remote, 'SOURCE_ROOT', source):
            result, code = self.invoke(check=True)
            self.assertEqual((result['status'], code), ('changes_pending', 0), result)
            self.assertEqual(self.snapshot(), before)
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['direct_pull_files'], 1)
        self.assertEqual(target.read_bytes(), b'initial rule\n')
        self.assertEqual(sentinel.read_bytes(), b'read-only program fixture\n')
        self.assertEqual(self.bare_head(), self.initial_head)


if __name__ == '__main__':
    unittest.main()
