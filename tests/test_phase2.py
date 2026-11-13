import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_sync import core, projects, records
from agent_sync.__main__ import main
from agent_sync.gitops import git_sync


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        home = self.root / "isolated-home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home),
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.environment = mock.patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.assertIsNotNone(shutil.which("git"), "验收要求 Git 可用，不跳过")
        self.c1 = self.root / "content-win"
        self.c2 = self.root / "content-mac"
        core.init_repo(self.c1)
        core.init_repo(self.c2)
        self.remote = self.root / "project.git"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.p1 = self.root / "win-project"
        self.p2 = self.root / "mac-project"
        self.git(self.root, "clone", str(self.remote), str(self.p1))
        self.commit(self.p1, "code.txt", "initial\n")
        self.git(self.p1, "push", "-u", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.p2))
        projects.add(self.c1, "demo", str(self.remote), "main")
        self.copy_registry()
        projects.bind(self.c1, "demo", self.p1)
        projects.bind(self.c2, "demo", self.p2)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.note = self.inputs / "notes.txt"
        self.note.write_text("Explicit notes\n", encoding="utf-8")

    def git(self, path, *args):
        result = subprocess.run(["git", "-C", str(path), *args], shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8", "strict").strip()

    def commit(self, path, name, content):
        (path / name).write_text(content, encoding="utf-8")
        self.git(path, "add", "--", name)
        self.git(path, "commit", "-m", "explicit test commit")
        return self.git(path, "rev-parse", "HEAD")

    def copy_registry(self):
        target = self.c2 / "data/projects"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.c1 / "data/projects/registry.json", target / "registry.json")

    def task(self):
        return records.task_add(self.c1, "demo", "Test task", self.note)

    def handoff(self, task):
        return records.handoff_create(self.c1, task["id"], self.note, self.note, self.note, self.note)

    def test_project_sync_sends_and_receives_committed_code(self):
        first = self.commit(self.p1, "one.txt", "device one\n")
        self.assertEqual(projects.sync(self.c1, "demo")["head"], first)
        projects.sync(self.c2, "demo")
        self.assertEqual((self.p2 / "one.txt").read_text(), "device one\n")
        second = self.commit(self.p2, "two.txt", "device two\n")
        projects.sync(self.c2, "demo")
        result = projects.sync(self.c1, "demo")
        self.assertEqual(result["head"], second)
        self.assertFalse(result["created_commit"])
        self.assertEqual(self.git(self.p1, "rev-parse", "HEAD"), second)
        self.assertTrue(projects.status(self.c1, "demo")["clean"])
        self.assertFalse((self.c1 / "data/projects/demo").exists())
        core._scan_tree(self.c1 / "data")

    def test_different_device_paths_same_logical_project(self):
        one = projects.list_projects(self.c1)[0]
        two = projects.list_projects(self.c2)[0]
        self.assertEqual(one["id"], two["id"])
        self.assertNotEqual(one["path"], two["path"])
        raw = (self.c1 / "data/projects/registry.json").read_text()
        self.assertNotIn(str(self.p1), raw)
        self.assertNotIn(str(self.p2), raw)
        self.assertEqual(json.loads(raw)["projects"]["demo"]["branch"], "main")

    def test_dirty_staged_and_untracked_are_preserved(self):
        before = self.git(self.p1, "rev-parse", "HEAD")
        (self.p1 / "code.txt").write_text("uncommitted", encoding="utf-8")
        with self.assertRaises(core.Error):
            projects.sync(self.c1, "demo")
        self.assertEqual((self.p1 / "code.txt").read_text(), "uncommitted")
        self.git(self.p1, "add", "code.txt")
        index_before = self.git(self.p1, "diff", "--cached")
        with self.assertRaises(core.Error):
            projects.sync(self.c1, "demo")
        self.assertEqual(self.git(self.p1, "diff", "--cached"), index_before)
        self.assertEqual(self.git(self.p1, "rev-parse", "HEAD"), before)
        (self.p2 / "new.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(core.Error):
            projects.sync(self.c2, "demo")
        self.assertEqual((self.p2 / "new.txt").read_text(), "keep")

    def test_divergent_commits_preserved(self):
        local = self.commit(self.p1, "left.txt", "left")
        remote = self.commit(self.p2, "right.txt", "right")
        self.git(self.p2, "push")
        with self.assertRaisesRegex(core.Error, "分叉"):
            projects.sync(self.c1, "demo")
        self.assertEqual(self.git(self.p1, "rev-parse", "HEAD"), local)
        self.assertEqual(self.git(self.p1, "rev-parse", "origin/main"), remote)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), remote)
        self.assertEqual((self.p1 / "left.txt").read_text(), "left")
        self.assertFalse((self.p1 / ".git/MERGE_HEAD").exists())

    def test_remote_and_pushurl_mismatch_preserved(self):
        before = self.git(self.p1, "rev-parse", "HEAD")
        wrong = self.root / "wrong.git"
        self.git(self.root, "init", "--bare", str(wrong))
        self.git(self.p1, "remote", "set-url", "origin", str(wrong))
        with self.assertRaises(core.Error):
            projects.sync(self.c1, "demo")
        self.assertEqual(self.git(self.p1, "rev-parse", "HEAD"), before)
        self.git(self.p1, "remote", "set-url", "origin", str(self.remote))
        self.git(self.p1, "remote", "set-url", "--push", "origin", str(wrong))
        with self.assertRaises(core.Error):
            projects.sync(self.c1, "demo")
        self.assertEqual(self.git(self.p1, "rev-parse", "HEAD"), before)

    def test_remote_rewrite_credentials_rejected_without_echo(self):
        self.git(self.p1, "config", "url.https://user:TOKEN@example.invalid/repo.insteadOf", str(self.remote))
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = main(["--repo", str(self.c1), "project", "sync", "demo"])
        self.assertEqual(result, 1)
        self.assertNotIn("TOKEN", error.getvalue())

    def test_detached_in_progress_and_wrong_root_rejected(self):
        head = self.git(self.p1, "rev-parse", "HEAD")
        self.git(self.p1, "checkout", "--detach", head)
        with self.assertRaises(core.Error):
            projects.sync(self.c1, "demo")
        self.git(self.p1, "checkout", "main")
        marker = self.p1 / ".git/rebase-merge"
        marker.mkdir()
        with self.assertRaises(core.Error):
            projects.sync(self.c1, "demo")
        marker.rmdir()
        child = self.p1 / "child"
        child.mkdir()
        with self.assertRaises(core.Error):
            projects.bind(self.c1, "demo", child)
        with self.assertRaises(core.Error):
            projects.bind(self.c1, "demo", self.remote)
        self.assertEqual(self.git(self.p1, "rev-parse", "HEAD"), head)

    def test_task_lifecycle_and_immutable_events(self):
        task = self.task()
        self.assertEqual(task["status"], "todo")
        first_event = self.c1 / "data/task-events" / (task["revision"] + ".json")
        original = first_event.read_bytes()
        revisions = [task["revision"]]
        for status in ("doing", "blocked", "done"):
            task = records.task_set(self.c1, task["id"], status=status)
            revisions.append(task["revision"])
        self.assertEqual(len(set(revisions)), 4)
        self.assertEqual(first_event.read_bytes(), original)
        self.assertEqual(records.task_show(self.c1, task["id"]), task)
        self.assertEqual(records.task_list(self.c1, "demo"), [task])
        self.assertEqual(len(list((self.c1 / "data/task-events").glob("*.json"))), 4)
        records.validate_shared(self.c1)
        with self.assertRaises(FileExistsError):
            records._immutable(first_event, {})
        self.assertEqual(first_event.read_bytes(), original)

    def test_unknown_project_illegal_status_and_snapshot_mismatch(self):
        with self.assertRaises(core.Error):
            records.task_add(self.c1, "unknown", "Title")
        task = self.task()
        with self.assertRaises(core.Error):
            records.task_set(self.c1, task["id"], status="passed")
        with self.assertRaises(core.Error):
            records.task_list(self.c1, "unknown")
        path = self.c1 / "data/tasks" / (task["id"] + ".json")
        altered = {**task, "notes": "unrecorded overwrite"}
        path.write_text(json.dumps(altered), encoding="utf-8")
        with self.assertRaises(core.Error):
            records.task_show(self.c1, task["id"])
        with self.assertRaises(core.Error):
            records.validate_shared(self.c1)

    def test_handoff_immutable_no_generated_paths_and_head(self):
        task = self.task()
        record = self.handoff(task)
        package = self.c1 / "data/handoffs" / record["id"]
        original = {p.name: p.read_bytes() for p in package.iterdir()}
        self.assertEqual(set(original), {"record.json", "summary.md"})
        self.assertEqual(record["project_state"]["head"], self.git(self.p1, "rev-parse", "HEAD"))
        self.assertTrue(record["project_state"]["clean"])
        for content in original.values():
            self.assertNotIn(str(self.root).encode(), content)
            self.assertNotIn(str(self.note).encode(), content)
        self.assertEqual(records.handoff_show(self.c1, record["id"]), record)
        records.task_set(self.c1, task["id"], status="done")
        (self.p1 / "code.txt").write_text("dirty", encoding="utf-8")
        second = self.handoff(task)
        self.assertNotEqual(second["id"], record["id"])
        self.assertFalse(second["project_state"]["clean"])
        self.assertEqual({p.name: p.read_bytes() for p in package.iterdir()}, original)
        self.assertEqual(len(records.handoff_list(self.c1, task["id"])), 2)
        records.validate_shared(self.c1)
        (package / "summary.md").write_text("tampered", encoding="utf-8")
        with self.assertRaises(core.Error):
            records.handoff_show(self.c1, record["id"])

    def test_traversal_and_parameter_injection_rejected(self):
        for ident in ("../outside", "--upload-pack=bad", "a/b", "con", "a\\b"):
            with self.subTest(ident=ident):
                with self.assertRaises(core.Error):
                    projects.add(self.c1, ident, str(self.remote), "main")
                with self.assertRaises(core.Error):
                    records.task_show(self.c1, ident)
                with self.assertRaises(core.Error):
                    records.handoff_show(self.c1, ident)
        for remote in ("--upload-pack=bad", "ext::bad", "https://u:TOKEN@example.invalid/repo",
                       "https://example.invalid/repo?token=TOKEN", "git@example.invalid:../repo.git"):
            with self.subTest(remote=remote):
                with self.assertRaises(core.Error) as raised:
                    projects.add(self.c1, "bad", remote, "main")
                self.assertNotIn("TOKEN", str(raised.exception))
        for branch in ("--help", "main..x", "a/../b", "main;bad", "a.lock"):
            with self.assertRaises(core.Error):
                projects.add(self.c1, "bad", str(self.remote), branch)
        with self.assertRaises(core.Error):
            records.task_add(self.c1, "demo", "Title", str(self.inputs / ".." / "inputs" / "notes.txt"))
        with self.assertRaises(core.Error):
            records.task_add(self.c1, "demo", "Title", "relative.txt")
        with self.assertRaises(core.Error):
            projects.bind(self.c1, "demo", self.c1)

    def test_real_symlink_input_rejected_without_skip(self):
        link = self.inputs / "linked.txt"
        # Deliberately no skip: authorized Windows acceptance must actually create it.
        link.symlink_to(self.note)
        self.assertTrue(link.is_symlink())
        with self.assertRaises(core.Error):
            records.task_add(self.c1, "demo", "Title", link)
        self.assertEqual(self.note.read_text(), "Explicit notes\n")

    def test_input_read_only_missing_empty_and_oversize(self):
        before = self.note.read_bytes()
        self.task()
        self.assertEqual(self.note.read_bytes(), before)
        with self.assertRaises(core.Error):
            records.task_add(self.c1, "demo", "Title", self.inputs / "missing.txt")
        empty = self.inputs / "empty.txt"
        empty.write_text("", encoding="utf-8")
        task = self.task()
        with self.assertRaises(core.Error):
            records.handoff_create(self.c1, task["id"], empty, self.note, self.note, self.note)
        large = self.inputs / "large.txt"
        large.write_bytes(b"x" * (records.LIMIT + 1))
        with self.assertRaises(core.Error):
            records.task_add(self.c1, "demo", "Title", large)

    def test_managed_namespace_overlap_is_rejected(self):
        from agent_sync import history
        for path in ('data/tasks', 'data/history', 'data/History/nested'):
            with self.subTest(path=path), self.assertRaises(core.Error):
                core.add_group(self.c1, "overlap", "memory", "tree", path)
        # Model an already-invalid manifest without going through the new add guard.
        core._save(self.c1 / 'manifest.json', {'version': 1, 'groups': [
            {'name': 'overlap', 'category': 'memory', 'kind': 'tree', 'path': 'data/history'}]})
        for operation in (
            lambda: core.apply(self.c1, 'push'),
            lambda: records.task_add(self.c1, 'demo', 'Title'),
            lambda: records.validate_shared(self.c1),
            lambda: history.create_archive(self.c1, b'text', 'text', tool='dsh'),
        ):
            with self.assertRaises(core.Error):
                operation()
        self.assertFalse((self.c1 / 'data/history').exists())

    def test_cli_commands_match_documented_interface(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["--repo", str(self.c1), "task", "add", "--project", "demo",
                           "--title", "CLI title", "--id", "cli-task", "--note-file", str(self.note)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["id"], "cli-task")
        for args in (["project", "list"], ["project", "status", "demo"],
                     ["task", "list", "--project", "demo"], ["task", "show", "cli-task"],
                     ["task", "set", "cli-task", "--status", "doing"], ["handoff", "list"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--repo", str(self.c1), *args]), 0)

    def bootstrap_content(self):
        remote = self.root / "content.git"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        self.git(self.c1, "init", "--initial-branch=main")
        self.git(self.c1, "config", "core.autocrlf", "false")
        self.git(self.c1, "add", "--", "manifest.json", ".gitignore", ".gitattributes", "data/")
        self.git(self.c1, "commit", "-m", "bootstrap")
        self.git(self.c1, "remote", "add", "origin", str(remote))
        self.git(self.c1, "push", "-u", "origin", "main")
        clone = self.root / "second-content-clone"
        self.git(self.root, "clone", str(remote), str(clone))
        self.git(clone, "config", "core.autocrlf", "false")
        core.init_repo(clone)
        projects.bind(clone, "demo", self.p2)
        return clone

    def test_feature_and_detached_status_and_handoff(self):
        task = self.task()
        for branch in ('feature/work', None):
            if branch is None:
                self.git(self.p1, 'checkout', '--detach')
            else:
                self.git(self.p1, 'checkout', '-b', branch)
            before = projects._snapshot(self.p1)
            status = projects.status(self.c1, 'demo')
            self.assertEqual(status['branch'], branch)
            self.assertEqual(status['registered_branch'], 'main')
            self.assertFalse(status['can_sync'])
            self.assertTrue(status['reasons'])
            self.assertEqual(projects._snapshot(self.p1), before)
            with self.assertRaises(core.Error):
                projects.sync(self.c1, 'demo')
            record = self.handoff(task)
            self.assertEqual(record['project_state']['branch'], branch)
            self.assertEqual(record['project_state']['registered_branch'], 'main')
            self.assertEqual(records.handoff_show(self.c1, record['id']), record)
            self.assertNotIn(str(self.root), json.dumps(record))
        legacy = self.handoff(task)
        legacy['project_state'] = {k: legacy['project_state'][k] for k in ('head', 'clean')}
        package = self.c1 / 'data/handoffs' / legacy['id']
        core._save(package / 'record.json', legacy)
        (package / 'summary.md').write_bytes(records._markdown(legacy))
        self.assertEqual(records.handoff_show(self.c1, legacy['id']), legacy)

    def test_staging_uses_ordinary_mkdir_and_cleans_failure(self):
        task = self.task()
        original = Path.mkdir
        observed = []
        def mkdir(path, *args, **kwargs):
            if path.parent == self.c1 / '.agent-sync/handoff-staging':
                observed.append((args, kwargs))
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'mkdir', mkdir), mock.patch('tempfile.mkdtemp', side_effect=AssertionError('no private staging')):
            self.handoff(task)
        self.assertTrue(observed)
        self.assertTrue(all(not args and kwargs.get('mode', 0o777) == 0o777 for args, kwargs in observed))
        with mock.patch.object(records.os, 'rename', side_effect=OSError('publication failure')):
            with self.assertRaises(OSError):
                self.handoff(task)
        self.assertEqual(list((self.c1 / '.agent-sync/handoff-staging').iterdir()), [])
        self.assertEqual(len(records.handoff_list(self.c1)), 1)

    def test_autocrlf_first_clone_preserves_handoff_and_history(self):
        from agent_sync import history
        self.note.write_bytes(b'first\r\nsecond\rthird\n')
        task = self.task()
        self.assertEqual(task['notes'], 'first\nsecond\nthird\n')
        handoff = self.handoff(task)
        raw = b'Original history\r\nkeep CR too\r'
        archive = history.create_archive(self.c1, raw, 'text', tool='dsh')
        self.bootstrap_content()
        clone = self.root / 'autocrlf-first-clone'
        remote = self.root / 'content.git'
        self.git(self.root, '-c', 'core.autocrlf=true', 'clone', str(remote), str(clone))
        self.git(clone, 'config', 'core.autocrlf', 'true')
        core.init_repo(clone)
        self.assertEqual(records.handoff_show(clone, handoff['id']), handoff)
        source = self.c1 / 'data/handoffs' / handoff['id'] / 'summary.md'
        target = clone / 'data/handoffs' / handoff['id'] / 'summary.md'
        self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertNotIn(b'\r', target.read_bytes())
        self.assertEqual(history.read_archive(clone, archive['archive_id'])[1], raw)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--repo', str(clone), 'history', 'verify', archive['archive_id']]), 0)
        self.assertEqual(git_sync(clone)['status'], 'pushed')
        self.assertEqual(self.note.read_bytes(), b'first\r\nsecond\rthird\n')

    def test_content_git_sync_transfers_tasks_events_and_handoffs(self):
        other = self.bootstrap_content()
        task = self.task()
        handoff = self.handoff(task)
        git_sync(self.c1)
        git_sync(other)
        self.assertEqual(records.task_show(other, task["id"]), task)
        self.assertEqual(records.handoff_show(other, handoff["id"]), handoff)
        updated = records.task_set(other, task["id"], status="doing")
        self.assertNotEqual(updated["device"], task["device"])
        git_sync(other)
        git_sync(self.c1)
        self.assertEqual(records.task_show(self.c1, task["id"]), updated)
        tracked = self.git(self.c1, "ls-files").splitlines()
        self.assertFalse(any(p.startswith(".agent-sync/") for p in tracked))
        self.assertFalse(any(".git/" in p for p in tracked))
        self.assertEqual(records.handoff_show(self.c1, handoff["id"]), handoff)

    def test_content_task_conflict_does_not_choose_a_side(self):
        task = self.task()
        other = self.bootstrap_content()
        left = records.task_set(self.c1, task["id"], status="doing")
        right = records.task_set(other, task["id"], status="blocked")
        git_sync(self.c1)
        remote_head = self.git(self.c1, "rev-parse", "HEAD")
        with self.assertRaises(core.Error):
            git_sync(other)
        self.assertEqual(records.task_show(other, task["id"]), right)
        self.assertEqual(records.task_show(self.c1, task["id"]), left)
        self.assertEqual(self.git(other, "rev-parse", "origin/main"), remote_head)
        self.assertFalse((other / ".git/MERGE_HEAD").exists())
        self.assertTrue((other / "data/task-events" / (right["revision"] + ".json")).exists())


if __name__ == "__main__":
    unittest.main()
