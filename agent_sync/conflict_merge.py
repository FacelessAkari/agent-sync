"""Build a bounded three-way text candidate from an authenticated local baseline."""

import codecs
import difflib
import itertools
from pathlib import Path
import re

from . import core, gitops


MAX_BYTES = 256 * 1024
MAX_LINES = 3000
HISTORY_LIMIT = 64
BACKUP_LIMIT = 64
_OID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)")
_BULLET = re.compile(r"^[-*+]\s+(\S.*)")
_KEY = re.compile(r"^[`*]*([\w][\w .\-/]{0,60}?)[`*]*\s*[:：=]\s*\S")
_ITEM_ID = re.compile(r"^\[([A-Za-z]+-\d+)\]")


def _document(data):
    if len(data) > MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n") if crlf else text
    if crlf and data.count(b"\n") != data.count(b"\r\n"):
        return None
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        return None
    final_newline = text.endswith("\n")
    lines = text.split("\n") if text else []
    if final_newline:
        lines.pop()
    if len(lines) > MAX_LINES:
        return None
    newline = "\r\n" if crlf else "\n" if "\n" in text else None
    return lines, final_newline, newline


def _edits(base, changed):
    matcher = difflib.SequenceMatcher(None, base, changed, autojunk=False)
    edits = []
    for tag, i, j, x, y in matcher.get_opcodes():
        if tag == "replace" and j - i == y - x:
            # Neighbouring edits may form one diff hunk even when only one of
            # those lines is shared with the other side.
            edits.extend((i + n, i + n + 1, [changed[x + n]]) for n in range(j - i))
        elif tag != "equal":
            edits.append((i, j, changed[x:y]))
    return edits


def _units(lines):
    """Split simultaneous insertions only at unambiguous Markdown entry boundaries."""
    first = next((line for line in lines if line.strip()), "")
    heading = _HEADING.match(first)
    if not heading and not _BULLET.match(first):
        return None
    if any(line.lstrip().startswith(("```", "~~~")) for line in lines):
        return None
    entries = []
    unit = []
    identity = None
    for line in lines:
        marker = _HEADING.match(line) if heading else _BULLET.match(line)
        if heading and marker and len(marker[1]) < len(heading[1]):
            return None
        if marker and (not heading or len(marker[1]) == len(heading[1])):
            if any(part.strip() for part in unit):
                entries.append((identity, unit))
                unit = []
            if heading:
                identity = marker[2].strip().casefold()
            else:
                key = _KEY.match(marker[1]) or _ITEM_ID.match(marker[1])
                identity = key[1].strip().casefold() if key else None
        elif not heading and line.strip() and not line.startswith((" ", "\t")):
            return None
        unit.append(line)
    entries.append((identity, unit))
    return ("section" if heading else "list"), entries


def _combine_insertions(local, repository):
    left, right = _units(local), _units(repository)
    if left is None or right is None or left[0] != right[0]:
        return None
    # Preserve each side's order. Shared units can be interleaved only when
    # their order agrees. Match whole units so a repeated line in two different
    # sections is never silently discarded.
    left_units, right_units = left[1], right[1]

    def signature(unit):
        return "\n".join(unit).strip("\n")

    lkeys = [signature(unit) for _, unit in left_units]
    rkeys = [signature(unit) for _, unit in right_units]
    if len(set(lkeys)) != len(lkeys) or len(set(rkeys)) != len(rkeys):
        return None
    shared = set(lkeys) & set(rkeys)
    if [k for k in lkeys if k in shared] != [k for k in rkeys if k in shared]:
        return None
    identities = {}
    for key, (identity, _) in zip(lkeys + rkeys, left_units + right_units):
        if identity is not None and identities.setdefault(identity, key) != key:
            return None

    merged = []
    matcher = difflib.SequenceMatcher(None, lkeys, rkeys, autojunk=False)
    for tag, i, j, x, y in matcher.get_opcodes():
        for _, lines in left_units[i:j]:
            merged.extend(lines)
        if tag != "equal":
            for _, lines in right_units[x:y]:
                merged.extend(lines)
    return merged


def make_candidate(base: bytes, local: bytes, repository: bytes) -> bytes | None:
    """Preserve disjoint edits; leave overlapping replacements to a caller's fallback."""
    before, left, right = (_document(data) for data in (base, local, repository))
    if before is None or left is None or right is None:
        return None
    source = before[0]
    edits = sorted(_edits(source, left[0]) + _edits(source, right[0]), key=lambda e: e[:2])
    cursor = 0
    result = []
    for (start, end), overlapping in itertools.groupby(edits, key=lambda e: e[:2]):
        if start < cursor:
            return None
        changes = [entry[2] for entry in overlapping]
        content = changes[0]
        if len(changes) == 2 and content != changes[1]:
            if start != end:
                return None
            content = _combine_insertions(content, changes[1])
            if content is None:
                return None
        result.extend(source[cursor:start])
        result.extend(content)
        cursor = end
    result.extend(source[cursor:])
    newline = left[2] or before[2] or right[2] or "\n"
    trailing = right[1] if left[1] == before[1] else left[1]
    text = newline.join(result) + (newline if trailing and result else "")
    data = text.encode("utf-8-sig" if local.startswith(codecs.BOM_UTF8) else "utf-8")
    return data if len(data) <= MAX_BYTES and len(result) <= MAX_LINES else None


def _git_base(repo, relative, digest):
    if core._abs(gitops._text(repo, ["rev-parse", "--show-toplevel"], "确认历史所在仓库")) != repo:
        return None
    flags = ["--no-replace-objects", "--literal-pathspecs"]
    # Older macOS Git has no --no-lazy-fetch. A full clone needs no such
    # option. On partial clones require the flag so no object is fetched.
    partial = gitops._git(repo, ["config", "--get-regexp",
                          r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$"],
                          "检查本机 Git 对象来源", (0, 1))
    if partial.returncode == 0:
        flags.append("--no-lazy-fetch")
    history = gitops._git(repo, flags + ["log", "--format=%H", "--no-show-signature",
                          "--max-count=" + str(HISTORY_LIMIT), "HEAD", "--", relative],
                          "读取本机文件历史").stdout
    seen = set()
    for commit in history.splitlines()[:HISTORY_LIMIT]:
        if not _OID.fullmatch(commit):
            return None
        record = gitops._git(repo, flags + ["ls-tree", "-l", "-z", commit.decode("ascii"), "--", relative],
                             "读取历史文件信息").stdout
        if not record:
            continue
        metadata, _, name = record.partition(b"\t")
        fields = metadata.split()
        if (name != relative.encode("utf-8") + b"\0" or len(fields) != 4
                or fields[0] not in {b"100644", b"100755"} or fields[1] != b"blob"):
            continue
        oid, size = fields[2:]
        if not _OID.fullmatch(oid) or oid in seen or not size.isdigit() or int(size) > MAX_BYTES:
            continue
        seen.add(oid)
        data = gitops._git(repo, flags + ["cat-file", "blob", oid.decode("ascii")], "读取本机历史文件").stdout
        if len(data) == int(size) and core._digest(data) == digest:
            return data
    return None


def _backup_base(repo, relative, digest):
    root = core._safe(repo / ".agent-sync/backups")
    if not root.is_dir():
        return None
    groups, bindings, _ = core._load(repo)
    for group in groups:
        if group["kind"] == "tree" and relative.startswith(group["path"] + "/"):
            names = [relative[len(group["path"]) + 1:]]
            break
        if group["kind"] == "file" and relative == group["path"]:
            names = [Path(relative).name]
            if group["name"] in bindings:
                names.append(Path(bindings[group["name"]]).name)
            break
    else:
        return None
    # A core.apply transaction stores the old bytes at <group>/<relative>.
    # Build that path from the validated manifest, never from a journal target.
    transactions = [path for path in itertools.islice(root.iterdir(), 4096)
                    if re.fullmatch(r"txn-[0-9a-f]{32}", path.name)]
    transactions.sort(key=lambda p: p.lstat().st_mtime_ns, reverse=True)
    for txn in transactions[:BACKUP_LIMIT]:
        for name in dict.fromkeys(names):
            path = core._safe(txn / group["name"] / name)
            if path.is_file() and path.stat().st_size <= MAX_BYTES:
                with path.open("rb") as handle:
                    data = handle.read(MAX_BYTES + 1)
                if len(data) <= MAX_BYTES and core._digest(data) == digest:
                    return data
    return None


def find_base(repo: Path, repo_relative: str, expected_sha256: str) -> bytes | None:
    """Look only at the named path in local commits and its mapped transaction backups."""
    if not isinstance(expected_sha256, str) or not core.HASH.fullmatch(expected_sha256):
        return None
    try:
        core._data_path(repo_relative)
        repo = core._safe(repo)
    except (core.Error, OSError):
        return None
    for lookup in (_git_base, _backup_base):
        try:
            data = lookup(repo, repo_relative, expected_sha256)
        except (core.Error, OSError):
            continue
        if data is not None:
            return data
    return None
