"""Device-local reminders for unresolved files. No report content leaves the sync workspace."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from . import core


_WINDOWS_IDENTITY = r"""
$ErrorActionPreference = 'Stop'
$id = 'AgentSync.LocalSync.Review'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
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
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class ShellLink {}
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface LinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int capacity, IntPtr findData, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int capacity);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int capacity);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int capacity);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short key);
        void SetHotkey(short key);
        void GetShowCmd(out int cmd);
        void SetShowCmd(int cmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int capacity, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr hwnd, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    [DllImport("ole32.dll")] static extern int PropVariantClear(ref Value value);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
    public static extern void SetCurrentProcessExplicitAppUserModelID(string id);
    static Key appId = new Key { format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), id = 5 };
    public static void CreateShortcut(string path, string target, string args, string id) {
        object raw = new ShellLink();
        try {
            LinkW link = (LinkW)raw;
            link.SetPath(target);
            link.SetArguments(args);
            link.SetDescription("查看本机 AgentSync 需要确认的文件");
            Store properties = (Store)raw;
            Value value = new Value { type = 31, text = Marshal.StringToCoTaskMemUni(id) };
            try { properties.SetValue(ref appId, ref value); properties.Commit(); }
            finally { PropVariantClear(ref value); }
            ((IPersistFile)raw).Save(path, true);
        } finally { Marshal.FinalReleaseComObject(raw); }
    }
    public static bool MatchesShortcut(string path, string target, string args, string id) {
        object raw = new ShellLink();
        try {
            ((IPersistFile)raw).Load(path, 0);
            LinkW link = (LinkW)raw;
            StringBuilder actualTarget = new StringBuilder(32768), actualArgs = new StringBuilder(32768);
            link.GetPath(actualTarget, actualTarget.Capacity, IntPtr.Zero, 0);
            link.GetArguments(actualArgs, actualArgs.Capacity);
            Value value;
            ((Store)raw).GetValue(ref appId, out value);
            try { return String.Equals(actualTarget.ToString(), target, StringComparison.OrdinalIgnoreCase)
                && actualArgs.ToString() == args && value.type == 31 && Marshal.PtrToStringUni(value.text) == id; }
            finally { PropVariantClear(ref value); }
        } finally { Marshal.FinalReleaseComObject(raw); }
    }
}
'@
[AgentSyncIdentity]::SetCurrentProcessExplicitAppUserModelID($id)
$instance = $env:AGENT_SYNC_REVIEW_INSTANCE
if ($instance -notmatch '^[0-9a-f]{16}$') { exit 2 }
"""
_WINDOWS_SHORTCUT = r"""
$null = [IO.Directory]::CreateDirectory($folder)
$linkPath = Join-Path $folder ('AgentSync 待确认清单 - ' + $instance + '.lnk')
$editor = Join-Path $env:SystemRoot 'System32\notepad.exe'
$arguments = '"' + $env:AGENT_SYNC_REVIEW_REPORT + '"'
if (-not (Test-Path -LiteralPath $linkPath)) {
    $temporary = Join-Path $folder ([Guid]::NewGuid().ToString('N') + '.lnk')
    try {
        [AgentSyncIdentity]::CreateShortcut($temporary, $editor, $arguments, $id)
        [IO.File]::Move($temporary, $linkPath)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}
if (-not [AgentSyncIdentity]::MatchesShortcut($linkPath, $editor, $arguments, $id)) { exit 2 }
"""
_WINDOWS = (_WINDOWS_IDENTITY + "\n$folder = Join-Path ([Environment]::GetFolderPath('Programs')) 'AgentSync'\n"
            + _WINDOWS_SHORTCUT) + r"""
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
    if ($null -eq $key.GetValue('DisplayName')) {
        $key.SetValue('DisplayName', 'AgentSync', [Microsoft.Win32.RegistryValueKind]::String)
    }
    if ($null -eq $key.GetValue('ShowInSettings')) {
        $key.SetValue('ShowInSettings', 1, [Microsoft.Win32.RegistryValueKind]::DWord)
    }
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
