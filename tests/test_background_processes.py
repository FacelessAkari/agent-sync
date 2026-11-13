import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_sync import codex_rpc, core, gitops, private_remote


FAKE_RPC = r'''import json
import sys

methods = []
for line in sys.stdin.buffer:
    request = json.loads(line)
    method = request['method']
    methods.append(method)
    if method == 'initialized':
        assert 'id' not in request
        continue
    if method == 'initialize':
        assert request['params']['clientInfo']['name'] == 'agent_sync_history'
        result = {}
    elif method == 'echo':
        result = {'echo': request['params'], 'methods': methods}
    else:
        raise RuntimeError('unexpected method')
    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
    sys.stdout.buffer.write((json.dumps(response) + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()
'''


class BackgroundProcessesTests(unittest.TestCase):
    def test_rpc_real_handshake_echo_and_close(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'fake_rpc.py'
            script.write_text(FAKE_RPC, encoding='utf-8')
            # Isolated Python: no user site, startup hooks or real Codex config.
            argv = [sys.executable, '-I', '-S', '-u', str(script)]
            rpc = codex_rpc.RPC(argv, timeout=10)
            with mock.patch.object(codex_rpc.subprocess, 'Popen',
                                   wraps=subprocess.Popen) as popen:
                with rpc:
                    self.assertEqual(rpc.call('echo', {'text': 'hello'}), {
                        'echo': {'text': 'hello'},
                        'methods': ['initialize', 'initialized', 'echo'],
                    })
                    popen.assert_called_once_with(
                        argv, shell=False, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        bufsize=0, creationflags=flags)
                    self.assertFalse(rpc.process.text_mode)
                    self.assertIsNone(rpc.process.poll())
                self.assertIsNotNone(rpc.process.poll())
                self.assertFalse(rpc.reader.is_alive())
                self.assertFalse(rpc.writer.is_alive())
                self.assertTrue(rpc.process.stdin.closed)
                self.assertTrue(rpc.process.stdout.closed)

    def test_git_kwargs_returncodes_and_redaction(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        env = {'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            argv = ['git', '-C', str(repo), 'status', '--porcelain']
            with mock.patch.object(private_remote, 'git_environment', return_value=env), \
                 mock.patch.object(gitops.subprocess, 'run') as run:
                completed = subprocess.CompletedProcess(
                    argv, 0, b'secret-stdout', b'secret-stderr')
                run.return_value = completed
                self.assertIs(gitops._git(repo, argv[3:], 'status'), completed)
                run.assert_called_once_with(
                    argv, shell=False, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=120, env=env,
                    creationflags=flags)
                self.assertIs(run.call_args.kwargs['env'], env)
                completed.returncode = 7
                self.assertIs(gitops._git(repo, argv[3:], 'status', allowed=(7,)),
                              completed)
                with self.assertRaises(core.Error) as caught:
                    gitops._git(repo, argv[3:], 'status')
                self.assertIn('(exit 7)', str(caught.exception))
                self.assertIn('已隐藏', str(caught.exception))
                self.assertNotIn('secret-stdout', str(caught.exception))
                self.assertNotIn('secret-stderr', str(caught.exception))
                for failure in (
                    OSError('secret-spawn'),
                    subprocess.TimeoutExpired(argv, 120, output=b'secret-stdout',
                                              stderr=b'secret-stderr'),
                ):
                    with self.subTest(failure=type(failure).__name__):
                        run.side_effect = failure
                        with self.assertRaises(core.Error) as caught:
                            gitops._git(repo, argv[3:], 'status')
                        self.assertIn('Git 步骤无法完成: status', str(caught.exception))
                        self.assertNotIn('secret-', str(caught.exception))
                        self.assertIs(caught.exception.__cause__, failure)


if __name__ == '__main__':
    unittest.main()
