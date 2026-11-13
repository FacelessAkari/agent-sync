import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

from agent_sync import core, gitops, private_remote as remote


class Response(io.BytesIO):
    status = 200


class PrivateRemoteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent-sync-private-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = self.base / 'content'
        self.repo.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        config = self.home / 'gitconfig'
        config.write_bytes(b'')
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'USERPROFILE': str(self.home),
            'XDG_CONFIG_HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': str(config), 'GH_TOKEN': 'fake-token', 'GITHUB_TOKEN': ''})
        env.start()
        self.addCleanup(env.stop)
        self.git('init', '--initial-branch=main')
        self.git('remote', 'add', 'origin', 'https://github.com/example/knowledge.git')
        self.git('config', 'branch.main.remote', 'origin')

    def git(self, *args):
        return gitops._git(self.repo, list(args), 'isolated test').stdout

    def metadata(self, document):
        raw = document if isinstance(document, bytes) else json.dumps(document).encode()
        return mock.patch('urllib.request.OpenerDirector.open', return_value=Response(raw))

    def snapshot(self):
        return {str(p.relative_to(self.base)): p.read_bytes()
                for p in self.base.rglob('*') if p.is_file()}

    def test_local_absolute_and_file_urls_use_no_authentication(self):
        bare = self.base / 'remote.git'
        gitops._git(self.base, ['init', '--bare', str(bare)], 'local bare')
        with mock.patch.object(remote, '_token', side_effect=AssertionError('credential')), \
             mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            for url in (str(bare), bare.as_uri()):
                self.git('remote', 'set-url', 'origin', url)
                self.assertEqual(remote.require_private(self.repo)['visibility'], 'local')
        for url in ('C:\\offline\\repo.git', 'C:/offline/repo.git'):
            with self.subTest(url=url):
                if os.name == 'nt':
                    self.assertEqual(remote.classify_url(url), ('local', None))
                else:
                    with self.assertRaises(core.Error):
                        remote.classify_url(url)

    def test_non_windows_rejects_drive_paths(self):
        with mock.patch.object(remote.os, 'name', 'posix'):
            for url in ('C:\\offline\\repo.git', 'C:/offline/repo.git'):
                with self.subTest(url=url), self.assertRaises(core.Error):
                    remote.classify_url(url)
            self.assertEqual(remote.classify_url('/offline/repo.git'), ('local', None))
            self.assertEqual(remote.classify_url('file:///offline/repo.git'), ('local', None))

    def test_url_grammar_rejects_network_local_disguises_and_credentials(self):
        for url in ('\\\\server\\share', '//server/share', 'file://server/share',
                    'file:////server/share', 'file:///%5Cserver/share',
                    'https://user:secret@github.com/example/knowledge',
                    'https://github.com/example/knowledge?x=1',
                    'https://github.com/example/knowledge#fragment',
                    'http://github.com/example/knowledge', 'https://evil.invalid/a/b',
                    'ssh://git@github.com:22/example/knowledge',
                    'git@evil.invalid:example/knowledge', 'relative/repo.git',
                    'https://github.com/example/../knowledge'):
            with self.subTest(url=url), self.assertRaises(core.Error):
                remote.classify_url(url)
        for url in ('https://github.com/example/knowledge.git',
                    'git@github.com:example/knowledge.git',
                    'ssh://git@github.com/example/knowledge.git'):
            self.assertEqual(remote.classify_url(url), ('private', 'example/knowledge'))

    def test_metadata_is_strict_and_is_never_cached(self):
        with self.metadata({'private': True, 'full_name': 'example/knowledge'}) as transport:
            self.assertEqual(remote.require_private(self.repo)['visibility'], 'private')
            request = transport.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.github.com/repos/example/knowledge')
            self.assertEqual(request.get_header('Authorization'), 'Bearer fake-token')
            self.assertLessEqual(transport.call_args.kwargs['timeout'], 30)
        before = self.snapshot()
        for value in ({'private': False, 'full_name': 'example/knowledge'},
                      {'private': 1, 'full_name': 'example/knowledge'},
                      {'private': True, 'full_name': 'example/other'},
                      {'private': True}, [], b'not json',
                      b'{"private":false,"private":true,"full_name":"example/knowledge"}'):
            with self.metadata(value), self.assertRaises(core.Error) as error:
                remote.require_private(self.repo)
            self.assertNotIn('fake-token', str(error.exception))
        self.assertEqual(before, self.snapshot())

    def test_public_git_sync_stops_before_lock_fetch_or_staging(self):
        before = self.snapshot()
        with self.metadata({'private': False, 'full_name': 'example/knowledge'}), \
             mock.patch.object(core, '_lock', side_effect=AssertionError('lock before gate')):
            with self.assertRaises(core.Error):
                gitops.git_sync(self.repo)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.repo / '.agent-sync').exists())

    def test_ambiguous_push_and_rewritten_urls_stop_before_credentials(self):
        with mock.patch.object(remote, '_token', side_effect=AssertionError('credential')):
            self.git('config', 'remote.origin.pushurl', 'https://github.com/example/other.git')
            with self.assertRaises(core.Error):
                remote.require_private(self.repo)
            self.git('config', '--unset-all', 'remote.origin.pushurl')
            self.git('config', '--add', 'remote.origin.url', 'https://github.com/example/other.git')
            with self.assertRaises(core.Error):
                remote.require_private(self.repo)
            self.git('config', '--unset-all', 'remote.origin.url')
            self.git('config', 'remote.origin.url', 'https://github.com/example/knowledge.git')
            self.git('config', 'url.https://evil.invalid/.insteadOf', 'https://github.com/')
            with self.assertRaises(core.Error):
                remote.require_private(self.repo)

    def test_credential_fill_has_fixed_stdin_sanitized_env_and_no_disk_output(self):
        before = self.snapshot()
        completed = subprocess.CompletedProcess([], 0,
                                                b'username=test\npassword=helper-secret\n',
                                                b'never display helper-secret')
        with mock.patch.dict(os.environ, {'GH_TOKEN': '', 'GITHUB_TOKEN': '',
                                         'GIT_DIR': '/injected', 'GIT_CONFIG_COUNT': '1',
                                         'GIT_CONFIG_KEY_0': 'credential.helper',
                                         'GIT_CONFIG_VALUE_0': 'injected'}), \
             mock.patch.object(remote.subprocess, 'run', return_value=completed) as helper:
            self.assertEqual(remote._token(self.repo), 'helper-secret')
            args, kwargs = helper.call_args
            self.assertEqual(args[0], ['git', '-C', str(self.repo), 'credential', 'fill'])
            self.assertEqual(kwargs['input'], b'protocol=https\nhost=github.com\n\n')
            self.assertEqual(kwargs['creationflags'],
                             subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.assertEqual(kwargs['stdout'], subprocess.PIPE)
            self.assertEqual(kwargs['timeout'], 15)
            self.assertEqual(set(kwargs), {'input', 'shell', 'stdout', 'stderr',
                                           'timeout', 'env', 'creationflags'})
            self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(kwargs['env']['GCM_INTERACTIVE'], 'never')
            for key in ('GIT_DIR', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'):
                self.assertNotIn(key, kwargs['env'])
            self.assertEqual(kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(before, self.snapshot())
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'first', 'GITHUB_TOKEN': 'second'}), \
             mock.patch.object(remote.subprocess, 'run', side_effect=AssertionError('helper')):
            self.assertEqual(remote._token(self.repo), 'first')

    def test_transport_errors_and_redirects_do_not_leak_authorization(self):
        with mock.patch('urllib.request.OpenerDirector.open',
                        side_effect=OSError('raw response fake-token')):
            with self.assertRaises(core.Error) as error:
                remote.require_private(self.repo)
        self.assertNotIn('fake-token', str(error.exception))
        request = urllib.request.Request('https://api.github.com/repos/example/knowledge',
                                         headers={'Authorization': 'Bearer fake-token'})
        handler = remote.NoRedirect()
        for code in (301, 302, 303, 307, 308):
            with mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('follow')), \
                 self.assertRaises(core.Error):
                handler.http_error_302(request, None, code, 'redirect',
                                       {'location': 'https://evil.invalid/token'})

    def test_source_overlap_is_rejected_without_resolving_user_bindings(self):
        for path in (remote.SOURCE_ROOT, remote.SOURCE_ROOT.parent,
                     remote.SOURCE_ROOT / 'data-not-created'):
            with self.assertRaises(core.Error):
                remote.validate_paths(path)
        with self.assertRaises(core.Error):
            remote.validate_paths(self.repo, self.repo / 'workspace')
