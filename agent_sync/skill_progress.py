"""Collect complete reviewed skill packages when unrelated items require attention."""
from collections import defaultdict
from pathlib import Path
import stat

from . import collector, core, gitops


def _package(relative):
    return "/".join(relative.split("/")[:2])


def _identities(config):
    from .local_bridge import Reader

    reader = Reader({})
    identities = {}
    for item in config["files"]:
        info = reader.safe(Path(item["source"]), source=True)
        if info is None or not stat.S_ISREG(info.st_mode) or not info.st_ino:
            raise core.Error("技能原件的文件标识无法确认")
        identities[item["mirror"]] = (info.st_dev, info.st_ino)
    return identities


def _components(identities):
    neighbours, first = defaultdict(set), {}
    for relative, file_id in identities.items():
        package = _package(relative)
        other = first.setdefault(file_id, package)
        neighbours[package].add(other)
        neighbours[other].add(package)
    remaining = set(neighbours)
    while remaining:
        group = {remaining.pop()}
        todo = list(group)
        while todo:
            new = remaining & neighbours[todo.pop()]
            remaining -= new
            group.update(new)
            todo.extend(new)
        yield group


def _dirty_paths(repo):
    changed = gitops._paths(repo, ["diff", "--no-renames", "--name-only", "-z", "HEAD", "--"],
                            "核对待提交技能文件")
    untracked = gitops._paths(repo, ["ls-files", "--others", "--exclude-standard", "-z"], "核对新增技能文件")
    return set(changed + untracked)


def _verify_repo_packages(repo, mirror_root, chosen, groups, bindings, snapshots, mirror_groups):
    # A binding can cover an entire family, one package, or a subdirectory.
    # Rescan exactly the intersection so an extra or concurrently edited file
    # cannot be omitted from an otherwise valid package.
    for group in groups:
        name = group["name"]
        if name not in mirror_groups:
            continue
        bound = Path(bindings[name])
        for package in chosen:
            root = mirror_root / package
            if root.is_relative_to(bound):
                suffix = root.relative_to(bound)
            elif bound.is_relative_to(root):
                suffix = Path()
            else:
                continue
            prefix = "" if suffix == Path() else suffix.as_posix() + "/"
            expected = {path[len(prefix):]: value for path, value in snapshots[name][0].items()
                        if path.startswith(prefix)}
            if core._scan_tree(repo / group["path"] / suffix) != expected:
                raise core.Error("部分收集前，仓库的技能包内容已变化")


def validate_scope(root, bindings, snapshots, mirror_groups, scope, hashes=None):
    """Recheck complete package membership against the post-collection snapshot."""
    if not scope:
        return
    mirror_root = root / "skill-mirror"
    selected = {(Path(bindings[name]) / path).relative_to(mirror_root).as_posix(): value
                for (name, path), value in scope.items()}
    chosen = {_package(rel) for rel in selected}
    if hashes is not None and {rel: value for rel, value in hashes.items() if _package(rel) in chosen} != selected:
        raise core.Error("部分同步前，技能选择或原件内容发生变化")
    found = set()
    for name in mirror_groups:
        remote, local, base = snapshots[name]
        for path in remote.keys() | local.keys() | base.keys():
            rel = (Path(bindings[name]) / path).relative_to(mirror_root).as_posix()
            if _package(rel) not in chosen:
                continue
            key = (name, path)
            if (key not in scope or local.get(path) != scope[key]
                    or core._action(remote.get(path), local.get(path), base.get(path)) not in {"push", "unchanged"}):
                raise core.Error("部分同步前，技能包成员或内容发生变化")
            found.add(key)
    if found != scope.keys():
        raise core.Error("部分同步前，技能包缺少已选成员")


def collect_ready(root, repo, config, state, hashes, mirror, groups, bindings, snapshots,
                  rows, projected, mirror_groups):
    """Return pinned hashes for every selected member of an independently publishable package."""
    if not config["files"]:
        return {}
    mirror_root = root / "skill-mirror"
    identities = _identities(config)
    dirty = _dirty_paths(repo)
    if set(hashes) != identities.keys():
        raise core.Error("技能选择清单与原件摘要不一致")

    group_paths = {g["name"]: g["path"] for g in groups}
    bound_roots = {name: Path(bindings[name]) for name in mirror_groups}
    entries = {}
    blocked, changed = set(), set()
    reviewed = {item["mirror"]: item["reviewed_sha256"] for item in config["files"]}
    for relative, source_hash in hashes.items():
        package = _package(relative)
        if package + "/SKILL.md" not in hashes or (relative not in state and source_hash != reviewed[relative]):
            blocked.add(package)
        destination = mirror_root / relative
        matches = [name for name, bound in bound_roots.items() if destination.is_relative_to(bound)]
        if len(matches) != 1:
            raise core.Error("技能镜像文件须唯一对应一个同步组")
        name = matches[0]
        path = destination.relative_to(bound_roots[name]).as_posix()
        entries[relative] = (name, path)
        if mirror.get(relative) != state.get(relative):
            # A brand-new, reviewed projection may already hold the exact source bytes.
            if relative in state or mirror.get(relative) != source_hash:
                blocked.add(package)
        if state.get(relative) != source_hash or group_paths[name] + "/" + path in dirty:
            changed.add(package)

    for row in [*rows, *projected]:
        name, path = row["group"], row["path"]
        if name not in bound_roots:
            continue
        relative = (bound_roots[name] / path).relative_to(mirror_root).as_posix()
        package = _package(relative)
        if relative not in hashes or row["action"] not in {"push", "unchanged"}:
            blocked.add(package)
        elif row["action"] == "push":
            changed.add(package)
    # A former selection or repo-only file also blocks its whole package.
    blocked.update(_package(rel) for rel in state.keys() | mirror.keys() if rel not in hashes)
    chosen = set()
    for component in _components(identities):
        if component & changed and not component & blocked:
            chosen.update(component)
    if not chosen:
        return {}

    subset = {rel: value for rel, value in hashes.items() if _package(rel) in chosen}
    with core._lock(repo):
        current_groups, current_bindings, _ = core._load(repo)
        if (groups, bindings) != (current_groups, current_bindings) or core._json(root / "selection.json") != config:
            raise core.Error("部分收集前，技能选择或绑定已变化")
        _verify_repo_packages(repo, mirror_root, chosen, groups, bindings, snapshots, mirror_groups)
        collector.collect(config, mirror_root, root / "collection-state.json", subset=subset,
                          expected_state=state, expected_mirror=mirror)
        if _identities(config) != identities:
            raise core.Error("收集期间技能原件的文件标识发生变化，请重试")
    return {entries[relative]: value for relative, value in subset.items()}
