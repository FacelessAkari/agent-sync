"""Refresh only explicitly selected, read-only conversation exports.

The caller owns the workspace lock and remote trust checks. History owns the
repository lock. All inputs and referenced archives are checked before writes.
State is committed once, after all archives succeed. A crash can leave immutable
orphans (and a later refresh can duplicate them), but never deletes history.
"""
import math
from pathlib import Path
import stat

from . import core, history
from .codex_rpc import DEFAULT_LIMIT, RPC, bounded_read, command_argv, json_bytes, json_loads


JSON_LIMIT = 1024 * 1024
MAX_LIMIT = 16 * 1024 * 1024
MAX_SOURCES = 100
CODE_ROOT = Path(__file__).resolve().parent.parent
_METADATA = ('tool', 'source_session_id', 'project_id')


def _require(condition):
    if not condition:
        raise core.Error('Invalid selected-source data')


def _absolute(value):
    path = Path(value)
    _require(path.is_absolute() and '..' not in path.parts)
    return core._safe(path)


def _external(value, protected):
    _require(isinstance(value, str))
    path = _absolute(value)
    _require(not any(core._overlap(path, root) for root in protected))
    _require(path.is_file())
    return path


def _stamp(path):
    info = core._safe(path).lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stable_read(path, limit):
    """Check identity around two bounded reads, including their exact hashes."""
    before = _stamp(path)
    _require(before[4] <= limit)
    raw = bounded_read(path, limit)
    middle = _stamp(path)
    again = bounded_read(path, limit)
    after = _stamp(path)
    _require(before == middle == after and len(raw) == before[4]
             and len(again) == len(raw) and history.sha(raw) == history.sha(again))
    return raw


def _document(raw):
    # json.loads also accepts UTF-16/32 bytes; configuration is strictly UTF-8.
    document = json_loads(raw.decode('utf-8'))
    # Also reject overflow-to-infinity numbers and escaped lone surrogates.
    json_bytes(document)
    return document


def _version(document):
    _require(type(document['version']) is int and document['version'] == 1)


def _config(raw, repo, protected):
    document = _document(raw)
    history.fields(document, {'version', 'sources'}, {'limit_bytes'})
    _version(document)
    limit = document.get('limit_bytes', DEFAULT_LIMIT)
    _require(type(limit) is int and 1 <= limit <= MAX_LIMIT)
    entries = document['sources']
    _require(isinstance(entries, list) and len(entries) <= MAX_SOURCES)
    selected = []
    labels = set()
    for entry in entries:
        _require(isinstance(entry, dict))
        kind = entry.get('kind')
        if kind == 'file':
            history.fields(entry, {'id', 'kind', 'path', 'format'},
                           {'tool', 'source_id', 'project_id'})
        elif kind == 'codex':
            history.fields(entry, {'id', 'kind', 'thread_id', 'command_file'},
                           {'project_id', 'timeout_seconds', 'max_pages'})
        else:
            _require(False)
        label = entry['id']
        _require(isinstance(label, str) and core.NAME.fullmatch(label)
                 and label not in labels)
        labels.add(label)
        item = dict(entry)
        history.project_id(item.get('project_id'))
        if kind == 'file':
            _require(item['format'] in ('text', 'agy-result', 'transcript-v1'))
            if 'tool' in item:
                _require(isinstance(item['tool'], str) and item['tool'] in history.TOOLS)
            if 'source_id' in item:
                history.opaque_id(item['source_id'], nullable=True)
            if item['format'] == 'text':
                _require('tool' in item)
            if item['format'] == 'agy-result':
                _require(item.get('project_id') is None)
            item['path'] = _external(item['path'], protected)
        else:
            history.opaque_id(item['thread_id'])
            # normalize has no associated project contract for Codex exports.
            _require(item.get('project_id') is None)
            timeout = item.get('timeout_seconds', 30)
            _require(type(timeout) in (int, float) and 0 < timeout <= 300
                     and math.isfinite(timeout))
            pages = item.get('max_pages', history.MAX_PAGES)
            _require(type(pages) is int and 1 <= pages <= history.MAX_PAGES)
            command = _external(item['command_file'], protected)
            command_raw = _stable_read(command, 64 * 1024)
            argv = command_argv(repo, command)
            # Execute the validated snapshot, never rediscover or reread it later.
            _require(argv == _document(command_raw))
            _require(_stable_read(command, 64 * 1024) == command_raw)
            item.update(argv=argv, timeout_seconds=timeout, max_pages=pages)
        selected.append(item)
    return selected, limit


def _state(path):
    path = core._safe(path)
    if not path.exists():
        return {}
    document = _document(_stable_read(path, JSON_LIMIT))
    history.fields(document, {'version', 'sources'})
    _version(document)
    entries = document['sources']
    _require(isinstance(entries, dict) and len(entries) <= MAX_SOURCES)
    archives = set()
    for label, entry in entries.items():
        _require(isinstance(label, str) and core.NAME.fullmatch(label))
        history.fields(entry, {'fingerprint', 'archive_id'})
        _require(isinstance(entry['fingerprint'], str)
                 and core.HASH.fullmatch(entry['fingerprint']))
        archive_id = entry['archive_id']
        _require(isinstance(archive_id, str) and history.ID_RE.fullmatch(archive_id)
                 and archive_id not in archives)
        archives.add(archive_id)
    return entries


def _fingerprint(raw, source_format):
    if source_format == 'codex-export-v1':
        document = _document(raw)
        del document['captured_at']
        return history.sha(json_bytes(document))
    return history.sha(raw)


def _payload(item, raw, source_format, limit):
    history.checked_size(raw, limit)
    kwargs = {}
    if source_format == 'text':
        kwargs = {'tool': item['tool'], 'source_session_id': item.get('source_id'),
                  'project': item.get('project_id')}
    else:
        _document(raw)
    normalized = history.normalize(raw, source_format, **kwargs)
    if item['kind'] == 'file' and source_format != 'text':
        for config_key, normalized_key in (('tool', 'tool'),
                                           ('source_id', 'source_session_id'),
                                           ('project_id', 'project_id')):
            if config_key in item:
                _require(item[config_key] == normalized[normalized_key])
    history.checked_size(history.render(normalized), limit)
    return {'id': item['id'], 'raw': raw, 'format': source_format,
            'kwargs': kwargs, 'normalized': normalized,
            'fingerprint': _fingerprint(raw, source_format)}


def _codex(item, limit):
    thread_id = item['thread_id']
    with RPC(item['argv'], timeout=item['timeout_seconds'], max_bytes=limit) as rpc:
        metadata = rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False})
        _require(isinstance(metadata, dict) and isinstance(metadata.get('thread'), dict)
                 and metadata['thread'].get('id') == thread_id)
        document = {
            'schema': history.EXPORT_SCHEMA, 'thread_id': thread_id,
            'captured_at': history.now(), 'metadata': metadata,
            'turns_pages': history.fetch_pages(rpc, thread_id, 'turns',
                                              max_pages=item['max_pages'], limit=limit),
            'items_pages': history.fetch_pages(rpc, thread_id, 'items',
                                              max_pages=item['max_pages'], limit=limit),
        }
    return _payload(item, json_bytes(document), 'codex-export-v1', limit)


def _refresh(workspace, repo):
    workspace, repo = _absolute(workspace), _absolute(repo)
    code_root = core._safe(CODE_ROOT)
    _require(workspace.is_dir() and repo.is_dir())
    _require(not core._overlap(workspace, repo)
             and not core._overlap(workspace, code_root)
             and not core._overlap(repo, code_root))
    config_path = core._safe(workspace / 'sources.json')
    result = {'status': 'not_configured', 'sources': 0, 'archived': 0,
              'unchanged': 0, 'items': []}
    if not config_path.exists():
        return result
    state_path = core._safe(workspace / 'sources-state.json')
    protected = (workspace, repo, code_root, config_path, state_path)
    selected, limit = _config(_stable_read(config_path, JSON_LIMIT), repo, protected)
    state = _state(state_path)

    # Check every known reference, even if its fingerprint changed or its source
    # was removed. State is only a hint, never evidence that an archive is sound.
    previous = {}
    for label, entry in state.items():
        record, raw, _, normalized = history.read_archive(repo, entry['archive_id'], limit)
        _require(_fingerprint(raw, record['source_format']) == entry['fingerprint'])
        previous[label] = (record, normalized)

    # Validate all file content before any RPC, then collect all RPC exports
    # before the first write. Never interpret archived records as instructions.
    prepared = {}
    for item in selected:
        if item['kind'] == 'file':
            prepared[item['id']] = _payload(
                item, _stable_read(item['path'], limit), item['format'], limit)
    for item in selected:
        if item['kind'] == 'codex':
            prepared[item['id']] = _codex(item, limit)

    for item in prepared.values():
        item['reuse'] = False
        old = state.get(item['id'])
        if old is not None and old['fingerprint'] == item['fingerprint']:
            record, normalized = previous[item['id']]
            item['reuse'] = (record['source_format'] == item['format']
                             and all(normalized[key] == item['normalized'][key]
                                     for key in _METADATA))
            # Text metadata is configuration-owned. A changed identity must
            # create a new immutable version even when the raw bytes match.

    result.update(status='refreshed', sources=len(selected))
    new_state = {}
    for selected_item in selected:
        item = prepared[selected_item['id']]
        if item['reuse']:
            archive_id = state[item['id']]['archive_id']
            status = 'unchanged'
        else:
            created = history.create_archive(repo, item['raw'], item['format'],
                                             limit=limit, **item['kwargs'])
            archive_id = created['archive_id']
            _require(isinstance(archive_id, str) and history.ID_RE.fullmatch(archive_id))
            status = 'archived'
        new_state[item['id']] = {'fingerprint': item['fingerprint'], 'archive_id': archive_id}
        result[status] += 1
        result['items'].append({'id': item['id'], 'status': status, 'archive_id': archive_id})

    # No per-item commits or rollback/deletion of immutable archives.
    encoded = history.checked_size(json_bytes({'version': 1, 'sources': new_state}), JSON_LIMIT)
    core._atomic(state_path, encoded)
    return result


def refresh(workspace, repo):
    """Return only local labels, counts and archive IDs; never print diagnostics."""
    try:
        return _refresh(workspace, repo)
    except Exception:
        # Dependencies can include paths, source metadata or RPC details in
        # exceptions. Raise outside the handler so even exception context is
        # absent, rather than merely hiding a sensitive chained exception.
        pass
    raise core.Error('Selected-source refresh failed; immutable archives may remain')
