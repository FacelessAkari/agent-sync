import unittest
from unittest import mock

from agent_sync import core, gitops, local_bridge, partial_sync
from tests import test_local_bridge


class PartialSyncTests(unittest.TestCase):
    # Reuse the isolated fixture, including its real bare origin and two skill bindings.
    git = test_local_bridge.FastSyncTests.git
    configure = test_local_bridge.FastSyncTests.configure

    def setUp(self):
        test_local_bridge.FastSyncTests.setUp(self)
        self.device = self.root / 'memory'
        self.device.mkdir()
        self.folder = self.repo / 'data/notes'
        for name in ('conflict.md', 'pull.txt', 'delete.md', 'helper.py'):
            (self.device / name).write_bytes(b'original\n')
        core.add_group(self.repo, 'notes', 'memory', 'tree', 'data/notes')
        core.bind(self.repo, 'notes', self.device)
        core.apply(self.repo, 'push')
        self.git(self.repo, 'add', '--', 'manifest.json', 'data/notes')
        self.git(self.repo, 'commit', '-m', 'synthetic direct bindings')
        self.git(self.repo, 'push')
        self.head = self.git(self.repo, 'rev-parse', 'HEAD').strip()

    def advance(self, change_after_snapshot=None, skill_scope=None):
        counts = {'direct_pull_files': 0, 'uploaded': False}
        with local_bridge.safe_git() as guard:
            facts = local_bridge.git_gate(self.repo)
            groups, bindings, baseline = core._load(self.repo)
            _, snapshots = core._collect(self.repo, groups, bindings, baseline)
            if change_after_snapshot is not None:
                change_after_snapshot()
            partial_sync.advance(self.repo, groups, bindings, snapshots, {'codex', 'shared'},
                                 guard, facts, counts, skill_scope=skill_scope)
        return counts

    def test_mixed_changes_preserve_blocked_files_and_commit_only_aligned_text(self):
        self.rule.write_bytes(b'push rule\n')
        (self.folder / 'pull.txt').write_bytes(b'pull this\n')
        (self.folder / 'conflict.md').write_bytes(b'repo edit\n')
        (self.device / 'conflict.md').write_bytes(b'device edit\n')
        (self.folder / 'delete.md').unlink()
        (self.folder / 'helper.py').write_bytes(b'# new code\n')
        (self.repo / 'data/codex/demo/SKILL.md').write_bytes(b'await installation\n')
        (self.repo / 'data/extra.md').write_bytes(b'unrelated new file\n')
        counts = self.advance()
        self.assertEqual(counts, {'direct_pull_files': 1, 'uploaded': True, 'partial_push_files': 2})
        self.assertEqual((self.device / 'pull.txt').read_bytes(), b'pull this\n')
        self.assertEqual((self.device / 'helper.py').read_bytes(), b'original\n')
        self.assertEqual((self.device / 'delete.md').read_bytes(), b'original\n')
        self.assertEqual((self.device / 'conflict.md').read_bytes(), b'device edit\n')
        self.assertEqual((self.folder / 'conflict.md').read_bytes(), b'repo edit\n')
        self.assertEqual(self.git(self.bare, 'show', 'main:data/notes/conflict.md'), b'original\n')
        self.assertEqual(self.git(self.bare, 'show', 'main:data/rule.md'), b'push rule\n')
        self.assertEqual(self.git(self.bare, 'diff', '--name-only', self.head.decode(), 'main').splitlines(),
                         [b'data/notes/pull.txt', b'data/rule.md'])
        self.assertEqual(self.git(self.repo, 'diff', '--cached', '--name-only'), b'')
        self.assertTrue(list((self.repo / '.agent-sync/backups').glob('txn-*/notes/pull.txt')))
        self.assertEqual(self.advance(), {'direct_pull_files': 0, 'uploaded': False})

    def test_stale_snapshot_refuses_even_the_other_eligible_files(self):
        self.rule.write_bytes(b'push me\n')
        (self.folder / 'pull.txt').write_bytes(b'copy me\n')
        with self.assertRaises(core.Error):
            self.advance(lambda: self.rule.write_bytes(b'another edit\n'))
        self.assertEqual((self.repo / 'data/rule.md').read_bytes(), b'initial rule\n')
        self.assertEqual((self.device / 'pull.txt').read_bytes(), b'original\n')

    def test_dirty_manifest_and_index_are_preserved_before_local_writes(self):
        manifest = self.repo / 'manifest.json'
        original = manifest.read_bytes()
        self.rule.write_bytes(b'push me\n')
        manifest.write_bytes(original + b'\n')
        with self.assertRaisesRegex(core.Error, 'manifest'):
            self.advance()
        manifest.write_bytes(original)
        (self.folder / 'helper.py').write_bytes(b'# other staged work\n')
        self.git(self.repo, 'add', '--', 'data/notes/helper.py')
        with self.assertRaises(local_bridge.Stop):
            self.advance()
        self.assertEqual(self.git(self.repo, 'diff', '--cached', '--name-only'), b'data/notes/helper.py\n')
        self.assertEqual((self.repo / 'data/rule.md').read_bytes(), b'initial rule\n')

    def test_partial_git_rejects_stale_head_hash_and_deletion(self):
        name = 'data/notes/pull.txt'
        (self.repo / name).write_bytes(b'new\n')
        with self.assertRaises(core.Error):
            gitops.git_sync(self.repo, paths={name}, expected_head='0' * len(self.head))
        with self.assertRaises(core.Error):
            gitops.git_sync(self.repo, paths={name: core._digest(b'older')})
        (self.repo / name).unlink()
        with self.assertRaises(core.Error):
            gitops.git_sync(self.repo, paths={name})
        self.assertEqual(self.git(self.bare, 'rev-parse', 'main').strip(), self.head)
        self.assertEqual(self.git(self.repo, 'diff', '--cached', '--name-only'), b'')

    def test_pinned_skill_package_includes_code_and_rejects_partial_or_stale_evidence(self):
        script = self.mirror.with_name('helper.py')
        script.write_bytes(b'# selected script\n')
        self.mirror.write_bytes(b'new docs\n')
        self.rule.write_bytes(b'also ready\n')
        package = {('codex', 'demo/' + p.name): core._digest(p.read_bytes()) for p in (self.mirror, script)}
        for broken in (package | {('codex', 'demo/helper.py'): '0' * 64},
                       package | {('codex', 'demo/gone.md'): '0' * 64}):
            with self.assertRaises(core.Error):
                self.advance(skill_scope=broken)
            self.assertEqual(self.git(self.bare, 'rev-parse', 'main').strip(), self.head)
            self.assertFalse((self.repo / 'data/codex/demo/helper.py').exists())
        counts = self.advance(skill_scope=package)
        self.assertEqual(counts['partial_skill_files'], 2)
        self.assertEqual(counts['partial_push_files'], 3)
        self.assertEqual(self.git(self.bare, 'show', 'main:data/codex/demo/helper.py'), script.read_bytes())
        self.assertEqual(self.source.read_bytes(), self.initial)

    def test_failed_push_leaves_the_real_index_and_branch_retryable(self):
        name = 'data/notes/pull.txt'
        (self.repo / name).write_bytes(b'pending upload\n')
        original_git = gitops._git
        def fail_push(repo, args, label, allowed=(0,)):
            if args[0] == 'push':
                raise core.Error('test unavailable origin')
            return original_git(repo, args, label, allowed)
        with mock.patch.object(gitops, '_git', side_effect=fail_push):
            with self.assertRaisesRegex(core.Error, 'test unavailable'):
                gitops.git_sync(self.repo, paths={name})
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual(self.git(self.repo, 'diff', '--cached', '--name-only'), b'')
        self.assertEqual(self.git(self.bare, 'rev-parse', 'main').strip(), self.head)
        self.assertEqual(gitops.git_sync(self.repo, paths={name})['status'], 'pushed')
