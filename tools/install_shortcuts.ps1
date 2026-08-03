<#
.SYNOPSIS
    Creates Start Menu and Desktop shortcuts for Transcriptor Prime.

.DESCRIPTION
    Run this once (or use install-shortcuts.bat) to get a proper Windows
    taskbar item:

        pwsh -File tools\install_shortcuts.ps1

    Then right-click the Start Menu entry and choose "Pin to taskbar".

    Each shortcut launches pythonw.exe from the project's virtual environment
    with no console window, carries the app icon, and is stamped with the same
    AppUserModelID the running process sets for itself
    (transcriptor_prime.APP_USER_MODEL_ID). That last part is what makes the
    pinned button and the live window share a single taskbar entry instead of
    appearing twice.

.PARAMETER NoDesktop
    Create only the Start Menu shortcut.

.PARAMETER Uninstall
    Remove the shortcuts this script created.
#>
[CmdletBinding()]
param(
    [switch]$NoDesktop,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ShortcutName = "Transcriptor Prime.lnk"
$StartMenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "Transcriptor Prime"
$StartMenuLink = Join-Path $StartMenuDir $ShortcutName
$DesktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) $ShortcutName

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if ($Uninstall) {
    foreach ($path in @($DesktopLink, $StartMenuLink)) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Output "removed $path" }
    }
    if ((Test-Path $StartMenuDir) -and -not (Get-ChildItem $StartMenuDir -Force)) {
        Remove-Item $StartMenuDir -Force
        Write-Output "removed $StartMenuDir"
    }
    Write-Output ""
    Write-Output "If the app was pinned, unpin it from the taskbar separately."
    return
}

# ---------------------------------------------------------------------------
# Locate the pieces
# ---------------------------------------------------------------------------
# pythonw.exe is the shortcut target (no console window); python.exe is used
# for the introspection call below, because pythonw has no usable stdout.
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Pythonw)) {
    throw "No virtual environment found at $Pythonw. Run run.bat once first, then re-run this script."
}

$IconPath = Join-Path $ProjectRoot "src\transcriptor_prime\assets\transcriptor-prime.ico"
if (-not (Test-Path $IconPath)) {
    throw "Icon not found at $IconPath. Run tools\make_icon.ps1 to regenerate it."
}

# Read the identity from the package so the shortcut can never drift from what
# the process sets at runtime.
$SrcDir = Join-Path $ProjectRoot "src"
$AppId = & $Python "-c" "import sys; sys.path.insert(0, r'$SrcDir'); import transcriptor_prime as t; sys.stdout.write(t.APP_USER_MODEL_ID)"
if ([string]::IsNullOrWhiteSpace($AppId)) {
    throw "Could not read APP_USER_MODEL_ID from the transcriptor_prime package."
}
$AppId = $AppId.Trim()

# PYTHONPATH is not inherited by a shortcut, so the launcher has to put src on
# sys.path itself. -c keeps that to a single self-contained command.
$Arguments = "-c ""import sys; sys.path.insert(0, r'$SrcDir'); from transcriptor_prime.app import run; run()"""

# ---------------------------------------------------------------------------
# Stamp System.AppUserModel.ID onto a .lnk
#
# WScript.Shell cannot set shell property-store values, so this drops to the
# COM interfaces (IShellLink / IPersistFile / IPropertyStore) directly.
# ---------------------------------------------------------------------------
if (-not ("ShortcutIdStamper" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential)]
public struct PropertyKey {
    public Guid fmtid;
    public uint pid;
    public PropertyKey(Guid f, uint p) { fmtid = f; pid = p; }
}

[StructLayout(LayoutKind.Explicit)]
public struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pointerValue;
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    int GetCount(out uint cProps);
    int GetAt(uint iProp, out PropertyKey pkey);
    int GetValue(ref PropertyKey key, out PropVariant pv);
    int SetValue(ref PropertyKey key, ref PropVariant pv);
    int Commit();
}

[ComImport, Guid("0000010B-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPersistFile {
    int GetClassID(out Guid pClassID);
    int IsDirty();
    int Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
    int Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, [MarshalAs(UnmanagedType.Bool)] bool fRemember);
    int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    int GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
}

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
public class ShellLink { }

public static class ShortcutIdStamper {
    [DllImport("ole32.dll")]
    private static extern int PropVariantClear(ref PropVariant pvar);

    private const ushort VT_LPWSTR = 31;

    // System.AppUserModel.ID
    private static readonly Guid AppUserModel =
        new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");

    public static void Stamp(string shortcutPath, string appId) {
        object link = new ShellLink();
        IPersistFile file = (IPersistFile)link;
        int hr = file.Load(shortcutPath, 2 /* STGM_READWRITE */);
        if (hr != 0) throw new COMException("IPersistFile.Load failed", hr);

        IPropertyStore store = (IPropertyStore)link;

        // A VT_LPWSTR PROPVARIANT is just the tag plus a CoTaskMem-allocated
        // wide string, so build it by hand rather than pulling in propsys.
        PropVariant value = new PropVariant();
        value.vt = VT_LPWSTR;
        value.pointerValue = Marshal.StringToCoTaskMemUni(appId);

        try {
            PropertyKey key = new PropertyKey(AppUserModel, 5);
            hr = store.SetValue(ref key, ref value);
            if (hr != 0) throw new COMException("IPropertyStore.SetValue failed", hr);
            hr = store.Commit();
            if (hr != 0) throw new COMException("IPropertyStore.Commit failed", hr);
            hr = file.Save(shortcutPath, true);
            if (hr != 0) throw new COMException("IPersistFile.Save failed", hr);
        } finally {
            // Frees the string allocation above.
            PropVariantClear(ref value);
            Marshal.ReleaseComObject(link);
        }
    }

    public static string Read(string shortcutPath) {
        object link = new ShellLink();
        IPersistFile file = (IPersistFile)link;
        int hr = file.Load(shortcutPath, 0 /* STGM_READ */);
        if (hr != 0) throw new COMException("IPersistFile.Load failed", hr);

        IPropertyStore store = (IPropertyStore)link;
        PropertyKey key = new PropertyKey(AppUserModel, 5);
        PropVariant value;
        hr = store.GetValue(ref key, out value);
        if (hr != 0) throw new COMException("IPropertyStore.GetValue failed", hr);

        try {
            if (value.vt != VT_LPWSTR) return null;
            return Marshal.PtrToStringUni(value.pointerValue);
        } finally {
            PropVariantClear(ref value);
            Marshal.ReleaseComObject(link);
        }
    }
}
"@
}

function New-AppShortcut {
    param([string]$Path)

    $dir = Split-Path $Path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    $shell = New-Object -ComObject WScript.Shell
    try {
        $link = $shell.CreateShortcut($Path)
        $link.TargetPath = $Pythonw
        $link.Arguments = $Arguments
        $link.WorkingDirectory = $ProjectRoot
        $link.IconLocation = "$IconPath,0"
        $link.Description = "Transcribe audio and video to timecoded text, entirely offline"
        $link.WindowStyle = 1
        $link.Save()
    } finally {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }

    [ShortcutIdStamper]::Stamp($Path, $AppId)
    Write-Output "created $Path"
}

New-AppShortcut -Path $StartMenuLink
if (-not $NoDesktop) { New-AppShortcut -Path $DesktopLink }

Write-Output ""
Write-Output "AppUserModelID: $AppId"
Write-Output ""
Write-Output "To pin to the taskbar: right-click the Start Menu entry"
Write-Output "  (Start > Transcriptor Prime) and choose 'Pin to taskbar'."
