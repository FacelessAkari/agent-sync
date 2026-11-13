import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
import uuid


class Error(Exception):
    pass


def configure_stdio():
    """Configure CLI text outputs in place; leave stdin and redirected StringIO alone."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


CATEGORIES = {"rules", "skills", "personas", "memory"}
MANAGED_SHARED = frozenset('data/' + name for name in (
    'projects', 'tasks', 'task-events', 'handoffs', 'history',
))
EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".ps1", ".sh",
    ".js", ".mjs", ".cjs", ".ts", ".png", ".jpg", ".jpeg", ".webp", ".svg",
}
DENIED = {
    "auth.json", "config.local.json", "credentials", "credential", "tokens.json",
    "token.json", "id_rsa", "id_ed25519", "node_modules", "sessions", "session",
    "cache", "caches", "brain", "memories_v2", "rollout_summaries", "__pycache__",
    "venv", "dist", "build", "target", "logs", "keychain", "cookies.json",
}
RESERVED = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
RESERVED |= {prefix + str(n) for prefix in ("com", "lpt") for n in range(1, 10)}
RESERVED |= {prefix + n for prefix in ("com", "lpt") for n in "¹²³"}
NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _abs(path):
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _safe(path):
    """Reject links/reparse points, hardlinked files and special files in all ancestors."""
    path = _abs(path)
    for item in reversed([path, *path.parents]):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Error("拒绝符号链接/junction/reparse point: " + str(item))
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise Error("拒绝特殊文件: " + str(item))
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise Error("拒绝硬链接文件: " + str(item))
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise Error("路径祖先不是目录: " + str(item))
    return path


def _mkdir(path):
    path = _safe(path)
    path.mkdir(parents=True, exist_ok=True)
    _safe(path)


def _bytes(path):
    path = _safe(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _atomic(path, data):
    path = _safe(path)
    _mkdir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".agent-sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _safe(path)
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _json(path):
    data = _bytes(path)
    if data is None:
        raise Error("缺少文件，请先 init: " + str(path))
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise Error("无效 JSON: " + str(path)) from exc


def _save(path, value):
    _atomic(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _names(relative, file=False):
    if not isinstance(relative, str) or not relative or chr(92) in relative:
        raise Error("路径必须是非空 POSIX 相对路径")
    parts = relative.split("/")
    for part in parts:
        low = part.casefold()
        if (not part or part in {".", ".."} or part.startswith(".")
                or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or unicodedata.normalize("NFC", part) != part
                or low.split(".")[0] in RESERVED
                or low in DENIED
                or low.startswith(("credentials", "credential.", "secret", "auth."))):
            raise Error("不允许的跨平台/敏感/运行时名称: " + relative)
    if file and Path(parts[-1]).suffix.casefold() not in EXTENSIONS:
        raise Error("文件类型不在白名单: " + relative)
    return parts


def _data_path(relative, kind="file"):
    parts = _names(relative, file=kind == "file")
    if len(parts) < 2 or parts[0] != "data":
        raise Error("仓库路径必须严格位于 data/ 下")
    return parts


def _case_check(paths):
    seen = {}
    for path in paths:
        parts = path.split("/")
        for n in range(1, len(parts) + 1):
            spelling = "/".join(parts[:n])
            key = unicodedata.normalize("NFC", spelling).casefold()
            if key in seen and seen[key] != spelling:
                raise Error("跨平台大小写/Unicode 名称碰撞: " + spelling)
            seen[key] = spelling


def _key(path):
    return unicodedata.normalize("NFC", str(path).replace(chr(92), "/")).casefold().rstrip("/")


def _overlap(a, b):
    a, b = _key(a), _key(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _groups(document):
    if (not isinstance(document, dict) or set(document) != {"version", "groups"}
            or document["version"] != 1 or not isinstance(document["groups"], list)):
        raise Error("manifest 格式错误")
    result = document["groups"]
    names = set()
    paths = []
    for group in result:
        if not isinstance(group, dict) or set(group) != {"name", "category", "kind", "path"}:
            raise Error("同步组字段错误")
        if any(not isinstance(group[k], str) for k in group):
            raise Error("同步组字段必须是字符串")
        if not NAME.fullmatch(group["name"]) or group["name"] in names:
            raise Error("同步组名称无效或重复")
        if group["category"] not in CATEGORIES or group["kind"] not in {"file", "tree"}:
            raise Error("同步组类别/类型无效")
        _data_path(group["path"], group["kind"])
        if any(_overlap(group["path"], other) for other in MANAGED_SHARED):
            raise Error("同步组不得与保留管理目录重叠: " + group["path"])
        if any(_overlap(group["path"], other) for other in paths):
            raise Error("仓库同步组路径重叠")
        paths.append(group["path"])
        names.add(group["name"])
    _case_check(paths)
    return result


def _binding_path(raw, group):
    if not isinstance(raw, str):
        raise Error("绑定路径必须为字符串")
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute() or ".." in expanded.parts:
        raise Error("本地绑定必须是绝对路径或 ~/ 路径，且不能含 ..")
    path = _safe(expanded)
    _names(path.name, file=group["kind"] == "file")
    return path


def _validate_bindings(repo, groups, bindings):
    if not isinstance(bindings, dict):
        raise Error("device.json bindings 格式错误")
    by_name = {g["name"]: g for g in groups}
    previous = []
    for name, raw in bindings.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Error("绑定组名称无效")
        if name not in by_name:
            # Orphans are inert metadata: do not inspect an obsolete device path.
            if (not isinstance(raw, str) or not Path(raw).is_absolute()
                    or ".." in Path(raw).parts or str(_abs(raw)) != raw):
                raise Error("孤立绑定路径格式错误")
            continue
        path = _binding_path(raw, by_name[name])
        if str(path) != raw:
            raise Error("device.json 必须存储规范化绝对路径，请使用 bind")
        if _overlap(path, repo):
            raise Error("拒绝绑定仓库、本地状态或其祖先/子路径")
        if any(_overlap(path, old) for old in previous):
            raise Error("本机绑定路径重叠")
        previous.append(path)
    _case_check([str(p).replace(chr(92), "/") for p in previous])


def _load(repo):
    groups = _groups(_json(repo / "manifest.json"))
    device = _json(repo / ".agent-sync/device.json")
    baseline = _json(repo / ".agent-sync/baseline.json")
    if not isinstance(device, dict) or set(device) != {"bindings"}:
        raise Error("device.json 格式错误")
    bindings = device["bindings"]
    _validate_bindings(repo, groups, bindings)
    if not isinstance(baseline, dict):
        raise Error("baseline.json 格式错误")
    by_name = {g["name"]: g for g in groups}
    for name, entry in baseline.items():
        if (not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(entry, dict)
                or set(entry) not in ({"binding", "files"}, {"binding", "path", "kind", "files"})
                or not isinstance(entry["binding"], str) or not isinstance(entry["files"], dict)):
            raise Error("同步基线条目错误")
        if "kind" in entry:
            if not isinstance(entry["kind"], str) or entry["kind"] not in {"file", "tree"}:
                raise Error("同步基线类型错误")
            _data_path(entry["path"], entry["kind"])
            baseline_kind = entry["kind"]
        else:
            # Legacy entries have no repository identity and are never reused.
            # Validate their old key shape independently of the current manifest.
            baseline_kind = "file" if "" in entry["files"] else "tree"
        for relative, digest in entry["files"].items():
            if baseline_kind == "file":
                if relative != "":
                    raise Error("file 基线键错误")
            else:
                _names(relative, file=True)
            if not isinstance(digest, str) or not HASH.fullmatch(digest):
                raise Error("同步基线哈希错误")
        _case_check(list(entry["files"]))
    return groups, bindings, baseline


@contextlib.contextmanager
def _lock(repo):
    repo = _safe(repo)
    state = _safe(repo / ".agent-sync")
    if not state.is_dir():
        raise Error("请先 init 本设备内容仓库")
    path = _safe(state / "lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Error("本地锁已存在；确认没有运行实例后才可手动移除 .agent-sync/lock") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()) + "\n")
        yield
    finally:
        _safe(path)
        path.unlink()


def init_repo(repo):
    repo = _safe(repo)
    _mkdir(repo)
    _mkdir(repo / ".agent-sync")
    with _lock(repo):
        manifest = repo / "manifest.json"
        if _bytes(manifest) is None:
            _save(manifest, {"version": 1, "groups": []})
        else:
            _groups(_json(manifest))
        _mkdir(repo / "data")
        for name, default in (("device.json", {"bindings": {}}), ("baseline.json", {})):
            path = repo / ".agent-sync" / name
            if _bytes(path) is None:
                _save(path, default)
        ignore = repo / ".gitignore"
        old = _bytes(ignore)
        text = "" if old is None else old.decode("utf-8")
        required = ("__pycache__/", ".DS_Store", "*.pyc", "/.agent-sync/")
        missing = [line for line in required if line not in text.splitlines()]
        if missing:
            if old is not None:
                _atomic(repo / ".agent-sync/backups" / ("init-" + uuid.uuid4().hex) / "gitignore.txt", old)
            _atomic(ignore, (text.rstrip("\n") + "\n" + "\n".join(missing) + "\n").lstrip("\n").encode("utf-8"))
        attributes = repo / ".gitattributes"
        if _bytes(attributes) is None:
            _atomic(attributes, b"* -text\n")
        _load(repo)


def add_group(repo, name, category, kind, relative):
    repo = _safe(repo)
    with _lock(repo):
        groups, bindings, baseline = _load(repo)
        if name in (bindings.keys() | baseline.keys()) - {g["name"] for g in groups}:
            raise Error("同名孤立状态尚未清理；先执行 unbind " + name)
        document = {"version": 1, "groups": groups + [
            {"name": name, "category": category, "kind": kind, "path": relative}
        ]}
        _groups(document)
        _scan_tree(repo / "data")
        _save(repo / "manifest.json", document)


def bind(repo, name, path):
    repo = _safe(repo)
    with _lock(repo):
        groups, bindings, baseline = _load(repo)
        group = next((g for g in groups if g["name"] == name), None)
        if group is None:
            raise Error("未知同步组: " + name)
        target = _binding_path(os.fspath(path), group)
        new = dict(bindings)
        new[name] = str(target)
        _validate_bindings(repo, groups, new)
        _snapshot(target, group["kind"])
        if bindings.get(name) != str(target):
            baseline.pop(name, None)
            # Clear first: a crash cannot reuse a baseline belonging to another path.
            _save(repo / ".agent-sync/baseline.json", baseline)
        _save(repo / ".agent-sync/device.json", {"bindings": new})


def unbind(repo, name):
    return _forget(repo, name, remove_manifest=False)


def remove(repo, name):
    return _forget(repo, name, remove_manifest=True)


def _forget(repo, name, remove_manifest):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Error("同步组名称无效")
    repo = _safe(repo)
    with _lock(repo):
        groups, bindings, baseline = _load(repo)
        if name not in bindings and name not in baseline and not any(g["name"] == name for g in groups):
            raise Error("未知同步组: " + name)
        # Clear baseline first. An interrupted cleanup must not reuse old hashes.
        baseline.pop(name, None)
        _save(repo / ".agent-sync/baseline.json", baseline)
        bindings.pop(name, None)
        _save(repo / ".agent-sync/device.json", {"bindings": bindings})
        if remove_manifest:
            _save(repo / "manifest.json", {"version": 1, "groups": [g for g in groups if g["name"] != name]})
        return {"group": name, "status": "removed" if remove_manifest else "unbound",
                "note": "仅清理登记/本机状态；未删除本机或仓库数据文件"}


def _ignored_cache(name):
    # Check only the entry name, before lstat/open: never follow ignored links.
    return name in {"__pycache__", ".DS_Store"} or name.endswith(".pyc")


def _scan_tree(root):
    root = _safe(root)
    if not root.exists():
        return {}
    if not root.is_dir():
        raise Error("tree 根不是目录: " + str(root))
    result = {}
    names = []

    def walk(directory, prefix):
        _safe(directory)
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda e: e.name)
        for entry in children:
            if _ignored_cache(entry.name):
                continue
            relative = prefix + entry.name
            path = _safe(Path(directory) / entry.name)
            info = path.lstat()
            is_directory = stat.S_ISDIR(info.st_mode)
            _names(relative, file=not is_directory)
            names.append(relative)
            if is_directory:
                walk(path, relative + "/")
            else:
                data = _bytes(path)
                if data is None:
                    raise Error("扫描期间文件消失，请停止编辑后重试")
                result[relative] = _digest(data)
    walk(root, "")
    _case_check(names)
    return result


def _snapshot(root, kind):
    if kind == "tree":
        return _scan_tree(root)
    data = _bytes(root)
    return {} if data is None else {"": _digest(data)}


def _action(remote, local, base):
    if remote == local:
        return "unchanged" if remote is not None or base is None else "deleted-both"
    if base is None:
        if remote is None:
            return "push"
        if local is None:
            return "pull"
        return "conflict"
    if remote != base and local != base:
        return "conflict"
    if remote is None or local is None:
        return "deletion"
    return "pull" if remote != base else "push"


def _collect(repo, groups, bindings, baseline):
    # The entire reserved data namespace must be portable and non-runtime content.
    _scan_tree(repo / "data")
    rows, snapshots = [], {}
    for name in sorted((bindings.keys() | baseline.keys()) - {g["name"] for g in groups}):
        rows.append({"group": name, "path": "", "action": "orphan",
                     "cleanup": "unbind " + name,
                     "note": "孤立绑定/基线未使用、未覆盖；清理不删除数据"})
    for group in groups:
        name = group["name"]
        if name not in bindings:
            rows.append({"group": name, "path": "", "action": "unbound"})
            continue
        remote = _snapshot(repo / group["path"], group["kind"])
        local = _snapshot(Path(bindings[name]), group["kind"])
        entry = baseline.get(name, {})
        same_identity = (
            entry.get("binding") == bindings[name]
            and entry.get("path") == group["path"]
            and entry.get("kind") == group["kind"]
        )
        base = entry.get("files", {}) if same_identity else {}
        snapshots[name] = (remote, local, base)
        for relative in sorted(remote.keys() | local.keys() | base.keys()):
            rows.append({
                "group": name, "path": relative,
                "action": _action(remote.get(relative), local.get(relative), base.get(relative)),
            })
    return rows, snapshots


def plan(repo):
    repo = _safe(repo)
    with _lock(repo):
        groups, bindings, baseline = _load(repo)
        return _collect(repo, groups, bindings, baseline)[0]


def _target(root, relative):
    return root if relative == "" else root.joinpath(*relative.split("/"))


def apply(repo, direction):
    if direction not in {"push", "pull"}:
        raise Error("direction 必须为 push 或 pull")
    repo = _safe(repo)
    with _lock(repo):
        groups, bindings, baseline = _load(repo)
        rows, snapshots = _collect(repo, groups, bindings, baseline)
        if any(row["action"] == "conflict" for row in rows):
            raise Error("发现冲突：整批未写入；运行 plan 并手工合并两端，禁止用首次绑定覆盖历史内容")
        by_name = {g["name"]: g for g in groups}
        operations = []
        for row in rows:
            if row["action"] != direction:
                continue
            name, relative = row["group"], row["path"]
            remote_root = repo / by_name[name]["path"]
            local_root = Path(bindings[name])
            source_root, destination_root = (local_root, remote_root) if direction == "push" else (remote_root, local_root)
            source = _target(source_root, relative)
            destination = _target(destination_root, relative)
            data, old = _bytes(source), _bytes(destination)
            remote, local, base = snapshots[name]
            expected = local.get(relative) if direction == "push" else remote.get(relative)
            expected_old = remote.get(relative) if direction == "push" else local.get(relative)
            if data is None or _digest(data) != expected or _digest(old) != expected_old:
                raise Error("预检后内容变化，请停止编辑后重试")
            operations.append((name, relative, destination, data, old))
        transaction = repo / ".agent-sync/backups" / ("txn-" + uuid.uuid4().hex)
        if operations:
            journal = []
            for name, relative, destination, data, old in operations:
                backup_relative = name + "/" + (relative or destination.name)
                if old is not None:
                    _atomic(transaction / backup_relative, old)
                journal.append({"target": str(destination), "backup": backup_relative if old is not None else None,
                                "before": _digest(old), "after": _digest(data)})
            _save(transaction / "index.json", journal)
        if _collect(repo, groups, bindings, baseline)[1] != snapshots:
            raise Error("备份期间内容变化：未写入同步目标，请重新 plan")
        completed = []
        try:
            for name, relative, destination, data, old in operations:
                if _bytes(destination) != old:
                    raise Error("写入前目标变化")
                _atomic(destination, data)
                completed.append((destination, data, old))
            new_baseline = dict(baseline)
            for name, (remote, local, base) in snapshots.items():
                remote, local = dict(remote), dict(local)
                for op_name, relative, destination, data, old in operations:
                    if op_name == name:
                        (remote if direction == "push" else local)[relative] = _digest(data)
                common = dict(base)
                for relative in remote.keys() | local.keys() | base.keys():
                    if remote.get(relative) == local.get(relative):
                        if remote.get(relative) is None:
                            common.pop(relative, None)
                        else:
                            common[relative] = remote[relative]
                new_baseline[name] = {
                    "binding": bindings[name],
                    "path": by_name[name]["path"],
                    "kind": by_name[name]["kind"],
                    "files": common,
                }
            _save(repo / ".agent-sync/baseline.json", new_baseline)
        except (OSError, Error, ValueError) as exc:
            failed = []
            for destination, data, old in reversed(completed):
                try:
                    if _bytes(destination) != data:
                        raise Error("目标被外部程序修改，拒绝回滚覆盖")
                    if old is None:
                        _safe(destination).unlink()
                    else:
                        _atomic(destination, old)
                except (OSError, Error, ValueError):
                    failed.append(str(destination))
            detail = "；部分回滚失败，请从备份人工恢复: " + ", ".join(failed) if failed else "；已回滚本次完成的文件写入"
            raise Error("应用失败" + detail + "；备份位置: " + str(transaction)) from exc
        return {
            "status": "applied", "direction": direction, "written": len(operations),
            "backup": str(transaction) if operations else None,
            "plan": rows,
            "note": "unchanged 已建立/保持基线；反方向、未绑定和 deletion 项未写入；不传播删除",
        }
