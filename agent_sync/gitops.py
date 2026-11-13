import json
import os
from pathlib import Path
import subprocess

from . import core


def _git(repo, args, label, allowed=(0,)):
    # Preserve local credential helpers, not repository/index/config injection.
    blocked = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
               "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
               "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}
    env = {k: v for k, v in os.environ.items()
           if k not in blocked and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no",
               GIT_ALLOW_PROTOCOL="https:ssh:file")
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args], shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.Error("Git 步骤无法完成: " + label + "；请手动检查 Git/凭证/网络，不回显命令错误正文") from exc
    if completed.returncode not in allowed:
        raise core.Error("Git 步骤失败: " + label + " (exit " + str(completed.returncode)
                         + ")；未报告成功。请在内容仓库手工检查；已隐藏可能含凭证的输出")
    return completed


def _text(repo, args, label):
    return _git(repo, args, label).stdout.decode("utf-8", "strict").strip()


def _paths(repo, args, label):
    raw = _git(repo, args, label).stdout
    try:
        return [p.decode("utf-8", "strict") for p in raw.split(bytes([0])) if p]
    except UnicodeError as exc:
        raise core.Error("Git 路径必须使用 UTF-8") from exc


def _allowed(path):
    return path == "manifest.json" or path.startswith("data/")


def _validate_names(paths):
    for path in paths:
        if any(core._ignored_cache(part) for part in path.split("/")):
            raise core.Error("拒绝已跟踪/待提交缓存；请手工从 Git 索引移除，保留本机文件")
        if path.startswith("data/"):
            core._data_path(path)
        elif path.startswith(".agent-sync/") or path == ".agent-sync":
            raise core.Error("拒绝 Git 中的本机状态文件")
    core._case_check(paths)


def _tree(repo, revision):
    records = _git(repo, ["ls-tree", "-r", "-z", revision], "检查提交树").stdout
    paths = []
    try:
        for record in records.split(bytes([0])):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, kind, object_id = metadata.split()
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise core.Error("Git 内容仓库不支持符号链接、子模块或特殊条目")
            paths.append(name.decode("utf-8", "strict"))
    except (UnicodeError, ValueError) as exc:
        raise core.Error("无法安全解析 Git 提交树") from exc
    _validate_names(paths)
    if "manifest.json" not in paths:
        raise core.Error("提交树缺少 manifest.json")
    if ".gitignore" not in paths or ".gitattributes" not in paths:
        raise core.Error("HEAD 和 upstream 必须已跟踪 bootstrap .gitignore 和 .gitattributes；请手工提交并同步")
    if _git(repo, ["show", revision + ":.gitattributes"], "检查保字节 attributes").stdout != b"* -text\n":
        raise core.Error("内容仓库 bootstrap .gitattributes 必须恰为 * -text 加 LF；请手工审查提交")
    try:
        rules = _git(repo, ["show", revision + ":.gitignore"], "检查已提交的 bootstrap ignore").stdout.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise core.Error("已提交的 .gitignore 必须为 UTF-8") from exc
    roots = [i for i, line in enumerate(rules) if line == "/.agent-sync/"]
    # Conservative: later negations could reopen the protected root. Never rely on
    # global excludes or .git/info/exclude to repair a committed bootstrap file.
    if not roots or any(line.startswith("!") for line in rules[roots[-1] + 1:]):
        raise core.Error("已提交的 .gitignore 必须明确包含 /.agent-sync/，且其后不得有否定规则；请手工修正 bootstrap")
    if not {"__pycache__/", ".DS_Store", "*.pyc"}.issubset(rules) or any(line.startswith("!") for line in rules):
        raise core.Error("bootstrap .gitignore 必须包含缓存模式 __pycache__/、.DS_Store、*.pyc，且不得有否定规则")
    _byte_rules(repo, [p for p in paths if _allowed(p)])
    try:
        document = json.loads(_git(repo, ["show", revision + ":manifest.json"], "检查远端 manifest").stdout)
        core._groups(document)
    except (ValueError, UnicodeError) as exc:
        raise core.Error("提交树中的 manifest 无效") from exc


def _byte_rules(repo, paths):
    """Check effective checkout attributes, including local info/attributes overrides."""
    names = sorted(set(paths) | {"manifest.json", "data/agent-sync-byte-probe.md"})
    attributes = ("text", "filter", "ident", "working-tree-encoding", "eol")
    for offset in range(0, len(names), 64):
        raw = _git(repo, ["check-attr", "-z", *attributes, "--", *names[offset:offset + 64]],
                   "检查生效的保字节规则").stdout
        fields = raw.split(b"\0")
        if not fields or fields.pop() != b"" or len(fields) % 3:
            raise core.Error("无法解析 Git attributes")
        if len(fields) != len(names[offset:offset + 64]) * len(attributes) * 3:
            raise core.Error("Git attributes 返回不完整")
        for index in range(0, len(fields), 3):
            attribute, value = fields[index + 1:index + 3]
            allowed = {b"unset"} if attribute == b"text" else {b"unset", b"unspecified"}
            if value not in allowed:
                raise core.Error("保字节规则未生效或存在 filter/encoding/eol/ident 覆盖；请手工审查 Git attributes")


def _git_path(repo, name):
    raw = _text(repo, ["rev-parse", "--git-path", name], "检查 Git 操作状态")
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def git_sync(repo):
    from .records import validate_shared

    repo = core._safe(repo)
    with core._lock(repo):
        core._load(repo)
        core._scan_tree(repo / "data")
        validate_shared(repo)
        root = core._abs(_text(repo, ["rev-parse", "--show-toplevel"], "确认内容仓库根目录"))
        if root != repo:
            raise core.Error("--repo 必须恰好是 Git 工作树根目录，不能借用上级代码仓库")
        _git(repo, ["rev-parse", "--verify", "HEAD"], "要求用户先完成 bootstrap 提交")
        branch = _text(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], "拒绝 detached HEAD")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            if _git_path(repo, marker).exists():
                raise core.Error("存在未完成的 Git 操作；请先手工完成或取消")
        if _git(repo, ["ls-files", "-u", "-z"], "检查未合并索引").stdout:
            raise core.Error("存在未合并文件")
        if _git(repo, ["diff", "--cached", "--quiet", "--exit-code"], "检查暂存区", (0, 1)).returncode:
            raise core.Error("存在预先 staged changes；拒绝代为提交")
        tracked = _paths(repo, ["ls-files", "-z"], "检查已跟踪路径")
        _validate_names(tracked)
        dirty = _paths(repo, ["diff", "--name-only", "-z", "HEAD", "--"], "检查工作树")
        if any(not _allowed(path) for path in dirty):
            raise core.Error("存在非白名单已跟踪改动；仅允许 manifest.json 和 data/")
        _validate_names(dirty)
        ignored = _git(repo, ["check-ignore", "--no-index", "-q", ".agent-sync/device.json"], "确认本地状态被忽略", (0, 1))
        if ignored.returncode:
            raise core.Error("必须先在 .gitignore 中忽略 /.agent-sync/，并手动提交 bootstrap .gitignore")
        upstream = _text(repo, ["rev-parse", "--symbolic-full-name", "@{upstream}"], "要求已配置 upstream")
        remote = _text(repo, ["config", "--get", "branch." + branch + ".remote"], "读取 upstream remote")
        merge_ref = _text(repo, ["config", "--get", "branch." + branch + ".merge"], "读取 upstream branch")
        if (not upstream.startswith("refs/remotes/") or not remote or remote.startswith("-")
                or remote == "." or not merge_ref.startswith("refs/heads/") or "\n" in merge_ref):
            raise core.Error("首版仅支持标准命名 remote 和远端分支 upstream")
        # Validate existing history too; tracked state must never become a transfer candidate.
        _tree(repo, "HEAD")
        _git(repo, ["fetch", "--", remote], "fetch upstream")
        _tree(repo, upstream)
        different = _paths(repo, ["diff", "--name-only", "-z", "HEAD", upstream, "--"], "检查远端修改范围")
        if any(not _allowed(path) for path in different):
            raise core.Error("HEAD 与 upstream 的非白名单文件不同；请手工审查/同步 bootstrap 文件后重试")
        # Hooks and signing can run user-defined programs. Disable them for our own commits/merges.
        hooks = repo / ".agent-sync/disabled-hooks"
        core._mkdir(hooks)
        if any(hooks.iterdir()):
            raise core.Error("本地 disabled-hooks 目录必须为空")
        options = ["-c", "core.hooksPath=" + str(hooks), "-c", "commit.gpgsign=false"]
        candidates = sorted({"manifest.json"}
                            | {"data/" + p for p in core._scan_tree(repo / "data")}
                            | {p for p in tracked if p.startswith("data/")})
        _validate_names(candidates)
        _byte_rules(repo, candidates)
        # Never stage a whole directory: ignored scan entries cannot become Git input,
        # even if local exclude configuration is changed between checks.
        for offset in range(0, len(candidates), 64):
            _git(repo, ["--literal-pathspecs", "add", "--", *candidates[offset:offset + 64]], "暂存允许的内容文件")
        staged = _paths(repo, ["diff", "--cached", "--name-only", "-z"], "复核暂存范围")
        _validate_names(staged)
        if any(not _allowed(path) for path in staged):
            raise core.Error("暂存区出现非白名单路径，停止；请手动检查暂存区")
        if staged:
            _git(repo, options + ["commit", "-m", "AgentSync: sync registered knowledge"], "提交允许的本地变更")
        merged = _git(repo, options + ["-c", "merge.autoStash=false", "merge", "--no-edit", "--no-autostash", upstream],
                      "merge upstream", (0, 1, 128))
        if merged.returncode:
            if _git_path(repo, "MERGE_HEAD").exists():
                aborted = _git(repo, ["merge", "--abort"], "abort merge", (0, 1, 128))
                if aborted.returncode:
                    raise core.Error("merge 失败且 abort 失败；停止 push。保留提交，请立即手工检查 Git 状态")
            raise core.Error("merge 未成功，已在需要时 abort；未 push。本地提交和 fetch 得到的远端提交均保留，请手工合并后重试")
        # A syntactically valid merge can still combine incompatible shared records.
        core._load(repo)
        core._scan_tree(repo / "data")
        validate_shared(repo)
        _tree(repo, "HEAD")
        _git(repo, ["push", "--", remote, "HEAD:" + merge_ref], "非强制 push upstream")
        return {"status": "pushed", "note": "只暂存 manifest.json 和 data/；未执行本机内容 apply"}
