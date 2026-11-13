"""Generate machine-specific launchers outside the public source tree."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import core, private_remote


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _preflight(files):
    for path, data in files.items():
        existing = core._bytes(core._safe(path))
        if existing is not None and existing != data:
            raise core.Error('入口目标已存在且内容不同；未覆盖任何已有文件。')


def _windows_link(config, output):
    """Return real Shell Link bytes; all temporary files stay in output."""
    if os.name != 'nt':
        raise core.Error('Windows .lnk 必须在实际 Windows 机器上生成。')
    script = """$ErrorActionPreference = 'Stop'
$c = Get-Content -LiteralPath $env:AGENTSYNC_LINK_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($c.link)
$link.TargetPath = $c.python
$link.Arguments = $c.arguments
$link.WorkingDirectory = $c.source
$link.Description = 'AgentSync quick sync'
$link.WindowStyle = 1
$link.Save()
"""
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    try:
        with tempfile.TemporaryDirectory(prefix='.agent-sync-link-', dir=output) as temp:
            temp = Path(temp)
            link = temp / 'AgentSync.lnk'
            document = {**config, 'link': str(link),
                        'arguments': subprocess.list2cmdline([str(output / 'launcher.py')])}
            config_file = temp / 'link.json'
            config_file.write_bytes(_json(document))
            env = os.environ.copy()
            env['AGENTSYNC_LINK_CONFIG'] = str(config_file)
            result = subprocess.run(
                ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                 '-EncodedCommand', encoded], shell=False, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode or not link.is_file():
                raise core.Error('无法创建 Windows 快捷方式。')
            return link.read_bytes()
    except (OSError, subprocess.SubprocessError):
        raise core.Error('无法创建 Windows 快捷方式；请检查本机 PowerShell。') from None


def create(workspace, repo, platform='auto', output=None):
    repo, workspace = private_remote.validate_paths(repo, workspace)
    if workspace is None:
        raise core.Error('必须明确指定本机工作区。')
    if platform == 'auto':
        platform = 'windows' if os.name == 'nt' else 'macos' if sys.platform == 'darwin' else ''
    if platform not in {'windows', 'macos'}:
        raise core.Error('仅支持 Windows/macOS；请明确指定目标平台。')
    output = core._safe(workspace / 'launchers' if output is None else output)
    if any(core._overlap(output, root) for root in [repo, *private_remote.source_roots()]):
        raise core.Error('入口输出必须与资料仓库及程序源码互不重叠。')
    selection = core._safe(workspace / 'selection.json')
    # Reject both ancestors and descendants before reading state or creating
    # anything (including Windows link temporary files).
    controls = [core._safe(workspace / name) for name in (
        'selection.json', 'collection-state.json', 'fast-sync-state.json',
        'quick-sync.lock', 'sources.json', 'sources-state.json',
        'skill-mirror', 'schedule',
    )]
    if any(core._overlap(output, control) for control in controls):
        raise core.Error('入口输出不能与本机工作区保留位置重叠。')
    target = private_remote.require_private(repo)
    groups, bindings, _ = core._load(repo)
    if not bindings:
        raise core.Error('请先在资料仓库中建立本机 core 绑定；不会自动初始化或发现资料。')
    if any(core._overlap(Path(bound), path)
           for bound in bindings.values() for path in [output, selection]):
        raise core.Error('入口或选择模板与已绑定资料重叠；已停止。')
    git = shutil.which('git')
    if not git:
        raise core.Error('未找到 Git；请先安装 Git 再生成入口。')
    git = Path(git)
    if (not git.is_absolute() or not git.is_file()
            or os.pathsep in str(git.parent)):
        raise core.Error('Git 必须位于可安全加入 PATH 的固定绝对目录。')
    config = {'python': os.path.abspath(sys.executable),
              'source': str(private_remote.SOURCE_ROOT),
              'workspace': str(workspace), 'repo': str(repo),
              'git_dir': str(git.parent)}
    launcher = ("# Generated machine-local AgentSync launcher. No content or credentials.\n"
                "import json\nimport math\nimport os\nfrom pathlib import Path\nimport subprocess\nimport sys\n\n"
                "CONFIG = " + repr(config) + "\n\n"
                + """SUCCESS = {'up_to_date': '资料已是最新。', 'synced': '同步已完成。'}
ATTENTION = {
    'selection_required': '尚需完成本机资料选择配置。',
    'bootstrap_required': '资料尚未准备完成。',
    'conflict': '资料存在冲突，尚未完成同步。',
    'conflicts': '资料存在冲突，尚未完成同步。',
    'remote_ahead': '远端资料有更新，尚未完成同步。',
    'remote_changed': '远端资料有更新，尚未完成同步。',
    'local_ahead': '本机资料有待上传，尚未完成同步。',
    'needs_skill_install_review': '技能安装映射尚需本机审阅。',
    'staged_changes': '资料仓库存在已暂存变更，请先处理。',
    'diverged': '本机与远端资料存在分歧。',
    'auth_required': '远端身份验证未通过。',
    'private_required': '资料仓库未通过私有性检查。',
    'busy': '另一次同步正在进行。',
    'error': '同步未能完成。',
}


def _count(value):
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, list):
        return str(len(value))
    return '未知'


def _elapsed(value):
    if type(value) is int and value >= 0:
        return str(value) + ' 秒'
    if type(value) is float and math.isfinite(value) and value >= 0:
        return str(value) + ' 秒'
    return '未知'


def _show_result(result):
    try:
        document = json.loads(result.stdout)
        if not isinstance(document, dict):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        print('需要处理：无法读取同步结果。')
        return
    status = document.get('status')
    if not isinstance(status, str):
        status = ''
    # Only fixed public-facing messages are displayed, never arbitrary CLI text.
    if result.returncode == 0 and status in SUCCESS:
        message = SUCCESS[status]
    else:
        message = '需要处理：' + ATTENTION.get(status, '同步结果需要确认。')
    print(message)
    print('变更文件：' + _count(document.get('changed_files'))
          + '；新建归档：' + _count(document.get('archives_created'))
          + '；耗时：' + _elapsed(document.get('elapsed_seconds')))


def main():
    output_tty = sys.stdout.isatty()
    interactive = sys.stdin.isatty() and output_tty
    encoding = (getattr(sys.stdout, 'encoding', None) or '').lower().replace('_', '-')
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if callable(reconfigure) and (output_tty or encoding not in {'utf-8', 'utf8'}):
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except (OSError, ValueError, TypeError):
            pass
    env = os.environ.copy()
    # Keep absolute user entries in their original order, including duplicates.
    # Path.is_absolute also rejects Windows drive-relative and rooted paths
    # without a drive. Never supply a default search path for a missing PATH.
    entries = [entry for entry in env.get('PATH', '').split(os.pathsep)
               if entry and Path(entry).is_absolute()]
    env['PATH'] = os.pathsep.join([CONFIG['git_dir'], *entries])
    try:
        result = subprocess.run([CONFIG['python'], '-m', 'agent_sync',
                                 '--repo', CONFIG['repo'], 'quick-sync',
                                 '--workspace', CONFIG['workspace']],
                                cwd=CONFIG['source'], env=env, shell=False,
                                capture_output=True, encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError):
        code = 1
        print('需要处理：无法启动同步，请稍后重试。')
    else:
        code = result.returncode
        _show_result(result)
    if interactive:
        print('按回车关闭窗口')
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == '__main__':
    raise SystemExit(main())
""").encode('utf-8')
    files = {output / 'launcher.py': launcher}
    executable = None
    if platform == 'windows':
        if any(c in config['python'] for c in '%!&^"<>|\r\n'):
            raise core.Error('Python 路径含 CMD 不支持的特殊字符；请使用普通安装路径。')
        files[output / 'AgentSync.cmd'] = (
            '@echo off\r\nchcp 65001 >nul\r\nsetlocal DisableDelayedExpansion\r\n'
            '"' + config['python'] + '" "%~dp0launcher.py"\r\nexit /b %errorlevel%\r\n'
        ).encode('utf-8')
    else:
        executable = output / 'AgentSync.command'
        files[executable] = (
            '#!/bin/sh\ncd ' + shlex.quote(config['source']) + ' || exit 1\nexec '
            + shlex.quote(config['python']) + ' ' + shlex.quote(str(output / 'launcher.py')) + '\n'
        ).encode('utf-8')

    raw_selection = core._bytes(selection)
    if raw_selection is None:
        files[selection] = _json({'version': 1, 'files': []})
        selected = False
    else:
        try:
            document = json.loads(raw_selection)
            selected = (isinstance(document, dict) and document.get('version') == 1
                        and isinstance(document.get('files'), list) and bool(document['files']))
        except (ValueError, UnicodeError):
            raise core.Error('现有 selection.json 无效；已保留，请先手工修正。') from None
    skills_required = any(g['category'] == 'skills' and g['name'] in bindings for g in groups) and not selected

    # Complete text preflight before even creating an output directory. For a
    # binary link, a configuration-bound ownership manifest validates reuse.
    _preflight(files)
    if platform == 'windows':
        link = core._safe(output / 'AgentSync.lnk')
        manifest = core._safe(output / '.agent-sync-launchers.json')
        old_link, old_manifest = core._bytes(link), core._bytes(manifest)
        if (old_link is None) != (old_manifest is None):
            raise core.Error('未知或不完整的 .lnk 所有权记录；未覆盖文件。')
        if old_link is None:
            core._mkdir(output)
            link_bytes = _windows_link(config, output)
        else:
            link_bytes = old_link
        files[link] = link_bytes
        files[manifest] = _json({'owner': 'AgentSync-launchers-v1',
                                 'config_sha256': _sha(_json(config)),
                                 'launcher_sha256': _sha(launcher),
                                 'lnk_sha256': _sha(link_bytes)})
    _preflight(files)
    # Exclusive creation rather than replacement: an unexpected concurrent file
    # is never silently overwritten. Like core, this is not a hostile-race sandbox.
    for path, data in files.items():
        if core._bytes(core._safe(path)) is None:
            core._mkdir(path.parent)
            with path.open('xb') as stream:
                stream.write(data)
    if executable is not None:
        executable.chmod(executable.stat().st_mode | 0o111)
    return {'status': 'created', 'visibility': target['visibility'],
            'platform': platform, 'output': str(output),
            'selection_required': skills_required,
            'message': ('技能需要逐机配置 selection.json 的已审阅映射；空模板未接入任何原技能。'
                        if skills_required else '入口已生成；未扫描家目录、复制资料或执行同步。')}
