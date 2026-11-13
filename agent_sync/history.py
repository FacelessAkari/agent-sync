"""Independent phase-three CLI: python -m agent_sync.history --repo CONTENT ..."""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .core import Error, _atomic, _groups, _ignored_cache, _json, _lock, _mkdir, _safe
from .codex_rpc import DEFAULT_LIMIT, RPC, bounded_read, command_argv, json_bytes, json_loads


TOOLS = {'codex', 'antigravity', 'dsh'}
COVERAGES = {'full-export', 'partial', 'user-supplied'}
FORMATS = {'transcript-v1', 'codex-export-v1', 'agy-result', 'text'}
ARCHIVE_SCHEMA = 'agent-sync.history/v1'
TRANSCRIPT_SCHEMA = 'agent-sync.transcript/v1'
EXPORT_SCHEMA = 'agent-sync.codex-export/v1'
ID_RE = re.compile(r'[0-9a-f]{32}\Z')
PROJECT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
MAX_PAGES = 1000


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_size(raw, limit):
    if type(limit) is not int or limit < 1:
        raise Error('字节上限必须是正整数')
    if len(raw) > limit:
        raise Error('数据超过字节上限；拒绝创建或读取，不会截断')
    return raw


def opaque_id(value, nullable=False):
    if nullable and value is None:
        return value
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise Error('来源/线程 ID 必须为 1..512 字符的无控制字符字符串')
    # Opaque metadata, NEVER a filename, shell fragment, or executable command.
    return value


def project_id(value):
    if value is not None and (not isinstance(value, str) or not PROJECT_RE.fullmatch(value)):
        raise Error('project_id 必须是简短逻辑标识，不得是本机路径')
    return value


def fields(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= set(value)
            or set(value) - set(required) - set(optional)):
        raise Error('输入 schema 字段缺失、未知或对象类型错误')


def message_v1(value):
    fields(value, {'role', 'text'}, {'phase'})
    if value['role'] not in ('user', 'assistant') or not isinstance(value['text'], str):
        raise Error('消息必须是 user/assistant 及字符串 text')
    if 'phase' in value:
        if value['role'] != 'assistant' or value['phase'] not in ('commentary', 'final_answer'):
            raise Error('phase 仅允许 assistant 的 commentary/final_answer')
    return dict(value)


def record_type(value):
    if isinstance(value, dict) and isinstance(value.get('type'), str):
        return value['type']
    return 'untyped-' + type(value).__name__


def page_items(result, kind):
    if not isinstance(result, dict) or 'nextCursor' not in result:
        raise Error('分页响应缺少 nextCursor；不能证明完整性')
    # Accommodate the named-list and data-list response envelopes, never merge them.
    keys = [key for key in ('data', kind) if key in result]
    if len(keys) != 1 or not isinstance(result[keys[0]], list):
        raise Error('分页响应列表结构不支持或有歧义')
    cursor = result['nextCursor']
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise Error('nextCursor 必须为非空字符串或 null')
    return result[keys[0]], cursor


def validate_pages(pages, kind):
    if not isinstance(pages, list) or not pages or len(pages) > MAX_PAGES:
        raise Error('分页记录为空或超过页数上限')
    expected = None
    seen = set()
    flattened = []
    for index, page in enumerate(pages):
        fields(page, {'cursor', 'result'})
        if page['cursor'] != expected:
            raise Error('分页 cursor 链断裂')
        items, next_cursor = page_items(page['result'], kind)
        flattened.extend(items)
        if next_cursor is None:
            if index != len(pages) - 1:
                raise Error('终止页后仍有额外分页')
        else:
            if next_cursor in seen:
                raise Error('检测到循环 cursor')
            seen.add(next_cursor)
            if index == len(pages) - 1:
                raise Error('分页未完成；不能标为 full-export')
        expected = next_cursor
    return flattened


def convert_response_item(item, omitted):
    """Conservative allowlist. No raw item is ever passed through to injection."""
    kind = record_type(item)
    if not isinstance(item, dict) or item.get('type') != 'message':
        omitted['item:' + kind] += 1
        return None
    role = item.get('role')
    if role not in ('user', 'assistant'):
        omitted['message:non-dialogue-role:' + str(role)] += 1
        return None
    allowed = {'type', 'id', 'role', 'content', 'phase', 'status', 'end_turn'}
    unknown = sorted(set(item) - allowed)
    reasons = []
    if unknown:
        reasons.extend('unknown-field:' + key for key in unknown)
    phase = item.get('phase')
    if phase is not None and (role != 'assistant' or phase not in ('commentary', 'final_answer')):
        reasons.append('unsupported-phase')
    content = item.get('content')
    text = []
    if not isinstance(content, list) or not content:
        reasons.append('missing-content')
    else:
        expected = 'input_text' if role == 'user' else 'output_text'
        for block in content:
            if not isinstance(block, dict):
                reasons.append('content:' + record_type(block))
                continue
            block_type = record_type(block)
            if block.get('type') != expected or not isinstance(block.get('text'), str):
                reasons.append('content:' + block_type)
                continue
            if set(block) - {'type', 'text', 'annotations', 'logprobs'}:
                reasons.append('content:unknown-fields:' + block_type)
                continue
            if block.get('annotations') not in (None, []) or block.get('logprobs') not in (None, []):
                reasons.append('content:annotations-or-logprobs:' + block_type)
                continue
            text.append(block['text'])
    if reasons:
        omitted['message:excluded'] += 1
        for reason in reasons:
            omitted['message-detail:' + reason] += 1
        return None
    joined = ''.join(text)
    if not joined:
        omitted['message:empty'] += 1
        return None
    for key in ('id', 'status', 'end_turn'):
        if key in item:
            omitted['message-metadata:' + key] += 1
    if len(text) > 1:
        omitted['message-metadata:content-block-boundaries'] += 1
    message = {'role': role, 'text': joined}
    if phase is not None:
        message['phase'] = phase
    return message


def convert_display_entry(entry, omitted):
    """ThreadItemEntry -> dialogue; never recursively search unknown structures."""
    if not isinstance(entry, dict) or not ({'item', 'turnId'} & set(entry)):
        return convert_response_item(entry, omitted)
    reasons = []
    allowed = {'turnId', 'item', 'startedAtMs', 'completedAtMs'}
    reasons.extend('entry:unknown-field:' + key for key in sorted(set(entry) - allowed))
    if not isinstance(entry.get('turnId'), str):
        reasons.append('entry:turnId')
    for key in ('startedAtMs', 'completedAtMs'):
        value = entry.get(key)
        if value is not None and (type(value) is not int or not -(2**63) <= value < 2**63):
            reasons.append('entry:' + key)
    item = entry.get('item')
    kind = record_type(item)
    message = None
    if not isinstance(item, dict) or kind not in ('userMessage', 'agentMessage'):
        reasons.append('item:' + kind)
    else:
        user = kind == 'userMessage'
        allowed_item = ({'type', 'id', 'clientId', 'content'} if user else
                        {'type', 'id', 'text', 'phase', 'delivery', 'memoryCitation', 'questions'})
        reasons.extend('unknown-field:' + key for key in sorted(set(item) - allowed_item))
        if not isinstance(item.get('id'), str):
            reasons.append('invalid-id')
        if user:
            if item.get('clientId') is not None and not isinstance(item['clientId'], str):
                reasons.append('invalid-clientId')
            content = item.get('content')
            texts = []
            if not isinstance(content, list) or not content:
                reasons.append('missing-content')
            else:
                for block in content:
                    if not isinstance(block, dict) or block.get('type') != 'text':
                        reasons.append('content:' + record_type(block))
                        continue
                    if set(block) - {'type', 'text', 'text_elements'}:
                        reasons.append('content:unknown-fields')
                    if not isinstance(block.get('text'), str):
                        reasons.append('content:invalid-text')
                    else:
                        texts.append(block['text'])
                    if block.get('text_elements', []) != []:
                        reasons.append('content:text_elements')
            message = {'role': 'user', 'text': ''.join(texts)}
            if len(texts) > 1:
                omitted['display-metadata:content-block-boundaries'] += 1
        else:
            phase = item.get('phase')
            if phase not in (None, 'commentary', 'final_answer'):
                reasons.append('unsupported-phase')
            if item.get('delivery') not in (None, 'async'):
                reasons.append('unsupported-delivery')
            citation = item.get('memoryCitation')
            if citation is not None and citation != {'entries': [], 'threadIds': []}:
                reasons.append('memoryCitation')
            if item.get('questions') not in (None, []):
                reasons.append('questions')
            if not isinstance(item.get('text'), str):
                reasons.append('invalid-text')
            else:
                message = {'role': 'assistant', 'text': item['text']}
                if phase is not None:
                    message['phase'] = phase
        for key in ('id', 'clientId', 'delivery', 'memoryCitation', 'questions'):
            if key in item:
                omitted['display-metadata:' + key] += 1
    for key in ('turnId', 'startedAtMs', 'completedAtMs'):
        if key in entry:
            omitted['display-metadata:' + key] += 1
    if reasons:
        omitted['display:excluded'] += 1
        for reason in reasons:
            omitted['display-detail:' + reason] += 1
        return None
    if message is None or not message['text']:
        omitted['display:empty'] += 1
        return None
    return message


def normalize(raw, source_format, *, tool=None, source_session_id=None, project=None):
    if source_format not in FORMATS:
        raise Error('未知来源格式')
    omitted = Counter()
    messages = []
    coverage = 'user-supplied'
    if source_format == 'text':
        if tool not in TOOLS:
            raise Error('纯文本导入必须显式指定 --tool')
        opaque_id(source_session_id, nullable=True)
        try:
            text = raw.decode('utf-8')
        except UnicodeError as exc:
            raise Error('纯文本必须为 UTF-8') from exc
        # No guessed role: unstructured text is an archive/handoff, not injectable dialogue.
        omitted['text:unstructured-user-export'] += 1
        project_id(project)
        return {'tool': tool, 'source_session_id': source_session_id,
                'project_id': project, 'coverage': coverage, 'messages': [],
                'omitted': dict(omitted), 'unstructured_text': text}
    doc = json_loads(raw)
    if source_format == 'transcript-v1':
        fields(doc, {'schema', 'tool', 'source_session_id', 'coverage', 'messages'},
               {'project_id', 'records'})
        if doc['schema'] != TRANSCRIPT_SCHEMA or doc['tool'] not in TOOLS:
            raise Error('transcript schema/tool 不支持')
        if doc['coverage'] not in COVERAGES or not isinstance(doc['messages'], list):
            raise Error('transcript coverage/messages 无效')
        tool = doc['tool']
        source_session_id = opaque_id(doc['source_session_id'], nullable=True)
        project = project_id(doc.get('project_id'))
        coverage = doc['coverage']
        messages = [message_v1(message) for message in doc['messages']]
        records = doc.get('records', [])
        if not isinstance(records, list):
            raise Error('records 必须是惰性 JSON 数据数组')
        for record in records:
            omitted['raw-record:' + record_type(record)] += 1
    elif source_format == 'agy-result':
        if (not isinstance(doc, dict) or not {'conversation_id', 'status', 'response', 'usage'} <= set(doc)
                or not isinstance(doc['status'], str) or not isinstance(doc['response'], str)):
            raise Error('AGY 单次结果必须含 conversation_id/status/response/usage，response 必须为文本')
        tool = 'antigravity'
        source_session_id = opaque_id(doc['conversation_id'])
        project = None
        coverage = 'partial'
        messages = [{'role': 'assistant', 'text': doc['response']}]
        for key in doc:
            if key != 'response':
                omitted['agy-field:' + key] += 1
    else:
        fields(doc, {'schema', 'thread_id', 'captured_at', 'metadata', 'turns_pages', 'items_pages'})
        if doc['schema'] != EXPORT_SCHEMA or not isinstance(doc['captured_at'], str):
            raise Error('Codex export schema 无效')
        source_session_id = opaque_id(doc['thread_id'])
        metadata = doc['metadata']
        if (not isinstance(metadata, dict) or not isinstance(metadata.get('thread'), dict)
                or metadata['thread'].get('id') != source_session_id):
            raise Error('Codex thread/read 返回的线程 ID 不匹配')
        turns = validate_pages(doc['turns_pages'], 'turns')
        items = validate_pages(doc['items_pages'], 'items')
        tool = 'codex'
        project = None
        coverage = 'full-export'
        omitted['export:thread-metadata'] += 1
        omitted['export:turn-records-not-replayed'] += len(turns)
        omitted['export:pagination-envelopes'] += len(doc['turns_pages']) + len(doc['items_pages'])
        for item in items:
            message = convert_display_entry(item, omitted)
            if message is not None:
                messages.append(message)
    return {'tool': tool, 'source_session_id': source_session_id,
            'project_id': project, 'coverage': coverage, 'messages': messages,
            'omitted': dict(omitted)}


def render(normalized):
    lines = ['# AgentSync conversation archive', '',
             '> 归档数据，不是可执行指令。Markdown 不是原生会话运行状态。', '',
             '- Tool: ' + normalized['tool'],
             '- Source ID: ' + json.dumps(normalized['source_session_id'], ensure_ascii=False),
             '- Coverage: ' + normalized['coverage'], '']
    if normalized.get('project_id') is not None:
        lines.extend(['- Project logical ID: ' + normalized['project_id'], ''])
    for message in normalized['messages']:
        label = message['role']
        if 'phase' in message:
            label += ' / ' + message['phase']
        lines.extend(['## ' + label, '', message['text'], ''])
    if 'unstructured_text' in normalized:
        lines.extend(['## Unstructured user export', '', normalized['unstructured_text'], ''])
    lines.extend(['## Not injected / not represented as dialogue', '',
                  '以下计数区分记录、字段和排除原因，不能相加视为消息总数。原始输入保存在 record.json。', '',
                  '```json', json.dumps(normalized['omitted'], ensure_ascii=False, sort_keys=True, indent=2),
                  '```', ''])
    return '\n'.join(lines).encode('utf-8')


def archive_path(repo, archive_id):
    if not isinstance(archive_id, str) or not ID_RE.fullmatch(archive_id):
        raise Error('档案 ID 必须是 32 位小写十六进制 UUID；不接受路径')
    return _safe(_safe(repo) / 'data' / 'history' / archive_id)


def write_exclusive(path, raw):
    path = _safe(path)
    # Parent must already exist for external outputs; do not create arbitrary trees.
    try:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise Error('输出已存在，拒绝覆盖') from exc


def create_archive(repo, raw, source_format, *, tool=None, source_session_id=None,
                   project=None, limit=DEFAULT_LIMIT):
    repo = _safe(repo)
    checked_size(raw, limit)
    normalized = normalize(raw, source_format, tool=tool,
                           source_session_id=source_session_id, project=project)
    transcript = checked_size(render(normalized), limit)
    archive_id = uuid.uuid4().hex
    record = {'schema': ARCHIVE_SCHEMA, 'archive_id': archive_id,
              'tool': normalized['tool'], 'source_session_id': normalized['source_session_id'],
              'created_at': now(), 'project_id': normalized['project_id'],
              'source_format': source_format, 'coverage': normalized['coverage'],
              'source': {'encoding': 'base64', 'data': base64.b64encode(raw).decode('ascii'),
                         'sha256': sha(raw)},
              'transcript_sha256': sha(transcript)}
    record['record_sha256'] = sha(json_bytes(record))
    encoded = checked_size(json_bytes(record), limit)
    with _lock(repo):
        _groups(_json(repo / 'manifest.json'))
        _mkdir(repo / 'data' / 'history')
        destination = archive_path(repo, archive_id)
        try:
            destination.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise Error('UUID 目标已存在，拒绝覆盖；请重试') from exc
        # Exclusive files: even a partial/crashed archive is never overwritten.
        write_exclusive(destination / 'transcript.md', transcript)
        write_exclusive(destination / 'record.json', encoded)
    return {'archive_id': archive_id, 'tool': normalized['tool'],
            'source_session_id': normalized['source_session_id'],
            'coverage': normalized['coverage'], 'status': 'archived'}


def import_file(repo, input_path, source_format, **kwargs):
    limit = kwargs.get('limit', DEFAULT_LIMIT)
    if source_format != 'text' and any(kwargs.get(k) is not None for k in ('tool', 'source_session_id', 'project')):
        raise Error('JSON 输入元数据来自文档本身，不接受 CLI 覆盖；这些选项仅用于 text')
    return create_archive(repo, bounded_read(input_path, limit), source_format, **kwargs)


def read_archive(repo, archive_id, limit=DEFAULT_LIMIT):
    directory = archive_path(repo, archive_id)
    if not directory.is_dir() or {
            p.name for p in directory.iterdir() if not _ignored_cache(p.name)
    } != {'record.json', 'transcript.md'}:
        raise Error('档案目录不完整或包含非预期文件')
    record_raw = bounded_read(directory / 'record.json', limit)
    record = json_loads(record_raw)
    required = {'schema', 'archive_id', 'tool', 'source_session_id', 'created_at',
                'project_id', 'source_format', 'coverage', 'source',
                'transcript_sha256', 'record_sha256'}
    fields(record, required)
    if record['schema'] != ARCHIVE_SCHEMA or record['archive_id'] != archive_id:
        raise Error('档案 schema/ID 不匹配')
    if not isinstance(record['created_at'], str):
        raise Error('档案 created_at 无效')
    unsigned = dict(record)
    del unsigned['record_sha256']
    if sha(json_bytes(unsigned)) != record['record_sha256']:
        raise Error('record SHA256 校验失败')
    source = record['source']
    fields(source, {'encoding', 'data', 'sha256'})
    if source['encoding'] != 'base64' or not isinstance(source['data'], str):
        raise Error('原始输入编码无效')
    try:
        raw = base64.b64decode(source['data'], validate=True)
    except (ValueError, UnicodeError) as exc:
        raise Error('原始输入 base64 无效') from exc
    checked_size(raw, limit)
    if sha(raw) != source['sha256']:
        raise Error('原始输入 SHA256 校验失败')
    normalized = normalize(raw, record['source_format'], tool=record['tool'],
                           source_session_id=record['source_session_id'], project=record['project_id'])
    for key in ('tool', 'source_session_id', 'project_id', 'coverage'):
        if record[key] != normalized[key]:
            raise Error('档案派生元数据与原始输入不一致')
    transcript = bounded_read(directory / 'transcript.md', limit)
    if sha(transcript) != record['transcript_sha256'] or transcript != render(normalized):
        raise Error('transcript SHA256 或派生内容校验失败')
    return record, raw, transcript, normalized


def list_archives(repo, limit=DEFAULT_LIMIT):
    root = _safe(_safe(repo) / 'data' / 'history')
    if not root.exists():
        return []
    result = []
    with os.scandir(root) as entries:
        for entry in entries:
            if _ignored_cache(entry.name):
                continue
            if len(result) >= 10000:
                raise Error('档案数量超过列出上限 10000')
            record, _, _, _ = read_archive(repo, entry.name, limit)
            result.append({key: record[key] for key in
                           ('archive_id', 'tool', 'source_session_id', 'created_at', 'coverage', 'project_id')})
    return sorted(result, key=lambda row: (row['created_at'], row['archive_id']))


def export_archive(repo, archive_id, output, kind='markdown', limit=DEFAULT_LIMIT):
    record, raw, transcript, _ = read_archive(repo, archive_id, limit)
    if kind == 'markdown':
        data = transcript
    elif kind == 'source':
        data = raw
    elif kind == 'record':
        data = json_bytes(record)
    else:
        raise Error('未知外部导出类型')
    write_exclusive(output, data)
    return {'archive_id': archive_id, 'status': 'exported', 'kind': kind}


def fetch_pages(rpc, thread_id, kind, *, max_pages=MAX_PAGES, limit=DEFAULT_LIMIT):
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise Error('max-pages 必须在 1..1000')
    pages = []
    seen = set()
    cursor = None
    total = 0
    for _ in range(max_pages):
        params = {'threadId': thread_id, 'limit': 100, 'sortDirection': 'asc'}
        if cursor is not None:
            params['cursor'] = cursor
        result = rpc.call('thread/' + kind + '/list', params)
        _, next_cursor = page_items(result, kind)
        page = {'cursor': cursor, 'result': result}
        total += len(json_bytes(page))
        if total > limit:
            raise Error('分页总数据超过字节上限；未创建档案')
        pages.append(page)
        if next_cursor is None:
            return pages
        if next_cursor in seen:
            raise Error('检测到循环 cursor；未创建档案')
        seen.add(next_cursor)
        cursor = next_cursor
    raise Error('达到分页上限但未结束；未创建档案')


def codex_export(repo, thread_id, *, command_file=None, timeout=30,
                 limit=DEFAULT_LIMIT, max_pages=MAX_PAGES, output=None):
    opaque_id(thread_id)
    if output is not None and _safe(output).exists():
        raise Error('输出已存在，拒绝覆盖')
    argv = command_argv(repo, command_file)
    with RPC(argv, timeout=timeout, max_bytes=limit) as rpc:
        metadata = rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False})
        if (not isinstance(metadata, dict) or not isinstance(metadata.get('thread'), dict)
                or metadata['thread'].get('id') != thread_id):
            raise Error('thread/read 返回错误线程；未继续读取')
        document = {'schema': EXPORT_SCHEMA, 'thread_id': thread_id, 'captured_at': now(),
                    'metadata': metadata,
                    'turns_pages': fetch_pages(rpc, thread_id, 'turns', max_pages=max_pages, limit=limit),
                    'items_pages': fetch_pages(rpc, thread_id, 'items', max_pages=max_pages, limit=limit)}
    raw = checked_size(json_bytes(document), limit)
    result = create_archive(repo, raw, 'codex-export-v1', limit=limit)
    if output is not None:
        try:
            write_exclusive(output, raw)
        except (Error, OSError) as exc:
            raise Error('档案已创建，ID=' + result['archive_id'] + '；外部输出失败，未覆盖已有文件') from exc
    return result


def injection_items(normalized):
    items = []
    omitted = Counter(normalized['omitted'])
    for message in normalized['messages']:
        if not message['text']:
            omitted['message:empty'] += 1
            continue
        role = message['role']
        item = {'type': 'message', 'role': role, 'content': [
            {'type': 'input_text' if role == 'user' else 'output_text', 'text': message['text']} ]}
        if 'phase' in message:
            item['phase'] = message['phase']
        items.append(item)
    return items, dict(sorted(omitted.items()))


def read_restore_map(path, limit):
    if not path.exists():
        return {'schema': 'agent-sync.history-restores/v1', 'restores': {}}
    value = json_loads(bounded_read(path, limit))
    fields(value, {'schema', 'restores'})
    if value['schema'] != 'agent-sync.history-restores/v1' or not isinstance(value['restores'], dict):
        raise Error('本地恢复映射结构错误')
    for archive_id, events in value['restores'].items():
        if not ID_RE.fullmatch(archive_id) or not isinstance(events, list):
            raise Error('本地恢复映射条目错误')
        for event in events:
            fields(event, {'thread_id', 'status', 'at'})
            opaque_id(event['thread_id'])
            if event['status'] not in ('created', 'injected', 'injection_failed_or_unknown') or not isinstance(event['at'], str):
                raise Error('本地恢复事件错误')
    return value


def restore_event(repo, archive_id, thread_id, status, limit):
    repo = _safe(repo)
    with _lock(repo):
        path = _safe(repo / '.agent-sync' / 'history-restores.json')
        value = read_restore_map(path, limit)
        value['restores'].setdefault(archive_id, []).append(
            {'thread_id': thread_id, 'status': status, 'at': now()})
        _atomic(path, checked_size(json_bytes(value), limit))


def resume_command(thread_id):
    # No shell execution. Only render a pasteable command for a conservative ID alphabet.
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,511}', thread_id):
        return 'codex resume ' + thread_id
    return None


def codex_restore(repo, archive_id, cwd, *, apply=False, command_file=None,
                  timeout=30, limit=DEFAULT_LIMIT):
    record, _, _, normalized = read_archive(repo, archive_id, limit)
    if record['tool'] != 'codex':
        raise Error('只支持同款 Codex 档案恢复；AGY/DSH 请导出 Markdown 交接，不制造原生导入命令')
    target = _safe(cwd)
    if not target.is_dir():
        raise Error('cwd 必须是用户明确提供且存在的本机项目目录')
    items, omitted = injection_items(normalized)
    plan = {'archive_id': archive_id, 'mode': 'context-to-new-thread',
            'preserves_source_id': False, 'full_native_restore': False,
            'source_session_id': record['source_session_id'], 'coverage': record['coverage'],
            'local_cwd': str(target), 'injectable_messages': len(items),
            'items': items, 'omitted': omitted,
            'note': '恢复文本上下文到新线程；注入未必创建 UI-visible turn，可能不在 UI 历史或再次分页导出中出现。不执行历史工具或注入 system/developer、内部推理。',
            'status': 'plan', 'can_apply': bool(items)}
    if not apply:
        return plan
    if not items:
        raise Error('无可注入消息，拒绝创建新会话')
    # Preflight size before thread/start; reserve room for the returned thread ID.
    checked_size(json_bytes({'jsonrpc': '2.0', 'id': 3, 'method': 'thread/inject_items',
                             'params': {'threadId': 'x' * 512, 'items': items}}) + b' ' * 4096, limit)
    repo = _safe(repo)
    with _lock(repo):
        read_restore_map(_safe(repo / '.agent-sync' / 'history-restores.json'), limit)
    argv = command_argv(repo, command_file)
    new_id = None
    try:
        with RPC(argv, timeout=timeout, max_bytes=limit) as rpc:
            try:
                started = rpc.call('thread/start', {'cwd': str(target)})
            except Error as exc:
                raise Error('thread/start 未确认成功；可能已创建未知 ID 的线程，禁止盲目重试：' + str(exc)) from exc
            if not isinstance(started, dict) or not isinstance(started.get('thread'), dict):
                raise Error('thread/start 响应不支持；可能已创建线程但无法取得 ID，请本机检查')
            try:
                new_id = opaque_id(started['thread'].get('id'))
            except Error as exc:
                raise Error('thread/start 返回无效 ID；可能已创建线程，请本机检查') from exc
            try:
                restore_event(repo, archive_id, new_id, 'created', limit)
            except (Error, OSError) as exc:
                raise Error('新 threadID=' + new_id + '；本地映射写入失败，未执行 inject，未删除线程') from exc
            try:
                # One injection only. Never turn/start, resume.history, or resume.path.
                rpc.call('thread/inject_items', {'threadId': new_id, 'items': items})
            except Error as exc:
                try:
                    restore_event(repo, archive_id, new_id, 'injection_failed_or_unknown', limit)
                except (Error, OSError):
                    pass
                raise Error('新 threadID=' + new_id + '；inject 失败或结果未知，可能已有内容；'
                            '未重试、未删除线程。' + str(exc)) from exc
            try:
                restore_event(repo, archive_id, new_id, 'injected', limit)
            except (Error, OSError) as exc:
                raise Error('新 threadID=' + new_id + '；inject 已确认成功，但最终本地映射更新失败；勿重复导入') from exc
    except OSError as exc:
        raise Error('恢复期间本地 I/O 失败；已知新 threadID=' + str(new_id) + '；未删除线程') from exc
    return {'archive_id': archive_id, 'status': 'context-restored-to-new-thread',
            'new_thread_id': new_id, 'preserves_source_id': False, 'full_native_restore': False,
            'injected_messages': len(items), 'omitted': omitted,
            'resume_argv': ['codex', 'resume', new_id], 'resume_command': resume_command(new_id),
            'note': '仅恢复文本上下文到新线程；注入内容未必出现在 UI 历史，可能无法再次分页导出，不保证无损往返或完整原生迁移。'}


def capabilities():
    return {
        'codex': {
            'archive': 'supported: explicit-ID public metadata + paginated persisted display turns/items, or transcript-v1',
            'full_export_scope': 'publicly visible pagination only; not all model-visible history or raw rollout fidelity',
            'roundtrip': 'not lossless: injected context may create no UI-visible turn and may be absent from UI and subsequent pagination',
            'original_id_cross_device': 'unsupported: no verified supported migration API',
            'new_session_context': 'supported API: thread/start + one thread/inject_items, text-only allowlist',
            'local_resume': 'existing local ID only: codex resume ID / thread/resume(threadId, excludeTurns=true)',
            'native_runtime_state': 'not migrated'},
        'antigravity': {
            'archive': 'transcript-v1, UTF-8 text, or --print single-result JSON (always partial)',
            'original_id_cross_device': 'unsupported', 'new_session_context': 'unsupported: no verified import API',
            'local_resume': 'existing local ID only: --conversation ID',
            'handoff': 'export --kind markdown; manual handoff is not native migration'},
        'dsh': {
            'archive': 'transcript-v1 or UTF-8 user-exported text',
            'original_id_cross_device': 'unsupported', 'new_session_context': 'unsupported: no verified import API',
            'local_resume': 'headless --session-id ID requires an existing persisted local session',
            'handoff': 'export --kind markdown; do not copy/write .db or session.v4.jsonl.zstd'}
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--repo', required=True, help='已初始化的 AgentSync 内容仓库')
    result.add_argument('--max-bytes', type=int, default=DEFAULT_LIMIT,
                        help='单文件、RPC 单方向总量和档案文件字节上限，默认 16 MiB')
    sub = result.add_subparsers(dest='action', required=True)
    sub.add_parser('capability')
    sub.add_parser('list')
    imp = sub.add_parser('import')
    imp.add_argument('--input', required=True)
    imp.add_argument('--format', choices=sorted(FORMATS), required=True)
    imp.add_argument('--tool', choices=sorted(TOOLS))
    imp.add_argument('--source-id')
    imp.add_argument('--project-id')
    for name in ('show', 'verify', 'export'):
        command = sub.add_parser(name)
        command.add_argument('archive_id')
        if name == 'show':
            command.add_argument('--kind', choices=['record', 'markdown'], default='markdown')
        if name == 'export':
            command.add_argument('--output', required=True)
            command.add_argument('--kind', choices=['record', 'markdown', 'source'], default='markdown')
    for name in ('codex-export', 'codex-restore'):
        command = sub.add_parser(name)
        command.add_argument('--codex-command-file')
        command.add_argument('--timeout', type=float, default=30)
        if name == 'codex-export':
            command.add_argument('--thread-id', required=True)
            command.add_argument('--max-pages', type=int, default=MAX_PAGES)
            command.add_argument('--output', help='可选：独占创建原始 codex-export-v1 JSON')
        else:
            command.add_argument('archive_id')
            command.add_argument('--cwd', required=True)
            command.add_argument('--apply', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        repo = _safe(args.repo)
        limit = args.max_bytes
        checked_size(b'', limit)
        if args.action == 'capability':
            value = capabilities()
        elif args.action == 'list':
            value = list_archives(repo, limit)
        elif args.action == 'import':
            value = import_file(repo, args.input, args.format, tool=args.tool,
                                source_session_id=args.source_id, project=args.project_id, limit=limit)
        elif args.action in ('show', 'verify'):
            record, _, transcript, _ = read_archive(repo, args.archive_id, limit)
            if args.action == 'verify':
                value = {'archive_id': args.archive_id, 'status': 'verified'}
            elif args.kind == 'markdown':
                sys.stdout.write(transcript.decode('utf-8'))
                return 0
            else:
                value = record
        elif args.action == 'export':
            value = export_archive(repo, args.archive_id, args.output, args.kind, limit)
        elif args.action == 'codex-export':
            value = codex_export(repo, args.thread_id, command_file=args.codex_command_file,
                                 timeout=args.timeout, limit=limit, max_pages=args.max_pages, output=args.output)
        else:
            value = codex_restore(repo, args.archive_id, args.cwd, apply=args.apply,
                                  command_file=args.codex_command_file, timeout=args.timeout, limit=limit)
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (Error, OSError, ValueError, TypeError, RecursionError) as exc:
        # RPC errors are already sanitized. Never print subprocess stderr or argv.
        message = str(exc) if isinstance(exc, Error) else '本地文件、数据结构或编码错误；操作未完整完成'
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
