"""Integration tests using only temporary roots, local bare Git and real core.

Run from the project root: python -m unittest discover -s tests -v
No network, real home directory, symlink support, or test skips are used.
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_sync import local_bridge as fast
from agent_sync import core, gitops
from agent_sync import collector as collect_skills


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FastSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-sync-local-test-')
        self.addCleanup(self.temp.cleanup)
        # Resolve ONLY our temporary root (including macOS /var indirection).
        self.base = Path(self.temp.name).resolve()
        home = self.base / 'home'
        home.mkdir()
        global_config = home / 'gitconfig'
        global_config.write_bytes(b'')
        env = {
            'HOME': str(home), 'USERPROFILE': str(home),
            'XDG_CONFIG_HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': str(global_config),
            'GIT_AUTHOR_NAME': 'Local Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Local Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
            'GIT_TERMINAL_PROMPT': '0',
        }
        self.environment = mock.patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = self.base / 'agent-sync-local'
        self.repo = self.base / 'my-agent-data'
        self.bare = self.base / 'remote.git'
        self.root.mkdir()
        self.git(self.base, 'init', '--bare', '--initial-branch=main', str(self.bare))
        self.git(self.base, 'clone', str(self.bare), str(self.repo))
        self.configure(self.repo)
        core.init_repo(self.repo)
        originals = self.root / 'originals'
        originals.mkdir()
        self.source = originals / 'first.md'
        self.link = originals / 'second.md'
        self.initial = b'original skill\r\n'
        self.source.write_bytes(self.initial)
        # No skip: an ordinary local hardlink is part of the input contract.
        os.link(self.source, self.link)
        self.selection = {'version': 1, 'files': [
            {'source': str(self.source), 'mirror': 'codex/demo/SKILL.md',
             'reviewed_sha256': sha(self.initial)},
            {'source': str(self.link), 'mirror': 'shared/demo/SKILL.md',
             'reviewed_sha256': sha(self.initial)},
        ]}
        core._save(self.root / 'selection.json', self.selection)
        collect_skills.collect(self.selection, self.root / 'skill-mirror',
                               self.root / 'collection-state.json')
        self.mirror = self.root / 'skill-mirror/codex/demo/SKILL.md'
        self.other_mirror = self.root / 'skill-mirror/shared/demo/SKILL.md'
        for name in ('codex', 'shared'):
            core.add_group(self.repo, name, 'skills', 'tree', 'data/' + name)
            core.bind(self.repo, name, self.root / 'skill-mirror' / name)
        self.rule = self.root / 'rule.md'
        self.rule.write_bytes(b'initial rule\n')
        core.add_group(self.repo, 'rules', 'rules', 'file', 'data/rule.md')
        core.bind(self.repo, 'rules', self.rule)
        core.apply(self.repo, 'push')
        self.git(self.repo, 'add', '--', '.gitignore', '.gitattributes', 'manifest.json', 'data')
        self.git(self.repo, 'commit', '-m', 'fixture bootstrap')
        self.git(self.repo, 'push', '-u', 'origin', 'main')
        self.initial_head = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self.original_id = (self.source.stat().st_dev, self.source.stat().st_ino)

    def git(self, repo, *args):
        result = gitops._git(repo, ['-c', 'core.hooksPath=' + os.devnull,
                                    '-c', 'commit.gpgsign=false', *args], 'temporary test Git')
        return result.stdout

    def configure(self, repo):
        self.git(repo, 'config', 'user.name', 'Local Test')
        self.git(repo, 'config', 'user.email', 'test@example.invalid')
        self.git(repo, 'config', 'core.autocrlf', 'false')
        self.git(repo, 'config', 'commit.gpgsign', 'false')

    def invoke(self, check=False):
        result, code = fast.run(self.root, self.repo, check=check)
        self.assertEqual(result['source_write_count'], 0)
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        return result, code

    def prime(self):
        result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'up_to_date')
        self.assertEqual(result['archives_created'], 0)
        self.assertEqual(result['archives_unchanged'], 0)
        self.assertIs(result['archives_checked'], True)
        self.assertTrue((self.root / 'fast-sync-state.json').is_file())

    def bare_head(self):
        return self.git(self.bare, 'rev-parse', 'refs/heads/main').strip()

    def bare_bytes(self, relative):
        return self.git(self.bare, 'show', 'refs/heads/main:' + relative)

    def assert_original_identity(self):
        for source in (self.source, self.link):
            info = source.stat()
            self.assertEqual((info.st_dev, info.st_ino), self.original_id)
            self.assertEqual(info.st_nlink, 2)
        self.assertEqual(self.mirror.stat().st_nlink, 1)
        self.assertEqual(self.other_mirror.stat().st_nlink, 1)

    def snapshot(self):
        result = {}
        # Include Git metadata, adapter state, sources, directories and bare repo.
        for directory, children, files in os.walk(self.base):
            path = Path(directory)
            result[path.relative_to(self.base).as_posix() + '/'] = None
            for name in files:
                file = path / name
                result[file.relative_to(self.base).as_posix()] = file.read_bytes()
        return result

    def test_existing_workspace_lock_is_busy_and_preserved(self):
        lock = self.root / 'quick-sync.lock'
        lock.write_bytes(b'existing owner\n')
        original = fast.identity(lock.lstat())
        before = self.snapshot()
        with mock.patch.object(fast, 'perform', side_effect=AssertionError('busy perform')):
            for check in (False, True):
                result, code = self.invoke(check)
                self.assertEqual((result['status'], code), ('busy', 2))
                self.assertEqual(fast.identity(lock.lstat()), original)
                self.assertEqual(lock.read_bytes(), b'existing owner\n')
        self.assertEqual(self.snapshot(), before)

    def test_exclusive_open_race_preserves_winning_lock(self):
        lock = self.root / 'quick-sync.lock'
        original_open = os.open
        attempts = []

        def race(path, flags, *args, **kwargs):
            if Path(path) == lock:
                self.assertTrue(flags & os.O_CREAT)
                self.assertTrue(flags & os.O_EXCL)
                self.assertFalse(lock.exists())
                lock.write_bytes(b'racing owner\n')
                attempts.append(fast.identity(lock.lstat()))
                # The real exclusive open must now raise FileExistsError.
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, 'open', side_effect=race), \
             mock.patch.object(fast, 'perform', side_effect=AssertionError('lost race')):
            result, code = self.invoke()
        self.assertEqual((result['status'], code), ('busy', 2))
        self.assertEqual(attempts, [fast.identity(lock.lstat())])
        self.assertEqual(lock.read_bytes(), b'racing owner\n')

    def test_check_never_opens_workspace_lock(self):
        lock = self.root / 'quick-sync.lock'
        self.source.write_bytes(b'pending source\n')
        original_open = os.open
        original_collect = collect_skills.collect
        original_plan = fast.full_plan

        def watch_open(path, *args, **kwargs):
            self.assertNotEqual(Path(path), lock)
            return original_open(path, *args, **kwargs)

        def watch_collect(*args, **kwargs):
            self.assertFalse(lock.exists())
            self.assertTrue(kwargs['check_only'])
            return original_collect(*args, **kwargs)

        def watch_plan(repo, check):
            self.assertTrue(check)
            self.assertFalse(lock.exists())
            return original_plan(repo, check)

        before = self.snapshot()
        with mock.patch.object(os, 'open', side_effect=watch_open), \
             mock.patch.object(collect_skills, 'collect', side_effect=watch_collect) as collect, \
             mock.patch.object(fast, 'full_plan', side_effect=watch_plan) as plan:
            result, code = self.invoke(check=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(lock.exists())

    def test_workspace_lock_spans_collection_plan_apply_and_upload(self):
        lock = self.root / 'quick-sync.lock'
        self.source.write_bytes(b'changed source\n')
        identities = []

        def observe(function):
            def wrapped(*args, **kwargs):
                self.assertTrue(lock.is_file())
                identities.append(fast.identity(lock.lstat()))
                result = function(*args, **kwargs)
                self.assertEqual(fast.identity(lock.lstat()), identities[0])
                return result
            return wrapped

        with mock.patch.object(collect_skills, 'collect',
                               side_effect=observe(collect_skills.collect)) as collect, \
             mock.patch.object(core, 'plan', side_effect=observe(core.plan)) as plan, \
             mock.patch.object(core, 'apply', side_effect=observe(core.apply)) as apply, \
             mock.patch.object(gitops, 'git_sync', side_effect=observe(gitops.git_sync)) as sync:
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(collect.call_count, 1)
        self.assertGreaterEqual(plan.call_count, 2)
        self.assertTrue(apply.called)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(len(set(identities)), 1)
        self.assertFalse(lock.exists())

    def test_workspace_lock_spans_checkpoint_and_private_preflight_is_once(self):
        lock = self.root / 'quick-sync.lock'
        checkpoint = self.root / 'fast-sync-state.json'
        original_save = core._save
        observed = []

        def watch_save(path, *args, **kwargs):
            if Path(path) == checkpoint:
                self.assertTrue(lock.is_file())
                observed.append(fast.identity(lock.lstat()))
            return original_save(path, *args, **kwargs)

        def private_preflight(repo):
            self.assertFalse(lock.exists())
            return original_private(repo)

        original_private = fast.private_remote.require_private
        with mock.patch.object(core, '_save', side_effect=watch_save), \
             mock.patch.object(fast.private_remote, 'require_private',
                               side_effect=private_preflight) as private:
            self.prime()
        self.assertEqual(private.call_count, 1)
        self.assertEqual(len(observed), 1)
        self.assertFalse(lock.exists())

    def test_exception_cleans_owned_workspace_lock(self):
        lock = self.root / 'quick-sync.lock'

        def fail(repo):
            self.assertTrue(lock.is_file())
            raise OSError('temporary test failure')

        with mock.patch.object(core, 'plan', side_effect=fail):
            result, code = self.invoke()
        self.assertEqual((result['status'], code), ('error', 2))
        self.assertFalse(lock.exists())

        with mock.patch.object(fast, 'perform', side_effect=RuntimeError('unexpected')):
            with self.assertRaises(RuntimeError):
                self.invoke()
        self.assertFalse(lock.exists())

    def test_replaced_workspace_lock_is_not_removed_on_exit(self):
        lock = self.root / 'quick-sync.lock'
        retired = self.root / 'retired-test-lock'
        for failure in (False, True):
            with self.subTest(failure=failure):
                replacement = []

                def replace(*args):
                    self.assertTrue(lock.is_file())
                    lock.rename(retired)
                    lock.write_bytes(b'replacement owner\n')
                    replacement.append(fast.identity(lock.lstat()))
                    if failure:
                        raise OSError('after replacement')
                    return 'up_to_date', 'test'

                with mock.patch.object(fast, 'perform', side_effect=replace):
                    result, code = self.invoke()
                self.assertEqual(code, 2 if failure else 0, result)
                self.assertEqual(fast.identity(lock.lstat()), replacement[0])
                self.assertEqual(lock.read_bytes(), b'replacement owner\n')
                lock.unlink()
                retired.unlink()

    def test_missing_workspace_is_not_created(self):
        missing = self.base / 'missing-workspace'
        before = self.snapshot()
        with mock.patch.object(fast, 'workspace_lock', side_effect=AssertionError('missing root')):
            result, code = fast.run(missing, self.repo)
        self.assertEqual(code, 2, result)
        self.assertFalse(missing.exists())
        self.assertEqual(self.snapshot(), before)

    def test_workspace_lock_cannot_be_selected_as_original_skill(self):
        self.selection['files'][0]['source'] = str(self.root / 'quick-sync.lock')
        core._save(self.root / 'selection.json', self.selection)
        for check in (False, True):
            result, code = self.invoke(check)
            self.assertEqual((result['status'], code), ('unsafe_path', 2))
            self.assertFalse((self.root / 'quick-sync.lock').exists())

    def test_first_check_uses_real_plan_then_fast_path_calls_no_product_sync(self):
        with mock.patch.object(core, 'plan', wraps=core.plan) as plan:
            self.prime()
            self.assertEqual(plan.call_count, 1)
        with mock.patch.object(core, 'plan', side_effect=AssertionError('plan on fast path')), \
             mock.patch.object(core, 'apply', side_effect=AssertionError('apply on fast path')), \
             mock.patch.object(gitops, 'git_sync', side_effect=AssertionError('git_sync on fast path')):
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'up_to_date')
        self.assertFalse(result['uploaded'])
        self.assertEqual(result['changed_files'], 0)
        self.assertEqual(result['archives_created'], 0)
        self.assertEqual(result['archives_unchanged'], 0)
        self.assertIs(result['archives_checked'], True)
        self.assert_original_identity()

    def test_refresh_new_data_bypasses_primed_fast_path_and_reaches_bare(self):
        self.prime()
        payload = b'synthetic pipeline fixture only\n'
        relative = 'data/refresh-pipeline.txt'
        lock = self.root / 'quick-sync.lock'

        def refresh(workspace, repo):
            self.assertEqual((workspace, repo), (self.root, self.repo))
            self.assertTrue(lock.is_file())
            private.assert_called_once_with(self.repo)
            gate.assert_called_once_with(self.repo)
            reader.assert_not_called()
            # Ordinary allowed data, not a fabricated history package. Sources
            # has separate tests for archive validity and immutability.
            (repo / relative).write_bytes(payload)
            return {'status': 'refreshed', 'sources': 2, 'archived': 1,
                    'unchanged': 1, 'items': []}

        with mock.patch.object(fast.private_remote, 'require_private',
                               wraps=fast.private_remote.require_private) as private, \
             mock.patch.object(fast, 'git_gate', wraps=fast.git_gate) as gate, \
             mock.patch.object(fast, 'Reader', wraps=fast.Reader) as reader, \
             mock.patch.object(fast.sources, 'refresh', side_effect=refresh) as refresh_call, \
             mock.patch.object(gitops, 'git_sync', wraps=gitops.git_sync) as sync:
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'synced')
        self.assertTrue(result['uploaded'])
        self.assertEqual(result['changed_files'], 0)
        self.assertEqual(result['archives_created'], 1)
        self.assertEqual(result['archives_unchanged'], 1)
        self.assertIs(result['archives_checked'], True)
        refresh_call.assert_called_once_with(self.root, self.repo)
        sync.assert_called_once_with(self.repo)
        # Initial gate plus the existing pre-upload gate; no extra full gate.
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(self.bare_bytes(relative), payload)
        self.assertNotEqual(self.bare_head(), self.initial_head)
        self.assertNotIn(payload.decode().strip(), json.dumps(result))
        self.assertFalse(lock.exists())
        self.assert_original_identity()

    def test_refresh_error_is_redacted_and_releases_workspace_lock(self):
        self.prime()
        with mock.patch.object(fast.sources, 'refresh',
                               side_effect=core.Error('synthetic private body and ID')) as refresh, \
             mock.patch.object(gitops, 'git_sync', side_effect=AssertionError('failed refresh')):
            result, code = self.invoke()
        refresh.assert_called_once_with(self.root, self.repo)
        self.assertEqual((result['status'], code), ('error', 2))
        self.assertFalse(result['uploaded'])
        self.assertNotIn('synthetic private body and ID', json.dumps(result))
        self.assertFalse((self.root / 'quick-sync.lock').exists())
        self.assertEqual(self.bare_head(), self.initial_head)

    def test_hardlinks_are_hashed_with_one_actual_source_open(self):
        self.prime()
        original_open = Path.open
        reads = []
        sources = {self.source, self.link}

        def watch(path, *args, **kwargs):
            if path in sources:
                mode = args[0] if args else kwargs.get('mode', 'r')
                self.assertEqual(mode, 'rb')
                reads.append(path)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, 'open', new=watch):
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['source_files'], 2)
        self.assertEqual(result['unique_source_reads'], 1)
        self.assertEqual(len(reads), 1)
        self.assert_original_identity()

    def test_changed_source_reaches_bare_using_real_product_functions(self):
        self.prime()
        before = self.source.stat()
        # Same length AND restored mtime: a persistent stat shortcut would fail.
        changed = b'new skill text\r\n'
        self.assertEqual(len(changed), len(self.initial))
        self.source.write_bytes(changed)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        with mock.patch.object(core, 'apply', wraps=core.apply) as apply, \
             mock.patch.object(gitops, 'git_sync', wraps=gitops.git_sync) as sync:
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(result['changed_files'], 2)
        self.assertEqual(sync.call_count, 1)
        self.assertIn(mock.call(self.repo, 'push'), apply.call_args_list)
        for name in ('codex', 'shared'):
            self.assertEqual(self.bare_bytes('data/' + name + '/demo/SKILL.md'), changed)
        self.assertEqual(self.mirror.read_bytes(), changed)
        self.assertEqual(self.source.read_bytes(), changed)
        self.assert_original_identity()

    def test_remote_new_commit_stops_without_fetch_or_local_writes(self):
        self.prime()
        peer = self.base / 'peer'
        self.git(self.base, 'clone', str(self.bare), str(peer))
        self.configure(peer)
        (peer / 'data/rule.md').write_bytes(b'remote rule\n')
        self.git(peer, 'add', '--', 'data/rule.md')
        self.git(peer, 'commit', '-m', 'remote change')
        self.git(peer, 'push', 'origin', 'main')
        old = self.snapshot()
        with mock.patch.object(core, 'apply', side_effect=AssertionError('must not apply')), \
             mock.patch.object(gitops, 'git_sync', side_effect=AssertionError('must not sync')):
            result, code = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'remote_changed')
        self.assertFalse(result['uploaded'])
        self.assertEqual(old, self.snapshot())
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD').strip(), self.initial_head)

    def test_projected_skill_conflict_preserves_original_and_mirror(self):
        self.prime()
        changed = b'user changed original\n'
        self.source.write_bytes(changed)
        repository_skill = self.repo / 'data/codex/demo/SKILL.md'
        repository_skill.write_bytes(b'independent repository change\n')
        old_mirror = self.mirror.read_bytes()
        old_state = (self.root / 'collection-state.json').read_bytes()
        result, code = self.invoke()
        self.assertNotEqual(code, 0)
        # A skill pull itself requires review before the projected conflict is
        # considered; either safe-stop preserves every user's version.
        self.assertIn(result['status'], {'conflict', 'needs_skill_install_review'})
        self.assertFalse(result['uploaded'])
        self.assertEqual(self.bare_head(), self.initial_head)
        self.assertEqual(self.source.read_bytes(), changed)
        self.assertEqual(self.mirror.read_bytes(), old_mirror)
        self.assertEqual(repository_skill.read_bytes(), b'independent repository change\n')
        self.assertEqual((self.root / 'collection-state.json').read_bytes(), old_state)
        self.assert_original_identity()

    def test_direct_conflict_is_reported_and_neither_side_is_overwritten(self):
        self.prime()
        self.rule.write_bytes(b'local rule\n')
        (self.repo / 'data/rule.md').write_bytes(b'repository rule\n')
        old = self.snapshot()
        result, code = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(old, self.snapshot())
        self.assertEqual(self.bare_head(), self.initial_head)

    def test_manual_mirror_edit_is_not_erased_by_collection(self):
        self.prime()
        self.source.write_bytes(b'new source\n')
        self.mirror.write_bytes(b'manual mirror\n')
        old = self.snapshot()
        result, code = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'needs_skill_install_review')
        self.assertEqual(old, self.snapshot())
        self.assertFalse(result['uploaded'])

    def test_check_cli_writes_nothing_even_without_checkpoint_and_with_changes(self):
        self.source.write_bytes(b'pending source\n')
        old = self.snapshot()
        output = io.StringIO()
        with redirect_stdout(output), \
             mock.patch.object(core, 'plan', side_effect=AssertionError('plan creates lock')), \
             mock.patch.object(core, 'apply', side_effect=AssertionError('check must not apply')), \
             mock.patch.object(fast.sources, 'refresh',
                               side_effect=AssertionError('check must not refresh')) as refresh, \
             mock.patch.object(gitops, 'git_sync', side_effect=AssertionError('check must not sync')):
            code = fast.main(['--root', str(self.root), '--repo', str(self.repo), '--check'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'changes_pending')
        self.assertFalse(result['uploaded'])
        refresh.assert_not_called()
        self.assertIs(result['archives_checked'], False)
        self.assertEqual(result['archives_created'], 0)
        self.assertEqual(result['archives_unchanged'], 0)
        self.assertEqual(old, self.snapshot())
        self.assertFalse((self.root / 'fast-sync-state.json').exists())
        self.assertEqual(self.mirror.read_bytes(), self.initial)

    def test_legal_dirty_repo_is_uploaded_not_misreported_remote_changed(self):
        self.prime()
        changed = b'edited in content repository\n'
        (self.repo / 'data/rule.md').write_bytes(changed)
        with mock.patch.object(core, 'apply', wraps=core.apply) as apply, \
             mock.patch.object(gitops, 'git_sync', wraps=gitops.git_sync) as sync:
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'synced')
        self.assertTrue(result['uploaded'])
        self.assertIn(mock.call(self.repo, 'pull'), apply.call_args_list)
        self.assertIn(mock.call(self.repo, 'push'), apply.call_args_list)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(self.rule.read_bytes(), changed)
        self.assertEqual(self.bare_bytes('data/rule.md'), changed)
        self.assertEqual(result['changed_files'], 1)
        self.assertEqual(result['direct_pull_files'], 1)

    def test_untracked_legal_repository_file_is_not_a_fast_path(self):
        self.prime()
        (self.repo / 'data/extra.md').write_bytes(b'new permitted content\n')
        result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['uploaded'])
        self.assertEqual(self.bare_bytes('data/extra.md'), b'new permitted content\n')

    def test_unknown_mirror_file_is_preserved_and_blocks_upload(self):
        self.prime()
        extra = self.root / 'skill-mirror/codex/unreviewed.md'
        extra.write_bytes(b'not reviewed\n')
        old = self.snapshot()
        result, code = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'needs_skill_install_review')
        self.assertEqual(old, self.snapshot())

    def test_changed_selection_digest_invalidates_checkpoint(self):
        self.prime()
        path = self.root / 'selection.json'
        path.write_bytes(path.read_bytes() + b'\n')
        with mock.patch.object(core, 'plan', wraps=core.plan) as plan:
            result, code = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'up_to_date')
        self.assertEqual(plan.call_count, 1)

    def test_local_ahead_is_pending_not_up_to_date_or_remote_changed(self):
        self.prime()
        (self.repo / 'data/extra.md').write_bytes(b'local committed content\n')
        self.git(self.repo, 'add', '--', 'data/extra.md')
        self.git(self.repo, 'commit', '-m', 'already committed by user')
        old = self.snapshot()
        result, code = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'local_ahead')
        self.assertFalse(result['uploaded'])
        self.assertEqual(old, self.snapshot())
        self.assertEqual(self.bare_head(), self.initial_head)

    def test_preexisting_staged_change_is_preserved(self):
        self.prime()
        (self.repo / 'data/rule.md').write_bytes(b'user staged change\n')
        self.git(self.repo, 'add', '--', 'data/rule.md')
        old = self.snapshot()
        result, code = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'staged_changes')
        self.assertEqual(old, self.snapshot())

    def test_public_remote_rejected_even_on_primed_fast_path_without_writes(self):
        self.prime()
        self.git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/example/knowledge.git')
        old = self.snapshot()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"private":false,"full_name":"example/knowledge"}'
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'fake-test-token'}), \
             mock.patch('urllib.request.OpenerDirector.open', return_value=response) as transport, \
             mock.patch.object(fast, 'workspace_lock', side_effect=AssertionError('public lock')), \
             mock.patch.object(core, 'apply', side_effect=AssertionError('public apply')), \
             mock.patch.object(collect_skills, 'collect', side_effect=AssertionError('public collect')):
            for check in (False, True):
                result, code = self.invoke(check)
                self.assertNotEqual(code, 0)
                self.assertFalse(result['uploaded'])
                self.assertNotIn('fake-test-token', json.dumps(result))
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(old, self.snapshot())

    def test_empty_or_missing_selection_never_claims_skills_are_connected(self):
        for missing in (False, True):
            path = self.root / 'selection.json'
            if missing:
                path.unlink()
            else:
                core._save(path, {'version': 1, 'files': []})
            old = self.snapshot()
            with mock.patch.object(collect_skills, 'collect', side_effect=AssertionError('no selection')):
                result, code = self.invoke()
            self.assertNotEqual(code, 0)
            self.assertEqual(result['status'], 'selection_required')
            self.assertEqual(old, self.snapshot())


if __name__ == '__main__':
    unittest.main()
