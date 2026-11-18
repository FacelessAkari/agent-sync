"""Fast-forward a clean content checkout through bound, ordinary file changes."""
import os
from pathlib import Path
import re

from . import core, gitops, private_remote
from .partial_sync import _plain_text
from .records import validate_shared


MAX_COMMITS = 32
MAX_CHANGES = 512
MAX_TEXT_BYTES = 1024 * 1024
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def _bound_change(path, groups):
    for group in groups:
        if ((group['kind'] == 'file' and path == group['path'])
                or (group['kind'] == 'tree' and path.startswith(group['path'] + '/'))):
            if group['category'] == 'skills':
                return 'skill'
            if group['category'] in ('rules', 'memory', 'personas') and Path(path).suffix.lower() in ('.md', '.txt'):
                return 'text'
    return None


def _eligible_history(repo, head, tip, groups):
    log = gitops._text(repo, ['rev-list', '--reverse', '--parents', '--max-count=' + str(MAX_COMMITS + 1),
                              head + '..' + tip], '核对远端线性历史').splitlines()
    if not log or len(log) > MAX_COMMITS:
        return False
    parent, count = head, 0
    for line in log:
        parts = line.split()
        if len(parts) != 2 or parts[1] != parent or not OID.fullmatch(parts[0]):
            return False
        commit = parts[0]
        raw = gitops._git(repo, ['diff-tree', '--no-commit-id', '--raw', '-z', '-r', '--no-renames',
                                 '--no-abbrev', parent, commit], '核对每次收取的内容').stdout
        entries = raw.split(b'\0')
        if entries.pop() != b'' or len(entries) % 2:
            return False
        count += len(entries) // 2
        if count > MAX_CHANGES:
            return False
        for header, path_bytes in zip(entries[::2], entries[1::2]):
            fields = header.decode('ascii').split()
            if len(fields) != 5:
                return False
            old_mode, mode, _, blob, status = fields
            if not ((status == 'A' and old_mode == ':000000')
                    or (status == 'M' and old_mode == ':' + mode)):
                return False
            if mode not in ('100644', '100755') or not OID.fullmatch(blob):
                return False
            path = path_bytes.decode('utf-8')
            kind = _bound_change(path, groups)
            if not kind:
                return False
            core._data_path(path)
            core._safe(repo / path)
            if kind == 'text':
                size = gitops._text(repo, ['cat-file', '-s', blob], '检查收取文本大小')
                if not size.isdecimal() or int(size) > MAX_TEXT_BYTES or not _plain_text(
                        gitops._git(repo, ['cat-file', 'blob', blob], '检查收取的文本').stdout):
                    return False
        parent = commit
    return parent == tip and count > 0


def _clean_behind(repo):
    from .local_bridge import Stop, git_gate
    if (any(core._safe(gitops._git_path(repo, name)).exists()
            for name in ('BISECT_START', 'index.lock', 'info/grafts'))
            or os.environ.get('GIT_REPLACE_REF_BASE')
            or gitops._git(repo, ['for-each-ref', '--count=1', '--format=%(refname)', 'refs/replace/'],
                           '排除被替换的本机提交历史').stdout):
        return False
    try:
        git_gate(repo)
    except Stop as exc:
        if exc.status != 'remote_changed':
            return False
        return not gitops._git(repo, ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
                               '要求收取前工作树完全干净').stdout
    return False


def fast_forward(repo) -> bool:
    from .local_bridge import remote_head

    repo = core._safe(repo)
    target = private_remote.require_private(repo)
    with core._lock(repo):
        if not _clean_behind(repo):
            return False
        groups, bindings, _ = core._load(repo)
        bound = [group for group in groups if group['name'] in bindings]
        validate_shared(repo)
        gitops._stageable(repo, {'data/' + name for name in core._scan_tree(repo / 'data')})
        head = gitops._text(repo, ['rev-parse', '--verify', 'HEAD'], '核对收取起点')
        branch = gitops._text(repo, ['symbolic-ref', '--quiet', '--short', 'HEAD'], '读取当前分支')
        remote = gitops._text(repo, ['config', '--get-all', 'branch.' + branch + '.remote'], '核对读取目标')
        ref = gitops._text(repo, ['config', '--get-all', 'branch.' + branch + '.merge'], '核对读取分支')
        tracking = gitops._text(repo, ['rev-parse', '--symbolic-full-name', '@{upstream}'], '读取跟踪引用')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', remote) or not ref.startswith('refs/heads/')
                or tracking != 'refs/remotes/' + remote + '/' + ref[len('refs/heads/'):]):
            return False
        private_remote.require_private(repo, remote, expected=target)
        tip = remote_head(repo, remote, ref)
        tracking_start = gitops._text(repo, ['rev-parse', '--verify', tracking], '读取原跟踪提交')
        if tracking_start not in (head, tip) or not OID.fullmatch(head) or tip == head:
            return False
        options = ['-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false']
        # Fetch the single branch's objects. Neither the checked-out branch, any
        # tracking ref, tags, nor FETCH_HEAD advances before content validation.
        gitops._git(repo, options + ['fetch', '--no-tags', '--no-recurse-submodules', '--no-auto-maintenance',
                                     '--no-write-fetch-head', '--refmap=', '--', remote, ref],
                    '收取指定分支的候选提交')
        if gitops._git(repo, ['merge-base', '--is-ancestor', head, tip], '证明可快进', (0, 1, 128)).returncode:
            return False
        if not _eligible_history(repo, head, tip, bound):
            return False
        gitops._tree(repo, tip)
        private_remote.require_private(repo, remote, expected=target)
        if (not _clean_behind(repo) or gitops._text(repo, ['symbolic-ref', '--quiet', '--short', 'HEAD'],
                                                   '复核当前分支') != branch
                or gitops._text(repo, ['rev-parse', '--verify', 'HEAD'], '复核收取起点') != head
                or gitops._text(repo, ['rev-parse', '--symbolic-full-name', '@{upstream}'], '复核跟踪分支') != tracking
                or remote_head(repo, remote, ref) != tip):
            return False
        # CAS may update a tracking ref even when a last-second local edit makes
        # Git decline the merge; the next run can retry that same fast-forward.
        gitops._git(repo, options + ['update-ref', tracking, tip, tracking_start], '更新已验证的跟踪提交')
        merged = gitops._git(repo, options + ['-c', 'merge.autoStash=false', 'merge', '--ff-only', '--no-autostash',
                                              '--no-overwrite-ignore', '--no-edit', tip],
                             '将干净内容工作树快进至已验证提交', (0, 1, 128))
        return merged.returncode == 0 and gitops._text(repo, ['rev-parse', '--verify', 'HEAD'],
                                                       '回读已收取提交') == tip
