import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_sync import core, history
from agent_sync.__main__ import main


class MainHistoryTests(unittest.TestCase):
    def test_forwarding_preserves_global_repo_and_history_options(self):
        argv = ['--repo', 'content with spaces', 'history', '--max-bytes', '4096', 'list']
        before = list(sys.argv)
        with mock.patch.object(history, 'main', return_value=7) as delegate:
            self.assertEqual(main(argv), 7)
        delegate.assert_called_once_with(['--repo', 'content with spaces', '--max-bytes', '4096', 'list'])
        self.assertEqual(sys.argv, before)

    def test_help_is_delegated(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main(['--repo', '.', 'history', '--help'])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('codex-restore', output.getvalue())

    def test_both_module_entrypoints_and_init(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve() / 'content with spaces'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--repo', str(repo), 'init']), 0)
            core._load(repo)
            for module, tail in (
                ('agent_sync', ['history', '--max-bytes', '4096', 'list']),
                ('agent_sync.history', ['--max-bytes', '4096', 'list']),
                ('agent_sync', ['history', 'capability']),
            ):
                result = subprocess.run(
                    [sys.executable, '-m', module, '--repo', str(repo), *tail],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                    cwd=Path(__file__).resolve().parents[1],
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                value = json.loads(result.stdout)
                if tail[-1] == 'list':
                    self.assertEqual(value, [])
                else:
                    self.assertIn('roundtrip', value['codex'])


if __name__ == '__main__':
    unittest.main()
