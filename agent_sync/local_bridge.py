"""Explicit, machine-local quick sync; importing this module does not sync.

The checkpoint is a performance hint, not a trust boundary. Run serially and
stop editors while syncing. Neither this adapter nor core is a hostile-race
sandbox. Original skills are always read-only.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from . import core, gitops, collector, private_remote, sources


class Stop(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def stop(status, message):
    raise Stop(status, message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@contextmanager
def workspace_lock(root, check=False):
    """Shared by manual and scheduled runs; never reclaim another run's lock."""
    path = root / 'quick-sync.lock'
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        stop('busy', '快捷同步锁已存在；请等待现有操作结束，不自动删除锁。')
    if check:
        yield
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        stop('busy', '其他快捷同步已取得锁；本次未继续。')
    owned = None
    try:
        try:
            owned = identity(os.fstat(fd))
        finally:
            os.close(fd)
        yield
    finally:
        if owned is not None:
            try:
                current = path.lstat()
                if stat.S_ISREG(current.st_mode) and identity(current) == owned:
                    path.unlink()
            except FileNotFoundError:
                pass


class Reader:
    """Invocation-local ancestor and read caches; never persists stat hints."""
    def __init__(self, counters):
        self.directories = set()
        self.copies = {}
        self.sources = {}
        self.counters = counters

    @staticmethod
    def inspect(path, allow_hardlink=False):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            stop('unsafe_path', '发现链接或 reparse point；未继续同步。')
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            stop('unsafe_path', '发现特殊文件；未继续同步。')
        if stat.S_ISREG(info.st_mode) and not allow_hardlink and info.st_nlink != 1:
            stop('unsafe_path', '镜像、绑定或状态文件不是普通独立副本。')
        return info

    def safe(self, path, source=False):
        if not path.is_absolute() or '..' in path.parts:
            stop('invalid_input', '输入路径必须是无 .. 的绝对路径。')
        for parent in reversed(path.parents):
            if parent in self.directories:
                continue
            try:
                info = self.inspect(parent)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(info.st_mode):
                stop('unsafe_path', '路径祖先不是普通目录。')
            self.directories.add(parent)
        try:
            return self.inspect(path, source)
        except FileNotFoundError:
            return None

    def read(self, path, source=False):
        before = self.safe(path, source)
        if before is None:
            if source:
                stop('source_missing', '已选择的原技能文件缺失；未自动传播删除。')
            return None
        if not stat.S_ISREG(before.st_mode):
            stop('unsafe_path', '应为普通文件的路径实际不是普通文件。')
        key = identity(before)
        cache = self.sources if source else self.copies
        cache_key = key if source else str(path)
        old = cache.get(cache_key)
        if old is not None:
            old_key, data = old
            after = self.safe(path, source)
            if old_key != key or after is None or identity(after) != key:
                stop('changed_during_read', '扫描期间文件变化；请停止编辑后重试。')
            return data
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if identity(opened) != key:
                stop('changed_during_read', '打开期间文件变化；请重试。')
            if not source and opened.st_nlink != 1:
                stop('unsafe_path', '打开的副本出现硬链接；已停止。')
            if source:
                self.counters['unique_source_reads'] += 1
            data = stream.read()
            after_open = os.fstat(stream.fileno())
        after = self.safe(path, source)
        if (identity(after_open) != key or after is None
                or identity(after) != key or len(data) != before.st_size
                or (not source and after_open.st_nlink != 1)):
            stop('changed_during_read', '读取期间文件变化；请停止编辑后重试。')
        cache[cache_key] = (key, data)
        return data

    def tree(self, root, strict=False):
        info = self.safe(root)
        if info is None:
            return {}
        if not stat.S_ISDIR(info.st_mode):
            stop('unsafe_path', '树绑定的根不是普通目录。')
        files, names = {}, []

        def walk(directory, prefix):
            info = self.safe(directory)
            if info is None or not stat.S_ISDIR(info.st_mode):
                stop('changed_during_read', '扫描期间目录变化；请重试。')
            self.directories.add(directory)
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for entry in children:
                if not strict and core._ignored_cache(entry.name):
                    continue
                relative = prefix + entry.name
                path = directory / entry.name
                info = self.safe(path)
                if info is None:
                    stop('changed_during_read', '扫描期间文件消失；请重试。')
                directory_entry = stat.S_ISDIR(info.st_mode)
                core._names(relative, file=not directory_entry)
                names.append(relative)
                if directory_entry:
                    walk(path, relative + '/')
                else:
                    data = self.read(path)
                    if data is None:
                        stop('changed_during_read', '扫描期间文件消失；请重试。')
                    files[relative] = digest(data)
        walk(root, '')
        core._case_check(names)
        return files

    def snapshot(self, root, kind):
        if kind == 'tree':
            return self.tree(root)
        data = self.read(root)
        return {} if data is None else {'': digest(data)}

    def document(self, path, default=None):
        data = self.read(path)
        if data is None:
            if default is not None:
                return default
            stop('invalid_input', '缺少必需的本机配置或产品状态文件。')
        return json.loads(data)


def text(repo, args):
    return gitops._git(repo, args, '本机同步安全核对').stdout.decode('utf-8', 'strict').strip()


def remote_head(repo, remote, ref):
    raw = text(repo, ['ls-remote', '--exit-code', '--refs', remote, ref])
    fields = raw.split()
    if (len(fields) != 2 or fields[1] != ref
            or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', fields[0])):
        stop('git_review_required', '无法唯一确认真实远端分支；请助手核对。')
    return fields[0]


@contextmanager
def safe_git():
    """Use the product environment, suppress optional index writes and hooks.

    The mutable guard is enabled only around git_sync. Its fetch is allowed,
    but a newly fetched remote commit must not reach commit/merge/push.
    """
    original = gitops._git
    guard = {}
    options = ['--no-optional-locks', '-c', 'core.fsmonitor=false',
               '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=' + os.devnull]

    def verify(repo):
        if remote_head(repo, guard['remote'], guard['ref']) != guard['head']:
            stop('remote_changed', '远端在同步期间变化；已停止，请助手收取。')
        if text(repo, ['rev-parse', '--verify', guard['upstream']]) != guard['head']:
            stop('remote_changed', 'fetch 发现远端更新；未自动合并或上传。')

    def wrapped(repo, args, label, allowed=(0,)):
        # git_sync's mutating commands have these tokens before their operands.
        mutation = bool(guard) and any(x in args for x in ('fetch', 'merge', 'push'))
        if mutation:
            verify(repo)
        result = original(repo, options + list(args), label, allowed)
        if guard and args and args[0] == 'fetch':
            verify(repo)
        return result

    gitops._git = wrapped
    try:
        yield guard
    finally:
        gitops._git = original


def git_gate(repo):
    root = core._abs(text(repo, ['rev-parse', '--show-toplevel']))
    if root != repo:
        stop('git_review_required', '--repo 必须是内容 Git 工作树根目录。')
    head = text(repo, ['rev-parse', '--verify', 'HEAD'])
    branch = text(repo, ['symbolic-ref', '--quiet', '--short', 'HEAD'])
    upstream = text(repo, ['rev-parse', '--symbolic-full-name', '@{upstream}'])
    remote = text(repo, ['config', '--get-all', 'branch.' + branch + '.remote'])
    ref = text(repo, ['config', '--get-all', 'branch.' + branch + '.merge'])
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', remote)
            or not ref.startswith('refs/heads/') or '\n' in ref
            or upstream != 'refs/remotes/' + remote + '/' + ref[len('refs/heads/'):]):
        stop('git_review_required', '仅支持已配置的标准单一 remote/upstream；不自动初始化。')
    fetch_url = text(repo, ['remote', 'get-url', '--all', remote])
    push_url = text(repo, ['remote', 'get-url', '--push', '--all', remote])
    if not fetch_url or '\n' in fetch_url or fetch_url != push_url:
        stop('git_review_required', '读取和上传目标不唯一或不相同；请手工核对 Git 设置。')
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                   'rebase-merge', 'rebase-apply', 'sequencer'):
        path = core._safe(gitops._git_path(repo, marker))
        if path.exists():
            stop('git_review_required', '存在未完成的 Git 操作；请先手工处理。')
    if gitops._git(repo, ['ls-files', '-u', '-z'], '检查未合并索引').stdout:
        stop('conflict', 'Git 索引存在冲突；未上传。')
    if gitops._git(repo, ['diff', '--cached', '--quiet', '--exit-code'],
                   '检查暂存区', (0, 1)).returncode:
        stop('staged_changes', '已有暂存改动已保留；请用户先处理，未代为提交。')
    flags = gitops._git(repo, ['ls-files', '-v', '-z'], '检查索引标志').stdout
    if any(record[:2] != b'H ' for record in flags.split(b'\0') if record):
        stop('git_review_required', '索引含隐藏工作树变化的标志或特殊条目；请手工处理。')
    tracked = gitops._paths(repo, ['ls-files', '-z'], '检查跟踪路径')
    dirty = gitops._paths(repo, ['diff', '--name-only', '-z', 'HEAD', '--'], '检查本机仓库改动')
    unknown = gitops._paths(repo, ['ls-files', '--others', '--exclude-standard', '-z'], '检查未跟踪文件')
    gitops._validate_names(tracked + dirty + unknown)
    if any(not gitops._allowed(path) for path in dirty + unknown):
        stop('git_review_required', '存在同步白名单之外的本机仓库改动；已保留。')
    status = gitops._git(repo, ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
                          '核对完整 Git 状态').stdout
    if gitops._git(repo, ['check-ignore', '--no-index', '-q', '.agent-sync/device.json'],
                   '检查本机状态忽略规则', (0, 1)).returncode:
        stop('git_review_required', '本机产品状态未被正确忽略；请完成 bootstrap。')
    gitops._tree(repo, 'HEAD')
    upstream_head = text(repo, ['rev-parse', '--verify', upstream])
    actual = remote_head(repo, remote, ref)
    if actual != head:
        ancestor = gitops._git(repo, ['merge-base', '--is-ancestor', actual, head],
                               '区分本地 ahead 和远端变化', (0, 1, 128)).returncode
        if upstream_head == actual and ancestor == 0:
            stop('local_ahead', '本地已有待推提交，未认作已同步；按远端必须等于 HEAD 的门禁，请助手审查并推送。')
        stop('remote_changed', '真实远端分支已变化；未收取、合并或上传，请助手处理。')
    if upstream_head != head:
        stop('git_review_required', '真实远端与 HEAD 相同，但本地 upstream 记录不同；请助手核对。')
    config = gitops._git(repo, ['config', '--null', '--list', '--show-origin', '--show-scope'],
                          '摘要 Git 配置').stdout
    return {'head': head, 'branch': branch, 'upstream': upstream,
            'remote': remote, 'ref': ref, 'config_sha256': digest(config),
            'clean': not bool(status)}


def selection(reader, root, repo, groups, bindings):
    config = reader.document(root / 'selection.json', {'version': 1, 'files': []})
    state = reader.document(root / 'collection-state.json', {})
    if (not isinstance(config, dict) or set(config) != {'version', 'files'}
            or type(config['version']) is not int or config['version'] != 1
            or not isinstance(config['files'], list) or not isinstance(state, dict)):
        stop('invalid_input', 'selection 或 collection-state 格式错误。')
    if not config['files']:
        if any(g['category'] == 'skills' and g['name'] in bindings for g in groups):
            stop('selection_required', '技能绑定尚无本机已审阅映射；请配置 workspace/selection.json，未接入或扫描原技能。')
        if any(g['name'] in bindings and g['category'] not in {'rules', 'personas', 'memory'}
               for g in groups):
            stop('review_required', '无选择映射时仅支持 rules/personas/memory 直接绑定。')
    mirror = root / 'skill-mirror'
    selected, originals = {}, []
    for item in config['files']:
        if (not isinstance(item, dict) or set(item) != {'source', 'mirror', 'reviewed_sha256'}
                or not isinstance(item['source'], str)
                or not isinstance(item['reviewed_sha256'], str)
                or not core.HASH.fullmatch(item['reviewed_sha256'])):
            stop('invalid_input', '已审阅清单条目格式错误。')
        rel = item['mirror']
        parts = core._names(rel, file=True)
        if parts[0] not in {'codex', 'shared', 'antigravity'} or len(parts) < 2 or rel in selected:
            stop('invalid_input', '镜像目标不合法或重复。')
        source = Path(item['source'])
        if not source.is_absolute() or '..' in source.parts:
            stop('invalid_input', '原技能必须使用明确的绝对路径。')
        protected = [repo, mirror, root / 'selection.json', root / 'collection-state.json',
                     root / 'fast-sync-state.json', root / 'quick-sync.lock']
        protected += private_remote.source_roots()
        protected += [Path(bindings[g['name']]) for g in groups if g['name'] in bindings]
        if any(core._overlap(source, target) for target in protected):
            stop('unsafe_path', '原技能与可能写入的目标路径重叠；拒绝同步。')
        selected[rel] = item
        originals.append(source)
    core._case_check(list(selected))
    for rel, value in state.items():
        core._names(rel, file=True)
        if not isinstance(value, str) or not core.HASH.fullmatch(value):
            stop('invalid_input', '收集状态含无效摘要。')
    core._case_check(list(state))
    mirror_groups = set()
    for group in groups:
        name = group['name']
        if name not in bindings:
            continue
        bound = Path(bindings[name])
        touches = core._overlap(bound, mirror)
        if group['category'] == 'skills':
            if (group['kind'] != 'tree'
                    or not any(bound.is_relative_to(mirror / family)
                               for family in ('codex', 'shared', 'antigravity'))):
                stop('unsafe_path', '技能树必须绑定到 skill-mirror 内的三类目录。')
        if touches:
            if group['category'] != 'skills':
                stop('unsafe_path', '镜像路径只能作为技能绑定。')
            mirror_groups.add(name)
        for control in ('selection.json', 'collection-state.json', 'fast-sync-state.json',
                        'quick-sync.lock'):
            if core._overlap(bound, root / control):
                stop('unsafe_path', '内容绑定与本机控制文件重叠。')
    for rel in selected:
        destination = mirror.joinpath(*rel.split('/'))
        if not any(destination.is_relative_to(Path(bindings[name])) for name in mirror_groups):
            stop('invalid_input', '存在未绑定到技能组的已选择镜像文件。')
    reader.counters['source_files'] = len(originals)
    hashes = {rel: digest(reader.read(source, source=True))
              for rel, source in zip(selected, originals)}
    return config, state, hashes, mirror_groups


def inspect_content(reader, root, repo, groups, bindings, baseline, hashes):
    mirror = reader.tree(root / 'skill-mirror', strict=True)
    if set(mirror) - set(hashes):
        stop('needs_skill_install_review', '镜像中存在未审阅文件；已保留，未上传。')
    # Validate the entire repository data namespace, including unbound files.
    data_files = reader.tree(repo / 'data')
    gitops._stageable(repo, {'manifest.json'} | {'data/' + p for p in data_files})
    snapshots, projected = {}, []
    matching = True
    for group in groups:
        name = group['name']
        if name not in bindings:
            matching = False
            continue
        bound = Path(bindings[name])
        remote = reader.snapshot(repo / group['path'], group['kind'])
        local = reader.snapshot(bound, group['kind'])
        entry = baseline.get(name, {})
        same = (entry.get('binding') == bindings[name]
                and entry.get('path') == group['path'] and entry.get('kind') == group['kind'])
        base = entry.get('files', {}) if same else {}
        matching = matching and same and remote == local == base
        snapshots[name] = (remote, local, base)
        future = dict(local)
        for rel, value in hashes.items():
            target = (root / 'skill-mirror').joinpath(*rel.split('/'))
            if target.is_relative_to(bound):
                future[target.relative_to(bound).as_posix()] = value
        for rel in sorted(remote.keys() | future.keys() | base.keys()):
            projected.append({'group': name, 'path': rel,
                              'action': core._action(remote.get(rel), future.get(rel), base.get(rel))})
    names = {g['name'] for g in groups}
    if (bindings.keys() | baseline.keys()) - names:
        matching = False
    return mirror, snapshots, projected, matching


def validate_rows(rows, mirror_groups):
    if any(row['action'] == 'conflict' for row in rows):
        stop('conflict', '发现冲突；已保留原技能、镜像和仓库改动，未上传。')
    if any(row['action'] == 'pull' and row['group'] in mirror_groups for row in rows):
        stop('needs_skill_install_review', '仓库中的技能需要安装审阅；未覆盖镜像，原技能不会自动生效。')
    if any(row['action'] not in {'unchanged', 'deleted-both', 'push', 'pull'} for row in rows):
        stop('review_required', '存在删除、孤立或未绑定项目；请先人工处理，未上传。')


def checkpoint_key(reader, root, repo, facts):
    paths = [root / 'selection.json', root / 'collection-state.json',
             repo / 'manifest.json', repo / '.agent-sync/device.json',
             repo / '.agent-sync/baseline.json', repo / '.gitignore', repo / '.gitattributes']
    paths += [gitops._git_path(repo, name) for name in ('info/attributes', 'info/exclude')]
    summaries = []
    for path in paths:
        data = reader.read(path)
        summaries.append([str(path), None if data is None else digest(data)])
    value = {'schema': 1, 'adapter': 'agent-sync-local-2',
             'root': str(root), 'repo': str(repo), 'git': facts, 'files': summaries}
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=True).encode('utf-8'))


def full_plan(repo, check):
    if check:
        # core.plan creates/removes a lock file. Use exactly its read-only body
        # instead, so --check does not even transiently create a local file.
        groups, bindings, baseline = core._load(repo)
        return core._collect(repo, groups, bindings, baseline)[0]
    return core.plan(repo)


def prepare(root, repo, counters):
    """Read-only preflight: reject unsafe/public targets before creating a lock."""
    root, repo = core._safe(root), core._safe(repo)
    private_remote.validate_paths(repo, root)
    if not root.is_dir() or not repo.is_dir():
        stop('invalid_input', '本机目录或内容仓库不存在；不自动初始化。')
    if core._overlap(root, repo):
        stop('unsafe_path', '本机适配器目录和内容仓库必须独立。')
    target = private_remote.require_private(repo)
    counters['visibility'] = target['visibility']
    # Validate loaded bindings before refresh, collection, apply or any lock.
    # Do not silently remove old bindings, including orphaned device entries.
    groups, bindings, baseline = core._load(repo)
    source_roots = private_remote.source_roots()
    if any(core._overlap(Path(bound), source)
           for bound in bindings.values() for source in source_roots):
        stop('unsafe_path', '内容绑定与程序源码或源码工作树重叠；请手工调整绑定。')
    return root, repo


def perform(root, repo, check, counters, guard):
    if core._safe(repo / '.agent-sync/lock').exists():
        stop('busy', '产品锁已存在；请等待现有操作结束。')
    facts = git_gate(repo)
    # Fail before refresh can write anything. Git status omits ignored untracked
    # data, so even a primed checkpoint must perform this live coverage check.
    data_files = core._scan_tree(repo / 'data')
    gitops._stageable(repo, {'manifest.json'} | {'data/' + p for p in data_files})
    if not check:
        refreshed = sources.refresh(root, repo)
        counters['archives_created'] = refreshed['archived']
        counters['archives_unchanged'] = refreshed['unchanged']
        counters['archives_checked'] = True
        if refreshed['archived'] > 0:
            facts['clean'] = False
    reader = Reader(counters)
    groups, bindings, baseline = core._load(repo)
    config, state, hashes, mirror_groups = selection(reader, root, repo, groups, bindings)
    mirror, snapshots, projected, matching = inspect_content(
        reader, root, repo, groups, bindings, baseline, hashes)
    counters['changed_files'] = sum(row['action'] not in {'unchanged', 'deleted-both'}
                                   for row in projected)
    for rel in hashes:
        old = state.get(rel)
        current = mirror.get(rel)
        if (old is not None and current != old) or (old is None and current not in (None, hashes[rel])):
            stop('needs_skill_install_review', '镜像有独立修改或缺失；已保留，请先审阅技能安装。')
    changed_sources = hashes != state
    # Include not-yet-created push targets, including projected collector output,
    # before either fast return or any collector/core.apply write.
    by_name = {group['name']: group for group in groups}
    candidates = set()
    for row in projected:
        if row['action'] == 'push':
            group = by_name[row['group']]
            candidates.add(group['path'] if group['kind'] == 'file'
                           else group['path'] + '/' + row['path'])
    gitops._stageable(repo, candidates)
    key = checkpoint_key(reader, root, repo, facts)
    saved = reader.document(root / 'fast-sync-state.json', {})
    stable = facts['clean'] and matching and not changed_sources and mirror == hashes
    if stable and saved == {'version': 1, 'key': key}:
        return 'up_to_date', '全部内容与基线一致；快捷核对完成，未上传。'

    rows = full_plan(repo, check)
    validate_rows(rows, mirror_groups)
    validate_rows(projected, mirror_groups)
    from .records import validate_shared
    validate_shared(repo)
    if stable and all(row['action'] == 'unchanged' for row in rows):
        if not check:
            core._save(root / 'fast-sync-state.json', {'version': 1, 'key': key})
        return 'up_to_date', '完整核对通过；内容无需上传。'

    # Check mode still runs the existing collector's complete preflight, but
    # never applies its operations, establishes baselines, or creates locks.
    if check:
        if changed_sources:
            collector.collect(config, root / 'skill-mirror',
                              root / 'collection-state.json', check_only=True)
        return 'changes_pending', '核对通过，有本机待同步变化；--check 未写入或上传。'

    if changed_sources:
        # Projected conflict checks above precede even the first mirror write.
        # collect itself performs the authoritative full mirror preflight.
        collector.collect(config, root / 'skill-mirror', root / 'collection-state.json')
        rows = core.plan(repo)
        validate_rows(rows, mirror_groups)
    if any(row['action'] == 'pull' for row in rows):
        # validate_rows has checked ALL pull rows, not just the first one.
        counters['direct_pull_files'] += core.apply(repo, 'pull')['written']
        rows = core.plan(repo)
        validate_rows(rows, mirror_groups)
    pushing = any(row['action'] == 'push' for row in rows)
    core.apply(repo, 'push')
    if pushing or not facts['clean']:
        fresh = git_gate(repo)
        if fresh['head'] != facts['head']:
            stop('git_review_required', '本机 HEAD 在运行期间变化；保留现状，请重新核对。')
        guard.update({key: facts[key] for key in ('head', 'remote', 'ref', 'upstream')})
        try:
            private_remote.require_private(repo)
            gitops.git_sync(repo)
        finally:
            guard.clear()
        counters['uploaded'] = True
        return 'synced', '本机变化已同步并上传；原技能未被写入。'
    return 'synced', '本机副本或基线已同步；无需上传，原技能未被写入。'


def run(workspace, repo, check=False):
    started = time.perf_counter()
    counters = {'source_files': 0, 'unique_source_reads': 0,
                'uploaded': False, 'source_write_count': 0,
                'changed_files': 0, 'direct_pull_files': 0,
                'archives_created': 0, 'archives_unchanged': 0,
                'archives_checked': False}
    code = 0
    try:
        root, repo = core._abs(workspace), core._abs(repo)
        with safe_git() as guard:
            root, repo = prepare(root, repo, counters)
            with workspace_lock(root, check):
                status, message = perform(root, repo, check, counters, guard)
    except Stop as exc:
        status, message, code = exc.status, exc.message, 2
    except (core.Error, OSError, ValueError, TypeError, KeyError, UnicodeError):
        # Do not echo exception strings: they can contain paths, content or
        # credential-bearing remote URLs. The original chained error stays local.
        status, message, code = 'error', '安全核对未完成；未报告成功。请检查输入、bootstrap/upstream、Git 凭证及产品状态；已有改动保留。', 2
    return {'status': status, 'message': message, **counters,
            'elapsed_seconds': round(time.perf_counter() - started, 6)}, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', '--root', dest='workspace', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    core.configure_stdio()
    result, code = run(args.workspace, args.repo, args.check)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
