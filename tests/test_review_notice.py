import base64
import os
from pathlib import Path
import subprocess
import sys
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
            self.assertEqual(send.call_args_list, [mock.call(1, self.state.with_name('pending.txt'))] * 3)

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
                self.assertTrue(review_notice._deliver(12, self.state.with_name('pending.txt')))
                first = runner.call_args.kwargs['env']['AGENT_SYNC_REVIEW_INSTANCE']
                self.assertTrue(review_notice._deliver(12, self.repo / 'other' / 'pending.txt'))
                second = runner.call_args.kwargs['env']['AGENT_SYNC_REVIEW_INSTANCE']
                self.assertNotEqual(first, second)
                self.assertRegex(first, r'^[0-9a-f]{16}$')
            args = runner.call_args.args[0]
            self.assertTrue(args[0].replace('\\', '/').endswith('System32/WindowsPowerShell/v1.0/powershell.exe'))
            script = base64.b64decode(args[-1]).decode('utf-16le')
            self.assertIn("AgentSync.LocalSync.Review", script)
            self.assertIn('12 个文件', script)
            self.assertNotIn(self.rows[0]['path'], script)
            self.assertFalse(runner.call_args.kwargs['shell'])
            with mock.patch.object(review_notice.sys, 'platform', 'darwin'):
                self.assertTrue(review_notice._deliver(12, self.state.with_name('pending.txt')))
            args = runner.call_args.args[0]
            self.assertEqual(args[:2], ['/usr/bin/osascript', '-e'])
            self.assertEqual(args[-1], '12')
            self.assertNotIn(self.rows[0]['path'], args[2])
            runner.return_value.returncode = 2
            with mock.patch.object(review_notice.sys, 'platform', 'darwin'):
                self.assertFalse(review_notice._deliver(12, self.state.with_name('pending.txt')))

    if sys.platform == 'win32':
        def test_foreign_shortcut_is_preserved(self):
            # Exercise the actual COM link on a temporary directory; no OS registration or toast.
            programs = self.repo / '测试 programs'
            programs.mkdir()
            instance = '1234567890abcdef'
            script = review_notice._WINDOWS_IDENTITY + r"""
$folder = $env:AGENT_SYNC_TEST_PROGRAMS
$path = Join-Path $folder ('AgentSync 待确认清单 - ' + $env:AGENT_SYNC_REVIEW_INSTANCE + '.lnk')
$target = Join-Path $PSHOME 'powershell.exe'
$arguments = '"另一份 report.txt"'
[AgentSyncIdentity]::CreateShortcut($path, $target, $arguments, 'Example.Foreign')
if (-not [AgentSyncIdentity]::MatchesShortcut($path, $target, $arguments, 'Example.Foreign')) {
    throw 'The synthetic Unicode shell link failed its native readback.'
}
"""
            args = [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
                    '-NoProfile', '-NonInteractive', '-EncodedCommand']
            env = dict(os.environ, AGENT_SYNC_TEST_PROGRAMS=str(programs),
                       AGENT_SYNC_REVIEW_INSTANCE=instance, AGENT_SYNC_REVIEW_REPORT=str(self.state.with_name('pending.txt')))
            def run(source, expected):
                diagnostics = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
                               "$ProgressPreference = 'SilentlyContinue'\n$ErrorActionPreference = 'Stop'\n"
                               "try {\n" + source + "\n} catch { [Console]::Error.WriteLine("
                               "$_.Exception.GetBaseException().Message + ' at ' + $_.InvocationInfo.PositionMessage); exit 1 }")
                encoded = base64.b64encode(diagnostics.encode('utf-16le')).decode('ascii')
                result = subprocess.run([*args, encoded], capture_output=True, timeout=20, env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, expected, result.stderr.decode('utf-8', errors='replace'))
            run(script, 0)
            shortcut = next(programs.glob('*.lnk'))
            before = shortcut.read_bytes()
            run(review_notice._WINDOWS_IDENTITY + '\n$folder = $env:AGENT_SYNC_TEST_PROGRAMS\n'
                + review_notice._WINDOWS_SHORTCUT, 2)
            self.assertEqual(shortcut.read_bytes(), before)
