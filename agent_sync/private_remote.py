"""Fail-closed, invocation-local verification of content upload destinations."""
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import core


SOURCE_ROOT = Path(__file__).resolve().parents[1]
_MESSAGE = '无法确认目标为 GitHub 私有仓库；已停止，请检查远端及凭证。'


def git_environment():
    # Keep installed credential helpers and explicit offline test configuration.
    blocked = {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
               'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
               'GIT_NAMESPACE', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT'}
    env = {k: v for k, v in os.environ.items()
           if k not in blocked and not k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never',
               GIT_MERGE_AUTOEDIT='no', GIT_ALLOW_PROTOCOL='https:ssh:file',
               GIT_ASKPASS=os.devnull, SSH_ASKPASS=os.devnull)
    return env


def source_roots():
    roots = [SOURCE_ROOT]
    for parent in (SOURCE_ROOT, *SOURCE_ROOT.parents):
        if (parent / '.git').exists():
            if parent not in roots:
                roots.append(parent)
            break
    return roots


def validate_paths(repo, workspace=None):
    # Only the installed source location is resolved. User paths still go through
    # core's reparse/ancestor checks, not resolve(), which would hide a link.
    repo = core._safe(repo)
    roots = source_roots()
    if any(core._overlap(repo, source) for source in roots):
        raise core.Error('资料仓库必须与程序源码及源码工作树互不重叠。')
    if workspace is not None:
        workspace = core._safe(workspace)
        if any(core._overlap(workspace, other) for other in [repo, *roots]):
            raise core.Error('本机工作区必须与资料仓库及程序源码互不重叠。')
    return repo, workspace


def classify_url(url):
    """Return (visibility, full_name); local means absolute, non-network Git."""
    if not isinstance(url, str) or not url or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise core.Error(_MESSAGE)
    normalized = url.replace('\\', '/')
    if normalized.startswith('//'):
        raise core.Error(_MESSAGE)
    if re.match(r'^[A-Za-z]:[\\/]', url):
        if os.name != 'nt':
            raise core.Error(_MESSAGE)
        return 'local', None
    if url.startswith('/'):
        return 'local', None
    if url.startswith('file:'):
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parsed.path)
        if (parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment
                or not url.startswith('file:///') or not path.startswith('/')
                or path.replace('\\', '/').startswith('//')
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise core.Error(_MESSAGE)
        return 'local', None
    match = re.fullmatch(
        r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
        r'([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,104})', url)
    if not match:
        raise core.Error(_MESSAGE)
    owner, name = match.groups()
    if name.endswith('.git'):
        name = name[:-4]
    if name in {'', '.', '..'} or len(name) > 100:
        raise core.Error(_MESSAGE)
    return 'private', owner + '/' + name


def _one(repo, args, optional=False):
    from . import gitops
    result = gitops._git(repo, args, '核验资料远端', (0, 1) if optional else (0,))
    if optional and result.returncode == 1:
        return None
    lines = result.stdout.decode('utf-8', 'strict').splitlines()
    if len(lines) != 1 or not lines[0]:
        raise core.Error(_MESSAGE)
    return lines[0]


def _target(repo, remote):
    if remote is None:
        branch = _one(repo, ['symbolic-ref', '--quiet', '--short', 'HEAD'])
        remote = _one(repo, ['config', '--get-all', 'branch.' + branch + '.remote'])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', remote):
        raise core.Error(_MESSAGE)
    raw = _one(repo, ['config', '--get-all', 'remote.' + remote + '.url'])
    raw_push = _one(repo, ['config', '--get-all', 'remote.' + remote + '.pushurl'], True)
    fetch = _one(repo, ['remote', 'get-url', '--all', remote])
    push = _one(repo, ['remote', 'get-url', '--push', '--all', remote])
    # Also reject insteadOf/pushInsteadOf rewrites: verification and transport
    # must refer to the same single literal destination.
    if raw != fetch or fetch != push or (raw_push is not None and raw_push != raw):
        raise core.Error(_MESSAGE)
    visibility, full_name = classify_url(raw)
    return {'remote': remote, 'url': raw, 'visibility': visibility, 'full_name': full_name}


def _token(repo):
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        result = subprocess.run(
            ['git', '-C', str(repo), 'credential', 'fill'],
            input=b'protocol=https\nhost=github.com\n\n', shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            env=git_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise core.Error(_MESSAGE)
        passwords = [line[len('password='):] for line in result.stdout.decode('utf-8').splitlines()
                     if line.startswith('password=')]
        if len(passwords) != 1:
            raise core.Error(_MESSAGE)
        token = passwords[0]
    if not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise core.Error(_MESSAGE)
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise core.Error(_MESSAGE)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate metadata field')
        result[key] = value
    return result


def _metadata(full_name, token):
    request = urllib.request.Request(
        'https://api.github.com/repos/' + full_name,
        headers={'Authorization': 'Bearer ' + token,
                 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'AgentSync-private-remote',
                 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        if response.status != 200:
            raise core.Error(_MESSAGE)
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise core.Error(_MESSAGE)
    document = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(document, dict) or document.get('private') is not True
            or document.get('full_name') != full_name):
        raise core.Error(_MESSAGE)


def assert_private_repo(repo):
    """Stable installation preflight; return require_private's target metadata."""
    return require_private(repo)


def require_private(repo, remote=None, expected=None):
    """No disk cache, credential persistence, redirects, or visibility changes."""
    repo, _ = validate_paths(repo)
    try:
        target = _target(repo, remote)
        if expected is not None and target != expected:
            raise core.Error(_MESSAGE)
        if target['visibility'] != 'local':
            _metadata(target['full_name'], _token(repo))
        return target
    except (core.Error, OSError, ValueError, UnicodeError, subprocess.SubprocessError,
            urllib.error.URLError):
        # Never expose response bodies, helper stderr, URLs or exception chains.
        raise core.Error(_MESSAGE) from None
