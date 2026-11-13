import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_sync import core


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve only the trusted TemporaryDirectory root (e.g. macOS /var).
        # Production bindings and links created inside this root remain unchecked here.
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "content-a"
        self.local = self.root / "device-a"
        core.init_repo(self.repo)

    def register(self, name="rules", kind="file", category="rules", relative="data/rules.md", local=None):
        target = local or self.local / ("AGENTS.md" if kind == "file" else "skills")
        core.add_group(self.repo, name, category, kind, relative)
        core.bind(self.repo, name, target)
        return target

    def actions(self, repo=None):
        return [row["action"] for row in core.plan(repo or self.repo)]

    def test_help(self):
        result = subprocess.run([sys.executable, "-m", "agent_sync", "--help"],
                                capture_output=True, shell=False, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git-sync", result.stdout)

    def test_two_devices_rules_skills_memory_persona(self):
        specs = [
            ("rules", "rules", "file", "data/rules.md", "AGENTS.md", "共同规则"),
            ("skills", "skills", "tree", "data/skills", "skills", None),
            ("memory", "memory", "tree", "data/memory", "memory", None),
            ("persona", "personas", "file", "data/personas/whale.md", "whale/AGENTS.md", "助手示例人设"),
        ]
        for name, category, kind, relative, binding, text in specs:
            core.add_group(self.repo, name, category, kind, relative)
            core.bind(self.repo, name, self.local / binding)
            if text is not None:
                put(self.local / binding, text)
        put(self.local / "skills/demo/SKILL.md", "# 示例技能")
        put(self.local / "skills/demo/run.py", "print('demo')\n")
        put(self.local / "memory/index.md", "- facts.md")
        put(self.local / "memory/facts.md", "仅使用虚构项目事实")
        baseline = (self.repo / ".agent-sync/baseline.json").read_bytes()
        self.assertTrue(all(a == "push" for a in self.actions()))
        self.assertEqual((self.repo / ".agent-sync/baseline.json").read_bytes(), baseline)
        self.assertFalse((self.repo / "data/rules.md").exists())
        self.assertEqual(core.apply(self.repo, "push")["written"], 6)
        self.assertTrue(all(a == "unchanged" for a in self.actions()))
        second = self.root / "content-b"
        second.mkdir()
        shutil.copy2(self.repo / "manifest.json", second / "manifest.json")
        shutil.copytree(self.repo / "data", second / "data")
        core.init_repo(second)
        device_b = self.root / "device-b"
        for name, category, kind, relative, binding, text in specs:
            core.bind(second, name, device_b / binding)
        self.assertTrue(all(a == "pull" for a in self.actions(second)))
        core.apply(second, "pull")
        self.assertEqual((device_b / "AGENTS.md").read_text(encoding="utf-8"), "共同规则")
        self.assertEqual((device_b / "memory/facts.md").read_bytes(), (self.local / "memory/facts.md").read_bytes())
        put(self.local / "AGENTS.md", "新规则")
        core.apply(self.repo, "push")
        shutil.copy2(self.repo / "data/rules.md", second / "data/rules.md")
        core.apply(second, "pull")
        self.assertEqual((device_b / "AGENTS.md").read_text(encoding="utf-8"), "新规则")
        backups = list((second / ".agent-sync/backups").rglob("AGENTS.md"))
        self.assertTrue(any(p.read_text(encoding="utf-8") == "共同规则" for p in backups))

    def test_first_divergence_blocks_entire_batch(self):
        local = self.register()
        put(local, "local")
        put(self.repo / "data/rules.md", "repo")
        extra = self.register("extra", relative="data/extra.md", local=self.local / "extra.md")
        put(extra, "new")
        self.assertIn("conflict", self.actions())
        for direction in ("push", "pull"):
            with self.assertRaises(core.Error):
                core.apply(self.repo, direction)
        self.assertFalse((self.repo / "data/extra.md").exists())
        self.assertEqual(local.read_text(), "local")
        self.assertEqual((self.repo / "data/rules.md").read_text(), "repo")
        self.assertEqual(json.loads((self.repo / ".agent-sync/baseline.json").read_text()), {})

    def test_equal_baseline_directions_double_edit_and_deletion(self):
        local = self.register()
        remote = self.repo / "data/rules.md"
        put(local, "base")
        put(remote, "base")
        self.assertEqual(self.actions(), ["unchanged"])
        core.apply(self.repo, "pull")
        put(local, "local edit")
        self.assertEqual(self.actions(), ["push"])
        self.assertEqual(core.apply(self.repo, "pull")["written"], 0)
        self.assertEqual(remote.read_text(), "base")
        put(remote, "remote edit")
        self.assertEqual(self.actions(), ["conflict"])
        with self.assertRaises(core.Error):
            core.apply(self.repo, "push")
        put(local, "merged")
        put(remote, "merged")
        core.apply(self.repo, "push")
        local.unlink()
        self.assertEqual(self.actions(), ["deletion"])
        for direction in ("push", "pull"):
            self.assertEqual(core.apply(self.repo, direction)["written"], 0)
        self.assertFalse(local.exists())
        self.assertEqual(remote.read_text(), "merged")
        put(remote, "changed after other end deleted")
        self.assertEqual(self.actions(), ["conflict"])

    def test_new_device_and_binding_change_do_not_reuse_baseline(self):
        local = self.register()
        put(local, "base")
        core.apply(self.repo, "push")
        replacement = self.root / "other-device/AGENTS.md"
        put(replacement, "different")
        core.bind(self.repo, "rules", replacement)
        self.assertEqual(self.actions(), ["conflict"])
        core.bind(self.repo, "rules", local)
        put(local, "edited after rebind")
        self.assertEqual(self.actions(), ["conflict"])

    def test_js_skill_and_png_bytes_round_trip(self):
        import struct
        import zlib

        def png(pixel):
            def chunk(kind, data):
                return (struct.pack(">I", len(data)) + kind + data
                        + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
            return (b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
                    + chunk(b"IEND", b""))

        skills = self.register("skills", "tree", "skills", "data/skills", self.local / "skills")
        image = self.register("portrait", "file", "personas", "data/personas/reference.png",
                              self.local / "reference.png")
        js = b'// skill fixture\r\nthrow new Error("must never execute");\r\n'
        first_png = png(b"\xff\x00\x80\xff")
        put(skills / "demo/SKILL.md", "# JavaScript skill\n")
        (skills / "demo/run.js").write_bytes(js)
        image.write_bytes(first_png)

        # Content sync must not launch the skill or any other subprocess.
        with mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected execution")), \
                mock.patch("os.system", side_effect=AssertionError("unexpected execution")):
            self.assertEqual(core.apply(self.repo, "push")["written"], 3)
            self.assertEqual((self.repo / "data/skills/demo/run.js").read_bytes(), js)
            self.assertEqual((self.repo / "data/personas/reference.png").read_bytes(), first_png)
            second = self.root / "asset-content-b"
            second.mkdir()
            shutil.copy2(self.repo / "manifest.json", second / "manifest.json")
            shutil.copytree(self.repo / "data", second / "data")
            core.init_repo(second)
            device_b = self.root / "asset-device-b"
            core.bind(second, "skills", device_b / "skills")
            core.bind(second, "portrait", device_b / "reference.png")
            self.assertEqual(core.apply(second, "pull")["written"], 3)
            self.assertEqual((device_b / "skills/demo/run.js").read_bytes(), js)
            self.assertEqual((device_b / "reference.png").read_bytes(), first_png)

            updated_js = b'// updated on device B\nthrow new Error("still never execute");\n'
            updated_png = png(b"\x00\xff\x1a\xff")
            (device_b / "skills/demo/run.js").write_bytes(updated_js)
            (device_b / "reference.png").write_bytes(updated_png)
            self.assertEqual(core.apply(second, "push")["written"], 2)
            shutil.copy2(second / "data/skills/demo/run.js", self.repo / "data/skills/demo/run.js")
            shutil.copy2(second / "data/personas/reference.png", self.repo / "data/personas/reference.png")
            self.assertEqual(core.apply(self.repo, "pull")["written"], 2)
            self.assertEqual((skills / "demo/run.js").read_bytes(), updated_js)
            self.assertEqual(image.read_bytes(), updated_png)
            self.assertTrue(all(action == "unchanged" for action in self.actions()))

    def test_added_asset_extensions_keep_sensitive_name_rejections(self):
        for extension in ("js", "mjs", "cjs", "ts", "png", "jpg", "jpeg", "webp", "svg"):
            with self.subTest(extension=extension):
                self.assertEqual(core._names("demo/asset." + extension, file=True),
                                 ["demo", "asset." + extension])
                self.assertEqual(core._names("demo/asset." + extension.upper(), file=True),
                                 ["demo", "asset." + extension.upper()])
                for relative in ("demo/secret." + extension, "node_modules/asset." + extension,
                                 "demo/.hidden." + extension):
                    with self.assertRaises(core.Error):
                        core._names(relative, file=True)
        with self.assertRaises(core.Error):
            core._names("demo/asset.exe", file=True)

    def test_manifest_path_change_invalidates_existing_baseline(self):
        local = self.register()
        old_remote = self.repo / "data/rules.md"
        put(local, "base\n")
        core.apply(self.repo, "push")
        baseline_path = self.repo / ".agent-sync/baseline.json"
        established = baseline_path.read_bytes()
        entry = json.loads(established)["rules"]
        self.assertEqual(entry["binding"], str(local))
        self.assertEqual(entry["path"], "data/rules.md")
        self.assertEqual(entry["kind"], "file")
        self.assertEqual(entry["files"], {"": core._digest(b"base\n")})

        put(local, "local edit\n")
        self.assertEqual(self.actions(), ["push"])
        new_remote = self.repo / "data/moved/rules.md"
        put(new_remote, "base\n")
        self.assertEqual(core._digest(new_remote.read_bytes()), entry["files"][""])
        manifest_path = self.repo / "manifest.json"
        document = json.loads(manifest_path.read_bytes())
        document["groups"][0]["path"] = "data/moved/rules.md"
        put(manifest_path, json.dumps(document))

        self.assertEqual(self.actions(), ["conflict"])
        groups, bindings, baseline = core._load(self.repo)
        self.assertEqual(core._collect(self.repo, groups, bindings, baseline)[1]["rules"][2], {})
        self.assertEqual(baseline_path.read_bytes(), established)
        for direction in ("push", "pull"):
            with self.assertRaises(core.Error):
                core.apply(self.repo, direction)
        self.assertEqual(new_remote.read_bytes(), b"base\n")
        self.assertEqual(old_remote.read_bytes(), b"base\n")
        self.assertEqual(local.read_bytes(), b"local edit\n")
        self.assertEqual(baseline_path.read_bytes(), established)

        put(new_remote, "local edit\n")
        self.assertEqual(core.apply(self.repo, "push")["written"], 0)
        refreshed = json.loads(baseline_path.read_bytes())["rules"]
        self.assertEqual(refreshed["path"], "data/moved/rules.md")
        self.assertEqual(refreshed["files"], {"": core._digest(b"local edit\n")})
        put(local, "next edit\n")
        self.assertEqual(self.actions(), ["push"])

    def test_manifest_kind_changes_invalidate_baseline_in_both_directions(self):
        for old_kind, new_kind in (("file", "tree"), ("tree", "file")):
            with self.subTest(old_kind=old_kind, new_kind=new_kind):
                repo = self.root / ("kind-repo-" + old_kind)
                local = self.root / ("kind-device-" + old_kind) / "AGENTS.md"
                relative = "data/rules.md"
                remote = repo / relative
                core.init_repo(repo)
                core.add_group(repo, "rules", "rules", old_kind, relative)
                core.bind(repo, "rules", local)
                old_key = "" if old_kind == "file" else "note.md"
                put(local if old_kind == "file" else local / old_key, "base\n")
                core.apply(repo, "push")
                baseline_path = repo / ".agent-sync/baseline.json"
                established = baseline_path.read_bytes()
                entry = json.loads(established)["rules"]
                self.assertEqual(entry["kind"], old_kind)
                self.assertEqual(entry["files"], {old_key: core._digest(b"base\n")})

                # Keep BOTH identity paths unchanged; only kind changes.
                for root in (local, remote):
                    if old_kind == "file":
                        root.unlink()
                        root.mkdir()
                    else:
                        shutil.rmtree(root)
                new_key = "" if new_kind == "file" else "note.md"
                new_local = local if new_kind == "file" else local / new_key
                new_remote = remote if new_kind == "file" else remote / new_key
                put(new_remote, "base\n")
                put(new_local, "local edit\n")
                manifest_path = repo / "manifest.json"
                document = json.loads(manifest_path.read_bytes())
                document["groups"][0]["kind"] = new_kind
                put(manifest_path, json.dumps(document))

                groups, bindings, baseline = core._load(repo)
                rows, snapshots = core._collect(repo, groups, bindings, baseline)
                self.assertEqual(bindings["rules"], entry["binding"])
                self.assertEqual(groups[0]["path"], entry["path"])
                # Conflict alone is insufficient: old/new key shapes differ.
                # Assert the old baseline was actually discarded, not merely unused by _action.
                self.assertEqual(snapshots["rules"][2], {})
                self.assertEqual(rows, [{"group": "rules", "path": new_key, "action": "conflict"}])
                for direction in ("push", "pull"):
                    with self.assertRaises(core.Error):
                        core.apply(repo, direction)
                self.assertEqual(new_remote.read_bytes(), b"base\n")
                self.assertEqual(new_local.read_bytes(), b"local edit\n")
                self.assertEqual(baseline_path.read_bytes(), established)

                put(new_remote, "local edit\n")
                self.assertEqual(core.apply(repo, "pull")["written"], 0)
                refreshed = json.loads(baseline_path.read_bytes())["rules"]
                self.assertEqual(refreshed["kind"], new_kind)
                self.assertEqual(refreshed["path"], relative)
                self.assertEqual(refreshed["files"], {new_key: core._digest(b"local edit\n")})
                put(new_local, "next edit\n")
                self.assertEqual(self.actions(repo), ["push"])

    def test_legacy_baseline_is_unestablished_until_equal_apply(self):
        local = self.register()
        remote = self.repo / "data/rules.md"
        put(local, "base\n")
        core.apply(self.repo, "push")
        baseline_path = self.repo / ".agent-sync/baseline.json"
        document = json.loads(baseline_path.read_bytes())
        document["rules"] = {key: document["rules"][key] for key in ("binding", "files")}
        put(baseline_path, json.dumps(document))
        legacy = baseline_path.read_bytes()
        put(local, "local edit\n")

        self.assertEqual(self.actions(), ["conflict"])
        groups, bindings, baseline = core._load(self.repo)
        self.assertEqual(core._collect(self.repo, groups, bindings, baseline)[1]["rules"][2], {})
        for direction in ("push", "pull"):
            with self.assertRaises(core.Error):
                core.apply(self.repo, direction)
        self.assertEqual(remote.read_bytes(), b"base\n")
        self.assertEqual(local.read_bytes(), b"local edit\n")
        self.assertEqual(baseline_path.read_bytes(), legacy)

        put(remote, "local edit\n")
        self.assertEqual(core.apply(self.repo, "pull")["written"], 0)
        refreshed = json.loads(baseline_path.read_bytes())["rules"]
        self.assertEqual(set(refreshed), {"binding", "path", "kind", "files"})
        self.assertEqual(refreshed["path"], "data/rules.md")
        self.assertEqual(refreshed["kind"], "file")
        self.assertEqual(refreshed["files"], {"": core._digest(b"local edit\n")})
        put(local, "next edit\n")
        self.assertEqual(self.actions(), ["push"])

    def test_backup_and_rollback_on_second_write_failure(self):
        first = self.register("first", relative="data/first.md", local=self.local / "first.md")
        second = self.register("second", relative="data/second.md", local=self.local / "second.md")
        put(first, "old first")
        put(second, "old second")
        core.apply(self.repo, "push")
        put(self.repo / "data/first.md", "new first")
        put(self.repo / "data/second.md", "new second")
        original = core._atomic
        fired = False

        def failing(path, data):
            nonlocal fired
            if Path(path) == second and not fired:
                fired = True
                raise OSError("injected write failure")
            return original(path, data)

        with mock.patch("agent_sync.core._atomic", side_effect=failing):
            with self.assertRaises(core.Error):
                core.apply(self.repo, "pull")
        self.assertTrue(fired)
        self.assertEqual(first.read_text(), "old first")
        self.assertEqual(second.read_text(), "old second")
        self.assertEqual(self.actions(), ["pull", "pull"])
        backups = list((self.repo / ".agent-sync/backups").rglob("first.md"))
        self.assertTrue(any(p.read_text() == "old first" for p in backups))
        core.apply(self.repo, "pull")
        self.assertEqual(first.read_text(), "new first")

    def test_traversal_absolute_forbidden_and_overlap(self):
        invalid = ["../escape.md", "/data/escape.md", "data/../escape.md", "data//x.md",
                   "data/C:/x.md", "data/.env", "data/auth.json", "data/credentials.json",
                   "data/a.sqlite", "data/a.db", "data/CON.md", "data/x.md.",
                   "data/config.local.json", "data/cache/x.md", "data/.git/x.md"]
        for relative in invalid:
            with self.subTest(relative=relative), self.assertRaises(core.Error):
                core.add_group(self.repo, "bad", "rules", "file", relative)
        self.register("tree", "tree", "skills", "data/skills", self.local / "skills")
        with self.assertRaises(core.Error):
            core.add_group(self.repo, "nested", "skills", "file", "data/skills/a.md")
        core.add_group(self.repo, "separate", "rules", "file", "data/separate.md")
        for path in (self.local / "skills/a.md", self.repo / "data/a.md",
                     self.repo / ".agent-sync/a.md", self.root):
            with self.subTest(path=path), self.assertRaises(core.Error):
                core.bind(self.repo, "separate", path)
        with self.assertRaises(core.Error):
            core.bind(self.repo, "separate", "relative/AGENTS.md")

    def test_manifest_tampering(self):
        local = self.register()
        put(local, "safe")
        path = self.repo / "manifest.json"
        document = json.loads(path.read_text())
        document["groups"][0]["path"] = "data/../../escape.md"
        path.write_text(json.dumps(document))
        with self.assertRaises(core.Error):
            core.apply(self.repo, "push")
        self.assertFalse((self.root / "escape.md").exists())

    def test_case_collisions_and_runtime_tree(self):
        core.add_group(self.repo, "one", "rules", "file", "data/Upper/a.md")
        with self.assertRaises(core.Error):
            core.add_group(self.repo, "two", "rules", "file", "data/upper/b.md")
        target = self.register("skills", "tree", "skills", "data/skills", self.local / "skills")
        put(target / "demo/SKILL.md", "safe")
        put(target / "demo/.env", "EXAMPLE_NOT_A_REAL_SECRET=1")
        with self.assertRaises(core.Error):
            core.plan(self.repo)
        (target / "demo/.env").unlink()
        put(target / "Case.md", "a")
        other = target / "case.md"
        if not other.exists():
            put(other, "b")
            with self.assertRaises(core.Error):
                core.plan(self.repo)
        else:
            # Case-insensitive filesystems cannot create the pair; manifest test above is unconditional.
            self.assertIn("push", self.actions())

    def test_symlink_in_tree_and_ancestor(self):
        outside = self.root / "outside"
        put(outside / "note.md", "do not traverse")
        target = self.register("skills", "tree", "skills", "data/skills", self.local / "skills")
        target.mkdir(parents=True)
        link = target / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统未授予创建 symlink 权限")
        with self.assertRaises(core.Error):
            core.plan(self.repo)
        with self.assertRaises(core.Error):
            core.bind(self.repo, "skills", link / "subdir")
        self.assertEqual((outside / "note.md").read_text(), "do not traverse")

    def test_reparse_detection_and_lock(self):
        target = self.root / "junction-like"
        target.mkdir()
        actual = Path.lstat

        def fake_lstat(path, *args, **kwargs):
            result = actual(path, *args, **kwargs)
            if path == target:
                return mock.Mock(st_mode=result.st_mode, st_nlink=result.st_nlink, st_file_attributes=0x400)
            return result

        with mock.patch.object(Path, "lstat", fake_lstat):
            with self.assertRaises(core.Error):
                core._safe(target / "child")
        with core._lock(self.repo):
            with self.assertRaises(core.Error):
                core.plan(self.repo)
        self.assertFalse((self.repo / ".agent-sync/lock").exists())

    def test_remove_preserves_files_and_unbind_preserves_registration(self):
        local = self.register()
        put(local, 'keep\n')
        core.apply(self.repo, 'push')
        core.unbind(self.repo, 'rules')
        self.assertEqual(self.actions(), ['unbound'])
        core.bind(self.repo, 'rules', local)
        core.apply(self.repo, 'push')
        core.remove(self.repo, 'rules')
        self.assertEqual(core._load(self.repo), ([], {}, {}))
        self.assertEqual(local.read_bytes(), b'keep\n')
        self.assertEqual((self.repo / 'data/rules.md').read_bytes(), b'keep\n')

    def test_ignored_cache_links_are_not_followed(self):
        target = self.register('skills', 'tree', 'skills', 'data/skills', self.local / 'skills')
        put(target / 'SKILL.md', 'safe')
        outside = self.root / 'outside-cache'
        put(outside / '.env', 'never read')
        (target / '__pycache__').symlink_to(outside, target_is_directory=True)
        (target / '.DS_Store').symlink_to(outside / '.env')
        (target / 'ignored.pyc').symlink_to(outside / '.env')
        self.assertEqual(core._scan_tree(target), {'SKILL.md': core._digest(b'safe')})
        self.assertEqual(core.apply(self.repo, 'push')['written'], 1)
        self.assertEqual((outside / '.env').read_bytes(), b'never read')

    def test_init_idempotent_and_unbound(self):
        core.add_group(self.repo, "rules", "rules", "file", "data/rules.md")
        before = (self.repo / "manifest.json").read_bytes()
        core.init_repo(self.repo)
        self.assertEqual(before, (self.repo / "manifest.json").read_bytes())
        self.assertEqual(self.actions(), ["unbound"])
        self.assertFalse((self.repo / ".git").exists())


if __name__ == "__main__":
    unittest.main()
