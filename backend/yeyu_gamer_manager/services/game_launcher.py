"""Manager-owned, fixed-path game client launch before an Adapter Host run."""

from __future__ import annotations

import csv
import configparser
import ctypes
import os
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from ctypes import wintypes
from typing import Any

from .window_capture import registered_game_process_names


_DLL_DIRECTORY_LOCK = threading.Lock()


class GameLaunchError(RuntimeError):
    """The configured game client could not be made available to its Adapter."""


class GameLaunchCancelled(GameLaunchError):
    """The persisted Manager cancellation intent stopped the pre-Adapter launch gate."""


class GameLaunchHumanRequired(GameLaunchError):
    """Preserve the verified launch surface for the existing Manager takeover flow."""

    def __init__(
        self, reason_code: str, message: str, *,
        detail: dict[str, object] | None = None,
        process_ids: frozenset[int] = frozenset(),
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = dict(detail or {})
        self.process_ids = process_ids


@dataclass(frozen=True, slots=True)
class GameLaunchReceipt:
    state: str
    process_id: int | None
    observed_process_name: str
    baseline_process_ids: tuple[int, ...] = ()
    expected_process_names: tuple[str, ...] = ()
    ready_window_pid: int | None = None
    ready_window_width: int | None = None
    ready_window_height: int | None = None
    ww_launcher_path: str | None = None

    def as_result(self) -> dict[str, object]:
        return {
            "state": self.state,
            "processId": self.process_id,
            "observedProcessName": self.observed_process_name,
            "managerOwned": self.state == "started",
            "readyWindowPid": self.ready_window_pid,
            "readyWindowWidth": self.ready_window_width,
            "readyWindowHeight": self.ready_window_height,
        }


@dataclass(frozen=True, slots=True)
class GameCloseReceipt:
    state: str
    requested_process_ids: tuple[int, ...]
    remaining_process_ids: tuple[int, ...]
    zombie_process_ids: tuple[int, ...] = ()
    unverified_process_ids: tuple[int, ...] = ()
    memory_before: dict[str, int] | None = None
    memory_after: dict[str, int] | None = None

    def as_result(self) -> dict[str, object]:
        return {
            "state": self.state,
            "requestedProcessIds": list(self.requested_process_ids),
            "remainingProcessIds": list(self.remaining_process_ids),
            "zombieProcessIds": list(self.zombie_process_ids),
            "unverifiedProcessIds": list(self.unverified_process_ids),
            "memoryBefore": self.memory_before,
            "memoryAfter": self.memory_after,
        }


@dataclass(frozen=True, slots=True)
class _QueueProcessIdentity:
    executable: Path
    created_at: int


@dataclass(frozen=True, slots=True)
class LaunchObservation:
    """One launch-phase checkpoint the Manager may turn into a screenshot/event.

    ``phase`` is one of ``launcher-waiting`` (periodic heartbeat while no game
    window exists yet), ``launcher-action`` (an audited launcher button was
    driven), ``ready`` (a stable game window appeared), ``launch-cancelled``
    (the durable Manager request stopped this gate) or ``launch-failed``.
    ``process_names``/``process_ids`` bound which windows may be captured.
    """

    game_id: str
    phase: str
    elapsed_seconds: float
    process_names: frozenset[str]
    process_ids: frozenset[int]
    detail: dict[str, object]


LaunchObserver = Callable[[LaunchObservation], None]
LaunchCancellationCheck = Callable[[], bool]


class _LaunchProgressMonitor:
    """Detect a launcher download/patch through process disk-write growth.

    ``GetProcessIoCounters`` needs only PROCESS_QUERY_LIMITED_INFORMATION, so
    it works against launcher and anti-cheat protected client processes alike.
    Log chatter stays far below ``minimum_bytes`` per sample window; a game
    update writes tens of MB per second.
    """

    def __init__(self, *, sample_seconds: float, minimum_bytes: int) -> None:
        self.sample_seconds = sample_seconds
        self.minimum_bytes = minimum_bytes
        self._last_sample_at: float | None = None
        self._last_written: dict[int, int] = {}
        self.last_delta = 0
        self.active = False

    def observe(self, running: dict[int, str], now: float) -> bool:
        if self._last_sample_at is not None and now - self._last_sample_at < self.sample_seconds:
            return self.active
        written = {pid: _process_bytes_written(pid) for pid in running}
        delta = 0
        for pid, total in written.items():
            previous = self._last_written.get(pid)
            if previous is not None and total is not None and total > previous:
                delta += total - previous
        first_sample = self._last_sample_at is None
        self._last_sample_at = now
        self._last_written = {pid: total for pid, total in written.items() if total is not None}
        if first_sample:
            return False
        self.last_delta = delta
        self.active = delta >= self.minimum_bytes
        return self.active


def _process_bytes_written(process_id: int) -> int | None:
    if os.name != "nt":
        return None

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_COUNTERS)]
    kernel32.GetProcessIoCounters.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, process_id)
    if not handle:
        return None
    try:
        counters = IO_COUNTERS()
        if not kernel32.GetProcessIoCounters(handle, ctypes.byref(counters)):
            return None
        return int(counters.WriteTransferCount)
    finally:
        kernel32.CloseHandle(handle)


class GameLaunchService:
    """Launch only a configured local EXE and wait for its fixed game binding."""

    START_TIMEOUT_SECONDS = 45.0
    READY_TIMEOUT_SECONDS = 300.0
    READY_STABLE_SECONDS = 2.0
    READY_STABLE_OVERRIDES: dict[str, float] = {"WW": 10.0}
    # While an official launcher is downloading or patching the game the
    # audited start button is hidden.  Disk-write activity of the launcher /
    # client processes is the update signal: as long as it keeps flowing the
    # launcher gate is extended (never past the hard cap) and no "no effect"
    # strikes are counted.
    LAUNCH_OBSERVATION_INTERVAL_SECONDS = 60.0
    LAUNCH_PROGRESS_SAMPLE_SECONDS = 15.0
    LAUNCH_PROGRESS_MIN_BYTES = 4 * 1024 * 1024
    LAUNCHER_UPDATE_HARD_CAP_SECONDS = 3 * 3600.0
    # The installed OK-WW profile has "Launch with DX11" enabled.  Its proven
    # formal launcher expands that option to these fixed arguments.  Keep the
    # Manager-owned game-first launch semantically identical; D3D12 has been
    # observed crashing Client-Win64-Shipping during automated combat here.
    WW_DX11_ARGUMENTS = "-dx11 -d3d11 -force-d3d11"
    WW_LAUNCHER_POLL_SECONDS = 3.0
    WW_LAUNCHER_UI_READY_SECONDS = 30.0
    WW_LAUNCHER_PROCESS_NAMES = frozenset({"launcher.exe", "launcher_main.exe", "launcher_updater.exe"})
    # Only the evidenced Chinese entry label is approved. No coordinate or
    # keyboard fallback, consent acceptance, updater action, or guessed flags.
    _WW_LAUNCHER_SIGNATURE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $path = [IO.Path]::GetFullPath($env:YEYU_WW_LAUNCHER_EXE)
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($path))
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    $info = [Diagnostics.FileVersionInfo]::GetVersionInfo($path)
    if ($drive.DriveType -ne [IO.DriveType]::Fixed -or
        [IO.Path]::GetFileName($path) -ine 'launcher.exe' -or
        $signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne '广州库洛科技有限公司' -or
        $info.ProductName -ne '鸣潮') { Write-Output 'untrusted:ww-launcher'; exit 2 }
    Write-Output 'trusted:ww-launcher'; exit 0
} catch { Write-Output 'untrusted:ww-launcher'; exit 2 }
'''
    _WW_LAUNCHER_UIA_RULES_SCRIPT = r'''
function Test-YeYuWwWebViewMetadata {
    param([string]$LauncherImage, [int]$WindowPid, [int]$ParentPid, [string]$Image,
          [string]$SignatureStatus, [string]$Signer)
    $expected = Join-Path ([IO.Path]::GetDirectoryName($LauncherImage)) 'KRWebViewRuntime\msedgewebview2.exe'
    return ($ParentPid -eq $WindowPid -and [IO.Path]::GetFullPath($Image) -ieq $expected -and
        $SignatureStatus -eq 'Valid' -and $Signer -ceq 'Microsoft Corporation')
}
function Get-YeYuWwElementDisposition {
    param([string]$Name, [string]$Kind, [string]$ClassName, [string]$LocalizedKind,
          [bool]$IsPassword, [bool]$IsModal)
    if ($IsPassword -or ($Kind -eq 'ControlType.Button' -and
        $Name -match '^(登录|立即登录|扫码登录|同意|同意并继续|接受|接受并继续)$')) { return 'human:login-or-consent' }
    # Dialog semantics and confirmation controls remain blocking even when
    # the normal entry button is still present behind the modal.
    if ($IsModal -or $Kind -eq 'ControlType.Window' -or $LocalizedKind -match '^(dialog|alertdialog|对话框|警报对话框)$' -or
        ($Kind -eq 'ControlType.Button' -and $Name -match '^(确认|确定|取消|重试)$')) { return 'human:modal-dialog' }
    $mainAction = $Kind -eq 'ControlType.Button' -and $ClassName -cmatch '(^|\s)launcher-button(\s|$)' -and
        $ClassName -cmatch '(^|\s)status-btn(\s|$)'
    if ($mainAction) {
        if ($Name -ceq '进入游戏') { return 'entry' }
        if ($Name -match '(更新|下载|安装|修复|重试|失败|异常)') { return 'human:update-or-error' }
        return 'human:unrecognized-primary-action'
    }
    if ($Kind -eq 'ControlType.Button' -and $Name -ceq '进入游戏') { return 'human:unrecognized-primary-action' }
    # Toolbar repair links and news text do not describe the primary state.
    return 'ignore'
}
'''
    _WW_LAUNCHER_UIA_SCRIPT = _WW_LAUNCHER_UIA_RULES_SCRIPT + r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $root = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($env:YEYU_WW_LAUNCHER_EXE))
    $prefix = $root.TrimEnd('\') + '\'
    $candidates = @()
    $windowImages = @{}
    foreach ($pidText in ($env:YEYU_WW_LAUNCHER_PIDS -split ',')) {
        if ($pidText -notmatch '^\d+$') { continue }
        $process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
        if ($null -eq $process -or $process.MainWindowHandle -eq 0) { continue }
        $path = [IO.Path]::GetFullPath($process.Path)
        if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $relative = $path.Substring($prefix.Length)
        if ($relative -notmatch '^(launcher\.exe|\d+\.\d+\.\d+\.\d+\\(launcher|launcher_main)\.exe)$') { continue }
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne '广州库洛科技有限公司') {
            Write-Output 'human:untrusted-window-owner'; exit 6
        }
        $window = [Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
        if ($window.Current.ProcessId -ne $process.Id -or $window.Current.IsOffscreen) { continue }
        $candidates += $window
        $windowImages[$process.Id] = $path
    }
    if ($candidates.Count -eq 0) { Write-Output 'waiting:launcher-window'; exit 3 }
    if ($candidates.Count -ne 1) { Write-Output 'human:ambiguous-launcher-window'; exit 6 }
    $window = $candidates[0]
    function Test-YeYuWwElementOwner {
        param([int]$OwnerPid)
        if ($OwnerPid -eq $window.Current.ProcessId) { return $true }
        $entry = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerPid"
        if ($null -eq $entry -or [int]$entry.ParentProcessId -ne $window.Current.ProcessId) { return $false }
        $launcherImage = $windowImages[$window.Current.ProcessId]
        $expected = Join-Path ([IO.Path]::GetDirectoryName($launcherImage)) 'KRWebViewRuntime\msedgewebview2.exe'
        if ([IO.Path]::GetFullPath($entry.ExecutablePath) -ine $expected) { return $false }
        foreach ($item in @($expected, (Split-Path -Parent $expected))) {
            if ((Get-Item -LiteralPath $item).Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $expected
        $signer = $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
        return Test-YeYuWwWebViewMetadata -LauncherImage $launcherImage -WindowPid $window.Current.ProcessId `
            -ParentPid $entry.ParentProcessId -Image $entry.ExecutablePath -SignatureStatus $signature.Status -Signer $signer
    }
    $all = $window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $buttons = @()
    foreach ($element in $all) {
        if ($element.Current.IsOffscreen) { continue }
        $name = $element.Current.Name.Trim()
        $kind = $element.Current.ControlType
        $windowPattern = $null
        $isModal = $false
        if ($element.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern, [ref]$windowPattern)) { $isModal = $windowPattern.Current.IsModal }
        $disposition = Get-YeYuWwElementDisposition -Name $name -Kind $kind.ProgrammaticName `
            -ClassName $element.Current.ClassName -LocalizedKind $element.Current.LocalizedControlType `
            -IsPassword $element.Current.IsPassword -IsModal $isModal
        if ($disposition -eq 'ignore') { continue }
        if (-not (Test-YeYuWwElementOwner -OwnerPid $element.Current.ProcessId)) { Write-Output 'human:untrusted-element-owner'; exit 6 }
        if ($disposition.StartsWith('human:')) { Write-Output $disposition; exit 6 }
        if ($disposition -eq 'entry') { $buttons += $element }
    }
    if ($buttons.Count -eq 0) { Write-Output 'waiting:unrecognized-launcher-ui'; exit 3 }
    if ($buttons.Count -ne 1) { Write-Output 'human:ambiguous-enter-game-button'; exit 6 }
    $button = $buttons[0]
    if (-not (Test-YeYuWwElementOwner -OwnerPid $button.Current.ProcessId) -or $button.Current.IsOffscreen -or
        -not $button.Current.IsEnabled -or $button.Current.Name -cne '进入游戏') {
        Write-Output 'human:button-owner-or-state-changed'; exit 6
    }
    $pattern = $null
    if (-not $button.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        Write-Output 'human:invoke-pattern-unavailable'; exit 6
    }
    if ($env:YEYU_WW_ALLOW_INVOKE -ne '1') { Write-Output 'ready:ww-enter-game'; exit 3 }
    $pattern.Invoke()
    Write-Output 'invoked:ww-enter-game'; exit 0
} catch { Write-Output 'human:uia-probe-failed'; exit 6 }
'''
    POLL_INTERVAL_SECONDS = 0.5
    # Prefer a normal window close. A successful termination request or final
    # exit code does not prove that the process has disappeared from enumeration.
    GRACEFUL_CLOSE_SECONDS = 25.0
    TERMINATE_CLOSE_SECONDS = 10.0
    READY_PROCESS_NAMES: dict[str, frozenset[str]] = {
        # The configured WW executable is a launcher.  Its presence never
        # means the interactive game window is ready for OK-WW.
        "WW": frozenset({"client-win64-shipping.exe"}),
        # NTE's official launcher replaces NTELauncher.exe with NTEGame.exe.
        # Neither shell is proof that the actual client is ready for ok-nte.
        "NTE": frozenset({"htgame.exe", "nte.exe", "neverness to everness.exe"}),
        # The Hypergryph launcher (Launcher.exe -> Games.exe) is only the
        # formal update/start surface.  It must never satisfy the game-ready
        # gate; OK-EF may start only after the actual client has a stable
        # visible window.
        "Endfield": frozenset({"endfield.exe", "endfield-win64-shipping.exe"}),
    }
    READY_TIMEOUT_OVERRIDES: dict[str, float] = {"NTE": 3600.0, "Endfield": 1800.0}
    STARTED_GAME_HANDOFF_SECONDS: dict[str, float] = {
        # These clients expose a large visible window before the account has
        # entered the playable scene.  Only a client created by this Manager
        # run receives a bounded startup delay before the Adapter starts.
        "PGR": 75.0,
        "StarRail": 75.0,
        "Endfield": 75.0,
    }
    # PGR normally enters the playable home screen without another click.  A
    # blind center click after that transition opens an item-detail modal and
    # prevents MPA's formal startup task from recognizing the main screen.
    # Keep PGR's startup delay, but reserve title/login clicks for clients that
    # are known to require them.
    STARTED_GAME_CLICK_HANDOFFS = frozenset({"StarRail", "Endfield"})
    NTE_LAUNCHER_POLL_SECONDS = 3.0
    NTE_MAX_NO_EFFECT_ACTIONS = 3
    ENDFIELD_LAUNCHER_POLL_SECONDS = 5.0
    ENDFIELD_MAX_NO_EFFECT_ACTIONS = 3
    # The Hypergryph launcher exposes its primary action as a Chromium button
    # that may be absent from UI Automation.  Once the web surface has loaded
    # but no audited button appeared for this long, the fixed primary-action
    # point becomes an allowed fallback.
    ENDFIELD_FIXED_FALLBACK_AFTER_SECONDS = 60.0
    # An official launcher that never exposes its audited primary action (blank
    # web surface, stuck bootstrap, endless splash) must fail with a typed
    # message instead of consuming the whole READY_TIMEOUT.  Game downloads
    # driven by the launcher keep the action hidden too, so this bound stays
    # generous.
    LAUNCHER_UI_READY_TIMEOUT_SECONDS = 900.0
    # Process-list probes see a client in kernel teardown as "running" for as
    # long as a driver holds its last thread.  Such a process has already
    # exited (GetExitCodeProcess != STILL_ACTIVE) and can never expose a game
    # window, so launch/ready/close gates must not treat it as a live client.
    STILL_ACTIVE = 259
    ENDFIELD_STALE_HEADLESS_SECONDS = 300.0
    # Current official public-desktop shortcut targets the root Launcher.exe.
    # Games.exe's internal --region=CN argument is not the root CLI contract.
    ENDFIELD_LAUNCHER_ARGUMENTS = ("--game=endfield", "--reason=4")
    ENDFIELD_REACTIVATION_READY_SECONDS = 90.0
    _ENDFIELD_REACTIVATION_IDENTITY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
    $launcher = [IO.Path]::GetFullPath($env:YEYU_ENDFIELD_REACTIVATE_EXE)
    $root = Split-Path -Parent $launcher
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($launcher))
    if ($drive.DriveType -ne [IO.DriveType]::Fixed -or (Split-Path -Leaf $root) -ne 'Hypergryph Launcher') { exit 6 }
    $targetPid = [int]$env:YEYU_ENDFIELD_REACTIVATE_PID
    $entry = Get-CimInstance Win32_Process -Filter "ProcessId=$targetPid"
    if ($null -eq $entry -or $entry.Name -ne 'Games.exe') { exit 6 }
    $image = [IO.Path]::GetFullPath($entry.ExecutablePath)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not $image.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { exit 6 }
    $relative = $image.Substring($prefix.Length)
    if ($relative -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?\\Games\.exe$') { exit 6 }
    $selectors = [regex]::Matches($entry.CommandLine, '(?i)(?:^|\s)--game=([^\s]+)')
    if ($selectors.Count -ne 1 -or $selectors[0].Groups[1].Value -ine 'Endfield') { exit 6 }
    if ($entry.CommandLine -notmatch '(?i)(?:^|\s)--region=CN(?:\s|$)') { exit 6 }
    foreach ($path in @($root, (Split-Path -Parent $image), $launcher, $image)) {
        if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 6 }
    }
    foreach ($path in @($launcher, $image)) {
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        $signer = $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
        $info = [Diagnostics.FileVersionInfo]::GetVersionInfo($path)
        if ($signature.Status -ne 'Valid' -or $signer -ne 'Shanghai Hypergryph Network Technology Co., Ltd.' -or
            $info.ProductName -ne '鹰角启动器' -or $info.OriginalFilename -ine [IO.Path]::GetFileName($path)) { exit 6 }
    }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class YeYuEndfieldVisibleProbe {
    public delegate bool Callback(IntPtr hwnd, IntPtr arg);
    [DllImport("user32.dll")] public static extern bool EnumWindows(Callback callback, IntPtr arg);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    public static bool HasWindow(uint pid) {
        bool found = false;
        if (!EnumWindows((hwnd, arg) => { uint owner; GetWindowThreadProcessId(hwnd, out owner);
            if (owner == pid && IsWindowVisible(hwnd)) found = true; return true; }, IntPtr.Zero))
            throw new InvalidOperationException("window enumeration failed");
        return found;
    }
}
'@
    if ([YeYuEndfieldVisibleProbe]::HasWindow($targetPid)) { Write-Output 'trusted:visible'; exit 0 }
    Write-Output 'trusted:headless'; exit 0
} catch { exit 6 }
'''
    ENDFIELD_LOCAL_LAUNCHER_CANDIDATES = (
        Path(r"C:\Program Files\Hypergryph Launcher\Launcher.exe"),
        Path(r"C:\Program Files (x86)\Hypergryph Launcher\Launcher.exe"),
        Path(r"C:\Hypergryph Launcher\Launcher.exe"),
    )

    @staticmethod
    def _external_process_environment() -> dict[str, str]:
        """Remove packaged-Python state before starting a native game client.

        PyInstaller deliberately adjusts its own DLL/Python lookup state.  A
        native child must not inherit that state: WW otherwise loads
        ``VCRUNTIME140*.dll`` from YeYu Gamer's ``_internal`` directory and can
        crash inside KERNELBASE during normal play.
        """

        environment = os.environ.copy()
        for key in tuple(environment):
            normalized = key.upper()
            if normalized.startswith("_PYI_") or normalized.startswith("PYINSTALLER_"):
                environment.pop(key, None)
        for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"):
            environment.pop(key, None)

        bundle_roots: list[Path] = [Path(sys.executable).resolve().parent]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundle_roots.append(Path(meipass).resolve())

        clean_path: list[str] = []
        for entry in environment.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            try:
                candidate = Path(entry).resolve()
            except OSError:
                clean_path.append(entry)
                continue
            if any(candidate == root or root in candidate.parents for root in bundle_roots):
                continue
            clean_path.append(entry)
        environment["PATH"] = os.pathsep.join(clean_path)
        return environment

    @staticmethod
    @contextmanager
    def _native_child_dll_scope():
        """Temporarily clear PyInstaller's Windows DLL directory for spawn."""

        if os.name != "nt":
            yield
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_directory = kernel32.GetDllDirectoryW
        get_directory.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        get_directory.restype = wintypes.DWORD
        set_directory = kernel32.SetDllDirectoryW
        set_directory.argtypes = [wintypes.LPCWSTR]
        set_directory.restype = wintypes.BOOL

        with _DLL_DIRECTORY_LOCK:
            size = int(get_directory(0, None))
            previous: str | None = None
            if size:
                buffer = ctypes.create_unicode_buffer(size + 1)
                get_directory(len(buffer), buffer)
                previous = buffer.value or None
            if not set_directory(None):
                raise GameLaunchError("could not isolate the native game DLL search path")
            try:
                yield
            finally:
                set_directory(previous)
    _NTE_UIA_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$signature = @'
using System;
using System.Runtime.InteropServices;
public static class YeYuNteInput {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool AllowSetForegroundWindow(int processId);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr processId);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint source, uint target, bool attach);
    [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool LockSetForegroundWindow(uint lockCode);
    [DllImport("user32.dll")] public static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hWnd, ref YeYuNtePoint point);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint message, UIntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(YeYuNtePoint point);
    [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr hWnd, uint flags);
}
[StructLayout(LayoutKind.Sequential)]
public struct YeYuNtePoint { public int X; public int Y; }
'@
Add-Type -TypeDefinition $signature
function Test-YeYuPointOwnedBy {
    param([int]$X, [int]$Y, [int]$ProcessId)
    $point = New-Object YeYuNtePoint
    $point.X = $X; $point.Y = $Y
    $hit = [YeYuNteInput]::WindowFromPoint($point)
    if ($hit -eq [IntPtr]::Zero) { return $false }
    $ownerPid = [uint32]0
    $ptr = [Runtime.InteropServices.Marshal]::AllocHGlobal(4)
    try {
        [void][YeYuNteInput]::GetWindowThreadProcessId($hit, $ptr)
        $ownerPid = [uint32][Runtime.InteropServices.Marshal]::ReadInt32($ptr)
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($ptr) }
    return ($ownerPid -eq [uint32]$ProcessId)
}
function Set-YeYuLauncherForeground {
    param([IntPtr]$Handle)
    # A background Manager may only take the foreground after a synthetic
    # keystroke; Windows otherwise silently refuses SetForegroundWindow.
    [YeYuNteInput]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [YeYuNteInput]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    [YeYuNteInput]::ShowWindowAsync($Handle, 9) | Out-Null
    [YeYuNteInput]::BringWindowToTop($Handle) | Out-Null
    [YeYuNteInput]::SetForegroundWindow($Handle) | Out-Null
    Start-Sleep -Milliseconds 400
    return ([YeYuNteInput]::GetForegroundWindow() -eq $Handle)
}
$expected = [IO.Path]::GetFullPath($env:YEYU_NTE_LAUNCHER_ROOT).TrimEnd('\') + '\'
$names = @('开始更新','继续更新','开始游戏','启动游戏','进入游戏','继续下载','开始下载','立即更新','更新游戏')
foreach ($process in @(Get-Process -Name 'NTEGame' -ErrorAction SilentlyContinue)) {
    try {
        $image = [IO.Path]::GetFullPath($process.MainModule.FileName)
        if (-not $image.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($process.MainWindowHandle -eq 0) { continue }
        $root = [Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
        $condition = New-Object Windows.Automation.PropertyCondition(
            [Windows.Automation.AutomationElement]::ControlTypeProperty,
            [Windows.Automation.ControlType]::Button)
        foreach ($button in @($root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition))) {
            if ($names -notcontains $button.Current.Name -or -not $button.Current.IsEnabled) { continue }
            $rect = $button.Current.BoundingRectangle
            if ($button.Current.IsOffscreen -or $rect.Width -lt 40 -or $rect.Height -lt 20) { continue }
            $initialName = $button.Current.Name
            $initialEnabled = $button.Current.IsEnabled
            $pattern = $button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
            if ($null -eq $pattern) { continue }
            # Dispatch one input at a time and verify its effect before trying
            # the next formal-GUI path.  The previous bridge invoked the same
            # toggle through UIA, LegacyIAccessible, and two mouse clicks in
            # one burst, which could start and immediately cancel the client.
            $pattern.Invoke()
            Start-Sleep -Milliseconds 1500
            $changed = ($button.Current.Name -ne $initialName -or $button.Current.IsEnabled -ne $initialEnabled)
            if (-not $changed -and -not (Get-Process -Name 'HTGame','NTE','Neverness to Everness' -ErrorAction SilentlyContinue)) {
                $currentThread = [YeYuNteInput]::GetCurrentThreadId()
                $targetThread = [YeYuNteInput]::GetWindowThreadProcessId($process.MainWindowHandle, [IntPtr]::Zero)
                $foreground = [YeYuNteInput]::GetForegroundWindow()
                $foregroundThread = if ($foreground -ne [IntPtr]::Zero) {
                    [YeYuNteInput]::GetWindowThreadProcessId($foreground, [IntPtr]::Zero)
                } else { 0 }
                $attachedTarget = $false
                $attachedForeground = $false
                try {
                    [YeYuNteInput]::AllowSetForegroundWindow(-1) | Out-Null
                    [YeYuNteInput]::LockSetForegroundWindow(2) | Out-Null
                    if ($targetThread -ne 0 -and $targetThread -ne $currentThread) {
                        $attachedTarget = [YeYuNteInput]::AttachThreadInput($currentThread, $targetThread, $true)
                    }
                    if ($foregroundThread -ne 0 -and $foregroundThread -ne $currentThread -and $foregroundThread -ne $targetThread) {
                        $attachedForeground = [YeYuNteInput]::AttachThreadInput($currentThread, $foregroundThread, $true)
                    }
                    [YeYuNteInput]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
                    [YeYuNteInput]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
                    [YeYuNteInput]::ShowWindowAsync($process.MainWindowHandle, 9) | Out-Null
                    [YeYuNteInput]::BringWindowToTop($process.MainWindowHandle) | Out-Null
                    [YeYuNteInput]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
                    [YeYuNteInput]::SetActiveWindow($process.MainWindowHandle) | Out-Null
                    [YeYuNteInput]::SetFocus($process.MainWindowHandle) | Out-Null
                    try { $button.SetFocus() } catch { }
                    [YeYuNteInput]::keybd_event(0x20, 0, 0, [UIntPtr]::Zero)
                    [YeYuNteInput]::keybd_event(0x20, 0, 0x0002, [UIntPtr]::Zero)
                    Start-Sleep -Milliseconds 1500
                    if (Get-Process -Name 'HTGame','NTE','Neverness to Everness' -ErrorAction SilentlyContinue) {
                        Write-Output ('clicked:' + $initialName + ':game-started')
                        exit 0
                    }
                    try {
                        if ($button.Current.Name -ne $initialName -or $button.Current.IsEnabled -ne $initialEnabled) {
                            Write-Output ('clicked:' + $initialName + ':state-changed')
                            exit 0
                        }
                    } catch {
                        Write-Output ('clicked:' + $initialName + ':element-replaced')
                        exit 0
                    }
                    $x = [int]($rect.Left + ($rect.Width / 2))
                    $y = [int]($rect.Top + ($rect.Height / 2))
                    if (-not (Test-YeYuPointOwnedBy -X $x -Y $y -ProcessId $process.Id)) {
                        # Never dispatch a physical click onto a foreign window
                        # (editor, browser, remote-desktop shell) that covers the
                        # audited button.
                        Write-Output ('blocked-by-foreign-window:' + $initialName)
                        exit 5
                    }
                    [YeYuNteInput]::SetCursorPos($x, $y) | Out-Null
                    $screenWidth = [Math]::Max(1, [YeYuNteInput]::GetSystemMetrics(0) - 1)
                    $screenHeight = [Math]::Max(1, [YeYuNteInput]::GetSystemMetrics(1) - 1)
                    $absoluteX = [uint][Math]::Min(65535, [Math]::Max(0, [Math]::Round($x * 65535.0 / $screenWidth)))
                    $absoluteY = [uint][Math]::Min(65535, [Math]::Max(0, [Math]::Round($y * 65535.0 / $screenHeight)))
                    [YeYuNteInput]::mouse_event(0x8001, $absoluteX, $absoluteY, 0, [UIntPtr]::Zero)
                    [YeYuNteInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
                    [YeYuNteInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
                } finally {
                    if ($attachedForeground) { [YeYuNteInput]::AttachThreadInput($currentThread, $foregroundThread, $false) | Out-Null }
                    if ($attachedTarget) { [YeYuNteInput]::AttachThreadInput($currentThread, $targetThread, $false) | Out-Null }
                }
            }
            $deadline = [DateTime]::UtcNow.AddSeconds(12)
            while ([DateTime]::UtcNow -lt $deadline) {
                if (Get-Process -Name 'HTGame','NTE','Neverness to Everness' -ErrorAction SilentlyContinue) {
                    Write-Output ('clicked:' + $initialName + ':game-started')
                    exit 0
                }
                try {
                    if ($button.Current.Name -ne $initialName -or $button.Current.IsEnabled -ne $initialEnabled) {
                        Write-Output ('clicked:' + $initialName + ':state-changed')
                        exit 0
                    }
                } catch {
                    Write-Output ('clicked:' + $initialName + ':element-replaced')
                    exit 0
                }
                Start-Sleep -Milliseconds 500
            }
            Write-Output ('no-effect:' + $initialName)
            exit 4
        }
    } catch { continue }
}
# The official launcher renders its primary action inside a Chromium surface
# on current releases.  UI Automation therefore sees the top-level window but
# no Button descendants.  Use one fixed, audited normalized point inside the
# official launcher window as the formal-launcher fallback.  This is not an
# arbitrary click surface: the executable root and process image were checked
# above, and the point is restricted to the launcher's primary-action area.
foreach ($process in @(Get-Process -Name 'NTEGame' -ErrorAction SilentlyContinue)) {
    try {
        $image = [IO.Path]::GetFullPath($process.MainModule.FileName)
        if (-not $image.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($process.MainWindowHandle -eq 0) { continue }
        $root = [Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
        $rect = $root.Current.BoundingRectangle
        if ($rect.Width -lt 640 -or $rect.Height -lt 360) { continue }
        # A blank launcher surface (no accessible descendants at all) means the
        # launcher has not finished loading; report not-ready instead of
        # clicking into an empty window.
        $descendants = $root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
        if ($descendants.Count -eq 0) { continue }
        if (-not (Set-YeYuLauncherForeground -Handle $process.MainWindowHandle)) { continue }
        $x = [int]($rect.Left + ($rect.Width * 0.84))
        $y = [int]($rect.Top + ($rect.Height * 0.88))
        if (-not (Test-YeYuPointOwnedBy -X $x -Y $y -ProcessId $process.Id)) {
            Write-Output 'blocked-by-foreign-window:launcher-primary-action'
            exit 5
        }
        [YeYuNteInput]::SetCursorPos($x, $y) | Out-Null
        Start-Sleep -Milliseconds 150
        [YeYuNteInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        [YeYuNteInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        $fallbackDeadline = [DateTime]::UtcNow.AddSeconds(12)
        while ([DateTime]::UtcNow -lt $fallbackDeadline) {
            if (Get-Process -Name 'HTGame','NTE','Neverness to Everness' -ErrorAction SilentlyContinue) {
                Write-Output 'clicked:launcher-primary-action:game-started'
                exit 0
            }
            Start-Sleep -Milliseconds 500
        }
        Write-Output 'no-effect:launcher-primary-action'
        exit 4
    } catch { continue }
}
exit 3
"""

    _ENDFIELD_UIA_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$signature = @'
using System;
using System.Runtime.InteropServices;
public static class YeYuEndfieldLauncherInput {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(YeYuEndfieldPoint point);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr processId);
}
[StructLayout(LayoutKind.Sequential)]
public struct YeYuEndfieldPoint { public int X; public int Y; }
'@
Add-Type -TypeDefinition $signature
function Test-YeYuPointOwnedBy {
    param([int]$X, [int]$Y, [int]$ProcessId)
    $point = New-Object YeYuEndfieldPoint
    $point.X = $X; $point.Y = $Y
    $hit = [YeYuEndfieldLauncherInput]::WindowFromPoint($point)
    if ($hit -eq [IntPtr]::Zero) { return $false }
    $ownerPid = [uint32]0
    $ptr = [Runtime.InteropServices.Marshal]::AllocHGlobal(4)
    try {
        [void][YeYuEndfieldLauncherInput]::GetWindowThreadProcessId($hit, $ptr)
        $ownerPid = [uint32][Runtime.InteropServices.Marshal]::ReadInt32($ptr)
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($ptr) }
    return ($ownerPid -eq [uint32]$ProcessId)
}
function Set-YeYuLauncherForeground {
    param([IntPtr]$Handle)
    [YeYuEndfieldLauncherInput]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [YeYuEndfieldLauncherInput]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    [YeYuEndfieldLauncherInput]::ShowWindowAsync($Handle, 9) | Out-Null
    [YeYuEndfieldLauncherInput]::BringWindowToTop($Handle) | Out-Null
    [YeYuEndfieldLauncherInput]::SetForegroundWindow($Handle) | Out-Null
    Start-Sleep -Milliseconds 400
    return ([YeYuEndfieldLauncherInput]::GetForegroundWindow() -eq $Handle)
}
function Test-YeYuEndfieldStarted {
    return [bool](Get-Process -Name 'Endfield','Endfield-Win64-Shipping' -ErrorAction SilentlyContinue)
}
function Invoke-YeYuPhysicalClick {
    param([IntPtr]$Handle, [int]$ProcessId, [int]$X, [int]$Y, [string]$Label)
    if (-not (Set-YeYuLauncherForeground -Handle $Handle)) { return $null }
    if (-not (Test-YeYuPointOwnedBy -X $X -Y $Y -ProcessId $ProcessId)) {
        Write-Output ('blocked-by-foreign-window:' + $Label)
        exit 5
    }
    [YeYuEndfieldLauncherInput]::SetCursorPos($X, $Y) | Out-Null
    Start-Sleep -Milliseconds 150
    [YeYuEndfieldLauncherInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [YeYuEndfieldLauncherInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-YeYuEndfieldStarted) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}
$expected = [IO.Path]::GetFullPath($env:YEYU_ENDFIELD_LAUNCHER_ROOT).TrimEnd('\') + '\'
$candidates = @(Get-CimInstance Win32_Process -Filter "Name='Games.exe'" -ErrorAction SilentlyContinue | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_.ExecutablePath) -and
    [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith($expected, [StringComparison]::OrdinalIgnoreCase) -and
    $_.CommandLine -match '(?i)(?:^|\s)--game=Endfield(?:\s|$)' -and
    $_.CommandLine -match '(?i)(?:^|\s)--region=CN(?:\s|$)'
})
if ($candidates.Count -ne 1) { Write-Output ('not-ready:games-exe-candidates=' + $candidates.Count); exit 3 }
$process = Get-Process -Id $candidates[0].ProcessId -ErrorAction SilentlyContinue
if ($null -eq $process -or $process.MainWindowHandle -eq 0) { Write-Output 'not-ready:launcher-window-missing'; exit 3 }
$window = $process.MainWindowHandle
$root = [Windows.Automation.AutomationElement]::FromHandle($window)
# The launcher renders through QtWebEngine.  An empty accessibility tree is
# still "not ready" until the Manager authorizes the bounded fixed-point
# fallback; after that delay the exact launcher window and pixel-owner guards
# below remain the click boundary.
$descendants = $root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
if ($descendants.Count -eq 0 -and $env:YEYU_ENDFIELD_ALLOW_FALLBACK -ne '1') { Write-Output 'not-ready:web-surface-not-loaded'; exit 3 }
# Download/update actions are part of the audited primary-action set: a paused
# game download shows 继续下载 in the same button slot as 开始游戏.
$buttonNames = @('开始游戏','启动游戏','进入游戏','开始更新','继续更新','确认更新','继续下载','开始下载','立即下载','立即更新','更新游戏','下载游戏')
$visibleButtons = @()
$condition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::Button)
foreach ($button in @($root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition))) {
    $candidateName = [string]$button.Current.Name
    if (-not [string]::IsNullOrWhiteSpace($candidateName) -and $visibleButtons.Count -lt 12) { $visibleButtons += $candidateName }
    if ($buttonNames -notcontains $button.Current.Name -or -not $button.Current.IsEnabled) { continue }
    $rect = $button.Current.BoundingRectangle
    if ($button.Current.IsOffscreen -or $rect.Width -lt 40 -or $rect.Height -lt 20) { continue }
    $name = $button.Current.Name
    $initialEnabled = $button.Current.IsEnabled
    $pattern = $button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
    if ($null -ne $pattern) {
        $pattern.Invoke()
        $deadline = [DateTime]::UtcNow.AddSeconds(6)
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-YeYuEndfieldStarted) { Write-Output ('invoked:' + $name + ':game-started'); exit 0 }
            try {
                if ($button.Current.Name -ne $name -or $button.Current.IsEnabled -ne $initialEnabled) {
                    Write-Output ('invoked:' + $name + ':state-changed'); exit 0
                }
            } catch { Write-Output ('invoked:' + $name + ':element-replaced'); exit 0 }
            Start-Sleep -Milliseconds 500
        }
    }
    # UIA Invoke is a no-op on this Chromium button; fall back to one real
    # foreground click on the audited button centre.
    $x = [int]($rect.Left + ($rect.Width / 2))
    $y = [int]($rect.Top + ($rect.Height / 2))
    $clicked = Invoke-YeYuPhysicalClick -Handle $window -ProcessId $process.Id -X $x -Y $y -Label $name
    if ($clicked -eq $true) { Write-Output ('clicked:' + $name + ':game-started'); exit 0 }
    try {
        if ($button.Current.Name -ne $name -or $button.Current.IsEnabled -ne $initialEnabled) {
            Write-Output ('clicked:' + $name + ':state-changed'); exit 0
        }
    } catch { Write-Output ('clicked:' + $name + ':element-replaced'); exit 0 }
    Write-Output ('no-effect:' + $name)
    exit 4
}
if ($env:YEYU_ENDFIELD_ALLOW_FALLBACK -ne '1') { Write-Output ('not-ready:no-audited-button; visible=' + ($visibleButtons -join '/')); exit 3 }
$rect = $root.Current.BoundingRectangle
if ($rect.Width -lt 640 -or $rect.Height -lt 360) { Write-Output 'not-ready:launcher-window-too-small'; exit 3 }
$x = [int]($rect.Left + ($rect.Width * 0.84))
$y = [int]($rect.Top + ($rect.Height * 0.88))
$clicked = Invoke-YeYuPhysicalClick -Handle $window -ProcessId $process.Id -X $x -Y $y -Label 'launcher-primary-action'
if ($clicked -eq $true) { Write-Output 'clicked:launcher-primary-action:game-started'; exit 0 }
if ($null -eq $clicked) { Write-Output 'not-ready:foreground-not-acquired'; exit 3 }
Write-Output 'no-effect:launcher-primary-action'
exit 4
"""

    @classmethod
    def _resolve_endfield_launcher(cls, configured_executable: Path) -> Path:
        """Resolve only a local installed Hypergryph formal launcher.

        The client executable needs the launcher's Endfield/CN handoff token on
        this machine.  Never probe a mapped/NAS drive here: production apps and
        their update lifecycle must remain local and an unavailable mapping can
        block the Manager thread for minutes.
        """

        if configured_executable.name.casefold() == "launcher.exe":
            candidates = (configured_executable,)
        else:
            candidates = cls.ENDFIELD_LOCAL_LAUNCHER_CANDIDATES
        for candidate in candidates:
            if (
                candidate.is_file()
                and candidate.parent.name.casefold() == "hypergryph launcher"
            ):
                return candidate.resolve()
        raise GameLaunchError(
            "the local Hypergryph formal launcher is required for Endfield"
        )

    def _reactivate_headless_endfield_launcher(
        self, launcher: Path, baseline: dict[int, str], *,
        cancel_requested: LaunchCancellationCheck | None,
    ) -> int | None:
        """Activate one verified Endfield/CN launcher instance without closing it."""
        if not baseline or any(name.casefold() != "games.exe" for name in baseline.values()):
            return None
        if len(baseline) != 1:
            raise GameLaunchHumanRequired(
                "endfield_launcher_identity_ambiguous",
                "Multiple Games.exe processes require inspection before Endfield activation.",
            )
        environment = self._external_process_environment()
        environment["YEYU_ENDFIELD_REACTIVATE_EXE"] = str(launcher)
        environment["YEYU_ENDFIELD_REACTIVATE_PID"] = str(next(iter(baseline)))
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            result = self._run_launcher_probe(
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", self._ENDFIELD_REACTIVATION_IDENTITY_SCRIPT],
                environment=environment, cancel_requested=cancel_requested,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GameLaunchHumanRequired(
                "endfield_launcher_identity_unverified",
                "Endfield launcher identity could not be verified; preserve the existing scene.",
            ) from error
        if result.returncode != 0 or result.stdout.strip() not in {"trusted:headless", "trusted:visible"}:
            raise GameLaunchHumanRequired(
                "endfield_launcher_identity_unverified",
                "Existing Games.exe must belong to the signed local Endfield/CN launcher.",
            )
        if result.stdout.strip() == "trusted:visible":
            return None
        self._raise_if_cancelled(cancel_requested)
        # Recheck immediately before dispatch: a client appearing during the
        # read-only identity probe must not receive a second launch request.
        if self._list_running(set(self.READY_PROCESS_NAMES["Endfield"])):
            return None
        try:
            with self._native_child_dll_scope():
                self._raise_if_cancelled(cancel_requested)
                process = self._start_endfield_launcher(launcher)
        except GameLaunchCancelled:
            raise
        except (OSError, GameLaunchError) as error:
            raise GameLaunchHumanRequired(
                "endfield_launcher_reactivation_failed",
                "The official Endfield activation request failed; inspect the preserved launcher.",
                process_ids=frozenset(baseline),
            ) from error
        return process.pid

    def _start_endfield_launcher(self, executable: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [str(executable), *self.ENDFIELD_LAUNCHER_ARGUMENTS],
            cwd=str(executable.parent), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            env=self._external_process_environment(),
        )

    def ensure_started(
        self,
        game_id: str,
        game_path: str,
        observer: LaunchObserver | None = None,
        *,
        cancel_requested: LaunchCancellationCheck | None = None,
    ) -> GameLaunchReceipt:
        started_at = time.monotonic()
        try:
            receipt = self._ensure_started(
                game_id,
                game_path,
                observer,
                started_at,
                cancel_requested,
            )
        except GameLaunchHumanRequired as error:
            self._notify(
                observer, game_id, "launch-human-required", started_at,
                self._launch_surface_names(game_id, game_path),
                {**error.detail, "reasonCode": error.reason_code, "message": str(error)},
                process_ids=error.process_ids,
            )
            raise
        except GameLaunchCancelled as error:
            cancellation_scope: dict[str, object] = {}
            if game_id == "WW" and Path(game_path).name.casefold() == "launcher.exe":
                try:
                    cancellation_scope["process_ids"] = frozenset(self._list_ww_running(Path(game_path), set(self._launch_surface_names(game_id, game_path))))
                except (GameLaunchError, OSError):
                    cancellation_scope["process_ids"] = frozenset()
            self._notify(
                observer,
                game_id,
                "launch-cancelled",
                started_at,
                self._launch_surface_names(game_id, game_path),
                {
                    "reasonCode": "manager_cancel_requested",
                    "message": str(error),
                },
                **cancellation_scope,
            )
            raise
        except GameLaunchError as error:
            if game_id == "WW" and Path(game_path).name.casefold() == "launcher.exe":
                scoped: dict[int, str] = {}
                try:
                    scoped = self._list_ww_running(Path(game_path), set(self._launch_surface_names(game_id, game_path)))
                except (GameLaunchError, OSError):
                    pass
                human_error = GameLaunchHumanRequired(
                    "ww_launcher_start_or_observation_failed",
                    "WW formal launch requires inspection; preserve any existing launcher or client.",
                    detail={"errorType": type(error).__name__}, process_ids=frozenset(scoped),
                )
                self._notify(
                    observer, game_id, "launch-human-required", started_at,
                    self._launch_surface_names(game_id, game_path),
                    {**human_error.detail, "reasonCode": human_error.reason_code, "message": str(human_error)},
                    process_ids=human_error.process_ids,
                )
                raise human_error from error
            self._notify(
                observer,
                game_id,
                "launch-failed",
                started_at,
                self._launch_surface_names(game_id, game_path),
                {"error": str(error)},
            )
            raise
        self._notify(
            observer,
            game_id,
            "ready",
            started_at,
            frozenset(receipt.expected_process_names),
            {
                "launchState": receipt.state,
                "processId": receipt.process_id,
                "readyWindowPid": receipt.ready_window_pid,
                "readyWindowWidth": receipt.ready_window_width,
                "readyWindowHeight": receipt.ready_window_height,
            },
            process_ids=frozenset(
                pid for pid in (receipt.process_id, receipt.ready_window_pid) if pid
            ),
        )
        return receipt

    def _launch_surface_names(self, game_id: str, game_path: str) -> frozenset[str]:
        names = set(registered_game_process_names(game_id))
        try:
            names.add(Path(game_path).name.casefold())
        except (TypeError, ValueError):
            pass
        if game_id == "Endfield":
            names.update({"launcher.exe", "games.exe"})
        if game_id == "NTE":
            names.update({"ntelauncher.exe", "ntegame.exe"})
        if game_id == "WW" and Path(game_path).name.casefold() == "launcher.exe":
            names.update(self.WW_LAUNCHER_PROCESS_NAMES)
        return frozenset(names)

    @classmethod
    def _ww_launcher_probe(
        cls, executable: Path, script: str, *,
        cancel_requested: LaunchCancellationCheck | None,
        process_ids: frozenset[int] = frozenset(), allow_invoke: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        environment = cls._external_process_environment()
        environment["YEYU_WW_LAUNCHER_EXE"] = str(executable)
        environment["YEYU_WW_LAUNCHER_PIDS"] = ",".join(str(pid) for pid in sorted(process_ids))
        environment["YEYU_WW_ALLOW_INVOKE"] = "1" if allow_invoke else "0"
        try:
            return cls._run_launcher_probe(
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                environment=environment, cancel_requested=cancel_requested,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GameLaunchHumanRequired(
                "ww_launcher_probe_failed", "WW launcher observation failed; preserve the scene for inspection.",
                detail={"errorType": type(error).__name__}, process_ids=process_ids,
            ) from error

    @classmethod
    def _verify_ww_launcher(
        cls, executable: Path, *, cancel_requested: LaunchCancellationCheck | None,
    ) -> None:
        result = cls._ww_launcher_probe(
            executable, cls._WW_LAUNCHER_SIGNATURE_SCRIPT, cancel_requested=cancel_requested,
        )
        if result.returncode != 0 or result.stdout.strip() != "trusted:ww-launcher":
            raise GameLaunchHumanRequired(
                "ww_launcher_identity_unverified",
                "The configured WW launcher must be a local, validly signed Kuro Wuthering Waves launcher.",
            )

    @classmethod
    def _probe_ww_launcher(
        cls, executable: Path, process_ids: frozenset[int], *,
        allow_invoke: bool, cancel_requested: LaunchCancellationCheck | None,
    ) -> str:
        result = cls._ww_launcher_probe(
            executable, cls._WW_LAUNCHER_UIA_SCRIPT,
            cancel_requested=cancel_requested, process_ids=process_ids, allow_invoke=allow_invoke,
        )
        outcome = result.stdout.strip()
        if result.returncode == 0 and outcome == "invoked:ww-enter-game" and allow_invoke:
            return outcome
        if result.returncode == 3 and outcome in {
            "waiting:launcher-window", "waiting:game-window", "waiting:unrecognized-launcher-ui", "ready:ww-enter-game",
        }:
            return outcome
        if result.returncode == 6 and outcome in {
            "human:untrusted-window-owner", "human:ambiguous-launcher-window",
            "human:login-or-consent", "human:update-or-error",
            "human:ambiguous-enter-game-button", "human:invoke-pattern-unavailable",
            "human:uia-probe-failed", "human:button-owner-or-state-changed",
            "human:modal-dialog", "human:unrecognized-primary-action", "human:untrusted-element-owner",
        }:
            return outcome
        return "human:invalid-probe-result"

    @classmethod
    def _list_ww_running(cls, launcher: Path, expected_names: set[str]) -> dict[int, str]:
        return cls._filter_ww_processes(launcher, cls._list_running(expected_names))

    @classmethod
    def _filter_ww_processes(cls, launcher: Path, running: dict[int, str]) -> dict[int, str]:
        root = launcher.resolve().parent
        scoped: dict[int, str] = {}
        for pid, name in running.items():
            path = cls._process_executable_path(pid)
            if path is None:
                continue
            try:
                parts = tuple(part.casefold() for part in path.resolve().relative_to(root).parts)
            except ValueError:
                continue
            version = parts[0].split(".") if parts else []
            if (
                path.resolve() == launcher.resolve()
                or (len(parts) == 2 and len(version) == 4 and all(part.isdigit() for part in version) and parts[1] in cls.WW_LAUNCHER_PROCESS_NAMES)
                or parts == ("wuthering waves game", "wuthering waves.exe")
                or parts == ("wuthering waves game", "client", "binaries", "win64", "client-win64-shipping.exe")
            ):
                scoped[pid] = name
        return scoped

    @staticmethod
    def _process_executable_path(process_id: int) -> Path | None:
        """Query executable identity only; never read the target's memory."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def _notify(
        self,
        observer: LaunchObserver | None,
        game_id: str,
        phase: str,
        started_at: float,
        process_names: frozenset[str] | set[str],
        detail: dict[str, object],
        *,
        process_ids: frozenset[int] | None = None,
    ) -> None:
        if observer is None:
            return
        if process_ids is None:
            try:
                process_ids = frozenset(self._list_running(set(process_names)))
            except GameLaunchError:
                process_ids = frozenset()
        try:
            observer(
                LaunchObservation(
                    game_id=game_id,
                    phase=phase,
                    elapsed_seconds=round(time.monotonic() - started_at, 1),
                    process_names=frozenset(process_names),
                    process_ids=process_ids,
                    detail=dict(detail),
                )
            )
        except Exception:
            # Evidence capture is best effort; it must never change the launch outcome.
            pass

    def _ensure_started(
        self,
        game_id: str,
        game_path: str,
        observer: LaunchObserver | None,
        started_at: float,
        cancel_requested: LaunchCancellationCheck | None,
    ) -> GameLaunchReceipt:
        self._raise_if_cancelled(cancel_requested)
        if os.name != "nt":
            raise GameLaunchError("game launch is available only on Windows")
        executable = self.validate_configured_executable(game_path)
        launch_executable = (
            self._resolve_endfield_launcher(executable)
            if game_id == "Endfield"
            else executable
        )
        ww_launcher = (
            launch_executable.resolve()
            if game_id == "WW" and launch_executable.name.casefold() == "launcher.exe"
            else None
        )
        if ww_launcher is not None:
            self._verify_ww_launcher(ww_launcher, cancel_requested=cancel_requested)

        expected_names = set(registered_game_process_names(game_id))
        expected_names.add(launch_executable.name.casefold())
        if game_id == "Endfield":
            expected_names.add("games.exe")
        if ww_launcher is not None:
            expected_names.update(self.WW_LAUNCHER_PROCESS_NAMES)
        baseline = self._list_ww_running(ww_launcher, expected_names) if ww_launcher else self._list_running(expected_names)
        if game_id == "Endfield" and self._retire_stale_headless_endfield(baseline):
            baseline = self._list_running(expected_names)
        existing = next(iter(baseline.values()), None)
        if existing:
            reactivation_pid = (
                self._reactivate_headless_endfield_launcher(
                    launch_executable, baseline, cancel_requested=cancel_requested,
                ) if game_id == "Endfield" else None
            )
            reactivation_scope = frozenset((*baseline, reactivation_pid)) if reactivation_pid else None
            if reactivation_scope is not None:
                self._notify(
                    observer, game_id, "launcher-action", started_at, expected_names,
                    {"action": "endfield-official-reactivation", "dispatched": True, "gameReady": False},
                    process_ids=reactivation_scope,
                )
            try:
                ready = self._wait_until_ready(
                    game_id,
                    expected_names,
                    None if reactivation_scope is not None else launch_executable.parent,
                    observer=observer,
                    started_at=started_at,
                    cancel_requested=cancel_requested,
                    **({"ww_launcher": ww_launcher} if ww_launcher else {}),
                    **({"endfield_reactivation": (executable.parent, reactivation_scope)} if reactivation_scope is not None else {}),
                )
            except (GameLaunchCancelled, GameLaunchHumanRequired):
                raise
            except (GameLaunchError, OSError) as error:
                if reactivation_scope is not None:
                    raise GameLaunchHumanRequired(
                        "endfield_launcher_reactivation_observation_failed",
                        "Endfield activation could not be observed safely; inspect the preserved scene.",
                        process_ids=reactivation_scope,
                    ) from error
                raise
            return GameLaunchReceipt(
                "started" if reactivation_pid else "already-running",
                reactivation_pid,
                ready[1],
                tuple(sorted(baseline)),
                tuple(sorted(expected_names)),
                ready[0],
                ready[2],
                ready[3],
                ww_launcher_path=str(ww_launcher) if ww_launcher else None,
            )

        try:
            environment = self._external_process_environment()
            if game_id == "PGR":
                self._prepare_pgr_window_preferences()
            with self._native_child_dll_scope():
                if game_id == "WW" and ww_launcher is None:
                    # Match OK-WW's proven formal launch semantics exactly.
                    # The environment and DLL scope must also be clean so the
                    # native client cannot load YeYu Gamer's packaged CRT.
                    process = subprocess.Popen(
                        f'start "" /b "{executable}" {self.WW_DX11_ARGUMENTS}',
                        cwd=str(executable.parent),
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=(
                            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        ),
                        env=environment,
                    )
                elif game_id == "Endfield":
                    process = self._start_endfield_launcher(launch_executable)
                else:
                    process = subprocess.Popen(
                        [str(executable)],
                        cwd=str(executable.parent),
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=(
                            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        ),
                        env=environment,
                    )
        except OSError as error:
            raise GameLaunchError(f"configured game executable could not start: {error}") from error

        deadline = time.monotonic() + self.START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel_requested)
            observed = self._list_ww_running(ww_launcher, expected_names) if ww_launcher else self._list_running(expected_names)
            if observed:
                try:
                    ready = self._wait_until_ready(
                        game_id,
                        expected_names,
                        launch_executable.parent,
                        observer=observer,
                        started_at=started_at,
                        cancel_requested=cancel_requested,
                        **({"ww_launcher": ww_launcher} if ww_launcher else {}),
                    )
                except (GameLaunchCancelled, GameLaunchHumanRequired):
                    # Cancellation deliberately preserves the launcher/client
                    # surface.  It is not a launch failure and must not invoke
                    # the normal Manager-owned process cleanup path.
                    raise
                except Exception as error:
                    if ww_launcher is not None:
                        raise GameLaunchHumanRequired(
                            "ww_launcher_observation_failed",
                            "WW formal launcher could not be observed safely; inspect the preserved scene.",
                            detail={"errorType": type(error).__name__},
                            process_ids=frozenset(observed),
                        ) from error
                    # ensure_started owns this process lineage even though the
                    # ready receipt was not produced yet.  Do not leak a
                    # windowless client or launcher after the gate fails.
                    self.close_started(
                        GameLaunchReceipt(
                            "started",
                            process.pid,
                            next(iter(observed.values()), launch_executable.name),
                            tuple(sorted(baseline)),
                            tuple(sorted(expected_names)),
                        )
                    )
                    raise
                if game_id in self.STARTED_GAME_HANDOFF_SECONDS:
                    if cancel_requested is None:
                        self._handoff_started_game(game_id, ready[0])
                    else:
                        self._handoff_started_game(
                            game_id,
                            ready[0],
                            cancel_requested=cancel_requested,
                        )
                return GameLaunchReceipt(
                    "started",
                    process.pid,
                    ready[1],
                    tuple(sorted(baseline)),
                    tuple(sorted(expected_names)),
                    ready[0],
                    ready[2],
                    ready[3],
                    ww_launcher_path=str(ww_launcher) if ww_launcher else None,
                )
            time.sleep(self.POLL_INTERVAL_SECONDS)

        if ww_launcher is not None:
            raise GameLaunchHumanRequired(
                "ww_launcher_process_not_observed",
                "WW formal launcher did not expose a verifiable process; inspect it before resuming.",
            )
        raise GameLaunchError(
            "configured game client did not expose a registered process before the Adapter launch timeout"
        )

    def _retire_stale_headless_endfield(self, running: dict[int, str]) -> bool:
        """Remove only an old, windowless Endfield orphan before formal launch.

        Endfield can leave ``Endfield.exe`` alive with no window after a failed
        bootstrap.  Treating that orphan as an already-running game prevents
        the official launcher from being started at all.  A visible client, a
        live official launcher, a young process, or a process whose age cannot
        be verified is always preserved.
        """

        normalized = {pid: name.casefold() for pid, name in running.items()}
        if any(name in {"launcher.exe", "games.exe"} for name in normalized.values()):
            return False
        ready_names = set(self.READY_PROCESS_NAMES["Endfield"])
        candidates = {
            pid for pid, name in normalized.items() if name in ready_names
        }
        if not candidates:
            return False
        if self._find_ready_window(running, ready_names, "Endfield") is not None:
            return False
        ages = [self._process_age_seconds(pid) for pid in candidates]
        if any(
            age is None or age < self.ENDFIELD_STALE_HEADLESS_SECONDS
            for age in ages
        ):
            return False

        self._request_graceful_close(candidates)
        remaining = self._wait_for_owned_exit(
            ready_names, set(), self.TERMINATE_CLOSE_SECONDS
        ) & candidates
        if remaining:
            self._terminate_owned(remaining, force=False)
            remaining = self._wait_for_owned_exit(
                ready_names, set(), self.TERMINATE_CLOSE_SECONDS
            ) & candidates
        if remaining:
            self._terminate_owned(remaining, force=True)
            remaining = self._wait_for_owned_exit(
                ready_names, set(), self.TERMINATE_CLOSE_SECONDS
            ) & candidates
        if remaining:
            raise GameLaunchError(
                "stale headless Endfield process could not be retired safely"
            )
        return True

    @staticmethod
    def _process_age_seconds(process_id: int) -> float | None:
        """Return verified Windows process age without shelling out."""

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
        finally:
            kernel32.CloseHandle(handle)
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        started_unix = (ticks / 10_000_000) - 11_644_473_600
        return max(0.0, time.time() - started_unix)

    def _wait_until_ready(
        self,
        game_id: str,
        expected_names: set[str],
        launcher_root: Path | None = None,
        *,
        observer: LaunchObserver | None = None,
        started_at: float | None = None,
        cancel_requested: LaunchCancellationCheck | None = None,
        ww_launcher: Path | None = None,
        endfield_reactivation: tuple[Path, frozenset[int]] | None = None,
    ) -> tuple[int, str, int, int]:
        ready_names = set(self.READY_PROCESS_NAMES.get(game_id, frozenset(expected_names)))
        ready_timeout = self.READY_TIMEOUT_OVERRIDES.get(game_id, self.READY_TIMEOUT_SECONDS)
        if endfield_reactivation is not None:
            ready_timeout = self.ENDFIELD_REACTIVATION_READY_SECONDS
        wait_started_at = time.monotonic()
        observation_origin = started_at if started_at is not None else wait_started_at
        deadline = wait_started_at + ready_timeout
        hard_cap = wait_started_at + self.LAUNCHER_UPDATE_HARD_CAP_SECONDS
        stable_key: tuple[int, int, int] | None = None
        stable_since = 0.0
        next_launcher_action_at = 0.0
        nte_no_effect_actions = 0
        endfield_no_effect_actions = 0
        launcher_wait_started_at = time.monotonic()
        next_observation_at = wait_started_at + self.LAUNCH_OBSERVATION_INTERVAL_SECONDS
        last_launcher_outcome: str | None = None
        ww_invoked = False
        running: dict[int, str] = {}
        progress = _LaunchProgressMonitor(
            sample_seconds=self.LAUNCH_PROGRESS_SAMPLE_SECONDS,
            minimum_bytes=self.LAUNCH_PROGRESS_MIN_BYTES,
        )
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel_requested)
            running = self._list_ww_running(ww_launcher, expected_names) if ww_launcher else self._list_running(expected_names)
            if endfield_reactivation is not None:
                game_root, launcher_pids = endfield_reactivation
                scoped = {pid: name for pid, name in running.items() if pid in launcher_pids}
                for pid, name in running.items():
                    if name.casefold() not in ready_names:
                        continue
                    path = self._process_executable_path(pid)
                    if path is not None and path.resolve().is_relative_to(game_root.resolve()):
                        scoped[pid] = name
                running = scoped
            now = time.monotonic()
            ready = self._find_ready_window(running, ready_names, game_id)
            updating = progress.observe(running, now)
            if endfield_reactivation is not None and ready is None and updating:
                raise GameLaunchHumanRequired(
                    "endfield_launcher_reactivation_update_observed",
                    "The official Endfield launcher is updating; finish its visible update before resuming.",
                    detail={"bytesWrittenDelta": progress.last_delta}, process_ids=frozenset(running),
                )
            if ww_launcher is not None and ready is None and updating:
                raise GameLaunchHumanRequired(
                    "ww_launcher_update_observed",
                    "WW launcher is writing an update; preserve it and finish the official update before resuming.",
                    detail={"bytesWrittenDelta": progress.last_delta}, process_ids=frozenset(running),
                )
            if updating and ready is None:
                # A download/patch is in flight: keep the launcher gate open and
                # do not count "no effect" strikes against a hidden start button.
                launcher_wait_started_at = now
                nte_no_effect_actions = 0
                endfield_no_effect_actions = 0
                deadline = min(max(deadline, now + ready_timeout), hard_cap)
                if now >= hard_cap:
                    raise GameLaunchError(
                        f"{game_id} launcher update did not finish within the hard cap"
                    )
            drive_launcher = launcher_root is not None and ready is None and not updating and endfield_reactivation is None
            if ww_launcher is not None and ready is None and now >= next_launcher_action_at:
                client_already_started = any(
                    name.casefold() in {"wuthering waves.exe", "client-win64-shipping.exe"}
                    for name in running.values()
                )
                outcome = self._probe_ww_launcher(
                    ww_launcher, frozenset(running), allow_invoke=not ww_invoked and not client_already_started,
                    cancel_requested=cancel_requested,
                )
                last_launcher_outcome = outcome
                if outcome == "invoked:ww-enter-game":
                    ww_invoked = True
                    self._notify(
                        observer, game_id, "launcher-action", observation_origin, expected_names,
                        {"action": "ww-enter-game", "dispatched": True, "gameReady": False},
                        process_ids=frozenset(running),
                    )
                elif outcome.startswith("human:"):
                    raise GameLaunchHumanRequired(
                        "ww_launcher_" + outcome.removeprefix("human:").replace("-", "_"),
                        "WW formal launcher requires inspection: " + outcome.removeprefix("human:"),
                        detail={"launcherOutcome": outcome}, process_ids=frozenset(running),
                    )
                elif not ww_invoked and now - launcher_wait_started_at >= self.WW_LAUNCHER_UI_READY_SECONDS:
                    raise GameLaunchHumanRequired(
                        "ww_launcher_existing_client_unready" if client_already_started else "ww_launcher_ui_unknown",
                        "WW already has a client process without a ready window; inspect it before another launch."
                        if client_already_started else
                        "WW launcher did not expose the verified Enter Game button; inspect the preserved launcher.",
                        detail={"launcherOutcome": outcome}, process_ids=frozenset(running),
                    )
                next_launcher_action_at = now + self.WW_LAUNCHER_POLL_SECONDS
            if (
                game_id == "NTE"
                and drive_launcher
                and now >= next_launcher_action_at
            ):
                outcome = (
                    self._drive_nte_launcher(launcher_root)
                    if cancel_requested is None
                    else self._drive_nte_launcher(
                        launcher_root,
                        cancel_requested=cancel_requested,
                    )
                )
                last_launcher_outcome = outcome
                if outcome == "acted":
                    nte_no_effect_actions = 0
                    self._notify(
                        observer, game_id, "launcher-action", observation_origin,
                        expected_names, {"outcome": outcome},
                        process_ids=frozenset(running),
                    )
                elif outcome == "no-effect":
                    nte_no_effect_actions += 1
                    if nte_no_effect_actions >= self.NTE_MAX_NO_EFFECT_ACTIONS:
                        raise GameLaunchError(
                            "NTE launcher audited start/update action had no visible effect"
                        )
                elif outcome == "foreground-interference":
                    raise GameLaunchError(
                        "NTE launcher primary action is covered by a foreign foreground window; "
                        "classify as external-hotkey-or-foreground-interference"
                    )
                elif now - launcher_wait_started_at >= self.LAUNCHER_UI_READY_TIMEOUT_SECONDS:
                    raise GameLaunchError(
                        "NTE official launcher never exposed its audited start action "
                        "(blank or stuck launcher UI)"
                    )
                next_launcher_action_at = now + self.NTE_LAUNCHER_POLL_SECONDS
            if (
                game_id == "Endfield"
                and drive_launcher
                and now >= next_launcher_action_at
            ):
                outcome = self._drive_endfield_launcher(
                    launcher_root,
                    allow_fixed_fallback=(
                        endfield_no_effect_actions > 0
                        or now - launcher_wait_started_at
                        >= self.ENDFIELD_FIXED_FALLBACK_AFTER_SECONDS
                    ),
                    **(
                        {"cancel_requested": cancel_requested}
                        if cancel_requested is not None
                        else {}
                    ),
                )
                last_launcher_outcome = outcome
                if outcome == "acted":
                    endfield_no_effect_actions = 0
                    self._notify(
                        observer, game_id, "launcher-action", observation_origin,
                        expected_names, {"outcome": outcome},
                        process_ids=frozenset(running),
                    )
                elif outcome == "no-effect":
                    endfield_no_effect_actions += 1
                    if (
                        endfield_no_effect_actions
                        >= self.ENDFIELD_MAX_NO_EFFECT_ACTIONS
                    ):
                        raise GameLaunchError(
                            "Endfield launcher audited start/update action had no visible effect"
                        )
                elif outcome == "foreground-interference":
                    raise GameLaunchError(
                        "Endfield launcher primary action is covered by a foreign foreground window; "
                        "classify as external-hotkey-or-foreground-interference"
                    )
                elif now - launcher_wait_started_at >= self.LAUNCHER_UI_READY_TIMEOUT_SECONDS:
                    raise GameLaunchError(
                        "Endfield official launcher never exposed its audited start action "
                        "(blank or stuck launcher UI)"
                    )
                next_launcher_action_at = (
                    now + self.ENDFIELD_LAUNCHER_POLL_SECONDS
                )
            if ready is None and now >= next_observation_at and (endfield_reactivation is None or running):
                next_observation_at = now + self.LAUNCH_OBSERVATION_INTERVAL_SECONDS
                self._notify(
                    observer,
                    game_id,
                    "launcher-waiting",
                    observation_origin,
                    expected_names,
                    {
                        "running": {str(pid): name for pid, name in sorted(running.items())},
                        "updating": updating,
                        "bytesWrittenDelta": progress.last_delta,
                        "lastLauncherOutcome": last_launcher_outcome,
                        "lastLauncherProbe": self._last_launcher_probe,
                        "secondsUntilDeadline": round(deadline - now, 1),
                    },
                    process_ids=frozenset(running),
                )
            if ready is None:
                stable_key = None
                stable_since = 0.0
            else:
                key = (ready[0], ready[2], ready[3])
                if key != stable_key:
                    stable_key = key
                    stable_since = now
                elif now - stable_since >= self.READY_STABLE_OVERRIDES.get(
                    game_id, self.READY_STABLE_SECONDS
                ):
                    return ready
            time.sleep(self.POLL_INTERVAL_SECONDS)
        if endfield_reactivation is not None:
            raise GameLaunchHumanRequired(
                "endfield_launcher_reactivation_game_window_missing",
                "One official Endfield activation did not yield a stable game window; inspect the preserved launcher.",
                detail={"reactivationDispatched": True, "gameReady": False}, process_ids=frozenset(running),
            )
        if ww_launcher is not None:
            raise GameLaunchHumanRequired(
                "ww_launcher_game_window_missing",
                "WW formal launcher did not yield a stable game window; inspect the preserved scene before resuming.",
                detail={"enterGameInvoked": ww_invoked, "lastLauncherOutcome": last_launcher_outcome},
                process_ids=frozenset(running),
            )
        raise GameLaunchError(
            "configured game client did not expose a stable visible game window before the Adapter launch timeout"
        )

    @staticmethod
    def _classify_launcher_outcome(returncode: int, stdout: str) -> str:
        """Map an audited launcher script result onto the launch state machine.

        ``acted``: an audited button was invoked/clicked and the client or the
        button state changed.  ``no-effect``: the action was dispatched but
        nothing changed.  ``not-ready``: the launcher window or its audited
        action is not available yet (still loading, updating, or blank).
        ``foreground-interference``: another process owns the pixels under the
        audited action, so no click may be dispatched.
        """

        text = stdout.strip()
        if returncode == 0 and text.startswith(("invoked:", "clicked:")):
            return "acted"
        if returncode == 5 or text.startswith("blocked-by-foreign-window:"):
            return "foreground-interference"
        if returncode == 4 or text.startswith("no-effect:"):
            return "no-effect"
        return "not-ready"

    @classmethod
    def _drive_nte_launcher(
        cls,
        launcher_root: Path,
        *,
        cancel_requested: LaunchCancellationCheck | None = None,
    ) -> str:
        """Invoke only audited buttons in the official NTE launcher window."""

        root = launcher_root.resolve()
        if root.name.casefold() != "ntelauncher" or not root.is_dir():
            raise GameLaunchError("configured NTE launcher root is invalid")
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file():
            raise GameLaunchError("Windows UI Automation host is unavailable")
        environment = os.environ.copy()
        environment["YEYU_NTE_LAUNCHER_ROOT"] = str(root)
        try:
            completed = cls._run_launcher_probe(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    cls._NTE_UIA_SCRIPT,
                ],
                environment=environment,
                cancel_requested=cancel_requested,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GameLaunchError(f"NTE launcher UI Automation failed: {error}") from error
        cls._last_launcher_probe = cls._launcher_probe_summary(completed)
        return cls._classify_launcher_outcome(completed.returncode, completed.stdout)

    _last_launcher_probe: str = ''

    @staticmethod
    def _launcher_probe_summary(completed: 'subprocess.CompletedProcess[str]') -> str:
        text = (completed.stdout or '').strip().splitlines()
        tail = text[-1] if text else ''
        error = (completed.stderr or '').strip().splitlines()
        error_tail = error[-1] if error else ''
        summary = f'exit={completed.returncode} out={tail[:200]}'
        if error_tail:
            summary += f' err={error_tail[:200]}'
        return summary

    @classmethod
    def _drive_endfield_launcher(
        cls,
        launcher_root: Path,
        *,
        allow_fixed_fallback: bool = False,
        cancel_requested: LaunchCancellationCheck | None = None,
    ) -> str:
        """Invoke only the audited Endfield/CN formal-launcher action."""

        root = launcher_root.resolve()
        if root.name.casefold() != "hypergryph launcher" or not root.is_dir():
            raise GameLaunchError("configured Endfield launcher root is invalid")
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file():
            raise GameLaunchError("Windows UI Automation host is unavailable")
        environment = os.environ.copy()
        environment["YEYU_ENDFIELD_LAUNCHER_ROOT"] = str(root)
        environment["YEYU_ENDFIELD_ALLOW_FALLBACK"] = (
            "1" if allow_fixed_fallback else "0"
        )
        try:
            completed = cls._run_launcher_probe(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    cls._ENDFIELD_UIA_SCRIPT,
                ],
                environment=environment,
                cancel_requested=cancel_requested,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GameLaunchError(
                f"Endfield launcher UI Automation failed: {error}"
            ) from error
        cls._last_launcher_probe = cls._launcher_probe_summary(completed)
        return cls._classify_launcher_outcome(completed.returncode, completed.stdout)

    @classmethod
    def _run_launcher_probe(
        cls,
        command: list[str],
        *,
        environment: dict[str, str],
        cancel_requested: LaunchCancellationCheck | None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one bounded UIA probe and stop only that probe on cancellation.

        The legacy no-cancellation path deliberately keeps ``subprocess.run``
        so standalone diagnostics retain their established behaviour.  Manager
        executions use the polling path: it can interrupt the owned PowerShell
        probe without terminating the visible launcher or game client.
        """

        # These probes use Windows PowerShell 5. An inherited PowerShell 7 or
        # Codex module directory can make its built-in signature module fail
        # AuthorizationManager checks. Keep normal policy and signatures;
        # isolate only this child's module search path to the matching host.
        environment = dict(environment)
        environment["PSModulePath"] = str(
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
        )

        if cancel_requested is None:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 45.0
        while True:
            if cancel_requested():
                cls._stop_launcher_probe(process)
                raise GameLaunchCancelled(
                    "Manager cancellation stopped the launcher action probe"
                )
            return_code = process.poll()
            if return_code is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(
                    command,
                    return_code,
                    stdout or "",
                    stderr or "",
                )
            if time.monotonic() >= deadline:
                cls._stop_launcher_probe(process)
                raise subprocess.TimeoutExpired(command, 45)
            time.sleep(min(cls.POLL_INTERVAL_SECONDS, 0.25))

    @staticmethod
    def _stop_launcher_probe(process: subprocess.Popen[str]) -> None:
        """Stop the Manager-owned PowerShell probe, never the launcher/client."""

        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _handoff_started_game(
        self,
        game_id: str,
        process_id: int,
        *,
        cancel_requested: LaunchCancellationCheck | None = None,
    ) -> None:
        """Wait for a newly owned client and perform only registered clicks."""

        self._raise_if_cancelled(cancel_requested)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        mouse_left_down, mouse_left_up = 0x0002, 0x0004
        deadline = time.monotonic() + self.STARTED_GAME_HANDOFF_SECONDS[game_id]
        next_handoff = time.monotonic() + 48.0
        handoff_count = 0
        click_handoff = game_id in self.STARTED_GAME_CLICK_HANDOFFS
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel_requested)
            if process_id not in self._list_running(set(registered_game_process_names(game_id))):
                raise GameLaunchError(f"{game_id} exited during the title/login handoff")
            if click_handoff and handoff_count < 3 and time.monotonic() >= next_handoff:
                hwnd = self._find_window_handle(process_id)
                if hwnd is None:
                    raise GameLaunchError(f"{game_id} started game window disappeared before title handoff")
                window = wintypes.HWND(hwnd)
                foreground = user32.GetForegroundWindow()
                current_thread = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentThreadId()
                target_thread = user32.GetWindowThreadProcessId(window, None)
                foreground_thread = (
                    user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
                )
                attached_target = False
                attached_foreground = False
                try:
                    user32.AllowSetForegroundWindow(-1)
                    if target_thread and target_thread != current_thread:
                        attached_target = bool(
                            user32.AttachThreadInput(current_thread, target_thread, True)
                        )
                    if (
                        foreground_thread
                        and foreground_thread != current_thread
                        and foreground_thread != target_thread
                    ):
                        attached_foreground = bool(
                            user32.AttachThreadInput(current_thread, foreground_thread, True)
                        )
                    user32.ShowWindowAsync(window, 9)
                    user32.BringWindowToTop(window)
                    user32.SetForegroundWindow(window)
                finally:
                    if attached_foreground:
                        user32.AttachThreadInput(current_thread, foreground_thread, False)
                    if attached_target:
                        user32.AttachThreadInput(current_thread, target_thread, False)
                rect = wintypes.RECT()
                if not user32.GetClientRect(window, ctypes.byref(rect)):
                    raise GameLaunchError(f"{game_id} started game window rectangle was unavailable")
                point = wintypes.POINT((rect.right - rect.left) // 2, (rect.bottom - rect.top) // 2)
                if not user32.ClientToScreen(window, ctypes.byref(point)):
                    raise GameLaunchError(f"{game_id} started game title handoff coordinates were unavailable")
                user32.SetCursorPos(point.x, point.y)
                for _ in range(2):
                    user32.mouse_event(mouse_left_down, 0, 0, 0, 0)
                    user32.mouse_event(mouse_left_up, 0, 0, 0, 0)
                    time.sleep(0.25)
                handoff_count += 1
                next_handoff = time.monotonic() + 10.0
            time.sleep(self.POLL_INTERVAL_SECONDS)

    @staticmethod
    def _raise_if_cancelled(
        cancel_requested: LaunchCancellationCheck | None,
    ) -> None:
        if cancel_requested is not None and cancel_requested():
            raise GameLaunchCancelled(
                "Manager cancellation stopped the pre-Adapter game launch"
            )

    # PGR stores its Unity screen preferences in the current user's registry.
    # MPA's promoted pipeline was verified against a 1280x720 window; a
    # borderless 2560x1440 client renders the 1920x1080 scene letterboxed, so
    # every template recognition fails ("识别错误，返回主菜单").  The launch
    # normalises only these Unity Screenmanager values before PGR starts.
    PGR_PLAYER_PREFS_KEY = r"Software\kurogame\战双帕弥什"
    PGR_WINDOW_PREFERENCES: dict[str, int] = {
        "Screenmanager Fullscreen mode": 3,  # Unity FullScreenMode.Windowed
        "Screenmanager Resolution Width": 1280,
        "Screenmanager Resolution Height": 720,
        "Screenmanager Resolution Use Native": 0,
    }

    @classmethod
    def _prepare_pgr_window_preferences(cls) -> dict[str, int]:
        """Pin PGR to a 1280x720 window before MPA attaches.

        Unity suffixes each PlayerPrefs name with a hash (``..._h3630240806``),
        so values are matched by their stable name prefix.  Missing values are
        left alone: a fresh install without preferences starts windowed anyway.
        """

        import winreg

        applied: dict[str, int] = {}
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                cls.PGR_PLAYER_PREFS_KEY,
                0,
                winreg.KEY_READ | winreg.KEY_SET_VALUE,
            )
        except OSError:
            return applied
        try:
            names: list[str] = []
            index = 0
            while True:
                try:
                    name, _value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                names.append(name)
                index += 1
            for prefix, wanted in cls.PGR_WINDOW_PREFERENCES.items():
                for name in names:
                    if name == prefix or name.startswith(prefix + "_h"):
                        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, wanted)
                        applied[name] = wanted
            # Kuro's own resolution memory ("2560,1440") re-applies a
            # letterboxed fullscreen target on start; keep it at the verified
            # window size.  Unity stores this string PlayerPref as bytes.
            for name in names:
                if name == "LastResolution" or name.startswith("LastResolution_h"):
                    winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, b"1280,720\x00")
                    applied[name] = 1280720
        except OSError:
            return applied
        finally:
            winreg.CloseKey(key)
        return applied

    @staticmethod
    def _find_window_handle(process_id: int) -> int | None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        candidates: list[tuple[int, int]] = []

        @callback_type
        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            pid_value = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            if int(pid_value.value) != process_id:
                return True
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if width >= 640 and height >= 360:
                    candidates.append((width * height, int(hwnd)))
            return True

        if not user32.EnumWindows(visit, 0):
            raise GameLaunchError("Windows game-window enumeration failed during title handoff")
        return max(candidates, default=(0, 0))[1] or None

    @staticmethod
    def _find_ready_window(
        running: dict[int, str], ready_names: set[str], game_id: str = ""
    ) -> tuple[int, str, int, int] | None:
        """Return the largest visible, non-minimized registered game window."""

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        candidates: list[tuple[int, str, int, int]] = []
        fatal_titles: list[str] = []

        @callback_type
        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            title_length = user32.GetWindowTextLengthW(hwnd)
            if title_length <= 0:
                return True
            pid_value = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            pid = int(pid_value.value)
            process_name = running.get(pid, "").casefold()
            if process_name not in ready_names:
                return True
            title_buffer = ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            title = title_buffer.value.strip()
            class_buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            window_class = class_buffer.value.strip()
            if game_id == "WW":
                lowered_title = title.casefold()
                if "序列化错误" in title or "serialization error" in lowered_title:
                    fatal_titles.append(title)
                    return True
                # OK-WW binds the actual client through its UnrealWindow.
                # Generic error dialogs owned by the protected client must not
                # authorize the automation GUI to start.
                if window_class.casefold() != "unrealwindow":
                    return True
            rect = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width >= 640 and height >= 360:
                candidates.append((pid, process_name, width, height))
            return True

        if not user32.EnumWindows(visit, 0):
            raise GameLaunchError("Windows game-window enumeration failed")
        if fatal_titles:
            raise GameLaunchError(
                "WW reported a fatal startup dialog before its UnrealWindow became ready: "
                + fatal_titles[0]
            )
        return max(candidates, key=lambda item: item[2] * item[3], default=None)

    def close_for_queue(
        self, game_id: str, game_path: str, *,
        cancel_requested: LaunchCancellationCheck | None = None,
    ) -> GameCloseReceipt:
        """Close the Manager-authorized game's clients, including preexisting ones.

        The caller owns queue membership and human-takeover protection. Only
        registered client names inside the configured local installation are
        actionable here. An unreadable identity remains a blocker; name-only
        enumeration never authorizes termination. No child-tree kill is used.
        """
        self._raise_if_cancelled(cancel_requested)
        if os.name != "nt":
            raise GameLaunchError("queue game close is available only on Windows")
        root, names = self._queue_close_binding(game_id, game_path)
        memory_before = self._available_memory()
        initial, unknown = self._queue_close_snapshot(root, names)
        self._raise_if_cancelled(cancel_requested)
        requested: set[int] = set()
        remaining = set(initial) | unknown
        had_candidates = bool(remaining)
        # A finite sequence: normal WM_CLOSE, one second pass for late windows,
        # then one forced request. Newly spawned instances remain blockers and
        # cannot silently acquire the original instance's close authorization.
        for force, timeout in (
            (False, self.GRACEFUL_CLOSE_SECONDS),
            (False, self.GRACEFUL_CLOSE_SECONDS / 2),
            (True, self.TERMINATE_CLOSE_SECONDS),
        ):
            self._raise_if_cancelled(cancel_requested)
            current, unknown = self._queue_close_snapshot(root, names)
            targets = {pid: identity for pid, identity in current.items() if initial.get(pid) == identity}
            if not targets:
                remaining = set(current) | unknown
                break
            requested.update(targets)
            self._close_verified_queue_processes(targets, force=force, cancel_requested=cancel_requested)
            deadline = time.monotonic() + timeout
            while True:
                self._raise_if_cancelled(cancel_requested)
                current, unknown = self._queue_close_snapshot(root, names)
                remaining = set(current) | unknown
                if not remaining or time.monotonic() >= deadline:
                    break
                time.sleep(self.POLL_INTERVAL_SECONDS)
            if not remaining:
                break
        # Read again even after an apparently successful wait; neither exit code
        # nor TerminateProcess's return value can stand in for this observation.
        self._raise_if_cancelled(cancel_requested)
        current, unknown = self._queue_close_snapshot(root, names)
        remaining = set(current) | unknown
        self._raise_if_cancelled(cancel_requested)
        return GameCloseReceipt(
            "close-failed" if remaining else ("closed" if had_candidates else "already-closed"),
            tuple(sorted(requested)), tuple(sorted(remaining)),
            unverified_process_ids=tuple(sorted(unknown)),
            memory_before=memory_before, memory_after=self._available_memory(),
        )

    @classmethod
    def _queue_close_binding(cls, game_id: str, game_path: str) -> tuple[Path | tuple[Path, ...], set[str]]:
        names = set(registered_game_process_names(game_id))
        if not names:
            raise GameLaunchError("queue close requires a registered game binding")
        # Reject network paths before any filesystem existence probe can reach
        # a disconnected share. The queue never resolves remote game installs.
        cls._require_queue_local_path(Path(game_path))
        executable = cls.validate_configured_executable(game_path)
        if game_id == "NTE" and executable.name.casefold() in {"ntelauncher.exe", "ntegame.exe"}:
            # NTEGame.exe is a registered bootstrap, not the actual client.
            # Both official entry names must retain the sibling Client scope.
            return cls._nte_queue_close_roots(executable), names
        if executable.name.casefold() in names:
            root = executable.parent
        elif game_id == "WW" and executable.name.casefold() == "launcher.exe":
            # The launcher itself is shared UI infrastructure, not a client
            # name authorized by the game registry. Close its game subtree only.
            root = executable.parent / "Wuthering Waves Game"
            cls._require_queue_local_path(root)
        else:
            raise GameLaunchError("configured launcher does not identify a registered game installation for queue close")
        if root == root.parent:
            raise GameLaunchError("queue close cannot use a drive root as a game installation")
        return root.resolve(), names

    @classmethod
    def _nte_queue_close_roots(cls, executable: Path) -> tuple[Path, Path]:
        launcher_root = executable.parent
        if launcher_root.name.casefold() != "ntelauncher":
            raise GameLaunchError("NTE queue close requires the verified NTELauncher installation layout")
        launcher = launcher_root / "NTELauncher.exe"
        config_path = launcher_root / "Config" / "Config.ini"
        cls._require_queue_local_path(launcher)
        cls.validate_configured_executable(str(launcher))
        cls._require_queue_local_path(config_path)
        try:
            if config_path.stat().st_size > 65536:
                raise GameLaunchError("NTE launcher installation configuration exceeds the bounded input size")
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, configparser.Error) as error:
            raise GameLaunchError("NTE queue close could not read the official installation layout") from error
        expected = {
            ("General", "Launcher"): "NTELauncher.exe",
            ("General", "Client"): "NTEGame.exe",
            ("General", "GameID"): "1289",
            ("General", "PackageName"): "com.hottagames.yh",
            ("Patcher", "ResRoot"): "/../Client",
        }
        if any(config.get(section, key, fallback="").casefold() != value.casefold() for (section, key), value in expected.items()):
            raise GameLaunchError("NTE launcher installation configuration does not match the verified Client layout")
        # Only this observed official relative layout is supported. Never turn
        # a configuration string into an arbitrary path or authorize the parent
        # directory (which could be Program Files or contain another install).
        client_root = launcher_root.parent / "Client"
        cls._require_queue_local_path(client_root)
        if not client_root.is_dir():
            raise GameLaunchError("NTE queue close requires the configured Client directory")
        return launcher_root.resolve(), client_root.resolve()

    @staticmethod
    def _require_queue_local_path(path: Path) -> None:
        if not path.is_absolute() or str(path).startswith("\\\\"):
            raise GameLaunchError("queue close requires an absolute local game path")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        if kernel32.GetDriveTypeW(path.anchor) not in {2, 3}:
            raise GameLaunchError("queue close rejects network or unverified game drives")
        try:
            for part in (path, *path.parents):
                if part.lstat().st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                    raise GameLaunchError("queue close rejects reparse-point game paths")
        except OSError as error:
            raise GameLaunchError("queue close could not verify the game installation path") from error

    @classmethod
    def _queue_close_snapshot(
        cls, root: Path | tuple[Path, ...], names: set[str],
    ) -> tuple[dict[int, _QueueProcessIdentity], set[int]]:
        verified: dict[int, _QueueProcessIdentity] = {}
        unknown: set[int] = set()
        roots = root if isinstance(root, tuple) else (root,)
        for pid, name in cls._list_enumerated(names).items():
            identity = cls._queue_process_identity(pid)
            if identity is None:
                unknown.add(pid)
                continue
            # A positively identified foreign installation is outside this
            # binding. Unreadable, replaced, or redirected images are blockers.
            if not any(identity.executable.is_relative_to(candidate) for candidate in roots):
                continue
            if identity.executable.name.casefold() != name.casefold():
                unknown.add(pid)
                continue
            try:
                cls._require_queue_local_path(identity.executable)
            except GameLaunchError:
                unknown.add(pid)
                continue
            verified[pid] = identity
        return verified, unknown

    @staticmethod
    def _queue_process_api():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    @staticmethod
    def _queue_identity_from_handle(kernel32: Any, handle: Any) -> _QueueProcessIdentity | None:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        path = Path(buffer.value)
        if not path.is_absolute():
            return None
        return _QueueProcessIdentity(path, (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime))

    @classmethod
    def _queue_process_identity(cls, process_id: int) -> _QueueProcessIdentity | None:
        kernel32 = cls._queue_process_api()
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return None
        try:
            return cls._queue_identity_from_handle(kernel32, handle)
        finally:
            kernel32.CloseHandle(handle)

    def _close_verified_queue_processes(
        self, targets: dict[int, _QueueProcessIdentity], *, force: bool,
        cancel_requested: LaunchCancellationCheck | None,
    ) -> None:
        kernel32 = self._queue_process_api()
        for pid, identity in sorted(targets.items()):
            self._raise_if_cancelled(cancel_requested)
            handle = kernel32.OpenProcess(0x1000 | (0x0001 if force else 0), False, pid)
            if not handle:
                continue
            try:
                if self._queue_identity_from_handle(kernel32, handle) != identity:
                    continue
                self._require_queue_local_path(identity.executable)
                self._raise_if_cancelled(cancel_requested)
                if force:
                    # Act on the same handle that supplied the verified image
                    # and creation time, so PID reuse cannot retarget the kill.
                    kernel32.TerminateProcess(handle, 1)
                else:
                    self._request_graceful_close({pid})
            finally:
                kernel32.CloseHandle(handle)

    @staticmethod
    def _available_memory() -> dict[str, int] | None:
        """Volatile Windows memory availability, not memory attributed to a close."""
        if os.name != "nt":
            return None
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in (
                    "total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended",
                )
            ]
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
            kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return {"availablePhysicalBytes": int(status.avail_phys), "availableCommitBytes": int(status.avail_page)}
        except OSError:
            pass
        return None

    def close_started(self, receipt: GameLaunchReceipt) -> GameCloseReceipt:
        """Close only game processes created after this Manager launch.

        A client that was already running before the batch is never owned by the
        Manager and is deliberately preserved.  Manager-owned windows receive a
        normal WM_CLOSE first; bounded task termination is only a fallback.
        """

        if receipt.state != "started":
            return GameCloseReceipt("preserved-preexisting", (), ())
        expected_names = set(receipt.expected_process_names)
        baseline = set(receipt.baseline_process_ids)
        launcher = Path(receipt.ww_launcher_path) if receipt.ww_launcher_path else None
        wait_scope = {"ww_launcher": launcher} if launcher else {}
        owned = set(self._list_ww_running(launcher, expected_names) if launcher else self._list_running(expected_names)) - baseline
        zombie_map = self._list_zombies(expected_names)
        if launcher:
            zombie_map = self._filter_ww_processes(launcher, zombie_map)
        zombies = tuple(sorted(set(zombie_map) - baseline))
        if not owned:
            listed = self._listed_cleanup_residuals(expected_names, baseline, launcher)
            return GameCloseReceipt("close-failed" if listed else "already-closed", (), tuple(sorted(listed)), zombies)

        requested = tuple(sorted(owned))
        self._request_graceful_close(owned)
        remaining = self._wait_for_owned_exit(expected_names, baseline, self.GRACEFUL_CLOSE_SECONDS, **wait_scope)
        if remaining:
            # A second WM_CLOSE reaches windows created after the first pass
            # (confirmation dialogs, relaunched launcher shells).
            self._request_graceful_close(remaining)
            remaining = self._wait_for_owned_exit(expected_names, baseline, self.GRACEFUL_CLOSE_SECONDS / 2, **wait_scope)
        if remaining:
            self._terminate_owned(remaining, force=False)
            remaining = self._wait_for_owned_exit(expected_names, baseline, self.TERMINATE_CLOSE_SECONDS, **wait_scope)
        if remaining:
            self._terminate_owned(remaining, force=True)
            remaining = self._wait_for_owned_exit(expected_names, baseline, self.TERMINATE_CLOSE_SECONDS, **wait_scope)
        zombie_map = self._list_zombies(expected_names)
        if launcher:
            zombie_map = self._filter_ww_processes(launcher, zombie_map)
        zombies = tuple(sorted(set(zombie_map) - baseline))
        remaining |= self._listed_cleanup_residuals(expected_names, baseline, launcher)
        return GameCloseReceipt(
            "closed" if not remaining else "close-failed",
            requested,
            tuple(sorted(remaining)),
            zombies,
        )

    def _listed_cleanup_residuals(self, names: set[str], baseline: set[int], launcher: Path | None) -> set[int]:
        listed = self._list_enumerated(names)
        if launcher:
            # An unreadable image cannot be silently discarded as a foreign
            # launcher: preserve it as an unresolved residual, without killing.
            unresolved = {pid for pid in listed if self._process_executable_path(pid) is None}
            listed = {**self._filter_ww_processes(launcher, listed), **{pid: listed[pid] for pid in unresolved}}
        return set(listed) - baseline

    @classmethod
    def list_zombies(cls, game_ids: "list[str] | tuple[str, ...]") -> dict[int, str]:
        """Return exited-but-still-listed game processes for the given games."""

        names: set[str] = set()
        for game_id in game_ids:
            names.update(registered_game_process_names(game_id))
        if not names:
            return {}
        return cls._list_zombies(names)

    @classmethod
    def reap_zombies(
        cls, game_ids: "list[str] | tuple[str, ...]", *, settle_seconds: float = 3.0
    ) -> dict[str, Any]:
        """Legacy best-effort retry for processes with observed final exit codes.

        ``released`` and ``stuck`` are compatibility field names for absence or
        presence in the subsequent enumeration. They do not diagnose a driver,
        memory ownership, or whether a reboot is necessary. Queue transitions
        use close_for_queue's configured-path and instance identity contract.
        """

        before = cls.list_zombies(game_ids)
        if not before:
            return {"attempted": {}, "released": [], "stuck": {}}
        for pid in before:
            cls._terminate_process_handle(pid)
        try:
            subprocess.run(
                ["taskkill", "/F", *sum((["/PID", str(pid)] for pid in before), [])],
                check=False,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            pass
        time.sleep(max(0.0, settle_seconds))
        after = cls.list_zombies(game_ids)
        stuck = {
            str(pid): {
                "name": name,
                **cls._process_diagnostics(pid),
            }
            for pid, name in sorted(after.items())
            if pid in before
        }
        return {
            "attempted": {str(pid): name for pid, name in sorted(before.items())},
            "released": sorted(pid for pid in before if pid not in after),
            "stuck": stuck,
        }

    @staticmethod
    def _terminate_process_handle(process_id: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0001, False, process_id)  # PROCESS_TERMINATE
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _process_diagnostics(process_id: int) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" -ErrorAction SilentlyContinue; "
                    "if ($p) { '{0}|{1}|{2}' -f $p.ThreadCount, $p.ParentProcessId, $p.HandleCount }" % process_id,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        parts = completed.stdout.strip().split("|")
        if len(parts) != 3:
            return {}
        try:
            return {
                "threadCount": int(parts[0]),
                "parentPid": int(parts[1]),
                "handleCount": int(parts[2]),
            }
        except ValueError:
            return {}

    @classmethod
    def _list_zombies(cls, expected_names: set[str]) -> dict[int, str]:
        try:
            listed = cls._list_enumerated(expected_names)
        except GameLaunchError:
            return {}
        return {pid: name for pid, name in listed.items() if not cls._process_is_live(pid)}

    @staticmethod
    def validate_configured_executable(game_path: str) -> Path:
        """Reject a directory, non-EXE, or missing client before config is saved."""

        executable = Path(game_path)
        if executable.suffix.casefold() != ".exe":
            raise GameLaunchError("gamePath must point to a local .exe file")
        if not executable.is_file():
            raise GameLaunchError("configured game executable was not found")
        return executable

    @classmethod
    def _list_running(cls, expected_names: set[str]) -> dict[int, str]:
        return {pid: name for pid, name in cls._list_enumerated(expected_names).items() if cls._process_is_live(pid)}

    @classmethod
    def _list_enumerated(cls, expected_names: set[str]) -> dict[int, str]:
        """Include every named entry, even when its handle or exit state is unavailable."""
        try:
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GameLaunchError(f"unable to inspect the game process list: {error}") from error
        if completed.returncode != 0:
            raise GameLaunchError("unable to inspect the game process list")
        result: dict[int, str] = {}
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) < 2 or row[0].casefold() not in expected_names:
                continue
            try:
                pid = int(row[1])
            except ValueError:
                continue
            if pid > 0:
                result[pid] = row[0]
        return result

    @classmethod
    def _process_is_live(cls, process_id: int) -> bool:
        """Filter observed final exit codes for launch readiness only.

        Access/query failures conservatively return True. This check does not
        explain an enumerated residual or prove that resources were released.
        """

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return True
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == cls.STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _request_graceful_close(process_ids: set[int]) -> None:
        if not process_ids:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        wm_close = 0x0010

        @callback_type
        def visit(hwnd: int, _lparam: int) -> bool:
            pid_value = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            if int(pid_value.value) in process_ids:
                user32.PostMessageW(hwnd, wm_close, 0, 0)
            return True

        user32.EnumWindows(visit, 0)

    def _wait_for_owned_exit(
        self,
        expected_names: set[str],
        baseline_process_ids: set[int],
        timeout_seconds: float,
        *,
        ww_launcher: Path | None = None,
    ) -> set[int]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = set(self._list_ww_running(ww_launcher, expected_names) if ww_launcher else self._list_running(expected_names)) - baseline_process_ids
            if not remaining or time.monotonic() >= deadline:
                return remaining
            time.sleep(self.POLL_INTERVAL_SECONDS)

    @staticmethod
    def _terminate_owned(process_ids: set[int], *, force: bool) -> None:
        for process_id in sorted(process_ids):
            command = ["taskkill", "/PID", str(process_id), "/T"]
            if force:
                command.append("/F")
            try:
                subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                continue
