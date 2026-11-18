import os
from pathlib import Path
import tempfile
import unittest

from agent_sync import collector, core, gitops, local_bridge, skill_progress


class SkillProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name).resolve()
        self.root = folder / "workspace"
        self.mirror = self.root / "skill-mirror"
        self.repo = folder / "data-repository"
        self.originals = folder / "source"
        core.init_repo(self.repo)
        self.config = {"version": 1, "files": []}
        for family, package in [("codex", "alpha"), ("shared", "alias"), ("codex", "beta")]:
            for filename in ["SKILL.md", "test.py"]:
                rel = "/".join((family, package, filename))
                path = self.originals / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if package == "alias" and filename == "SKILL.md":
                    os.link(self.originals / "codex/alpha/SKILL.md", path)
                else:
                    path.write_bytes((package + " original\n").encode())
                self.config["files"].append({"source": str(path), "mirror": rel,
                                             "reviewed_sha256": core._digest(path.read_bytes())})
        core._save(self.root / "selection.json", self.config)
        for name, prefix in [("codex-group", "codex"), ("shared-alias", "shared/alias")]:
            core.add_group(self.repo, name, "skills", "tree", "data/" + prefix)
            core.bind(self.repo, name, self.mirror / prefix)
        collector.collect(self.config, self.mirror, self.root / "collection-state.json")
        core.apply(self.repo, "push")
        gitops._git(self.repo, ["init"], "fixture")
        gitops._git(self.repo, ["add", "."], "fixture")
        gitops._git(self.repo, ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgSign=false", "-c", "core.hooksPath=.no-hooks", "commit", "-m", "initial"], "fixture")

    def inputs(self):
        groups, bindings, baseline = core._load(self.repo)
        reader = local_bridge.Reader({"source_files": 0, "unique_source_reads": 0})
        cfg, state, hashes, mirrors = local_bridge.selection(reader, self.root, self.repo, groups, bindings)
        mirror, snapshots, projected, _ = local_bridge.inspect_content(
            reader, self.root, self.repo, groups, bindings, baseline, hashes)
        rows = core._collect(self.repo, groups, bindings, baseline)[0]
        return (self.root, self.repo, cfg, state, hashes, mirror, groups, bindings, snapshots, rows, projected, mirrors)

    def test_complete_independent_package_advances_and_retries_until_committed(self):
        alpha = self.originals / "codex/alpha/SKILL.md"
        alpha.write_bytes(b"local for both hardlink projections")
        remote = self.repo / "data/shared/alias/test.py"
        remote.write_bytes(b"remote skill change needs install")
        beta = self.originals / "codex/beta/test.py"
        beta.write_bytes(b"updated only here\n")
        old_state = core._json(self.root / "collection-state.json")
        scope = skill_progress.collect_ready(*self.inputs())
        self.assertEqual(set(scope), {("codex-group", "beta/SKILL.md"), ("codex-group", "beta/test.py")})
        self.assertEqual(scope["codex-group", "beta/test.py"], core._digest(beta.read_bytes()))
        self.assertEqual((self.mirror / "codex/beta/test.py").read_bytes(), beta.read_bytes())
        self.assertEqual((self.mirror / "codex/alpha/SKILL.md").read_bytes(), b"alpha original\n")
        self.assertEqual(remote.read_bytes(), b"remote skill change needs install")
        self.assertEqual((self.originals / "shared/alias/test.py").read_bytes(), b"alias original\n")
        next_state = core._json(self.root / "collection-state.json")
        self.assertEqual(old_state.keys(), next_state.keys())
        self.assertEqual([k for k in old_state if old_state[k] != next_state[k]], ["codex/beta/test.py"])
        # Retry both after collection and after the core baseline has advanced.
        self.assertEqual(skill_progress.collect_ready(*self.inputs()), scope)
        args = self.inputs()
        core.apply(self.repo, "both", scope=set(scope), expected=args[8], identity=(args[6], args[7]))
        self.assertEqual(skill_progress.collect_ready(*self.inputs()), scope)

    def test_unselected_repository_file_blocks_its_package_and_linked_alias(self):
        (self.repo / "data/codex/alpha/from-other.md").write_bytes(b"not in selection")
        (self.originals / "shared/alias/test.py").write_bytes(b"unpublished local asset")
        self.assertEqual(skill_progress.collect_ready(*self.inputs()), {})
        self.assertEqual((self.mirror / "shared/alias/test.py").read_bytes(), b"alias original\n")

    def test_repo_or_source_changes_after_planning_preserve_mirror_and_state(self):
        path = self.originals / "codex/beta/test.py"
        path.write_bytes(b"first")
        args = self.inputs()
        state = (self.root / "collection-state.json").read_bytes()
        extra = self.repo / "data/codex/beta/unplanned.md"
        extra.write_bytes(b"late addition")
        with self.assertRaises(core.Error):
            skill_progress.collect_ready(*args)
        extra.unlink()
        path.write_bytes(b"second")
        with self.assertRaises(ValueError):
            skill_progress.collect_ready(*args)
        self.assertEqual((self.root / "collection-state.json").read_bytes(), state)
        self.assertEqual((self.mirror / "codex/beta/test.py").read_bytes(), b"beta original\n")

    def test_replanned_scope_rejects_late_repository_additions_and_source_edits(self):
        path = self.originals / "codex/beta/test.py"
        path.write_bytes(b"reviewed local change")
        scope = skill_progress.collect_ready(*self.inputs())
        args = self.inputs()
        skill_progress.validate_scope(self.root, args[7], args[8], args[11], scope, args[4])
        extra = self.repo / "data/codex/beta/late.md"
        extra.write_bytes(b"new during collection")
        args = self.inputs()
        with self.assertRaises(core.Error):
            skill_progress.validate_scope(self.root, args[7], args[8], args[11], scope, args[4])
        extra.unlink()
        path.write_bytes(b"new after collection")
        args = self.inputs()
        with self.assertRaises(core.Error):
            skill_progress.validate_scope(self.root, args[7], args[8], args[11], scope, args[4])

    def test_changed_first_review_or_independent_mirror_edit_blocks_whole_package(self):
        new_entry = self.originals / "codex/beta/new.md"
        new_entry.write_bytes(b"different from what was reviewed")
        self.config["files"].append({"source": str(new_entry), "mirror": "codex/beta/new.md",
                                     "reviewed_sha256": core._digest(b"reviewed")})
        core._save(self.root / "selection.json", self.config)
        (self.originals / "codex/alpha/test.py").write_bytes(b"local")
        (self.mirror / "shared/alias/test.py").write_bytes(b"independent mirror edit")
        self.assertEqual(skill_progress.collect_ready(*self.inputs()), {})
        self.assertFalse((self.mirror / "codex/beta/new.md").exists())
