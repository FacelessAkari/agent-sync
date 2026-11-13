import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from agent_sync import core
from agent_sync.gitops import git_sync


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@unittest.skipUnless(shutil.which("git"), "需要系统 Git")
class GitTests(unittest.TestCase):
    def git(self, repo, *args, check=True):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
        )
        if check and result.returncode:
            self.fail("测试 Git 命令失败: " + " ".join(args) + "\n" + result.stderr.decode(errors="replace"))
        return result

    def identity(self, repo):
        self.git(repo, "config", "user.name", "AgentSync Test")
        self.git(repo, "config", "user.email", "agent-sync-test@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")
        self.git(repo, "config", "core.autocrlf", "false")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve only the trusted TemporaryDirectory root (e.g. macOS /var).
        self.root = Path(self.temp.name).resolve()
        # Never read a real user's HOME or global/system Git configuration in these tests.
        home = self.root / "empty-home"
        home.mkdir()
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("GIT_"):
                self.env.pop(key)
        self.env.update({"HOME": str(home), "USERPROFILE": str(home),
                         "XDG_CONFIG_HOME": str(home / "xdg"), "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_CONFIG_GLOBAL": str(home / "no-global-config"), "GIT_TERMINAL_PROMPT": "0"})
        from unittest import mock
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        self.git(self.remote, "init", "--bare")
        seed = self.root / "seed"
        core.init_repo(seed)
        core.add_group(seed, "knowledge", "memory", "tree", "data/knowledge")
        put(seed / "data/knowledge/shared.md", "base\n")
        put(seed / "code.py", "# unrelated tracked bootstrap file\n")
        self.git(seed, "init", "-b", "main")
        self.identity(seed)
        self.git(seed, "add", "--", "manifest.json", "data/", ".gitignore", ".gitattributes", "code.py")
        self.git(seed, "commit", "-m", "bootstrap")
        self.git(seed, "remote", "add", "origin", str(self.remote))
        self.git(seed, "push", "-u", "origin", "main")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.a, self.b = self.root / "clone-a", self.root / "clone-b"
        for repo in (self.a, self.b):
            self.git(self.root, "clone", str(self.remote), str(repo))
            self.identity(repo)
            core.init_repo(repo)

    def assert_precommit_rejected(self, repo):
        put(repo / 'data/knowledge/pending.md', 'must remain uncommitted\n')
        head = self.git(repo, 'rev-parse', 'HEAD').stdout
        remote = self.git(self.remote, 'rev-parse', 'main').stdout
        with self.assertRaises(core.Error):
            git_sync(repo)
        self.assertEqual(self.git(repo, 'rev-parse', 'HEAD').stdout, head)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main').stdout, remote)
        self.assertEqual(self.git(repo, 'diff', '--cached', '--name-only').stdout, b'')

    def test_remote_group_removal_leaves_device_state_reviewable(self):
        local = self.root / 'device-b-skills'
        core.bind(self.b, 'knowledge', local)
        core.apply(self.b, 'pull')
        original = (local / 'shared.md').read_bytes()
        baseline = (self.b / '.agent-sync/baseline.json').read_bytes()
        core._save(self.a / 'manifest.json', {'version': 1, 'groups': []})
        git_sync(self.a)
        git_sync(self.b)
        rows = core.plan(self.b)
        self.assertEqual(rows[0]['action'], 'orphan')
        self.assertEqual(rows[0]['cleanup'], 'unbind knowledge')
        self.assertEqual((self.b / '.agent-sync/baseline.json').read_bytes(), baseline)
        core.apply(self.b, 'push')
        self.assertEqual((self.b / '.agent-sync/baseline.json').read_bytes(), baseline)
        with self.assertRaises(core.Error):
            core.add_group(self.b, 'knowledge', 'memory', 'tree', 'data/knowledge')
        core.unbind(self.b, 'knowledge')
        self.assertEqual(core.plan(self.b), [])
        self.assertEqual(core._load(self.b)[1:], ({}, {}))
        self.assertEqual((local / 'shared.md').read_bytes(), original)
        self.assertEqual((self.b / 'data/knowledge/shared.md').read_bytes(), original)

    def test_missing_attributes_local_and_upstream_fail_before_commit(self):
        self.git(self.a, 'rm', '--cached', '.gitattributes')
        self.git(self.a, 'commit', '-m', 'missing attributes fixture')
        self.assert_precommit_rejected(self.a)
        self.git(self.a, 'push')
        self.assert_precommit_rejected(self.b)

    def test_effective_attribute_override_rejected_before_commit(self):
        put(self.a / '.git/info/attributes', 'data/** text\n')
        self.assert_precommit_rejected(self.a)

    def test_caches_are_neither_baselined_nor_staged(self):
        local = self.root / 'local-skills'
        put(local / 'SKILL.md', '# safe\n')
        for root in (local, self.a / 'data/knowledge'):
            put(root / '__pycache__/run.pyc', 'cache')
            put(root / '.DS_Store', 'finder')
            put(root / 'run.pyc', 'cache')
        core.bind(self.a, 'knowledge', local)
        core.apply(self.a, 'push')
        core.plan(self.a)
        git_sync(self.a)
        baseline = (self.a / '.agent-sync/baseline.json').read_bytes()
        tree = self.git(self.a, 'ls-tree', '-r', '--name-only', 'HEAD').stdout
        for value in (b'__pycache__', b'.DS_Store', b'.pyc'):
            self.assertNotIn(value, baseline)
            self.assertNotIn(value, tree)
        for name in ('.env', 'auth.json', 'id_ed25519'):
            path = local / name
            put(path, 'synthetic sensitive name')
            with self.assertRaises(core.Error):
                core.plan(self.a)
            path.unlink()

    def test_git_environment_cannot_redirect_index_or_repository(self):
        from unittest import mock
        external = self.root / 'external-index'
        external.write_bytes(b'untouched external index')
        poison = {
            'GIT_DIR': str(self.remote), 'GIT_WORK_TREE': str(self.root),
            'GIT_COMMON_DIR': str(self.remote), 'GIT_INDEX_FILE': str(external),
            'GIT_OBJECT_DIRECTORY': str(self.root / 'objects'),
            'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(self.root / 'alternates'),
            'GIT_NAMESPACE': 'poison', 'GIT_CONFIG_PARAMETERS': 'invalid',
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare',
            'GIT_CONFIG_VALUE_0': 'true', 'GIT_ALLOW_PROTOCOL': 'http',
        }
        put(self.a / 'data/knowledge/new.md', 'new\n')
        with mock.patch.dict(os.environ, poison):
            self.assertEqual(git_sync(self.a)['status'], 'pushed')
        self.assertEqual(external.read_bytes(), b'untouched external index')
        self.assertEqual(self.git(self.a, 'show', 'HEAD:data/knowledge/new.md').stdout, b'new\n')

    def test_http_transport_is_not_enabled_by_local_configuration(self):
        self.git(self.a, 'remote', 'set-url', 'origin', 'http://127.0.0.1:1/repo')
        self.git(self.a, 'config', 'protocol.http.allow', 'always')
        self.assert_precommit_rejected(self.a)

    def test_untracked_bootstrap_ignore_rejected_before_commit(self):
        self.git(self.a, 'rm', '--cached', '.gitignore')
        self.git(self.a, 'commit', '-m', 'remove tracked bootstrap fixture')
        self.assertEqual(self.git(self.a, 'check-ignore', '-q', '.agent-sync/device.json').returncode, 0)
        self.assert_precommit_rejected(self.a)

    def test_global_only_ignore_rejected_before_commit(self):
        put(self.a / '.gitignore', '# no repository protection\n')
        self.git(self.a, 'add', '.gitignore')
        self.git(self.a, 'commit', '-m', 'global only fixture')
        excludes = self.root / 'global-ignore'
        put(excludes, '/.agent-sync/\n')
        self.git(self.a, 'config', '--global', 'core.excludesFile', str(excludes))
        self.assertEqual(self.git(self.a, 'check-ignore', '-q', '.agent-sync/device.json').returncode, 0)
        self.assert_precommit_rejected(self.a)

    def test_remote_missing_ignore_rejected_before_commit(self):
        self.git(self.a, 'rm', '.gitignore')
        self.git(self.a, 'commit', '-m', 'remote missing bootstrap fixture')
        self.git(self.a, 'push')
        self.assert_precommit_rejected(self.b)

    def test_remote_missing_root_rule_rejected_before_commit(self):
        put(self.a / '.gitignore', '# missing root rule\n')
        self.git(self.a, 'add', '.gitignore')
        self.git(self.a, 'commit', '-m', 'remote missing rule fixture')
        self.git(self.a, 'push')
        self.assert_precommit_rejected(self.b)

    def test_negation_cannot_reopen_committed_state_directory(self):
        put(self.a / '.gitignore', '/.agent-sync/\n!/.agent-sync/\n')
        self.git(self.a, 'add', '.gitignore')
        self.git(self.a, 'commit', '-m', 'negation fixture')
        self.assert_precommit_rejected(self.a)

    def test_corrupt_history_rejected_before_commit(self):
        from agent_sync import history
        result = history.create_archive(self.a, b'archive text', 'text', tool='dsh')
        git_sync(self.a)
        path = history.archive_path(self.a, result['archive_id']) / 'transcript.md'
        path.write_bytes(path.read_bytes() + b'corrupt')
        self.assert_precommit_rejected(self.a)

    def test_remote_corrupt_history_is_not_pushed_after_merge(self):
        from agent_sync import history
        result = history.create_archive(self.a, b'archive text', 'text', tool='dsh')
        git_sync(self.a)
        git_sync(self.b)
        path = history.archive_path(self.a, result['archive_id']) / 'transcript.md'
        path.write_bytes(path.read_bytes() + b'corrupt')
        self.git(self.a, 'add', 'data/')
        self.git(self.a, 'commit', '-m', 'corrupt remote fixture')
        self.git(self.a, 'push')
        remote = self.git(self.remote, 'rev-parse', 'main').stdout
        put(self.b / 'data/knowledge/local.md', 'local\n')
        with self.assertRaises(core.Error):
            git_sync(self.b)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main').stdout, remote)
        self.assertNotEqual(self.git(self.b, 'rev-parse', 'HEAD').stdout, remote)

    def test_independent_additions_merge_and_only_whitelist_committed(self):
        put(self.a / "data/knowledge/a.md", "device a\n")
        put(self.b / "data/knowledge/b.md", "device b\n")
        put(self.b / "not-registered.txt", "leave untracked\n")
        self.assertEqual(git_sync(self.a)["status"], "pushed")
        self.assertEqual(git_sync(self.b)["status"], "pushed")
        self.assertEqual((self.b / "data/knowledge/a.md").read_text(), "device a\n")
        self.assertEqual((self.b / "data/knowledge/b.md").read_text(), "device b\n")
        git_sync(self.a)
        self.assertEqual((self.a / "data/knowledge/b.md").read_text(), "device b\n")
        tracked = self.git(self.b, "ls-files").stdout.decode()
        self.assertNotIn(".agent-sync", tracked)
        self.assertNotIn("not-registered.txt", tracked)
        self.assertTrue((self.b / "not-registered.txt").exists())
        tree = self.git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout.decode()
        self.assertIn("data/knowledge/a.md", tree)
        self.assertIn("data/knowledge/b.md", tree)
        self.assertNotIn(".agent-sync", tree)

    def test_same_file_conflict_aborts_and_preserves_both_commits(self):
        relative = "data/knowledge/shared.md"
        original_b = self.git(self.b, "rev-parse", "HEAD").stdout
        put(self.a / relative, "remote edit\n")
        put(self.b / relative, "local edit\n")
        git_sync(self.a)
        remote_head = self.git(self.remote, "rev-parse", "main").stdout
        with self.assertRaisesRegex(core.Error, "merge"):
            git_sync(self.b)
        self.assertEqual((self.b / relative).read_text(), "local edit\n")
        self.assertNotEqual(self.git(self.b, "rev-parse", "HEAD").stdout, original_b)
        self.assertEqual(self.git(self.b, "show", "origin/main:" + relative).stdout, b"remote edit\n")
        self.assertEqual(self.git(self.b, "show", "HEAD:" + relative).stdout, b"local edit\n")
        self.assertEqual(self.git(self.remote, "rev-parse", "main").stdout, remote_head)
        self.assertEqual(self.git(self.b, "status", "--porcelain").stdout, b"")
        self.assertNotEqual(self.git(self.b, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode, 0)

    def test_dirty_code_and_preexisting_staging_are_rejected(self):
        before = self.git(self.remote, "rev-parse", "main").stdout
        put(self.a / "code.py", "# dirty code\n")
        put(self.a / "data/knowledge/new.md", "allowed but must not be committed\n")
        with self.assertRaisesRegex(core.Error, "非白名单"):
            git_sync(self.a)
        self.assertEqual(self.git(self.a, "diff", "--cached", "--name-only").stdout, b"")
        self.git(self.a, "restore", "--", "code.py")
        self.git(self.a, "add", "--", "data/knowledge/new.md")
        with self.assertRaisesRegex(core.Error, "staged"):
            git_sync(self.a)
        self.assertIn(b"data/knowledge/new.md", self.git(self.a, "diff", "--cached", "--name-only").stdout)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").stdout, before)

    def test_detached_upstream_and_tracked_state_rejected(self):
        self.git(self.a, "checkout", "--detach")
        with self.assertRaises(core.Error):
            git_sync(self.a)
        self.git(self.a, "checkout", "main")
        self.git(self.a, "branch", "--unset-upstream")
        with self.assertRaises(core.Error):
            git_sync(self.a)
        self.git(self.a, "branch", "--set-upstream-to=origin/main")
        self.git(self.a, "add", "-f", "--", ".agent-sync/device.json")
        self.git(self.a, "commit", "-m", "intentionally unsafe test fixture")
        with self.assertRaises(core.Error):
            git_sync(self.a)

    def test_remote_unsafe_content_rejected_before_local_commit(self):
        put(self.a / "data/knowledge/auth.json", '{"example": "not a credential"}\n')
        self.git(self.a, "add", "--", "data/knowledge/auth.json")
        self.git(self.a, "commit", "-m", "unsafe filename fixture")
        self.git(self.a, "push")
        before = self.git(self.b, "rev-parse", "HEAD").stdout
        put(self.b / "data/knowledge/local.md", "local addition\n")
        with self.assertRaises(core.Error):
            git_sync(self.b)
        self.assertEqual(self.git(self.b, "rev-parse", "HEAD").stdout, before)
        self.assertFalse((self.b / "data/knowledge/auth.json").exists())
        self.assertEqual(self.git(self.b, "diff", "--cached", "--name-only").stdout, b"")

    def test_push_failure_is_not_success_and_keeps_local_commit(self):
        # A nonexistent local push destination needs no network and guarantees failure.
        self.git(self.a, "remote", "set-url", "--push", "origin", str(self.root / "missing.git"))
        put(self.a / "data/knowledge/pending.md", "pending\n")
        with self.assertRaisesRegex(core.Error, "push"):
            git_sync(self.a)
        self.assertEqual(self.git(self.a, "show", "HEAD:data/knowledge/pending.md").stdout, b"pending\n")
        self.assertEqual(self.git(self.a, "status", "--porcelain").stdout, b"")


if __name__ == "__main__":
    unittest.main()
