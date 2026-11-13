import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from agent_sync import core, history
from agent_sync.__main__ import main


ROOT = Path(__file__).resolve().parents[1]
MAIN_CALL = 'from agent_sync.__main__ import main; raise SystemExit(main())'
HISTORY_CALL = 'from agent_sync.history import main; raise SystemExit(main())'


class CLIEncodingTests(unittest.TestCase):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
        # Keep pipes binary: validate the actual bytes, not the parent's locale.
        return subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=env,
            capture_output=True, shell=False, timeout=15,
        )

    def test_help_utf8_under_cp1252(self):
        for entry, expected in (
            (['-m', 'agent_sync'], '显式同步'),
            (['-m', 'agent_sync', 'history'], '已初始化的'),
            (['-m', 'agent_sync.history'], '已初始化的'),
            (['-c', MAIN_CALL], '显式同步'),
            (['-c', HISTORY_CALL], '已初始化的'),
        ):
            with self.subTest(entry=entry):
                result = self.run_cli(*entry, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b'')
                self.assertIn(expected.encode('utf-8'), result.stdout)
                self.assertIn(expected, result.stdout.decode('utf-8'))

    def test_main_error_utf8_under_cp1252(self):
        for entry in (['-m', 'agent_sync'], ['-c', MAIN_CALL]):
            with self.subTest(entry=entry):
                result = self.run_cli(*entry, 'unbind', 'INVALID')
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, b'')
                expected = 'AgentSync: 同步组名称无效'
                self.assertEqual(result.stderr.rstrip(b'\r\n'), expected.encode('utf-8'))
                self.assertEqual(result.stderr.decode('utf-8').strip(), expected)

    def test_history_json_error_utf8_under_cp1252(self):
        for entry in (
            ['-m', 'agent_sync', 'history'],
            ['-m', 'agent_sync.history'],
            ['-c', HISTORY_CALL],
        ):
            with self.subTest(entry=entry):
                result = self.run_cli(*entry, '--repo', '.', '--max-bytes', '0', 'list')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
                expected = '字节上限必须是正整数'
                self.assertIn(expected.encode('utf-8'), result.stderr)
                self.assertEqual(json.loads(result.stderr.decode('utf-8')), {'error': expected})

    def test_import_does_not_configure_stdio(self):
        script = (
            'import sys; '
            'streams = (sys.stdin, sys.stdout, sys.stderr); '
            'before = [(s.encoding, s.errors) for s in streams]; '
            'assert sys.flags.utf8_mode == 0; '
            'assert all(s.encoding == "cp1252" for s in streams); '
            'import agent_sync.core, agent_sync.__main__, agent_sync.history; '
            'assert all(a is b for a, b in zip(streams, (sys.stdin, sys.stdout, sys.stderr))); '
            'assert before == [(s.encoding, s.errors) for s in streams]'
        )
        result = self.run_cli('-c', script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')

    def test_configure_outputs_in_place_without_touching_stdin(self):
        with io.TextIOWrapper(io.BytesIO(), encoding='cp1252') as output, \
                io.TextIOWrapper(io.BytesIO(), encoding='cp1252') as error:
            stdin = mock.Mock()
            with mock.patch.object(sys, 'stdin', stdin), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                core.configure_stdio()
                self.assertIs(sys.stdout, output)
                self.assertIs(sys.stderr, error)
                self.assertIs(sys.stdin, stdin)
                stdin.reconfigure.assert_not_called()
                output.write('中文输出')
                error.write('中文错误')
                output.flush()
                error.flush()
                self.assertEqual(output.buffer.getvalue(), '中文输出'.encode('utf-8'))
                self.assertEqual(error.buffer.getvalue(), '中文错误'.encode('utf-8'))

    def test_main_accepts_stringio_and_preserves_runner_streams(self):
        original = (sys.stdout, sys.stderr)
        for entry in (main, history.main):
            with self.subTest(entry=entry.__module__):
                output, error = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    with self.assertRaises(SystemExit) as caught:
                        entry(['--help'])
                    self.assertEqual(caught.exception.code, 0)
                    self.assertIs(sys.stdout, output)
                    self.assertIs(sys.stderr, error)
                self.assertIn('--repo', output.getvalue())
                self.assertEqual(error.getvalue(), '')
                self.assertIs(sys.stdout, original[0])
                self.assertIs(sys.stderr, original[1])


if __name__ == '__main__':
    unittest.main()
