import codecs
from pathlib import Path
import tempfile
import unittest

from agent_sync import conflict_merge, core, gitops


class CandidateTests(unittest.TestCase):
    def test_concurrent_list_appends_keep_order_and_shared_item_once(self):
        base = b"# Inbox\n- old\n"
        local = base + b"- first local\n- shared\n- last local\n"
        remote = base + b"- first remote\n- shared\n- last remote\n"
        self.assertEqual(conflict_merge.make_candidate(base, local, remote),
                         base + b"- first local\n- first remote\n- shared\n- last local\n- last remote\n")

    def test_distinct_sections_preserve_each_body_including_repeated_lines(self):
        base = b"# Rules\n"
        local = base + b"\n## Local\n- run the check\n"
        remote = base + b"\n## Remote\n- run the check\n"
        self.assertEqual(conflict_merge.make_candidate(base, local, remote),
                         local + b"\n## Remote\n- run the check\n")

    def test_different_lines_can_merge_and_keep_identical_changes_once(self):
        base = b"# Name\nfirst\nsecond\nfooter\n"
        local = b"# Shared\nFIRST\nsecond\nfooter\n"
        remote = b"# Name\nfirst\nSECOND\nfooter\n"
        self.assertEqual(conflict_merge.make_candidate(base, local, remote),
                         b"# Shared\nFIRST\nSECOND\nfooter\n")
        self.assertEqual(conflict_merge.make_candidate(base, local, local), local)

    def test_hard_conflicts_decline_instead_of_choosing_a_side(self):
        base = b"mode: original\n"
        self.assertIsNone(conflict_merge.make_candidate(base, b"mode: A\n", b"mode: B\n"))
        self.assertIsNone(conflict_merge.make_candidate(base, b"", b"mode: B\n"))
        for local, remote in [(b"- retry: 2", b"- retry: 4"),
                              (b"- [ITEM-1] one", b"- [ITEM-1] two"),
                              (b"## Mode\nA", b"## Mode\nB"),
                              (b"an unfinished sentence", b"a different ending")]:
            with self.subTest(local=local):
                self.assertIsNone(conflict_merge.make_candidate(base, base + local, base + remote))

    def test_newlines_bom_and_no_final_newline(self):
        base = b"first\nsecond"
        for local_eol, remote_eol, bom in [(b"\r\n", b"\n", codecs.BOM_UTF8), (b"\n", b"\r\n", b"")]:
            with self.subTest(local_eol=local_eol):
                local = bom + b"FIRST" + local_eol + b"second"
                remote = b"first" + remote_eol + b"SECOND"
                self.assertEqual(conflict_merge.make_candidate(base, local, remote),
                                 bom + b"FIRST" + local_eol + b"SECOND")
        self.assertEqual(conflict_merge.make_candidate(b"- old", b"- old\n- L", b"- old\n- R"),
                         b"- old\n- L\n- R")

    def test_unsafe_or_excessive_input_produces_no_candidate(self):
        for data in (b"\xff", b"embedded\x00", b"a\r\nb\nc\n",
                     b"x" * (conflict_merge.MAX_BYTES + 1), b"a\n" * (conflict_merge.MAX_LINES + 1)):
            with self.subTest(size=len(data)):
                self.assertIsNone(conflict_merge.make_candidate(b"old", data, b"remote"))


class BaselineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name).resolve() / "repo"
        core.init_repo(self.repo)
        self.relative = "data/rules/codex/AGENTS.md"
        self.target = self.repo / self.relative
        self.target.parent.mkdir(parents=True)

    def git(self, *args):
        return gitops._git(self.repo, ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                            "-c", "commit.gpgSign=false", "-c", "core.hooksPath=.no-hooks", *args], "fixture")

    def test_local_history_must_match_both_named_path_and_verified_raw_hash(self):
        self.git("init")
        base = codecs.BOM_UTF8 + b"base\r\n"
        self.target.write_bytes(base)
        other = self.target.with_name("separate.md")
        other.write_bytes(b"only in another file\n")
        self.git("add", ".gitattributes", self.relative, "data/rules/codex/separate.md")
        self.git("commit", "-m", "initial")
        self.target.write_bytes(b"changed\n")
        self.git("commit", "-am", "later")
        self.assertEqual(conflict_merge.find_base(self.repo, self.relative, core._digest(base)), base)
        for content in (other.read_bytes(), b"never stored", b"base\n"):
            with self.subTest(content=content):
                self.assertIsNone(conflict_merge.find_base(self.repo, self.relative, core._digest(content)))
        self.assertIsNone(conflict_merge.find_base(self.repo, "data/../separate.md", core._digest(base)))

    def test_mapped_transaction_backup_can_recover_baseline_absent_from_git(self):
        core.add_group(self.repo, "codex-rules", "rules", "file", self.relative)
        bound = self.repo.parent / "local-rules.md"
        core.bind(self.repo, "codex-rules", bound)
        backup = self.repo / ".agent-sync/backups" / ("txn-" + "b" * 32) / "codex-rules"
        backup.mkdir(parents=True)
        base = b"prior local bytes\r\n"
        (backup / bound.name).write_bytes(base)
        self.assertEqual(conflict_merge.find_base(self.repo, self.relative, core._digest(base)), base)
        self.assertIsNone(conflict_merge.find_base(self.repo, self.relative, core._digest(b"older")))
