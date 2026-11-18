"""Collect an explicitly reviewed file list into ordinary, byte-preserving copies.

This adapter only reads original skill files, including ordinary hardlinks. It
never follows reparse points or writes back through source files. New files must
be reviewed and added to selection.json explicitly.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
from . import core


def source_bytes(path):
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Source must be an absolute path')
    for ancestor in reversed([path, *path.parents]):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Refusing reparse point: ' + str(ancestor))
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('Refusing special file: ' + str(ancestor))
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Source is not a regular file: ' + str(path))
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('Source changed during open: ' + str(path))
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source changed during read: ' + str(path))
    final = path.lstat()
    if (stat.S_ISLNK(final.st_mode) or getattr(final, 'st_file_attributes', 0) & 0x400
            or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or len(data) != after.st_size):
        raise ValueError('Source changed during read')
    return data


def collect(config, mirror, state_file, check_only=False, *, subset=None,
            expected_state=None, expected_mirror=None):
    mirror, state_file = core._safe(mirror), core._safe(state_file)
    if core._overlap(mirror, state_file):
        raise ValueError('Mirror overlaps collection state')
    if (not isinstance(config, dict) or set(config) != {'version', 'files'}
            or type(config['version']) is not int or config['version'] != 1
            or not isinstance(config['files'], list)):
        raise ValueError('Invalid selection')
    names = []
    for item in config['files']:
        if (not isinstance(item, dict) or set(item) != {'source', 'mirror', 'reviewed_sha256'}
                or not isinstance(item['source'], str)
                or not isinstance(item['reviewed_sha256'], str)
                or not core.HASH.fullmatch(item['reviewed_sha256'])):
            raise ValueError('Invalid selection entry')
        core._names(item['mirror'], file=True)
        names.append(item['mirror'])
        source = Path(item['source'])
        if not source.is_absolute() or '..' in source.parts:
            raise ValueError('Source must be an explicit absolute path')
        if any(core._overlap(source, target) for target in (mirror, state_file)):
            raise ValueError('Source overlaps a write target')
    if len(set(names)) != len(names):
        raise ValueError('Duplicate mirror destination')
    core._case_check(names)
    if subset is not None and (not isinstance(subset, dict) or subset.keys() - set(names)
            or any(not isinstance(value, str) or not core.HASH.fullmatch(value) for value in subset.values())
            or not isinstance(expected_state, dict) or not isinstance(expected_mirror, dict)):
        raise ValueError('Invalid pinned collection subset')
    raw_state = core._bytes(state_file)
    state = json.loads(raw_state) if raw_state is not None else {}
    if not isinstance(state, dict):
        raise ValueError('Invalid collection state')
    for name, value in state.items():
        core._names(name, file=True)
        if not isinstance(value, str) or not core.HASH.fullmatch(value):
            raise ValueError('Invalid collection digest')
    core._case_check(list(state))
    if subset is not None and state != expected_state:
        raise ValueError('Collection state changed since partial plan')
    operations = []
    next_state = state.copy() if subset is not None else {}
    for item in config['files']:
        rel = item['mirror']
        if subset is not None and rel not in subset:
            continue
        core._names(rel, file=True)
        source = Path(item['source'])
        data = source_bytes(source)
        digest = hashlib.sha256(data).hexdigest()
        destination = core._safe(mirror.joinpath(*rel.split('/')))
        previous = core._bytes(destination)
        old_hash = core._digest(previous)
        if subset is not None and (digest != subset[rel] or old_hash != expected_mirror.get(rel)):
            raise ValueError('Source or mirror changed since partial plan')
        base = state.get(rel)
        if previous != data and old_hash != base:
            raise ValueError('Mirror has an independent change; resolve before collecting: ' + rel)
        # First collection must match the exact reviewed snapshot.
        if base is None and digest != item['reviewed_sha256']:
            raise ValueError('Source changed since review: ' + str(source))
        if previous != data:
            operations.append((destination, data, previous))
        next_state[rel] = digest
    expected = {item['mirror'] for item in config['files']}
    actual = set(core._scan_tree(mirror)) if mirror.exists() else set()
    if actual - expected:
        raise ValueError('Unexpected files in mirror; review required')
    if not check_only:
        written = []
        try:
            for destination, data, old in operations:
                if core._bytes(destination) != old:
                    raise ValueError('Mirror changed before collection completed')
                core._atomic(destination, data)
                written.append((destination, old, data))
            if core._bytes(state_file) != raw_state:
                raise ValueError('Collection state changed before collection completed')
            if subset is None or next_state != state:
                core._atomic(state_file, (json.dumps(next_state, indent=2) + '\n').encode())
        except Exception:
            for destination, old, data in reversed(written):
                if core._bytes(destination) != data:
                    continue
                if old is None:
                    destination.unlink()
                else:
                    core._atomic(destination, old)
            raise
    return {'selected_files': len(config['files']) if subset is None else len(subset), 'changed_files': len(operations),
            'mode': 'check' if check_only else 'collected',
            'source_write_count': 0}
