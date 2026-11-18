"""Device-local reminders for unresolved files. No report content leaves the sync workspace."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from . import core


_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
$id = 'AgentSync.LocalSync.Review'
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\AppUserModelId\' + $id)
try {
    $key.SetValue('DisplayName', 'AgentSync', [Microsoft.Win32.RegistryValueKind]::String)
    $key.SetValue('ShowInSettings', 1, [Microsoft.Win32.RegistryValueKind]::DWord)
} finally { $key.Dispose() }
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
$null = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>AgentSync 同步需要确认</text><text>有 COUNT 个文件等待你确认。请在 Codex 中说“查看 AgentSync 待确认清单”。</text></binding></visual></toast>')
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)
if ($notifier.Setting.ToString() -ne 'Enabled') { exit 2 }
$notice = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notice.Tag = 'pending'
$notice.Group = 'review'
$notifier.Show($notice)
"""
_MACOS = """
on run argv
    set messageText to "有 " & (item 1 of argv) & " 个文件等待你确认。请在 Codex 中说“查看 AgentSync 待确认清单”。"
    display notification messageText with title "AgentSync 同步需要确认"
end run
"""


def _deliver(count):
    if sys.platform == 'win32':
        # Use the inbox Windows PowerShell with WinRT, independent of the scheduled user's PATH.
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = _WINDOWS.replace('COUNT', str(count))
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        args = [str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    elif sys.platform == 'darwin':
        args = ['/usr/bin/osascript', '-e', _MACOS, str(count)]
    else:
        return False
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=20, shell=False,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode == 0


def notify_pending(repo, report, signature):
    """Called under the workspace lock. 'sent' means accepted by the native notification API."""
    if not isinstance(report, list) or (report and (not isinstance(signature, str) or not core.HASH.fullmatch(signature))):
        return 'unavailable'
    try:
        with core._lock(repo):
            path = core._safe(Path(repo) / '.agent-sync/conflicts/notice.json')
            raw = core._bytes(path)
            if not report:
                if raw is not None:
                    core._safe(path).unlink()
                return 'clear'
            try:
                previous = json.loads(raw) if raw else None
            except (UnicodeError, ValueError):
                previous = None
            sent = {'version': 1, 'signature': signature, 'status': 'sent'}
            if previous == sent:
                return 'unchanged'
            try:
                accepted = _deliver(len(report))
            except (KeyError, OSError, subprocess.SubprocessError):
                accepted = False
            state = sent if accepted else {**sent, 'status': 'unavailable'}
            if state != previous:
                core._save(path, state)
            return state['status']
    except (ValueError, core.Error, OSError):
        return 'unavailable'
