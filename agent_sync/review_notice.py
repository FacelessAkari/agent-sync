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
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AgentSyncIdentity {
    [StructLayout(LayoutKind.Sequential)] struct Key { public Guid format; public uint id; }
    [StructLayout(LayoutKind.Explicit, Size=24)] struct Value {
        [FieldOffset(0)] public ushort type; [FieldOffset(8)] public IntPtr text;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface Store {
        void GetCount(out uint count);
        void GetAt(uint index, out Key key);
        void GetValue(ref Key key, out Value value);
        void SetValue(ref Key key, ref Value value);
        void Commit();
    }
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
    static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr context, uint flags,
        ref Guid interfaceId, [MarshalAs(UnmanagedType.Interface)] out Store store);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
    static extern void SHChangeNotify(uint change, uint flags, string path, IntPtr unused);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
    public static extern void SetCurrentProcessExplicitAppUserModelID(string id);
    public static void SetShortcut(string path, string id) {
        Guid guid = typeof(Store).GUID;
        Store store;
        SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref guid, out store);
        Key key = new Key { format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), id = 5 };
        Value value = new Value { type = 31, text = Marshal.StringToCoTaskMemUni(id) };
        try { store.SetValue(ref key, ref value); store.Commit(); }
        finally { Marshal.FreeCoTaskMem(value.text); Marshal.FinalReleaseComObject(store); }
        SHChangeNotify(0x2000, 0x1005, path, IntPtr.Zero);
    }
}
'@
[AgentSyncIdentity]::SetCurrentProcessExplicitAppUserModelID($id)
$instance = $env:AGENT_SYNC_REVIEW_INSTANCE
if ($instance -notmatch '^[0-9a-f]{16}$') { exit 2 }
$folder = Join-Path ([Environment]::GetFolderPath('Programs')) 'AgentSync'
$null = [IO.Directory]::CreateDirectory($folder)
$linkPath = Join-Path $folder ('AgentSync 待确认清单 - ' + $instance + '.lnk')
$editor = Join-Path $env:SystemRoot 'System32\notepad.exe'
$arguments = '"' + $env:AGENT_SYNC_REVIEW_REPORT + '"'
$wsh = New-Object -ComObject WScript.Shell
if (-not (Test-Path -LiteralPath $linkPath)) {
    $temporary = Join-Path $folder ([Guid]::NewGuid().ToString('N') + '.lnk')
    try {
        $link = $wsh.CreateShortcut($temporary)
        $link.TargetPath = $editor
        $link.Arguments = $arguments
        $link.Description = '查看本机 AgentSync 需要确认的文件'
        $link.Save()
        [AgentSyncIdentity]::SetShortcut($temporary, $id)
        [IO.File]::Move($temporary, $linkPath)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}
$link = $wsh.CreateShortcut($linkPath)
if ($link.TargetPath -ne $editor -or $link.Arguments -ne $arguments) { exit 2 }
$shellFolder = (New-Object -ComObject Shell.Application).NameSpace($folder)
if ($shellFolder.ParseName([IO.Path]::GetFileName($linkPath)).ExtendedProperty('System.AppUserModel.ID') -ne $id) { exit 2 }
# A click opens one fixed local report. No URI-supplied argument enters the command.
$scheme = 'agentsync-review-' + $instance
$command = '"' + $editor + '" ' + $arguments
$existing = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey($scheme)
if ($null -eq $existing) {
    $handler = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\' + $scheme)
    try {
        $handler.SetValue('', 'URL:AgentSync review')
        $handler.SetValue('URL Protocol', '')
        $open = $handler.CreateSubKey('shell\open\command')
        try { $open.SetValue('', $command) } finally { $open.Dispose() }
    } finally { $handler.Dispose() }
} else {
    try {
        $open = $existing.OpenSubKey('shell\open\command')
        if ($null -eq $open) { exit 2 }
        try {
            if ($existing.GetValue('') -ne 'URL:AgentSync review' -or
                $existing.GetValue('URL Protocol', 'absent') -ne '' -or $open.GetValue('') -ne $command) { exit 2 }
        } finally { $open.Dispose() }
    } finally { $existing.Dispose() }
}
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\AppUserModelId\' + $id)
try {
    $key.SetValue('DisplayName', 'AgentSync', [Microsoft.Win32.RegistryValueKind]::String)
    $key.SetValue('ShowInSettings', 1, [Microsoft.Win32.RegistryValueKind]::DWord)
} finally { $key.Dispose() }
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
$null = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>AgentSync 同步需要确认</text><text>有 COUNT 个文件等待你确认。点击查看清单，或在 Codex 中说“查看 AgentSync 待确认清单”。</text></binding></visual></toast>')
$xml.DocumentElement.SetAttribute('activationType', 'protocol')
$xml.DocumentElement.SetAttribute('launch', $scheme + ':open')
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)
try {
    if ($notifier.get_Setting().ToString() -ne 'Enabled') { exit 2 }
} catch {
    # Classic desktop senders may have no per-app notification record until their first toast.
    if ($_.Exception.GetBaseException().HResult -ne -2147023728) { throw }
    $systemNotifications = [Microsoft.Win32.Registry]::GetValue('HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\PushNotifications', 'ToastEnabled', 1)
    $perApp = [Microsoft.Win32.Registry]::GetValue(('HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\' + $id), 'Enabled', 1)
    if ($systemNotifications -eq 0 -or $perApp -eq 0) { exit 2 }
}
$notice = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notice.Tag = $instance
$notice.Group = 'review'
$notifier.Show($notice)
"""
_MACOS = """
on run argv
    set messageText to "有 " & (item 1 of argv) & " 个文件等待你确认。请在 Codex 中说“查看 AgentSync 待确认清单”。"
    display notification messageText with title "AgentSync 同步需要确认"
end run
"""


def _deliver(count, report_path):
    env = None
    if sys.platform == 'win32':
        # Use the inbox Windows PowerShell with WinRT, independent of the scheduled user's PATH.
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = _WINDOWS.replace('COUNT', str(count))
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        args = [str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
        instance = core._digest(os.fsencode(os.path.normcase(report_path)))[:16]
        env = dict(os.environ, AGENT_SYNC_REVIEW_REPORT=str(report_path), AGENT_SYNC_REVIEW_INSTANCE=instance)
    elif sys.platform == 'darwin':
        args = ['/usr/bin/osascript', '-e', _MACOS, str(count)]
    else:
        return False
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, timeout=20, shell=False,
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
                accepted = _deliver(len(report), path.with_name('pending.txt'))
            except (KeyError, OSError, subprocess.SubprocessError):
                accepted = False
            state = sent if accepted else {**sent, 'status': 'unavailable'}
            if state != previous:
                core._save(path, state)
            return state['status']
    except (ValueError, core.Error, OSError):
        return 'unavailable'
