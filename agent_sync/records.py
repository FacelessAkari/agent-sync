from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from . import core, projects


STATUSES = {"todo", "doing", "blocked", "done"}
LIMIT = 1024 * 1024
TASK_KEYS = {"id", "title", "project", "status", "notes", "updated_at", "device", "revision"}
HANDOFF_KEYS = {"id", "task", "task_revision", "task_title", "task_status", "project",
                "device", "created_at", "project_state", "completed", "next_steps",
                "blockers", "verification"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _time(value):
    if not isinstance(value, str):
        raise core.Error("记录时间无效")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise core.Error("记录时间无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise core.Error("记录时间必须包含 UTC 时区")


def _device_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"d[0-9a-f]{32}", value):
        raise core.Error("来源 device id 无效")


def _device(repo):
    path = repo / ".agent-sync/identity.json"
    if core._bytes(path) is None:
        core._save(path, {"id": "d" + uuid.uuid4().hex})
    document = core._json(path)
    if not isinstance(document, dict) or set(document) != {"id"}:
        raise core.Error("本机 device identity 格式错误")
    _device_value(document["id"])
    return document["id"]


def _text(value, required=False, limit=LIMIT):
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > limit:
        raise core.Error("文本无效或超过长度限制")
    if required and not value.strip():
        raise core.Error("必须明确提供非空文本；未验证时请写明未验证")
    return value


def _input(raw, required=False):
    if raw is None:
        if required:
            raise core.Error("必须明确指定本地输入文件")
        return ""
    path = projects._local_path(os.fspath(raw))
    if path.suffix.lower() not in {".txt", ".md"} or not path.is_file():
        raise core.Error("输入必须是显式本地普通 .txt/.md 文件")
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise core.Error("输入文件超过 1 MiB")
    try:
        return _text(data.decode("utf-8-sig", "strict").replace("\r\n", "\n").replace("\r", "\n"), required)
    except UnicodeError as exc:
        raise core.Error("输入文件必须使用 UTF-8") from exc


def _encode(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _immutable(path, document):
    path = core._safe(path)
    core._mkdir(path.parent)
    # Exclusive creation is intentional: unlike _atomic, never replace evidence.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_encode(document))
        stream.flush()
        os.fsync(stream.fileno())


def _task_shape(repo, task):
    if not isinstance(task, dict) or set(task) != TASK_KEYS:
        raise core.Error("任务字段错误")
    projects._id(task["id"])
    projects._id(task["revision"])
    projects._entry(repo, task["project"])
    _text(task["title"], True, 4096)
    _text(task["notes"])
    if not isinstance(task["status"], str) or task["status"] not in STATUSES:
        raise core.Error("非法任务 status")
    _time(task["updated_at"])
    _device_value(task["device"])


def _event(repo, ident):
    projects._id(ident)
    event = core._json(repo / "data/task-events" / (ident + ".json"))
    if (not isinstance(event, dict)
            or set(event) != {"id", "task", "device", "created_at", "before", "after"}
            or event["id"] != ident):
        raise core.Error("任务事件格式错误")
    _task_shape(repo, event["after"])
    after = event["after"]
    if (after["revision"] != ident or event["task"] != after["id"]
            or event["device"] != after["device"] or event["created_at"] != after["updated_at"]):
        raise core.Error("任务事件与后快照不一致")
    before = event["before"]
    if before is not None:
        _task_shape(repo, before)
        if (before["id"] != after["id"] or before["project"] != after["project"]
                or before["revision"] == ident):
            raise core.Error("任务事件与前快照不一致")
    return event


def _task(repo, ident):
    projects._id(ident)
    task = core._json(repo / "data/tasks" / (ident + ".json"))
    _task_shape(repo, task)
    if task["id"] != ident or _event(repo, task["revision"])["after"] != task:
        raise core.Error("任务快照与引用事件不一致；请手工检查 Git 冲突或中断写入")
    return task


def _json_ids(directory):
    directory = core._safe(directory)
    if not directory.exists():
        return []
    result = []
    for child in sorted(directory.iterdir()):
        if core._ignored_cache(child.name):
            continue
        child = core._safe(child)
        if not child.is_file() or child.suffix != ".json":
            raise core.Error("管理目录内存在非预期文件")
        result.append(projects._id(child.stem))
    return result


def _change(repo, before, after):
    revision = "e" + uuid.uuid4().hex
    after = {**after, "revision": revision, "device": _device(repo), "updated_at": _now()}
    _task_shape(repo, after)
    event = {"id": revision, "task": after["id"], "device": after["device"],
             "created_at": after["updated_at"], "before": before, "after": after}
    _immutable(repo / "data/task-events" / (revision + ".json"), event)
    core._save(repo / "data/tasks" / (after["id"] + ".json"), after)
    return after


def task_add(repo, project, title, note_file=None, ident=None):
    ident = projects._id(ident if ident is not None else "t" + uuid.uuid4().hex)
    _text(title, True, 4096)
    notes = _input(note_file)
    with projects._context(repo) as repo:
        projects._entry(repo, project)
        if core._safe(repo / "data/tasks" / (ident + ".json")).exists():
            raise core.Error("任务已存在；拒绝覆盖")
        return _change(repo, None, {"id": ident, "project": project, "title": title,
                                    "status": "todo", "notes": notes})


def task_set(repo, ident, status=None, title=None, note_file=None):
    if status is not None and (not isinstance(status, str) or status not in STATUSES):
        raise core.Error("非法任务 status")
    if title is not None:
        _text(title, True, 4096)
    if status is None and title is None and note_file is None:
        raise core.Error("task set 至少需要一个显式变更字段")
    notes = _input(note_file) if note_file is not None else None
    with projects._context(repo) as repo:
        before = _task(repo, ident)
        after = dict(before)
        for key, value in (("status", status), ("title", title), ("notes", notes)):
            if value is not None:
                after[key] = value
        return _change(repo, before, after)


def task_show(repo, ident):
    with projects._context(repo) as repo:
        return _task(repo, ident)


def task_list(repo, project=None):
    with projects._context(repo) as repo:
        if project is not None:
            projects._entry(repo, project)
        tasks = [_task(repo, ident) for ident in _json_ids(repo / "data/tasks")]
        return [task for task in tasks if project is None or task["project"] == project]


def _markdown(record):
    lines = ["# AgentSync 交接 " + record["id"], "",
             "以下完成/下一步/阻塞/验证为用户明确输入，不代表 AgentSync 执行验证。", "",
             "- Project: " + record["project"], "- Task: " + record["task"],
             "- Task revision: " + record["task_revision"],
             "- Device: " + record["device"], "- UTC: " + record["created_at"],
             "- Committed HEAD: " + record["project_state"]["head"],
             "- Git worktree clean: " + str(record["project_state"]["clean"]).lower(), ""]
    state = record["project_state"]
    if "branch" in state:
        lines.extend(["- Branch: " + (state["branch"] if state["branch"] is not None else "(detached)"),
                      "- Registered branch: " + state["registered_branch"], ""])
    for heading, key in (("完成情况", "completed"), ("下一步", "next_steps"),
                         ("阻塞", "blockers"), ("验证结果（用户输入）", "verification")):
        lines.extend(["## " + heading, "", record[key], ""])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _handoff(repo, ident):
    projects._id(ident)
    directory = core._safe(repo / "data/handoffs" / ident)
    record = core._json(directory / "record.json")
    if not isinstance(record, dict) or set(record) != HANDOFF_KEYS or record["id"] != ident:
        raise core.Error("交接记录格式错误")
    projects._entry(repo, record["project"])
    projects._id(record["task"])
    projects._id(record["task_revision"])
    _device_value(record["device"])
    _time(record["created_at"])
    _text(record["task_title"], True, 4096)
    if not isinstance(record["task_status"], str) or record["task_status"] not in STATUSES:
        raise core.Error("交接任务状态无效")
    event_task = _event(repo, record["task_revision"])["after"]
    if any(record[key] != event_task[target] for key, target in
           (("task", "id"), ("project", "project"), ("task_title", "title"), ("task_status", "status"))):
        raise core.Error("交接引用的任务历史不一致")
    state = record["project_state"]
    if (not isinstance(state, dict) or set(state) not in (
            {"head", "clean"}, {"head", "clean", "branch", "registered_branch"})
            or not isinstance(state["head"], str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", state["head"])
            or type(state["clean"]) is not bool):
        raise core.Error("交接项目快照无效")
    if "branch" in state:
        if state["branch"] is not None:
            projects._branch(state["branch"])
        projects._branch(state["registered_branch"])
    for key in ("completed", "next_steps", "blockers", "verification"):
        _text(record[key], True)
    if core._bytes(directory / "summary.md") != _markdown(record):
        raise core.Error("交接 JSON/Markdown 不一致；拒绝静默修复")
    if {p.name for p in directory.iterdir() if not core._ignored_cache(p.name)} != {"record.json", "summary.md"}:
        raise core.Error("交接包含非预期文件")
    return record


def handoff_create(repo, task_id, summary_file, next_file, blockers_file, verification_file):
    inputs = {key: _input(raw, True) for key, raw in
              (("completed", summary_file), ("next_steps", next_file),
               ("blockers", blockers_file), ("verification", verification_file))}
    with projects._context(repo) as repo:
        task = _task(repo, task_id)
        path = projects._bound(repo, task["project"])
        snapshot = projects._snapshot(path)
        if snapshot["branch"] is not None:
            projects._branch(snapshot["branch"])
        registered_branch = projects._entry(repo, task["project"])["branch"]
        ident = "h" + uuid.uuid4().hex
        record = {"id": ident, "task": task["id"], "task_revision": task["revision"],
                  "task_title": task["title"], "task_status": task["status"],
                  "project": task["project"], "device": _device(repo), "created_at": _now(),
                  "project_state": {"head": snapshot["head"], "clean": snapshot["clean"],
                                    "branch": snapshot["branch"], "registered_branch": registered_branch}, **inputs}
        parent = repo / "data/handoffs"
        core._mkdir(parent)
        target = core._safe(parent / ident)
        if target.exists():
            raise core.Error("交接 id 已存在；拒绝覆盖")
        staging_parent = repo / ".agent-sync/handoff-staging"
        core._mkdir(staging_parent)
        staging = core._safe(staging_parent / ("package-" + uuid.uuid4().hex))
        staging.mkdir(exist_ok=False)  # Ordinary mkdir: inherit ACL/umask, never chmod.
        core._safe(staging)
        try:
            core._atomic(staging / "record.json", _encode(record))
            core._atomic(staging / "summary.md", _markdown(record))
            if projects._snapshot(path) != snapshot:
                raise core.Error("生成交接期间项目改变；未发布交接，请停止编辑后重试")
            core._safe(target)
            if target.exists():
                raise core.Error("交接 id 已存在；拒绝覆盖")
            os.rename(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return record


def _handoff_ids(repo):
    parent = core._safe(repo / "data/handoffs")
    if not parent.exists():
        return []
    result = []
    for path in sorted(parent.iterdir()):
        if core._ignored_cache(path.name):
            continue
        path = core._safe(path)
        if not path.is_dir():
            raise core.Error("交接根目录只能包含交接包目录")
        result.append(projects._id(path.name))
    return result


def handoff_show(repo, ident):
    with projects._context(repo) as repo:
        return _handoff(repo, ident)


def handoff_list(repo, task_id=None):
    with projects._context(repo) as repo:
        if task_id is not None:
            _task(repo, task_id)
        records = [_handoff(repo, ident) for ident in _handoff_ids(repo)]
        return [record for record in records if task_id is None or record["task"] == task_id]


def validate_shared(repo):
    """Caller owns core._lock. Never acquire it again, including during git-sync."""
    from .history import list_archives

    repo = core._safe(repo)
    core._groups(core._json(repo / "manifest.json"))
    list_archives(repo)  # Validates complete archive structure, source and derived hashes; no lock.
    if not any(core._safe(repo / name).exists() for name in projects.MANAGED):
        return
    projects._ready(repo)
    projects._registry(repo)
    task_ids = _json_ids(repo / "data/tasks")
    for ident in task_ids:
        _task(repo, ident)
    events = {ident: _event(repo, ident) for ident in _json_ids(repo / "data/task-events")}
    for event in events.values():
        before = event["before"]
        if before is not None:
            previous = events.get(before["revision"])
            if previous is None or previous["after"] != before:
                raise core.Error("事件历史前快照不一致或父事件缺失")
        # Multiple historical branches are retained as evidence, not auto-selected.
        visited = set()
        cursor = event
        while cursor["before"] is not None:
            ident = cursor["id"]
            if ident in visited:
                raise core.Error("事件历史出现循环")
            visited.add(ident)
            parent = events.get(cursor["before"]["revision"])
            if parent is None:
                raise core.Error("事件历史缺少父事件")
            cursor = parent
    for ident in _handoff_ids(repo):
        _handoff(repo, ident)
