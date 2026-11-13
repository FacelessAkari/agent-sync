import contextlib
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from . import core


MANAGED = ("data/projects", "data/tasks", "data/task-events", "data/handoffs")
MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")


def _id(value):
    if not isinstance(value, str) or not core.NAME.fullmatch(value):
        raise core.Error("无效逻辑 id；只允许以小写字母开头的安全名称，最长 64 字符")
    core._names(value)
    return value


def _branch(value):
    if (not isinstance(value, str) or len(value) > 200
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
            or ".." in value or "//" in value):
        raise core.Error("无效项目分支")
    for part in value.split("/"):
        if not part or part.startswith((".", "-")) or part.endswith((".", ".lock")):
            raise core.Error("无效项目分支")
    return value


def _local_path(raw):
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw):
        raise core.Error("本地路径无效")
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute() or ".." in path.parts:
        raise core.Error("要求显式本地绝对路径或 ~/ 路径，不能含 ..")
    return core._safe(path)


def _remote(value):
    # Never include rejected input in diagnostics: it may contain credentials.
    if (not isinstance(value, str) or not value or value.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise core.Error("远程地址无效；不回显可能含凭证的输入")
    if value.startswith("https://"):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise core.Error("HTTPS 远程地址无效") from exc
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or "@" in parsed.netloc or parsed.query or parsed.fragment
                or "?" in value or "#" in value or "\\" in value
                or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
                or not parsed.path or parsed.path == "/"
                or (port is not None and not 1 <= port <= 65535)):
            raise core.Error("HTTPS remote 必须无内嵌凭证、query 或 fragment")
        return value
    if re.fullmatch(r"git@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        parts = value.split(":", 1)[1].split("/")
        if len(parts) < 2 or any(p in {"", ".", ".."} or p.startswith("-") for p in parts):
            raise core.Error("SSH remote 路径无效")
        return value
    # Absolute filesystem paths are intentionally only an offline test transport.
    candidate = Path(value)
    if candidate.is_absolute() and ".." not in candidate.parts and "://" not in value:
        if any(c in value for c in "?\n\r#"):
            raise core.Error("本地测试 remote 无效")
        return str(core._safe(candidate))
    raise core.Error("只支持无凭证 HTTPS、git@host:owner/repo.git 或绝对本地测试路径")


def _ready(repo):
    groups, _, _ = core._load(repo)
    if any(core._overlap(g["path"], reserved) for g in groups for reserved in MANAGED):
        raise core.Error("二期管理目录不能绑定为一期同步组；请先手工迁移重叠登记")


@contextlib.contextmanager
def _context(repo):
    repo = core._safe(repo)
    with core._lock(repo):
        _ready(repo)
        yield repo


def _registry(repo):
    path = repo / "data/projects/registry.json"
    document = {"version": 1, "projects": {}} if core._bytes(path) is None else core._json(path)
    if (not isinstance(document, dict) or set(document) != {"version", "projects"}
            or document["version"] != 1 or not isinstance(document["projects"], dict)):
        raise core.Error("项目 registry 格式错误")
    for ident, entry in document["projects"].items():
        _id(ident)
        if not isinstance(entry, dict) or set(entry) != {"remote", "branch"}:
            raise core.Error("项目登记字段错误")
        if _remote(entry["remote"]) != entry["remote"]:
            raise core.Error("项目 remote 必须规范化")
        _branch(entry["branch"])
    return document


def _entry(repo, ident):
    _id(ident)
    entries = _registry(repo)["projects"]
    if ident not in entries:
        raise core.Error("未知 project id")
    return entries[ident]


def _bindings(repo):
    path = repo / ".agent-sync/projects.json"
    document = {} if core._bytes(path) is None else core._json(path)
    if not isinstance(document, dict):
        raise core.Error("本机项目绑定格式错误")
    seen = []
    for ident, raw in document.items():
        _id(ident)
        target = _local_path(raw)
        if str(target) != raw or core._overlap(target, repo):
            raise core.Error("项目绑定不规范或与内容仓库重叠")
        if any(core._overlap(target, old) for old in seen):
            raise core.Error("本机项目绑定重叠")
        seen.append(target)
    return document


def _git(path, args, allowed=(0,)):
    # Keep credentials in local Git's trust boundary, but do not inherit alternate
    # repository/index/worktree selection or config-injection environment variables.
    blocked = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
               "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
               "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}
    env = {k: v for k, v in os.environ.items()
           if k not in blocked and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no", GIT_OPTIONAL_LOCKS="0",
               GIT_ALLOW_PROTOCOL="https:ssh:file")
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.Error("项目 Git 无法完成；请手工检查 Git、认证及网络；输出已隐藏") from exc
    if result.returncode not in allowed:
        raise core.Error("项目 Git 步骤失败；保留现状，请手工检查；输出已隐藏")
    return result


def _text(path, args):
    try:
        return _git(path, args).stdout.decode("utf-8", "strict").strip()
    except UnicodeError as exc:
        raise core.Error("项目 Git 输出不是 UTF-8") from exc


def _root(repo, raw):
    path = _local_path(raw)
    if core._overlap(path, repo):
        raise core.Error("项目必须与内容仓库分离")
    if not path.is_dir() or not core._safe(path / ".git").is_dir():
        raise core.Error("项目必须是已存在的普通 Git 根目录，不支持 linked worktree/submodule")
    if (_text(path, ["rev-parse", "--is-bare-repository"]) != "false"
            or core._abs(_text(path, ["rev-parse", "--show-toplevel"])) != path
            or core._abs(_text(path, ["rev-parse", "--absolute-git-dir"])) != path / ".git"):
        raise core.Error("绑定必须恰好是普通项目 Git 工作树根目录")
    _git(path, ["rev-parse", "--verify", "HEAD^{commit}"])
    return path


def _bound(repo, ident):
    _entry(repo, ident)
    bindings = _bindings(repo)
    if ident not in bindings:
        raise core.Error("此设备尚未绑定 project")
    return _root(repo, bindings[ident])


def _snapshot(path):
    head = _text(path, ["rev-parse", "--verify", "HEAD^{commit}"])
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise core.Error("项目 HEAD 无效")
    branch_result = _git(path, ["symbolic-ref", "--quiet", "--short", "HEAD"], (0, 1))
    branch = branch_result.stdout.decode("utf-8", "strict").strip() if branch_result.returncode == 0 else None
    dirty = _git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]).stdout
    operating = any(core._safe(path / ".git" / marker).exists() for marker in MARKERS)
    return {"head": head, "clean": not bool(dirty), "branch": branch,
            "operation_in_progress": operating}


def _config(path, key, required=True):
    result = _git(path, ["config", "--get-all", key], (0, 1))
    values = result.stdout.decode("utf-8", "strict").splitlines()
    if not values and not required:
        return None
    if len(values) != 1:
        raise core.Error("项目 Git 配置缺失或有多个值")
    return values[0]


def _match(path, entry, snapshot):
    branch = snapshot["branch"]
    if branch != entry["branch"]:
        raise core.Error("项目 detached 或本地分支与登记不一致")
    _branch(branch)
    remote = _config(path, "branch." + branch + ".remote")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", remote):
        raise core.Error("只支持安全命名的标准 upstream remote")
    merge_ref = "refs/heads/" + branch
    if _config(path, "branch." + branch + ".merge") != merge_ref:
        raise core.Error("upstream 分支与登记不一致")
    upstream = "refs/remotes/" + remote + "/" + branch
    if _text(path, ["rev-parse", "--symbolic-full-name", "@{upstream}"]) != upstream:
        raise core.Error("upstream 映射不是登记的标准远端分支")
    raw = _config(path, "remote." + remote + ".url")
    push = _config(path, "remote." + remote + ".pushurl", required=False)
    for value in (raw, push if push is not None else raw):
        if _remote(value) != entry["remote"]:
            raise core.Error("项目 remote 与共享登记不一致")
    for mode in ([], ["--push"]):
        expanded = _text(path, ["remote", "get-url", *mode, "--all", remote]).splitlines()
        if len(expanded) != 1 or _remote(expanded[0]) != entry["remote"]:
            raise core.Error("Git URL 重写或多个传输地址与登记不一致")
    mirror = _config(path, "remote." + remote + ".mirror", required=False)
    if mirror is not None and mirror.lower() not in {"false", "no", "off", "0"}:
        raise core.Error("拒绝 mirror remote")
    return remote, upstream, merge_ref


def add(repo, ident, remote, branch):
    _id(ident)
    remote, branch = _remote(remote), _branch(branch)
    with _context(repo) as repo:
        document = _registry(repo)
        if ident in document["projects"]:
            raise core.Error("project id 已登记；拒绝覆盖")
        document["projects"][ident] = {"remote": remote, "branch": branch}
        core._save(repo / "data/projects/registry.json", document)
        return {"id": ident, "remote": remote, "branch": branch}


def bind(repo, ident, raw):
    with _context(repo) as repo:
        entry = _entry(repo, ident)
        path = _root(repo, os.fspath(raw))
        _match(path, entry, _snapshot(path))
        bindings = _bindings(repo)
        if any(name != ident and core._overlap(path, old) for name, old in bindings.items()):
            raise core.Error("本机项目绑定重叠")
        bindings[ident] = str(path)
        core._save(repo / ".agent-sync/projects.json", bindings)
        return {"id": ident, "status": "bound", "path": str(path)}


def list_projects(repo):
    with _context(repo) as repo:
        bindings = _bindings(repo)
        return [{"id": ident, **entry, "path": bindings.get(ident)}
                for ident, entry in sorted(_registry(repo)["projects"].items())]


def status(repo, ident):
    with _context(repo) as repo:
        path = _bound(repo, ident)
        snapshot = _snapshot(path)
        entry = _entry(repo, ident)
        reasons = []
        matched = False
        try:
            _match(path, entry, snapshot)
            matched = True
        except core.Error as exc:
            reasons.append(str(exc))
        if not snapshot["clean"]:
            reasons.append("项目有未提交改动或未跟踪文件")
        if snapshot["operation_in_progress"]:
            reasons.append("存在进行中的 Git 操作")
        return {"id": ident, **snapshot, "registered_branch": entry["branch"],
                "remote_matches": True if matched else None,
                "can_sync": not reasons, "reasons": reasons,
                "note": "只读本机预检；未 fetch，不能预判远端分叉、认证或网络结果"}


def sync(repo, ident):
    with _context(repo) as repo:
        path = _bound(repo, ident)
        entry = _entry(repo, ident)
        before = _snapshot(path)
        remote, upstream, merge_ref = _match(path, entry, before)
        if not before["clean"] or before["operation_in_progress"]:
            raise core.Error("项目有未提交改动或进行中的 Git 操作；未 fetch、未修改项目")
        hooks = repo / ".agent-sync/project-disabled-hooks"
        core._mkdir(hooks)
        if any(hooks.iterdir()):
            raise core.Error("禁用 hooks 的本地目录必须为空")
        options = ["-c", "core.hooksPath=" + str(hooks),
                   "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                   "-c", "protocol.ssh.allow=always", "-c", "protocol.file.allow=always"]
        _git(path, options + ["fetch", "--no-tags", "--no-recurse-submodules", "--",
                              remote, merge_ref + ":" + upstream])
        if _snapshot(path) != before:
            raise core.Error("fetch 期间项目被修改；未 merge/push")
        _match(path, entry, before)
        counts = _text(path, ["rev-list", "--left-right", "--count", "HEAD..." + upstream]).split()
        if len(counts) != 2:
            raise core.Error("无法读取项目 ahead/behind 状态")
        ahead, behind = map(int, counts)
        if ahead and behind:
            raise core.Error("项目双边分叉；保留两侧提交，未 merge/push，请手工处理")
        _git(path, options + ["-c", "merge.autoStash=false", "merge", "--ff-only",
                              "--no-edit", "--no-autostash", upstream])
        after = _snapshot(path)
        if not after["clean"] or after["operation_in_progress"]:
            raise core.Error("merge 后项目状态改变；未 push，请手工检查")
        _match(path, entry, after)
        _git(path, options + ["push", "--no-follow-tags", "--recurse-submodules=no", "--",
                              remote, "HEAD:" + merge_ref])
        return {"id": ident, "status": "pushed", "before": before["head"],
                "head": after["head"], "fetched": True, "created_commit": False}
