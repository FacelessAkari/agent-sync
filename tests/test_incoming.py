import unittest
from unittest.mock import patch

from agent_sync import incoming
from tests import test_local_bridge as fixtures


class IncomingTests(unittest.TestCase):
    setUp = fixtures.FastSyncTests.setUp
    git = fixtures.FastSyncTests.git
    configure = fixtures.FastSyncTests.configure

    def remote_edit(self, path='data/rule.md', data=b'incoming\n'):
        peer = self.base / 'peer'
        if not peer.exists():
            self.git(self.base, 'clone', str(self.bare), str(peer))
            self.configure(peer)
        target = peer / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            target.unlink()
        else:
            target.write_bytes(data)
        self.git(peer, 'add', '--', path)
        self.git(peer, 'commit', '-m', 'peer edit')
        self.git(peer, 'push', 'origin', 'main')
        return self.git(peer, 'rev-parse', 'HEAD').strip()

    def assert_held(self):
        self.assertFalse(incoming.fast_forward(self.repo))
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD').strip(), self.initial_head)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'origin/main').strip(), self.initial_head)

    def test_fast_forward_only_updates_content_checkout(self):
        self.rule.write_bytes(b'local device edit\n')
        original = self.source.read_bytes()
        self.remote_edit()
        tip = self.remote_edit('data/codex/demo/new.md', b'new skill file\n')
        self.assertTrue(incoming.fast_forward(self.repo))
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD').strip(), tip)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'origin/main').strip(), tip)
        self.assertEqual((self.repo / 'data/rule.md').read_bytes(), b'incoming\n')
        self.assertEqual(self.rule.read_bytes(), b'local device edit\n')
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(incoming.fast_forward(self.repo))

    def test_never_moves_head_for_unrelated_history_even_if_tip_is_clean(self):
        self.remote_edit('data/unknown.md')
        self.remote_edit('data/unknown.md', None)
        self.remote_edit()
        self.assert_held()

    def test_rejects_deletion_binary_or_dirty_checkout(self):
        self.remote_edit('data/rule.md', b'\0\xff')
        self.assert_held()
        self.remote_edit('data/rule.md', None)
        self.assert_held()
        self.remote_edit('data/rule.md', b'new')
        (self.repo / 'data/rule.md').write_bytes(b'checkout modification')
        self.assert_held()
        self.git(self.repo, 'add', '--', 'data/rule.md')
        self.assert_held()

    def test_revalidates_real_tip_before_checkout(self):
        from agent_sync.local_bridge import remote_head
        tip = self.remote_edit()

        def racing_tip(repo, remote, ref):
            value = remote_head(repo, remote, ref)
            calls.append(value)
            return '0' * 40 if len(calls) == 4 else value

        calls = []
        with patch('agent_sync.local_bridge.remote_head', side_effect=racing_tip):
            self.assert_held()
        self.assertEqual(calls, [tip.decode()] * 4)

    def test_declines_unfinished_operation_and_divergent_local_commit(self):
        self.remote_edit()
        marker = self.repo / '.git/MERGE_HEAD'
        marker.write_bytes(self.initial_head + b'\n')
        self.assert_held()
        marker.unlink()
        (self.repo / 'data/rule.md').write_bytes(b'locally committed\n')
        self.git(self.repo, 'commit', '-am', 'local change')
        head = self.git(self.repo, 'rev-parse', 'HEAD')
        self.assertFalse(incoming.fast_forward(self.repo))
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'origin/main').strip(), self.initial_head)
