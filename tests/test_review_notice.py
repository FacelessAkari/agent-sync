import base64
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_sync import core, review_notice


class ReviewNoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve() / 'repo'
        core.init_repo(self.repo)
        self.rows = [{'path': 'example private name.md'}]
        self.signature = core._digest(b'first synthetic conflict')
        self.state = self.repo / '.agent-sync/conflicts/notice.json'

    def test_same_batch_is_quiet_until_its_content_changes_or_is_cleared(self):
        with mock.patch.object(review_notice, '_deliver', return_value=True) as send:
            notify = review_notice.notify_pending
            self.assertEqual(notify(self.repo, [], None), 'clear')
            self.assertEqual(notify(self.repo, self.rows, self.signature), 'sent')
            self.assertEqual(notify(self.repo, self.rows, self.signature), 'unchanged')
            new = core._digest(b'edited same file')
            self.assertEqual(notify(self.repo, self.rows, new), 'sent')
            self.assertNotIn('private', self.state.read_text(encoding='utf-8'))
            self.assertEqual(notify(self.repo, [], None), 'clear')
            self.assertFalse(self.state.exists())
            self.assertEqual(notify(self.repo, self.rows, self.signature), 'sent')
            self.assertEqual(send.call_args_list, [mock.call(1)] * 3)

    def test_disabled_or_timed_out_notifications_retry_and_corrupt_state_recovers(self):
        errors = [False, subprocess.TimeoutExpired('test', 1), True, True]
        with mock.patch.object(review_notice, '_deliver', side_effect=errors):
            for wanted in ['unavailable', 'unavailable', 'sent']:
                self.assertEqual(review_notice.notify_pending(self.repo, self.rows, self.signature), wanted)
            self.state.write_bytes(b'broken JSON')
            self.assertEqual(review_notice.notify_pending(self.repo, self.rows, self.signature), 'sent')

    def test_native_commands_use_fixed_text_and_only_include_the_count(self):
        with mock.patch.object(review_notice.subprocess, 'run') as runner:
            runner.return_value.returncode = 0
            with mock.patch.object(review_notice.sys, 'platform', 'win32'), \
                 mock.patch.dict(review_notice.os.environ, {'SystemRoot': 'C:/Windows'}):
                self.assertTrue(review_notice._deliver(12))
            args = runner.call_args.args[0]
            self.assertTrue(args[0].replace('\\', '/').endswith('System32/WindowsPowerShell/v1.0/powershell.exe'))
            script = base64.b64decode(args[-1]).decode('utf-16le')
            self.assertIn("AgentSync.LocalSync.Review", script)
            self.assertIn('12 个文件', script)
            self.assertNotIn(self.rows[0]['path'], script)
            self.assertFalse(runner.call_args.kwargs['shell'])
            with mock.patch.object(review_notice.sys, 'platform', 'darwin'):
                self.assertTrue(review_notice._deliver(12))
            args = runner.call_args.args[0]
            self.assertEqual(args[:2], ['/usr/bin/osascript', '-e'])
            self.assertEqual(args[-1], '12')
            self.assertNotIn(self.rows[0]['path'], args[2])
            runner.return_value.returncode = 2
            with mock.patch.object(review_notice.sys, 'platform', 'darwin'):
                self.assertFalse(review_notice._deliver(12))
