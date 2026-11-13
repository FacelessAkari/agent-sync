"""User-session scheduling. No native command is run merely by importing this module.

The command transport and spec builders are injectable for offline tests. Production
transports refuse to operate on a different OS. No shell, credentials, or source-tree
state is used. Interrupted registration transactions deliberately require review.
"""

import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

from . import core

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
MAX_FILE = 1024 * 1024
LOG_LIMIT = 64 * 1024
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
MANAGED_FILES = (
    "state.json", "pending.json", "task.xml", "agent.plist", "candidate.xml",
    "candidate.plist", "management.lock", "run.lock", "latest-run.json",
    "runs.log", "runs.log.1", "runs.log.2", "telemetry.json",
)


class Review(core.Error):
    """Only fixed, non-sensitive messages from this module may use this class."""


class Busy(Review):
    pass


def assert_private_repo(repo):
    from .private_remote import assert_private_repo as validate
    return validate(repo)


def bridge_run(workspace, repo, check=False):
    from .local_bridge import run
    return run(workspace, repo, check=check)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, limit=MAX_FILE):
    path = core._safe(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > limit:
        raise Review("生成文件类型或大小异常，请人工检查；未覆盖。")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Review("生成文件超过大小限制，请人工检查；未覆盖。")
    return data


def _directory(raw):
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path)):
        raise Review("路径必须是无 ..、无控制字符的绝对路径。")
    path = core._safe(path)
    if not path.is_dir():
        raise Review("指定路径必须是已存在的真实目录。")
    return path


def _ordinary(raw):
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise Review("运行程序必须使用绝对路径。")
    path = core._safe(path)
    if not path.is_file():
        raise Review("运行程序不存在或不是普通文件；Windows 需要 pythonw.exe。")
    return path


def _git_path(raw):
    # Metadata validation is lexical: status/removal must survive runtime upgrades.
    if (not isinstance(raw, str) or not raw or os.pathsep in raw
            or any(ord(c) < 32 for c in raw)):
        raise Review("Git 可执行文件路径元数据无效。")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise Review("Git 可执行文件路径必须是无 .. 的绝对路径。")
    return path


def _validate_git(raw):
    try:
        path = _git_path(raw)
        # Only the trusted Git endpoint may be hardlinked; ancestors stay strict.
        parent = core._safe(path.parent)
        path = parent / path.name
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISREG(info.st_mode)
                or not os.access(path, os.X_OK)):
            raise OSError()
        return path
    except (core.Error, OSError, ValueError) as exc:
        raise Review("Git 可执行文件已缺失、不可执行、类型异常或含符号链接/重解析点；未同步，请重新安装调度。") from exc


def _resolve_git():
    # Resolve the installer's trusted PATH entry (including Homebrew symlinks)
    # once; scheduled runs validate the recorded target without following links.
    try:
        found = shutil.which("git")
        if found is None:
            raise FileNotFoundError()
        path = Path(found).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Review("当前环境中找不到可用的 Git；未同步或安装调度。") from exc
    return _validate_git(str(path))


@contextlib.contextmanager
def _git_on_path(executable):
    original = os.environ.get("PATH")
    try:
        os.environ["PATH"] = str(executable.parent) + (os.pathsep + original if original else "")
        yield
    finally:
        if original is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original


def validate_paths(workspace, repo):
    workspace, repo, source = (_directory(workspace), _directory(repo),
                               _directory(SOURCE_ROOT))
    if any(core._overlap(a, b) for a, b in
           ((workspace, repo), (workspace, source), (repo, source))):
        raise Review("工作区、私有数据仓库和程序安装目录必须两两完全独立。")
    for relative in ("manifest.json", ".agent-sync/device.json", ".agent-sync/baseline.json"):
        if _read(repo / relative) is None:
            raise Review("数据仓库尚未初始化：需要 manifest、device 和 baseline 文件。")
    directory = core._safe(workspace / "schedule")
    if directory.exists() and not directory.is_dir():
        raise Review("schedule 必须是真实目录。")
    for name in MANAGED_FILES:
        path = core._safe(directory / name)
        if path.exists() and not path.is_file():
            raise Review("调度状态必须是普通文件。")
    return workspace, repo, source


def _interval(value):
    if type(value) is not int or not 1 <= value <= 10080:
        raise Review("间隔必须是 1 至 10080 的整数分钟，默认 60。")
    return value


def managed_identity(workspace, repo, platform):
    # normcase follows the host filesystem conventions, not an injected OS name.
    identity = _hash(_json_bytes([os.path.normcase(str(workspace)),
                                 os.path.normcase(str(repo))]))
    name = ("AgentSync-" if platform == "win32" else "com.agentsync.sync.") + identity[:12]
    return name, "AgentSync scheduler/v1:" + identity


def _argv(executable, workspace, repo):
    return [str(executable), "-B", "-m", "agent_sync.scheduler", "run",
            "--workspace", str(workspace), "--repo", str(repo)]


def windows_spec(name, owner, sid, executable, workspace, repo, source,
                 minutes=60, start=None):
    """Pure builder; executable is the explicit pythonw.exe action path."""
    _interval(minutes)
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise Review("无法确认当前用户 SID。")
    if start is None:
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    root = ET.Element("Task", {"xmlns": NS, "version": "1.2"})

    def child(parent, tag, text=None, **attrs):
        node = ET.SubElement(parent, tag, attrs)
        if text is not None:
            node.text = str(text)
        return node

    registration = child(root, "RegistrationInfo")
    child(registration, "Description", owner)
    child(registration, "URI", "\\" + name)
    triggers = child(root, "Triggers")
    trigger = child(triggers, "TimeTrigger")
    repetition = child(trigger, "Repetition")
    child(repetition, "Interval", "PT%dM" % minutes)
    child(repetition, "StopAtDurationEnd", "false")
    child(trigger, "StartBoundary", start)
    child(trigger, "Enabled", "true")
    principals = child(root, "Principals")
    principal = child(principals, "Principal", id="Author")
    child(principal, "UserId", sid)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    for tag, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "false"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
    ):
        child(settings, tag, value)
    idle = child(settings, "IdleSettings")
    child(idle, "Duration", "PT10M")
    child(idle, "WaitTimeout", "PT1H")
    child(idle, "StopOnIdleEnd", "true")
    child(idle, "RestartOnIdle", "false")
    for tag, value in (
        ("AllowStartOnDemand", "true"), ("Enabled", "true"),
        ("Hidden", "true"), ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"), ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        child(settings, tag, value)
    actions = child(root, "Actions", Context="Author")
    action = child(actions, "Exec")
    child(action, "Command", executable)
    child(action, "Arguments", subprocess.list2cmdline(_argv(executable, workspace, repo)[1:]))
    child(action, "WorkingDirectory", source)
    # schtasks requires a declaration consistent with the on-disk encoding.
    # Explicit LE + BOM is deterministic even on a big-endian build host.
    text = "<?xml version='1.0' encoding='utf-16'?>\n" + ET.tostring(root, encoding="unicode")
    return b"\xff\xfe" + text.encode("utf-16-le")


def macos_spec(name, owner, executable, workspace, repo, source, minutes=60):
    """Pure builder; arbitrary spaces, quotes and ampersands remain argv data."""
    _interval(minutes)
    return plistlib.dumps({
        "Label": name, "AgentSyncOwner": owner,
        "ProgramArguments": _argv(executable, workspace, repo),
        "WorkingDirectory": str(source), "StartInterval": minutes * 60,
        "RunAtLoad": False, "ProcessType": "Background",
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def _duration(text):
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text)
    if not match or not any(match.groups()):
        raise Review("任务时间格式不能安全识别。")
    days, hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _xml_semantics(data):
    """Order/prefix/ISO-duration insensitive, but reject unrecognized edits.

    Windows may add these documented default settings when exporting XML. All
    other additions, including additional actions/triggers, remain significant.
    """
    if len(data) > MAX_FILE:
        raise Review("任务 XML 不可安全识别。")

    class NoDTD(ET.TreeBuilder):
        def doctype(self, name, pubid, system):
            # Called before parsing the internal subset / expanding entities;
            # unlike byte searches, this also covers UTF-16 LE and BE input.
            raise Review("任务 XML 不允许 DTD。")

    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=NoDTD()))
    except ET.ParseError as exc:
        raise Review("任务 XML 不可安全识别。") from exc
    if root.tag != "{" + NS + "}Task":
        raise Review("任务 XML 命名空间不匹配。")
    defaults = {
        "Task/Triggers/TimeTrigger/Repetition": {"StopAtDurationEnd": "false"},
        "Task/Triggers/TimeTrigger": {"Enabled": "true"},
        "Task/Principals/Principal": {"RunLevel": "LeastPrivilege"},
        "Task/Settings": {
            "Enabled": "true",
            "RunOnlyIfNetworkAvailable": "false",
            "AllowStartOnDemand": "true",
            "RunOnlyIfIdle": "false",
            "WakeToRun": "false",
            "Priority": "7",
            "DisallowStartOnRemoteAppSession": "false",
            "UseUnifiedSchedulingEngine": "false",
            "Volatile": "false",
        },
    }

    def walk(node, parent=""):
        if not node.tag.startswith("{" + NS + "}"):
            raise Review("任务 XML 含未知命名空间。")
        tag = node.tag.split("}", 1)[1]
        path = parent + "/" + tag if parent else tag
        attrs = dict(node.attrib)
        if tag == "Task":
            attrs.pop("version", None)
        text = (node.text or "").strip()
        if tag in {"Interval", "Duration", "WaitTimeout", "ExecutionTimeLimit"} and text:
            text = str(_duration(text))
        if tag in {"StartBoundary", "EndBoundary"} and text:
            value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if value.tzinfo is not None:
                text = value.astimezone(dt.timezone.utc).isoformat()
        children = []
        known = defaults.get(path, {})
        seen = set()
        for sub in node:
            # Validate namespaces and descendants BEFORE considering omission.
            semantic = walk(sub, path)
            subtag = semantic[0]
            if subtag in known:
                if subtag in seen:
                    raise Review("任务 XML 含重复默认节点。")
                seen.add(subtag)
                if not sub.attrib and not len(sub) and (sub.text or "").strip() == known[subtag]:
                    continue
            children.append(semantic)
        return tag, tuple(sorted(attrs.items())), text, tuple(sorted(children))

    return walk(root)


class NativeBackend:
    platform = None

    def __init__(self, command=None, executable=None):
        self.command = command
        self.executable = Path(executable) if executable is not None else Path(sys.executable)

    def call(self, args):
        if self.command is not None:
            result = self.command(list(args))
        else:
            if sys.platform != self.platform:
                raise Review("不能在其他操作系统上注册真实任务。")
            kwargs = {}
            if self.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                result = subprocess.run(args, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=30, check=False, **kwargs)
            except (OSError, subprocess.SubprocessError) as exc:
                raise Review("系统调度命令无法完成；请检查本机调度服务。") from exc
        if len(result.stdout or b"") > 8 * MAX_FILE:
            raise Review("系统调度响应过大，无法安全确认。")
        return result

    def checked(self, args):
        result = self.call(args)
        if result.returncode != 0:
            raise Review("系统调度操作失败；登记状态需要检查，未报告安装成功。")
        return result.stdout

    def runtime(self):
        return _ordinary(self.executable)

    def external(self, name):
        return None


def _text(data):
    if isinstance(data, str):
        return data
    # Command transport only; never use this to repair local XML files.
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="strict")
        # XML's BOM-less UTF-16 signatures, independent of its declaration.
        if data.startswith(b"<\x00"):
            return data.decode("utf-16-le", errors="strict")
        if data.startswith(b"\x00<"):
            return data.decode("utf-16-be", errors="strict")
        try:
            return data.decode("utf-8-sig", errors="strict")
        except UnicodeError:
            if os.name != "nt":
                raise
            return data.decode("mbcs", errors="strict")
    except UnicodeError as exc:
        raise Review("系统调度响应编码无法识别。") from exc


class WindowsBackend(NativeBackend):
    platform = "win32"

    def runtime(self):
        return _ordinary(self.executable.with_name("pythonw.exe"))

    def sid(self):
        output = self.checked(["whoami.exe", "/user", "/fo", "csv", "/nh"])
        rows = list(csv.reader(io.StringIO(_text(output))))
        if len(rows) != 1 or len(rows[0]) != 2:
            raise Review("无法确认当前交互用户 SID。")
        sid = rows[0][1].strip()
        if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
            raise Review("无法确认当前交互用户 SID。")
        return sid

    def build(self, name, owner, executable, workspace, repo, source, minutes):
        return windows_spec(name, owner, self.sid(), executable, workspace, repo, source, minutes)

    def query(self, name):
        result = self.call(["schtasks.exe", "/Query", "/TN", name, "/XML"])
        if result.returncode == 0:
            # schtasks stdout may declare UTF-16 but use UTF-8/the local code page.
            # Preserve raw evidence; only this controlled transport is decoded.
            data = result.stdout
            if len(data) > MAX_FILE:
                raise Review("任务 XML 不可安全识别。")
            text = _text(data)
            _xml_semantics(text)  # Includes DTD and duplicate-default rejection.
            root = ET.fromstring(text)
            enabled = root.find("{%s}Settings/{%s}Enabled" % (NS, NS))
            return {"raw": data,
                    "enabled": enabled is None or (enabled.text or "").strip() == "true",
                    "running": None}
        # A per-task query error is NOT proof of absence. A successful complete
        # enumeration is required; localized status/error messages are ignored.
        output = self.checked(["schtasks.exe", "/Query", "/FO", "CSV", "/NH"])
        rows = list(csv.reader(io.StringIO(_text(output))))
        names = []
        for row in rows:
            if len(row) < 3 or not row[0].startswith("\\"):
                raise Review("无法可靠枚举系统任务；未认定任务不存在。")
            names.append(row[0].casefold())
        if ("\\" + name).casefold() in names:
            raise Review("任务存在但无法读取其定义；请人工检查。")
        return None

    def verify(self, live, spec, external):
        if (live is None or len(live["raw"]) > MAX_FILE
                or _xml_semantics(_text(live["raw"])) != _xml_semantics(spec)):
            raise Review("系统任务不是原有受管定义，或已被用户修改；拒绝覆盖或删除。")

    def apply(self, name, candidate, spec, external, update):
        args = ["schtasks.exe", "/Create", "/TN", name, "/XML", str(candidate)]
        if update:
            args.append("/F")
        self.checked(args)

    def delete(self, name):
        # /F suppresses a confirmation prompt, not an ownership check. The caller
        # has already verified this exact name and its complete managed semantics.
        self.checked(["schtasks.exe", "/Delete", "/TN", name, "/F"])


def _launch_fields(text):
    """Conservative parser for launchctl print's top-level fields.

    This is deliberately not a generic parser for all launchd diagnostics. Unknown
    output variants fail closed when verifying a job, rather than permitting edits.
    """
    fields, arguments = {}, None
    depth = 0
    in_arguments = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_arguments:
            if stripped == "}":
                in_arguments = False
                depth -= 1
            else:
                arguments.append(stripped)
            continue
        if depth == 1 and stripped == "arguments = {":
            arguments = []
            in_arguments = True
            depth += 1
            continue
        if depth == 1 and " = " in stripped and not stripped.endswith("{"):
            key, value = stripped.split(" = ", 1)
            if key in fields:
                raise Review("launchd 响应含重复字段。")
            fields[key] = value
        if stripped.endswith("{"):
            depth += 1
        elif stripped == "}":
            depth -= 1
    if depth != 0 or in_arguments:
        raise Review("launchd 响应格式无法安全识别。")
    fields["arguments"] = arguments
    return fields


class MacOSBackend(NativeBackend):
    platform = "darwin"

    def __init__(self, command=None, executable=None, launch_dir=None, uid=None):
        super().__init__(command, executable)
        # Injection is only allowed with a non-native command transport.
        if command is None and (launch_dir is not None or uid is not None):
            raise Review("真实 launchd 登记只能使用当前用户域。")
        self.uid = uid
        self.launch_dir = Path(launch_dir) if launch_dir is not None else None

    def _domain(self):
        if self.uid is None:
            if sys.platform != "darwin":
                raise Review("当前系统不支持真实 launchd 登记。")
            self.uid = os.getuid()
        return "gui/%d" % self.uid

    def external(self, name):
        self._domain()
        if self.launch_dir is None:
            import pwd
            # Only identify the standard, explicit install destination. Do not
            # enumerate or read the home directory or any personal configuration.
            home = Path(pwd.getpwuid(self.uid).pw_dir)
            self.launch_dir = home / "Library" / "LaunchAgents"
        path = self.launch_dir / (name + ".plist")
        if not path.is_absolute() or ".." in path.parts:
            raise Review("LaunchAgents 路径无效。")
        core._safe(path)
        if path.exists() and not path.is_file():
            raise Review("LaunchAgent 文件不是普通文件。")
        return path

    def build(self, name, owner, executable, workspace, repo, source, minutes):
        return macos_spec(name, owner, executable, workspace, repo, source, minutes)

    def query(self, name):
        target = self._domain() + "/" + name
        result = self.call(["launchctl", "print", target])
        if result.returncode == 0:
            fields = _launch_fields(_text(result.stdout))
            state = fields.get("state")
            running = True if state == "running" else False if state in {"not running", "waiting"} else None
            disabled = self.checked(["launchctl", "print-disabled", self._domain()])
            text = _text(disabled)
            entries = re.findall(r'^\s*"([^"\n]+)"\s*=>\s*(true|false)\s*$', text, re.M)
            # Require the recognizable container before inferring the default.
            if not text.strip().startswith("disabled services = {") or not text.strip().endswith("}"):
                raise Review("无法确认 launchd 启用状态。")
            enabled = not any(label == name and value == "true" for label, value in entries)
            return {"raw": fields, "enabled": enabled, "running": running}
        # launchctl uses 113 for an unknown service. Require an accessible GUI
        # domain as well, and reject any mention of the supposedly missing label.
        if result.returncode != 113:
            raise Review("无法查询 launchd 服务；未认定服务不存在。")
        domain = _text(self.checked(["launchctl", "print", self._domain()]))
        if not domain.lstrip().startswith(self._domain() + " = {") or name in domain:
            raise Review("launchd 域不能可靠证明服务不存在。")
        return None

    def verify(self, live, spec, external):
        expected = plistlib.loads(spec)
        if live is None:
            raise Review("受管 launchd 服务缺失，需要人工检查。")
        fields = live["raw"]
        if (fields.get("path") != str(external)
                or fields.get("program") != expected["ProgramArguments"][0]
                or fields.get("arguments") != expected["ProgramArguments"]
                or fields.get("working directory") != expected["WorkingDirectory"]
                or fields.get("run interval") != "%d seconds" % expected["StartInterval"]
                or live["enabled"] is not True):
            raise Review("launchd 实际登记与受管定义不一致；拒绝覆盖或删除。")

    def apply(self, name, candidate, spec, external, update):
        if update:
            self.delete(name)
            if self.query(name) is not None:
                raise Review("原 launchd 服务尚未卸载；停止更新。")
        core._safe(external.parent)
        external.parent.mkdir(parents=True, exist_ok=True)
        core._safe(external.parent)
        core._atomic(external, spec)
        self.checked(["launchctl", "bootstrap", self._domain(), str(external)])

    def delete(self, name):
        self.checked(["launchctl", "bootout", self._domain() + "/" + name])


def native_backend():
    if sys.platform == "win32":
        return WindowsBackend()
    if sys.platform == "darwin":
        return MacOSBackend()
    raise Review("周期任务安装仅支持 Windows 和 macOS。")


@contextlib.contextmanager
def _exclusive(path):
    path = core._safe(path)
    token = (uuid.uuid4().hex + "\n").encode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Busy("已有调度操作或同步正在运行；未重复执行。") from exc
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        # Never remove a replacement lock, including a manually substituted file.
        current = core._safe(path)
        try:
            info = current.stat()
        except FileNotFoundError:
            pass
        else:
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino) and _read(current) == token:
                current.unlink()


def _envelope(value):
    # Local corruption detection, not authentication against the local user.
    return {"record": value, "sha256": _hash(_json_bytes(value))}


def _unseal(data):
    try:
        wrapper = json.loads(data)
        if set(wrapper) != {"record", "sha256"} or wrapper["sha256"] != _hash(_json_bytes(wrapper["record"])):
            raise ValueError()
        return wrapper["record"]
    except (ValueError, TypeError, KeyError) as exc:
        raise Review("本地受管状态已修改或损坏；未覆盖，请人工检查。") from exc


class Scheduler:
    """A workspace has one scheduling slot, bound to one private repository.

    backend injection is for testing. Native backends still enforce their OS in
    their command transport. run_once does not require an OS backend.
    """

    def __init__(self, workspace, repo, backend=None):
        self.workspace, self.repo, self.source = validate_paths(workspace, repo)
        self.directory = self.workspace / "schedule"
        self.backend = backend

    def _backend(self):
        if self.backend is None:
            self.backend = native_backend()
        return self.backend

    def _mkdir(self):
        core._safe(self.directory)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        core._safe(self.directory)

    def _state(self):
        data = _read(self.directory / "state.json")
        if data is None:
            return None
        state = _unseal(data)
        required = {"schema", "workspace", "repo", "source", "platform", "name", "owner",
                    "created_at", "updated_at", "installed", "interval_minutes", "runtime",
                    "argv", "spec_file", "spec_sha256", "external", "git_executable"}
        if not isinstance(state, dict) or set(state) != required:
            raise Review("登记状态版本或字段不受支持。")
        _git_path(state["git_executable"])
        platform = state["platform"]
        if platform not in {"win32", "darwin"}:
            raise Review("登记平台无效。")
        name, owner = managed_identity(self.workspace, self.repo, platform)
        if (state["schema"] != SCHEMA or state["workspace"] != str(self.workspace)
                or state["repo"] != str(self.repo) or state["source"] != str(self.source)
                or state["name"] != name or state["owner"] != owner
                or type(state["installed"]) is not bool
                or state["spec_file"] != ("task.xml" if platform == "win32" else "agent.plist")
                or state["argv"] != _argv(state["runtime"], self.workspace, self.repo)):
            raise Review("登记状态身份不匹配；请保留文件并人工检查。")
        _interval(state["interval_minutes"])
        spec = _read(self.directory / state["spec_file"])
        if spec is None or _hash(spec) != state["spec_sha256"]:
            raise Review("本地任务定义已修改或缺失；拒绝覆盖或删除。")
        # Also tie the action and interval to metadata, not only to its hash.
        if platform == "darwin":
            expected = macos_spec(name, owner, state["runtime"], self.workspace, self.repo,
                                  self.source, state["interval_minutes"])
            if plistlib.loads(spec) != plistlib.loads(expected):
                raise Review("plist 与登记元数据不一致。")
        else:
            root = ET.fromstring(spec)
            sid = root.findtext("{%s}Principals/{%s}Principal/{%s}UserId" % (NS, NS, NS))
            start = root.findtext("{%s}Triggers/{%s}TimeTrigger/{%s}StartBoundary" % (NS, NS, NS))
            expected = windows_spec(name, owner, sid or "", state["runtime"], self.workspace,
                                    self.repo, self.source, state["interval_minutes"], start)
            if _xml_semantics(spec) != _xml_semantics(expected):
                raise Review("XML 与登记元数据不一致。")
        return state

    def _no_pending(self):
        if _read(self.directory / "pending.json") is not None:
            raise Review("上次登记事务未完成；保留了 pending.json，请人工核对系统任务后恢复。")

    def _external(self, backend, name):
        external = backend.external(name)
        if external is not None:
            if any(core._overlap(external, root) for root in (self.source, self.repo, self.workspace)):
                raise Review("LaunchAgent 安装位置不得与程序、数据仓库或工作区重叠。")
            _read(external)
        return external

    def _owned(self, state, backend, live, external):
        if state["platform"] != backend.platform or state["external"] != (str(external) if external else None):
            raise Review("平台或 LaunchAgent 位置变化；拒绝接管登记。")
        spec = _read(self.directory / state["spec_file"])
        if state["installed"]:
            if external is not None and _read(external) != spec:
                raise Review("LaunchAgent 文件已修改或缺失；拒绝覆盖或删除。")
            backend.verify(live, spec, external)
        elif live is not None or (external is not None and _read(external) is not None):
            raise Review("已移除的登记发生名称碰撞；拒绝接管。")
        return spec

    def _management_ready(self):
        self._no_pending()
        if _read(self.directory / "run.lock") is not None:
            raise Busy("同步锁已存在；未改变调度登记。")

    def install(self, interval_minutes=60):
        minutes = _interval(interval_minutes)
        backend = self._backend()
        name, owner = managed_identity(self.workspace, self.repo, backend.platform)
        external = self._external(backend, name)
        executable = backend.runtime()
        git_executable = _resolve_git()
        # No state files, locks, plist, or OS task creation before privacy approval.
        # The returned metadata is intentionally discarded.
        assert_private_repo(self.repo)
        self._mkdir()
        with _exclusive(self.directory / "management.lock"):
            self._management_ready()
            state = self._state()
            live = backend.query(name)
            if state is None:
                if live is not None or (external is not None and _read(external) is not None):
                    raise Review("同名任务或 plist 已存在且未受管；拒绝覆盖。")
                for filename in ("task.xml", "agent.plist", "candidate.xml", "candidate.plist"):
                    if _read(self.directory / filename) is not None:
                        raise Review("发现无登记的生成定义；拒绝覆盖，请人工检查。")
            else:
                self._owned(state, backend, live, external)
            suffix = "xml" if backend.platform == "win32" else "plist"
            candidate = self.directory / ("candidate." + suffix)
            if _read(candidate) is not None:
                raise Review("候选任务定义已存在；拒绝覆盖。")
            spec = backend.build(name, owner, executable, self.workspace, self.repo, self.source, minutes)
            timestamp = _now()
            record = {
                "schema": SCHEMA, "workspace": str(self.workspace), "repo": str(self.repo),
                "source": str(self.source), "platform": backend.platform, "name": name,
                "owner": owner, "created_at": state["created_at"] if state else timestamp,
                "updated_at": timestamp, "installed": True, "interval_minutes": minutes,
                "runtime": str(executable), "argv": _argv(executable, self.workspace, self.repo),
                "git_executable": str(git_executable),
                "spec_file": "task.xml" if suffix == "xml" else "agent.plist",
                "spec_sha256": _hash(spec), "external": str(external) if external else None,
            }
            pending = {"schema": SCHEMA, "operation": "install", "created_at": timestamp,
                       "candidate": record, "previous_state_sha256":
                       _hash(_read(self.directory / "state.json")) if state else None}
            pending_bytes = _json_bytes(pending)
            core._atomic(self.directory / "pending.json", pending_bytes)
            core._atomic(candidate, spec)
            # From here any exception leaves a transaction marker, old state and
            # candidate. We never guess whether a timed-out OS operation committed.
            backend.apply(name, candidate, spec, external, bool(state and state["installed"]))
            live = backend.query(name)
            backend.verify(live, spec, external)
            if live["enabled"] is not True:
                raise Review("登记未启用；未报告安装成功。")
            if external is not None and _read(external) != spec:
                raise Review("登记期间 plist 发生变化；未报告安装成功。")
            # Recheck local ownership before replacing the committed definition.
            if state is not None:
                self._state()
            elif _read(self.directory / record["spec_file"]) is not None:
                raise Review("登记期间本地定义发生变化；停止提交。")
            core._atomic(self.directory / record["spec_file"], spec)
            core._atomic(self.directory / "state.json", _json_bytes(_envelope(record)))
            self._cleanup(candidate, spec, pending_bytes)
            return {"status": "installed", "message": "已安装当前用户周期同步。",
                    "interval_minutes": minutes, "platform": backend.platform, "name": name,
                    "installed": True, "enabled": True, "running": live["running"]}

    def _cleanup(self, candidate, spec, pending_bytes):
        if candidate is not None:
            if _read(candidate) != spec:
                raise Review("候选定义发生变化；保留事务标记。")
            core._safe(candidate).unlink()
        pending = self.directory / "pending.json"
        if _read(pending) != pending_bytes:
            raise Review("事务标记发生变化；未删除。")
        core._safe(pending).unlink()

    def remove(self):
        backend = self._backend()
        # Do not create a workspace directory merely to remove an unknown task.
        if not self.directory.exists():
            raise Review("没有本地登记；不会删除未知系统任务。")
        with _exclusive(self.directory / "management.lock"):
            self._management_ready()
            state = self._state()
            if state is None:
                raise Review("没有本地登记；不会删除未知系统任务。")
            external = self._external(backend, state["name"])
            live = backend.query(state["name"])
            spec = self._owned(state, backend, live, external)
            if not state["installed"]:
                return {"status": "removed", "message": "该受管登记已经移除。", "installed": False}
            pending_bytes = _json_bytes({"schema": SCHEMA, "operation": "remove",
                                         "created_at": _now(), "name": state["name"]})
            core._atomic(self.directory / "pending.json", pending_bytes)
            backend.delete(state["name"])
            if backend.query(state["name"]) is not None:
                raise Review("系统任务仍存在；未报告移除成功。")
            if external is not None:
                if _read(external) != spec:
                    raise Review("LaunchAgent 文件已变化；未删除。")
                core._safe(external).unlink()
            self._state()
            state["installed"] = False
            state["updated_at"] = _now()
            core._atomic(self.directory / "state.json", _json_bytes(_envelope(state)))
            self._cleanup(None, None, pending_bytes)
            return {"status": "removed", "message": "已移除受管系统任务；保留本地定义和运行记录。",
                    "installed": False, "name": state["name"]}

    def status(self):
        backend = self._backend()
        name, _ = managed_identity(self.workspace, self.repo, backend.platform)
        result = {"status": "unknown", "message": "正在核对本地与系统登记。",
                  "platform": backend.platform, "name": name, "interval_minutes": None,
                  "installed": None, "enabled": None, "running": None,
                  "run_lock_present": _read(self.directory / "run.lock") is not None,
                  "management_lock_present": _read(self.directory / "management.lock") is not None,
                  "latest_run": None}
        try:
            state = self._state()
            if state:
                result.update(platform=state["platform"], name=state["name"],
                              interval_minutes=state["interval_minutes"])
            telemetry = self._telemetry()
            if telemetry.get("latest-run.json") is not None:
                result["latest_run"] = json.loads(_read(self.directory / "latest-run.json"))
            if result["management_lock_present"]:
                raise Busy("调度操作正在进行；当前登记状态暂不确定。")
            self._no_pending()
            external = self._external(backend, result["name"])
            live = backend.query(result["name"])
            result.update(installed=live is not None,
                          enabled=live["enabled"] if live else None,
                          running=live["running"] if live else False)
            if state:
                self._owned(state, backend, live, external)
                result["status"] = "installed" if state["installed"] else "removed"
                result["message"] = "已核对受管登记。"
            elif live is not None or (external is not None and _read(external) is not None):
                raise Review("发现未受管的同名任务或文件；不会接管。")
            else:
                result.update(status="not_installed", message="尚未安装周期同步。")
            return result, 0
        except Exception as exc:
            result.update(status="needs_review", message=_message(exc))
            return result, 2

    def _telemetry(self):
        names = ("latest-run.json", "runs.log", "runs.log.1", "runs.log.2")
        data = _read(self.directory / "telemetry.json")
        records = {} if data is None else _unseal(data)
        if not isinstance(records, dict) or any(name not in names for name in records):
            raise Review("运行记录索引异常；未覆盖。")
        for name in names:
            existing = _read(self.directory / name)
            actual = None if existing is None else _hash(existing)
            if actual != records.get(name):
                raise Review("运行记录已被修改或写入曾中断；未覆盖，请人工保留并检查。")
        return records

    def _record_run(self, result):
        self._telemetry()
        latest = _json_bytes(result)
        if len(latest) > 8192:
            raise Review("最终运行摘要超过大小限制。")
        log = _read(self.directory / "runs.log") or b""
        line = (result["finished_at"] + " status=" + result["status"] +
                " exit=" + str(result["exit_code"]) + "\n").encode("utf-8")
        if len(log) + len(line) > LOG_LIMIT:
            older = _read(self.directory / "runs.log.1")
            if older is not None:
                core._atomic(self.directory / "runs.log.2", older)
            core._atomic(self.directory / "runs.log.1", log)
            log = b""
        core._atomic(self.directory / "runs.log", log + line)
        core._atomic(self.directory / "latest-run.json", latest)
        records = {}
        for name in ("latest-run.json", "runs.log", "runs.log.1", "runs.log.2"):
            data = _read(self.directory / name)
            if data is not None:
                records[name] = _hash(data)
        core._atomic(self.directory / "telemetry.json", _json_bytes(_envelope(records)))

    def run_once(self):
        self._mkdir()
        try:
            with _exclusive(self.directory / "management.lock"):
                self._no_pending()
                # Check ownership/integrity if there is a registration, but do not
                # invoke OS commands or nest the bridge's own core locks.
                state = self._state()
                with _exclusive(self.directory / "run.lock"):
                    self._telemetry()  # Do not sync if the result cannot safely be recorded.
                    started = _now()
                    try:
                        git_executable = (_validate_git(state["git_executable"])
                                          if state is not None else _resolve_git())
                        with _git_on_path(git_executable):
                            raw, code = bridge_run(self.workspace, self.repo, check=False)
                        if type(code) is not int or not isinstance(raw, dict):
                            raise Review("同步桥返回了无法识别的结果。")
                        result = _summary(raw, code)
                    except Exception as exc:
                        code = 2
                        result = {"status": "needs_review", "message": _message(exc)}
                    result.update(started_at=started, finished_at=_now(), exit_code=code)
                    self._record_run(result)
                    return result, code
        except Busy as exc:
            return {"status": "busy", "message": _message(exc), "exit_code": 2}, 2
        except Exception as exc:
            return {"status": "needs_review", "message": _message(exc), "exit_code": 2}, 2


def _summary(raw, code):
    # No raw messages, paths, filenames, stderr, tokens or exception strings enter
    # scheduler telemetry. Counts and a small fixed status vocabulary are enough.
    allowed = {"success", "ok", "synced", "unchanged", "applied", "needs_review", "conflict", "failed", "error",
               "up_to_date", "checked", "changes_pending", "remote_changed", "needs_skill_install_review", "busy"}
    status = raw.get("status")
    if not isinstance(status, str) or status not in allowed:
        status = "success" if code == 0 else "needs_review"
    if code != 0 and status in {"success", "ok", "synced", "unchanged", "applied", "up_to_date", "checked"}:
        status = "needs_review"
    result = {"status": status, "message": "同步完成。" if code == 0 else "同步需要检查；保留冲突或失败状态，未强制覆盖。"}
    for key in ("source_files", "unique_source_reads", "elapsed_seconds", "uploaded",
                "source_write_count", "changed_files", "direct_pull_files"):
        value = raw.get(key)
        if isinstance(value, (list, tuple, dict)):
            result[key + "_count"] = len(value)
        elif isinstance(value, bool):
            result[key] = value
        elif type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**15:
            result[key] = value
    return result


def _message(exc):
    if isinstance(exc, Review):
        return str(exc)
    return "操作未完成：路径、私有仓库验证或系统服务需要检查；敏感错误详情未输出。"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Review("命令参数无效；需要 install/status/remove/run、--workspace ABS 和 --repo ABS。")


def main(argv=None):
    core.configure_stdio()
    parser = _Parser(prog="agent_sync schedule", description="当前用户周期同步（默认 60 分钟）")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "status", "remove", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("--workspace", required=True)
        sub.add_argument("--repo", required=True)
        if command == "install":
            sub.add_argument("--interval-minutes", type=int, default=60)
    try:
        args = parser.parse_args(argv)
        if args.command == "install":
            _interval(args.interval_minutes)
        scheduler = Scheduler(args.workspace, args.repo)
        if args.command == "install":
            result, code = scheduler.install(args.interval_minutes), 0
        elif args.command == "remove":
            result, code = scheduler.remove(), 0
        elif args.command == "status":
            result, code = scheduler.status()
        else:
            result, code = scheduler.run_once()
    except Exception as exc:
        result, code = {"status": "needs_review", "message": _message(exc)}, 2
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
