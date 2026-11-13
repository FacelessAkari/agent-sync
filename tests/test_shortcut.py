import io
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_sync import core, gitops, private_remote, shortcut
from agent_sync.__main__ import main
from agent_sync.local_bridge import run


class Response(io.BytesIO):
    status = 200


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent-sync-shortcut-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        home = self.base / 'home'
        home.mkdir()
        global_config = home / 'gitconfig'
        global_config.write_bytes(b'')
        env = mock.patch.dict(os.environ, {
            'HOME': str(home), 'USERPROFILE': str(home), 'XDG_CONFIG_HOME': str(home),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(global_config),
            'GH_TOKEN': 'must-not-be-written-credential', 'GITHUB_TOKEN': '',
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.base / '资料 repo'
        self.workspace = self.base / '本机 workspace'
        self.output = self.base / '入口 output'
        self.bare = self.base / 'remote.git'
        self.git(self.base, 'init', '--bare', '--initial-branch=main', str(self.bare))
        self.git(self.base, 'clone', str(self.bare), str(self.repo))
        self.git(self.repo, 'config', 'user.name', 'Test')
        self.git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.git(self.repo, 'config', 'core.autocrlf', 'false')
        self.git(self.repo, 'config', 'commit.gpgsign', 'false')
        core.init_repo(self.repo)
        self.rule = self.base / 'private-rule.md'
        self.rule.write_bytes(b'private actual rule body must not appear in launchers\n')
        core.add_group(self.repo, 'rule', 'rules', 'file', 'data/rule.md')
        core.bind(self.repo, 'rule', self.rule)
        core.apply(self.repo, 'push')
        self.git(self.repo, 'add', '.gitignore', '.gitattributes', 'manifest.json', 'data')
        self.git(self.repo, 'commit', '-m', 'bootstrap')
        self.git(self.repo, 'push', '-u', 'origin', 'main')

    def git(self, repo, *args):
        return gitops._git(repo, ['-c', 'core.hooksPath=' + os.devnull, *args], 'isolated fixture')

    def snapshot(self):
        return {str(p.relative_to(self.base)): p.read_bytes()
                for p in self.base.rglob('*') if p.is_file()}

    def assert_no_content(self):
        for path in self.output.iterdir():
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(self.rule.read_bytes().strip(), data)
                self.assertNotIn(b'must-not-be-written-credential', data)

    def test_macos_quotes_real_paths_and_launcher_uses_list_argv_and_fixed_git(self):
        result = shortcut.create(self.workspace, self.repo, 'macos', self.output)
        self.assertEqual(result['visibility'], 'local')
        command = self.output / 'AgentSync.command'
        if os.name != 'nt':
            self.assertTrue(command.stat().st_mode & 0o111)
        words = shlex.split(command.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(words[0], 'exec')
        self.assertEqual(words[2], str(self.output / 'launcher.py'))
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as start, \
                mock.patch.object(sys, 'stdin', io.StringIO()), \
                mock.patch.object(sys, 'stdout', io.StringIO()):
            with self.assertRaises(SystemExit) as exit_code:
                runpy.run_path(str(self.output / 'launcher.py'), run_name='__main__')
        self.assertEqual(exit_code.exception.code, 7)
        args, kwargs = start.call_args
        self.assertEqual(args[0][1:], ['-m', 'agent_sync', '--repo', str(self.repo),
                                      'quick-sync', '--workspace', str(self.workspace)])
        self.assertTrue(Path(args[0][0]).is_absolute())
        self.assertEqual(words, ['exec', args[0][0], str(self.output / 'launcher.py')])
        expected_exec = 'exec ' + shlex.quote(args[0][0]) + ' ' + shlex.quote(
            str(self.output / 'launcher.py')) + '\n'
        self.assertEqual(command.read_bytes().splitlines(keepends=True)[-1],
                         expected_exec.encode('utf-8'))
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['cwd'], str(private_remote.SOURCE_ROOT))
        self.assertEqual(kwargs['env']['PATH'].split(os.pathsep)[0],
                         str(Path(os.path.abspath(shutil.which('git'))).parent))
        self.assert_no_content()

    def launcher_main(self):
        shortcut.create(self.workspace, self.repo, 'macos', self.output)
        return runpy.run_path(str(self.output / 'launcher.py'))['main']

    def test_reserved_outputs_reject_without_reads_or_writes(self):
        reserved = (
            'selection.json', 'collection-state.json', 'fast-sync-state.json',
            'quick-sync.lock', 'sources.json', 'sources-state.json',
            'skill-mirror', 'schedule',
        )
        for populated in (False, True):
            if populated:
                self.workspace.mkdir()
                for name in reserved:
                    path = self.workspace / name
                    if name in {'skill-mirror', 'schedule'}:
                        (path / 'nested').mkdir(parents=True)
                        (path / 'nested' / 'state').write_bytes(b'secret state')
                    else:
                        path.write_bytes(b'secret control')
            before = self.snapshot()
            directories = {p for p in self.base.rglob('*') if p.is_dir()}
            targets = [self.workspace, self.workspace.parent]
            for name in reserved:
                root = self.workspace / name
                targets.extend([root, root / 'child', root / 'nested' / 'deep'])
            for platform in ('macos', 'windows'):
                for output in targets:
                    with self.subTest(populated=populated, platform=platform, output=output):
                        with mock.patch.object(private_remote, 'require_private',
                                               side_effect=AssertionError('private read')) as private, \
                                mock.patch.object(core, '_load',
                                                  side_effect=AssertionError('binding read')) as load, \
                                mock.patch.object(core, '_bytes',
                                                  side_effect=AssertionError('content read')) as read, \
                                mock.patch.object(core, '_mkdir',
                                                  side_effect=AssertionError('directory write')) as mkdir, \
                                mock.patch.object(shortcut, '_preflight',
                                                  side_effect=AssertionError('late guard')) as preflight, \
                                mock.patch.object(shortcut, '_windows_link',
                                                  side_effect=AssertionError('link write')) as link:
                            with self.assertRaises(core.Error):
                                shortcut.create(self.workspace, self.repo, platform, output)
                        for operation in (private, load, read, mkdir, preflight, link):
                            operation.assert_not_called()
                        self.assertEqual(before, self.snapshot())
                        self.assertEqual(
                            directories, {p for p in self.base.rglob('*') if p.is_dir()})

    def test_independent_launchers_and_external_desktop_outputs_are_allowed(self):
        for platform in ('macos', 'windows'):
            workspace = self.base / ('workspace-' + platform)
            desktop = self.base / 'Desktop' / platform
            for output in (None, workspace / 'launchers' / 'independent', desktop):
                with self.subTest(platform=platform, output=output):
                    with mock.patch.object(shortcut, '_windows_link',
                                           return_value=b'test COM binary link'):
                        result = shortcut.create(workspace, self.repo, platform, output)
                    expected = workspace / 'launchers' if output is None else output
                    self.assertEqual(result['status'], 'created')
                    self.assertEqual(result['output'], str(expected))
                    self.assertTrue((expected / 'launcher.py').is_file())
                    self.assertTrue((expected / (
                        'AgentSync.cmd' if platform == 'windows'
                        else 'AgentSync.command')).is_file())
                    self.assertEqual(json.loads((workspace / 'selection.json').read_bytes()),
                                     {'version': 1, 'files': []})

    def test_launcher_runpy_filters_path_without_defaults_or_global_changes(self):
        git_dir = str(Path(shutil.which('git')).parent)
        first = str(self.base / 'user bin B')
        second = str(self.base / 'user bin A')
        relative = ['', '.', './', '.\\', 'relative', './relative', '../bin',
                    'C:relative']
        if os.name == 'nt':
            relative.extend(['\\rooted-without-drive', '/rooted-without-drive'])
        mixed = os.pathsep.join([*relative, first, '', second, '.', first, 'tail'])
        cases = [
            ({'PATH': mixed}, [first, second, first]),
            ({'PATH': ''}, []),
            ({}, []),
            ({'PATH': os.pathsep.join(relative)}, []),
        ]
        fake_default = os.pathsep.join(
            ['.', str(self.base / 'unsafe-default-bin'), r'C:\bin'])
        for platform in ('macos', 'windows'):
            output = self.base / ('path-launcher-' + platform)
            with mock.patch.object(shortcut, '_windows_link',
                                   return_value=b'test COM binary link'):
                shortcut.create(self.workspace, self.repo, platform, output)
            for environment, retained in cases:
                with self.subTest(platform=platform, environment=environment):
                    result = subprocess.CompletedProcess(
                        [], 0, json.dumps({'status': 'up_to_date'}), '')
                    with mock.patch.dict(os.environ, environment, clear=True), \
                            mock.patch.object(os, 'defpath', fake_default), \
                            mock.patch.object(subprocess, 'run', return_value=result) as start, \
                            mock.patch.object(sys, 'stdin', io.StringIO()), \
                            mock.patch.object(sys, 'stdout', io.StringIO()):
                        before = dict(os.environ)
                        with self.assertRaises(SystemExit) as stopped:
                            runpy.run_path(str(output / 'launcher.py'), run_name='__main__')
                        self.assertEqual(dict(os.environ), before)
                    self.assertEqual(stopped.exception.code, 0)
                    start.assert_called_once()
                    child_env = start.call_args.kwargs['env']
                    self.assertEqual(child_env['PATH'].split(os.pathsep),
                                     [git_dir, *retained])
                    self.assertEqual(child_env, {
                        **environment, 'PATH': os.pathsep.join([git_dir, *retained])})
                    self.assertFalse(start.call_args.kwargs['shell'])

    def test_relative_or_missing_git_cannot_be_fixed_into_launcher_path(self):
        before = self.snapshot()
        for git in ('git', './bin/git', str(self.base / 'missing-git')):
            with self.subTest(git=git):
                with mock.patch.object(shutil, 'which', return_value=git):
                    with self.assertRaises(core.Error):
                        shortcut.create(self.workspace, self.repo, 'macos', self.output)
                self.assertEqual(before, self.snapshot())
                self.assertFalse(self.workspace.exists())
                self.assertFalse(self.output.exists())

    def test_local_bridge_attention_statuses_have_fixed_safe_messages(self):
        shortcut.create(self.workspace, self.repo, 'macos', self.output)
        messages = {
            'remote_changed': '远端资料有更新，尚未完成同步。',
            'local_ahead': '本机资料有待上传，尚未完成同步。',
            'needs_skill_install_review': '技能安装映射尚需本机审阅。',
            'staged_changes': '资料仓库存在已暂存变更，请先处理。',
        }
        for status, message in messages.items():
            for code in (0, 7, 23):
                with self.subTest(status=status, code=code):
                    raw = json.dumps({
                        'status': status, 'message': 'secret message /private/path',
                        'error': 'secret error', 'details': {'secret': 'credential'},
                        'changed_files': ['secret filename'],
                        'archives_created': ['secret archive'], 'elapsed_seconds': 2,
                    })
                    output, errors = io.StringIO(), io.StringIO()
                    with mock.patch.object(subprocess, 'run', return_value=
                                           subprocess.CompletedProcess(
                                               [], code, raw, 'secret stderr')), \
                            mock.patch.object(sys, 'stdin', io.StringIO()), \
                            mock.patch.object(sys, 'stdout', output), \
                            mock.patch.object(sys, 'stderr', errors), \
                            mock.patch('builtins.input') as enter:
                        with self.assertRaises(SystemExit) as stopped:
                            runpy.run_path(str(self.output / 'launcher.py'),
                                           run_name='__main__')
                    self.assertEqual(stopped.exception.code, code)
                    self.assertEqual(output.getvalue(),
                                     '需要处理：' + message + '\n'
                                     '变更文件：1；新建归档：1；耗时：2 秒\n')
                    self.assertEqual(errors.getvalue(), '')
                    enter.assert_not_called()
                    for hidden in ('secret', '/private/path', 'credential', status):
                        self.assertNotIn(hidden, output.getvalue())

    def test_launcher_captures_utf8_and_displays_only_safe_summary(self):
        launch = self.launcher_main()

        class LimitedOutput(io.StringIO):
            encoding = 'cp1252'

            def reconfigure(self, **kwargs):
                self.settings = kwargs

        output = LimitedOutput()
        document = {'status': 'synced', 'message': 'secret-message /private/path',
                    'changed_files': ['secret-file-a', 'secret-file-b'],
                    'archives_created': 1, 'elapsed_seconds': 1.25}
        raw = json.dumps(document, ensure_ascii=False)
        before = self.snapshot()
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, raw, 'secret-stderr credential')) as start, \
                mock.patch.object(sys, 'stdin', io.StringIO()), \
                mock.patch.object(sys, 'stdout', output), \
                mock.patch('builtins.input') as enter:
            self.assertEqual(launch(), 0)
        enter.assert_not_called()
        self.assertEqual(output.settings, {'encoding': 'utf-8', 'errors': 'replace'})
        args, kwargs = start.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][1:], ['-m', 'agent_sync', '--repo', str(self.repo),
                                      'quick-sync', '--workspace', str(self.workspace)])
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['cwd'], str(private_remote.SOURCE_ROOT))
        self.assertEqual(kwargs['env']['PATH'].split(os.pathsep)[0],
                         str(Path(os.path.abspath(shutil.which('git'))).parent))
        self.assertTrue(kwargs['capture_output'])
        self.assertEqual(kwargs['encoding'], 'utf-8')
        self.assertEqual(kwargs['errors'], 'replace')
        text = output.getvalue()
        self.assertIn('同步已完成', text)
        self.assertIn('变更文件：2', text)
        self.assertIn('新建归档：1', text)
        self.assertIn('耗时：1.25 秒', text)
        for hidden in (raw, 'secret', '/private/path', 'credential', 'changed_files',
                       '按回车关闭窗口'):
            self.assertNotIn(hidden, text)
        self.assertEqual(before, self.snapshot())

    def test_launcher_interactive_wait_preserves_sync_exit_code(self):
        launch = self.launcher_main()
        raw = json.dumps({'status': 'selection_required', 'message': 'secret bootstrap',
                          'changed_files': 0, 'archives_created': 0, 'elapsed_seconds': 0})
        for interruption in (None, EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption):
                incoming, output = io.StringIO(), io.StringIO()
                with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 7, raw, 'secret stderr')), \
                        mock.patch.object(sys, 'stdin', incoming), \
                        mock.patch.object(sys, 'stdout', output), \
                        mock.patch.object(incoming, 'isatty', return_value=True), \
                        mock.patch.object(output, 'isatty', return_value=True), \
                        mock.patch('builtins.input', side_effect=interruption,
                                   return_value='') as enter:
                    self.assertEqual(launch(), 7)
                enter.assert_called_once_with()
                text = output.getvalue()
                self.assertIn('需要处理', text)
                self.assertIn('本机资料选择配置', text)
                self.assertIn('按回车关闭窗口', text)
                self.assertNotIn('secret', text)
                self.assertNotIn('bootstrap', text)

    def test_launcher_noninteractive_invalid_results_and_start_failures_are_safe(self):
        launch = self.launcher_main()
        secret = 'credential-secret /private/path bootstrap'
        cases = [
            (subprocess.CompletedProcess([], 9, secret, secret), 9, '无法读取同步结果'),
            (subprocess.CompletedProcess([], 0, '{}\n' + secret, secret), 0, '无法读取同步结果'),
            (subprocess.CompletedProcess([], 0, '[]', secret), 0, '无法读取同步结果'),
            (subprocess.CompletedProcess([], 0, '', secret), 0, '无法读取同步结果'),
            (OSError(secret), 1, '无法启动同步'),
            (subprocess.SubprocessError(secret), 1, '无法启动同步'),
        ]
        for status, code in [('bootstrap_required', 0), ([], 4), ('synced', 5),
                             ('unknown-' + secret, 0)]:
            raw = json.dumps({'status': status, 'message': secret,
                              'changed_files': True, 'archives_created': -1,
                              'elapsed_seconds': False})
            cases.append((subprocess.CompletedProcess([], code, raw, secret), code,
                          '变更文件：未知；新建归档：未知；耗时：未知'))
        for value in (None, 'secret', {}, float('nan'), float('inf'), -1.5):
            raw = json.dumps({'status': 'error', 'changed_files': value,
                              'archives_created': value, 'elapsed_seconds': value})
            cases.append((subprocess.CompletedProcess([], 3, raw, secret), 3,
                          '变更文件：未知；新建归档：未知；耗时：未知'))
        cases.append((subprocess.CompletedProcess([], 0, '{}', secret), 0,
                      '变更文件：未知；新建归档：未知；耗时：未知'))
        before = self.snapshot()
        for outcome, code, expected in cases:
            for input_tty, output_tty in ((False, False), (True, False), (False, True)):
                with self.subTest(outcome=outcome, tty=(input_tty, output_tty)):
                    incoming, output, errors = io.StringIO(), io.StringIO(), io.StringIO()
                    options = ({'side_effect': outcome} if isinstance(outcome, Exception)
                               else {'return_value': outcome})
                    with mock.patch.object(subprocess, 'run', **options), \
                            mock.patch.object(sys, 'stdin', incoming), \
                            mock.patch.object(sys, 'stdout', output), \
                            mock.patch.object(sys, 'stderr', errors), \
                            mock.patch.object(incoming, 'isatty', return_value=input_tty), \
                            mock.patch.object(output, 'isatty', return_value=output_tty), \
                            mock.patch('builtins.input') as enter:
                        self.assertEqual(launch(), code)
                    enter.assert_not_called()
                    text = output.getvalue()
                    self.assertIn('需要处理', text)
                    self.assertIn(expected, text)
                    self.assertEqual(errors.getvalue(), '')
                    for hidden in ('secret', '/private/path', 'bootstrap', 'Traceback',
                                   'changed_files', '按回车关闭窗口', str(self.repo)):
                        self.assertNotIn(hidden, text)
        self.assertEqual(before, self.snapshot())

    def test_windows_link_is_owned_and_idempotent_and_unknown_link_is_not_overwritten(self):
        with mock.patch.object(shortcut, '_windows_link', return_value=b'test COM binary link') as com:
            shortcut.create(self.workspace, self.repo, 'windows', self.output)
            first = self.snapshot()
            shortcut.create(self.workspace, self.repo, 'windows', self.output)
            self.assertEqual(com.call_count, 1)
            config, output = com.call_args.args
            self.assertEqual(config['workspace'], str(self.workspace))
            self.assertEqual(config['repo'], str(self.repo))
            self.assertEqual(output, self.output)
            self.assertEqual(first, self.snapshot())
            self.assertIn(b'%~dp0launcher.py', (self.output / 'AgentSync.cmd').read_bytes())
            (self.output / 'AgentSync.lnk').write_bytes(b'user modified link')
            old = self.snapshot()
            with self.assertRaises(core.Error):
                shortcut.create(self.workspace, self.repo, 'windows', self.output)
            self.assertEqual(old, self.snapshot())
        self.assert_no_content()

    def test_all_text_targets_are_preflighted_before_template_or_other_files(self):
        self.output.mkdir()
        (self.output / 'AgentSync.command').write_bytes(b'user owned command')
        before = self.snapshot()
        with self.assertRaises(core.Error):
            shortcut.create(self.workspace, self.repo, 'macos', self.output)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.workspace.exists())
        self.assertFalse((self.output / 'launcher.py').exists())

    def test_public_target_creates_no_workspace_or_output(self):
        self.git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/example/knowledge.git')
        old = self.snapshot()
        with mock.patch('urllib.request.OpenerDirector.open',
                        return_value=Response(b'{"private":false,"full_name":"example/knowledge"}')):
            with self.assertRaises(core.Error):
                shortcut.create(self.workspace, self.repo, 'macos', self.output)
        self.assertEqual(old, self.snapshot())
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.output.exists())

    def test_template_does_not_scan_home_and_direct_rules_can_quick_sync(self):
        with mock.patch.object(Path, 'home', side_effect=AssertionError('home discovery')):
            shortcut.create(self.workspace, self.repo, 'macos', self.output)
        template = json.loads((self.workspace / 'selection.json').read_bytes())
        self.assertEqual(template, {'version': 1, 'files': []})
        first = self.snapshot()
        shortcut.create(self.workspace, self.repo, 'macos', self.output)
        self.assertEqual(first, self.snapshot())
        # Direct-only workspaces also work with no selection file at all.
        (self.workspace / 'selection.json').unlink()
        result, code = run(self.workspace, self.repo, check=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'up_to_date')
        self.assertFalse((self.workspace / 'selection.json').exists())
        self.rule.write_bytes(b'changed direct rule\n')
        result, code = run(self.workspace, self.repo)
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(self.git(self.bare, 'show', 'main:data/rule.md').stdout, self.rule.read_bytes())

    def test_skills_empty_template_is_explicitly_selection_required(self):
        core.add_group(self.repo, 'skills', 'skills', 'tree', 'data/skills')
        mirror = self.workspace / 'skill-mirror/codex'
        core._mkdir(mirror)
        core.bind(self.repo, 'skills', mirror)
        result = shortcut.create(self.workspace, self.repo, 'macos', self.output)
        self.assertTrue(result['selection_required'])
        before = self.snapshot()
        result, code = run(self.workspace, self.repo)
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'selection_required')
        self.assertEqual(before, self.snapshot())

    def test_missing_git_and_source_overlap_stop_without_creating_files(self):
        before = self.snapshot()
        with mock.patch.object(shutil, 'which', return_value=None):
            with self.assertRaises(core.Error):
                shortcut.create(self.workspace, self.repo, 'macos', self.output)
        with self.assertRaises(core.Error):
            shortcut.create(private_remote.SOURCE_ROOT, self.repo, 'macos', self.output)
        with self.assertRaises(core.Error):
            shortcut.create(self.workspace, self.repo, 'macos', private_remote.SOURCE_ROOT)
        self.assertEqual(before, self.snapshot())

    def test_cli_check_returns_only_json_and_requires_explicit_repo(self):
        self.workspace.mkdir()
        output = io.StringIO()
        before = self.snapshot()
        from contextlib import redirect_stdout, redirect_stderr
        with redirect_stdout(output):
            code = main(['--repo', str(self.repo), 'quick-sync', '--workspace', str(self.workspace), '--check'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'], 'up_to_date')
        self.assertEqual(before, self.snapshot())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['quick-sync', '--workspace', str(self.workspace)])
