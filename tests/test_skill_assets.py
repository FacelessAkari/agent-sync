from pathlib import Path
import shutil
import tempfile
import unittest

from agent_sync import core


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


class SkillAssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # Resolve only the trusted temporary root (including macOS /var).
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "content"
        self.local = self.root / "device-a" / "skills"
        self.remote = self.repo / "data" / "skills"
        core.init_repo(self.repo)
        core.add_group(self.repo, "skills", "skills", "tree", "data/skills")
        core.bind(self.repo, "skills", self.local)

    def test_skill_tree_push_rebind_pull_preserves_sets_and_bytes(self):
        expected = {
            "demo/SKILL.md": b"# Synthetic skill fixture\r\n",
            "demo/references/auth.md": b"# Authentication documentation\r\n",
            "demo/other/AuTh.MD": b"# Case-insensitive documentation name\n",
            "demo/run.js": b"// inert fixture\r\n",
        }
        assets = {
            "html": b"<!doctype html><p>fixture</p>\r\n",
            "htm": b"<p>legacy page</p>\n",
            "jsx": b"export const View = () => <p>fixture</p>;\r\n",
            "tsx": b"export const View = (): JSX.Element => <p>fixture</p>;\n",
            "xml": b'<?xml version="1.0"?><fixture/>\r\n',
            "gif": bytes.fromhex(
                "47494638396101000100800000000000ffffff"
                "21f90401000000002c00000000010001000002024401003b"
            ),
        }
        for extension, data in assets.items():
            expected["demo/assets/lower." + extension] = data
            expected["demo/assets/upper." + extension.upper()] = data
        for relative, data in expected.items():
            put(self.local / relative, data)

        pushed = core.apply(self.repo, "push")
        self.assertEqual(pushed["written"], len(expected))
        self.assertEqual(set(files(self.remote)), set(expected))
        self.assertEqual(files(self.remote), expected)

        destination = self.root / "device-b" / "skills"
        core.bind(self.repo, "skills", destination)
        rows = core.plan(self.repo)
        self.assertEqual({row["path"] for row in rows}, set(expected))
        self.assertTrue(all(row["action"] == "pull" for row in rows))
        pulled = core.apply(self.repo, "pull")
        self.assertEqual(pulled["written"], len(expected))
        self.assertEqual(set(files(destination)), set(expected))
        self.assertEqual(files(destination), expected)
        self.assertEqual(files(self.remote), expected)
        self.assertEqual(files(self.local), expected)
        self.assertTrue(all(row["action"] == "unchanged"
                            for row in core.plan(self.repo)))

    def test_auth_exception_is_only_for_final_file_component(self):
        for name in ("auth.md", "AUTH.MD", "AuTh.Md"):
            with self.subTest(name=name):
                relative = "references/" + name
                self.assertEqual(core._names(relative, file=True),
                                 ["references", name])
                with self.assertRaises(core.Error):
                    core._names(relative, file=False)
                for leaf in ("note.md", name):
                    with self.assertRaises(core.Error):
                        core._names(relative + "/" + leaf, file=True)
        for relative in (
            "credentials/auth.md", "secret/auth.md", "auth.txt/auth.md",
            "references/auth.md.txt", "references/auth.mdx",
        ):
            with self.subTest(relative=relative), self.assertRaises(core.Error):
                core._names(relative, file=True)

    def test_forbidden_entries_block_real_push_and_pull(self):
        forbidden = [
            ("references/auth.json", False),
            ("references/AUTH.JSON", False),
            ("references/auth.txt", False),
            ("references/auth.html", False),
            ("references/auth.md", True),
            ("references/AUTH.MD", True),
            ("references/auth.txt", True),
            ("references/credentials.json", False),
            ("references/credential.md", False),
            ("references/secret.md", False),
            ("references/config.local.json", False),
            ("credentials", True),
            ("secret", True),
            ("assets/package.skill", False),
            ("assets/package.zip", False),
            ("assets/package.SKILL", False),
            ("assets/package.ZIP", False),
            ("assets/payload.bin", False),
            ("assets/payload.exe", False),
        ]
        baseline = self.repo / ".agent-sync" / "baseline.json"
        before = baseline.read_bytes()
        for direction, source, destination in (
            ("push", self.local, self.remote),
            ("pull", self.remote, self.local),
        ):
            for relative, directory in forbidden:
                with self.subTest(direction=direction, relative=relative,
                                  directory=directory):
                    bad = source / relative
                    if directory:
                        put(bad / "note.md", b"synthetic fixture\n")
                    else:
                        put(bad, b"synthetic fixture\x00\xff\r\n")
                    try:
                        with self.assertRaises(core.Error):
                            core.apply(self.repo, direction)
                        self.assertEqual(files(destination), {})
                        self.assertEqual(baseline.read_bytes(), before)
                    finally:
                        if directory:
                            shutil.rmtree(bad)
                        else:
                            bad.unlink()


if __name__ == "__main__":
    unittest.main()
