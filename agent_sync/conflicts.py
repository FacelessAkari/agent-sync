"""Device-local policy, reviewed three-way merges, and resumable per-file writes."""
from pathlib import Path
import json

from . import core


TEXT_TYPES = {'.md', '.txt'}
MAX_FILE = 64 * 1024


def policy(repo, mode=None):
    repo = core._safe(repo)
    path = repo / '.agent-sync/conflict-policy.json'
    if mode is not None:
        if mode not in {'manual', 'codex'}:
            raise core.Error('冲突模式无效')
        with core._lock(repo):
            core._load(repo)
            core._save(path, {'version': 1, 'mode': mode})
    current = core._json(path) if core._safe(path).exists() else {'version': 1, 'mode': 'manual'}
    if current not in ({'version': 1, 'mode': 'manual'}, {'version': 1, 'mode': 'codex'}):
        raise core.Error('本机冲突策略格式无效')
    return current['mode']


REASONS = {
    'manual_type': '这类文件需要人工比对。',
    'overlapping_edits': '两端修改了同一处，或无法生成保留两边改动的候选。',
    'no_shared_version': '没有找到两边上次同步的共同原文。',
    'size_limit': '文件超过自动审阅大小限制。',
    'codex_requested_review': 'Codex 判断两边改动需要你决定。',
    'unsupported_input': '内容超过审阅限制，或含有不宜发送的信息。',
    'codex_unavailable': '本机 Codex 暂时不可用，将在下一轮重试。',
    'retry_next_run': '已排入下一轮自动审阅。',
    'skill_conflict': '本机和资料库都改了这个技能，请比对后决定如何安装。',
    'skill_install': '本机技能和资料库版本不同，需要审阅安装。',
}


def pending(repo, rows, versions=None):
    """The scheduler logs only counts. The local report identifies files to review."""
    folder = repo / '.agent-sync/conflicts'
    path = folder / 'pending.json'
    report = {'version': 1, 'files': rows}
    if rows or core._safe(path).exists():
        if not path.exists() or core._json(path) != report:
            core._save(path, report)
        lines = ['AgentSync 同步待处理', '', '待处理文件数：' + str(len(rows))]
        for row in rows:
            name = row['group'] + (' / ' + row['path'] if row['path'] else '')
            lines.extend(('', name, REASONS[row['reason']]))
            lines.extend(('本机文件：' + row['local_file'], '资料库文件：' + row['repository_file']))
        if rows:
            lines.extend(('', '请在发起同步的设备上，打开 Codex 并输入：',
                          '“请查看 AgentSync 本机待处理清单，逐项告诉我两边改了什么。”'))
        message = ('\n'.join(lines) + '\n').encode('utf-8-sig')
        if core._bytes(folder / 'pending.txt') != message:
            core._atomic(folder / 'pending.txt', message)
        from .review_notice import notify_pending
        signature = core._digest(json.dumps([rows, versions or []], ensure_ascii=True,
                                             sort_keys=True).encode())
        return notify_pending(repo, rows, signature)
    return 'clear'


def _bound_paths(group, bound, relative, repo):
    return (core._target(Path(bound), relative),
            core._target(repo / group['path'], relative))


def _fingerprint(*fields):
    return core._digest(b'\x00'.join(value.encode('utf-8') for value in fields))


def resolve(workspace, repo, rows, groups, bindings, snapshots, before_write, counters):
    from . import conflict_codex, conflict_merge

    by_name = {group['name']: group for group in groups}
    reasons = {}
    remaining_reviews = 4
    for row in rows:
        if row['action'] != 'conflict':
            continue
        name, relative = row['group'], row['path']
        key = (name, relative)
        group = by_name[name]
        local, remote = _bound_paths(group, bindings[name], relative, repo)
        if group['category'] not in {'rules', 'memory', 'personas'} or remote.suffix.lower() not in TEXT_TYPES:
            reasons[key] = 'skill_conflict' if group['category'] == 'skills' else 'manual_type'
            continue
        remote_hash, local_hash, base_hash = (side.get(relative) for side in snapshots[name])
        if None in (remote_hash, local_hash, base_hash):
            reasons[key] = 'no_shared_version'
            continue
        if max(core._safe(local).stat().st_size, core._safe(remote).stat().st_size) > MAX_FILE:
            reasons[key] = 'size_limit'
            continue
        left, right = core._bytes(local), core._bytes(remote)
        if core._digest(left) != local_hash or core._digest(right) != remote_hash:
            raise core.Error('审阅前文件已变化，请重新核对')
        identity = _fingerprint(name, relative, bindings[name], group['path'], group['kind'], group['category'])
        directory = repo / '.agent-sync/conflicts' / identity
        record = directory / 'decision.json'
        previous = core._json(record) if core._safe(record).is_file() else {}
        if not isinstance(previous, dict):
            raise core.Error('上次合并记录格式无效')
        case_id = _fingerprint(identity, base_hash, local_hash, remote_hash)
        if previous.get('id') == case_id and previous.get('decision') == 'review':
            reasons[key] = 'codex_requested_review'
            continue
        # A crash can leave the local half written. Finish only the exact approved
        # pair and verify the saved bytes before reusing the proposal.
        resume = (previous.get('decision') == 'approve' and previous.get('base') == base_hash
                  and isinstance(previous.get('id'), str) and core.HASH.fullmatch(previous['id'])
                  and isinstance(previous.get('local'), str) and core.HASH.fullmatch(previous['local'])
                  and previous['id'] == _fingerprint(identity, base_hash, previous['local'], remote_hash)
                  and previous.get('remote') == remote_hash and local_hash in
                  (previous.get('local'), previous.get('merged')))
        if resume:
            saved = directory / previous['id']
            originals = [core._bytes(saved / f) for f in ('base', 'local', 'repository')]
            merged = core._bytes(saved / 'merged')
            if (any(x is None for x in originals) or merged is None
                    or [core._digest(x) for x in originals] != [base_hash, previous['local'], remote_hash]
                    or core._digest(merged) != previous.get('merged')
                    or conflict_merge.make_candidate(*originals) != merged):
                raise core.Error('上次合并的本机备份校验失败')
        else:
            base = conflict_merge.find_base(repo, remote.relative_to(repo).as_posix(), base_hash)
            if base is None or len(base) > MAX_FILE:
                reasons[key] = 'no_shared_version'
                continue
            merged = conflict_merge.make_candidate(base, left, right)
            if merged is None:
                reasons[key] = 'overlapping_edits'
                continue
            case = dict(zip(('base', 'local', 'repository', 'proposed'),
                            (data.decode('utf-8-sig') for data in (base, left, right, merged))))
            if remaining_reviews == 0:
                reasons[key] = 'retry_next_run'
                continue
            try:
                remaining_reviews -= 1
                verdict = conflict_codex.approve_merge(workspace, repo, [{'id': case_id, **case}])
            except conflict_codex.UnsupportedInput:
                remaining_reviews += 1
                reasons[key] = 'unsupported_input'
                continue
            except core.Error:
                remaining_reviews = 0
                reasons[key] = 'codex_unavailable'
                counters['codex_failed_files'] += 1
                continue
            if set(verdict) != {case_id} or type(verdict[case_id]) is not bool:
                reasons[key] = 'codex_unavailable'
                counters['codex_failed_files'] += 1
                continue
            if not verdict[case_id]:
                core._save(record, {'id': case_id, 'decision': 'review'})
                reasons[key] = 'codex_requested_review'
                continue
        before_write()
        with core._lock(repo):
            fresh_groups, fresh_bindings, fresh_base = core._load(repo)
            entry = fresh_base.get(name, {})
            if (group not in fresh_groups or fresh_bindings.get(name) != bindings[name]
                    or entry.get('binding') != bindings[name] or entry.get('path') != group['path']
                    or entry.get('kind') != group['kind'] or entry['files'].get(relative) != base_hash
                    or core._bytes(local) != left or core._bytes(remote) != right):
                raise core.Error('审阅期间绑定、基线或内容已变化，请重新核对')
            if not resume:
                backup = directory / case_id
                for filename, content in [('base', base), ('local', left), ('repository', right), ('merged', merged)]:
                    core._atomic(backup / filename, content)
                core._save(record, {'id': case_id, 'decision': 'approve', 'base': base_hash,
                                    'local': local_hash, 'remote': remote_hash, 'merged': core._digest(merged),
                                    'group': name, 'path': relative})
            if left != merged:
                core._atomic(local, merged)
            if core._bytes(local) != merged or core._bytes(remote) != right:
                raise core.Error('写入期间文件已变化，下次同步将重新核对')
            if right != merged:
                core._atomic(remote, merged)
        counters['resolved_files'] += 1
    return reasons
