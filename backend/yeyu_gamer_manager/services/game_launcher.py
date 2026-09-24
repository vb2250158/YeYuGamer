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

_ERROR_ALREADY_EXISTS = 183


def named_mutex_is_held(name: str) -> bool:
    """Report whether somebody currently holds a named Windows mutex.

    Games guard against a second instance with a mutex in
    ``\\Sessions\\<n>\\BaseNamedObjects``, and the kernel releases that mutex only
    when the owning process is destroyed.  Asking the kernel to create the same
    mutex and inspecting ``ERROR_ALREADY_EXISTS`` is the non-destructive way to
    find out whether it is still held; when it is not, the mutex we created is
    closed again immediately and leaves nothing behind.
    """

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - non-Windows host
        return False
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return error == _ERROR_ALREADY_EXISTS


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
    process_close_observations: tuple[dict[str, object], ...] = ()

    def as_result(self) -> dict[str, object]:
        result: dict[str, object] = {
            "state": self.state,
            "requestedProcessIds": list(self.requested_process_ids),
            "remainingProcessIds": list(self.remaining_process_ids),
            "zombieProcessIds": list(self.zombie_process_ids),
            "unverifiedProcessIds": list(self.unverified_process_ids),
            "memoryBefore": self.memory_before,
            "memoryAfter": self.memory_after,
        }
        if self.process_close_observations:
            result["processCloseObservations"] = list(self.process_close_observations)
        return result


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
    """Sample process write activity; this does not confirm a game update.

    ``GetProcessIoCounters`` needs only PROCESS_QUERY_LIMITED_INFORMATION, so
    it works against launcher and anti-cheat protected client processes alike.
    Write counters also include non-update activity. They must not be exposed
    as an official update state or as task progress.
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

    # The single-instance mutex each client takes for itself, read off this
    # installation on 2026-09-22 with Sysinternals handle64:
    #   PGR.exe(6392)          -> \Sessions\1\BaseNamedObjects\comkurogameharukuro
    #   GF2_Exilium.exe(27768) -> \Sessions\1\BaseNamedObjects\
    #                             ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default
    # The kernel releases the mutex only when the owning process is destroyed,
    # so a client that never finishes terminating keeps it forever and no later
    # launch can succeed.  Games without a measured guard are absent on purpose:
    # the launch is only refused where the mechanism has been observed.
    CLIENT_SINGLE_INSTANCE_MUTEXES: dict[str, str] = {
        "PGR": "comkurogameharukuro",
        "GF2": "ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default",
    }

    START_TIMEOUT_SECONDS = 45.0
    READY_TIMEOUT_SECONDS = 300.0
    READY_STABLE_SECONDS = 2.0
    READY_STABLE_OVERRIDES: dict[str, float] = {"WW": 10.0}
    # A client that has only created its window renders nothing yet: the frame
    # is uniformly dark.  Declaring readiness there starts the official tool
    # against a black screen, which makes every behavior-tree node fail
    # (measured on NIKKE 2026-09-21: ready at 16s on a black frame, tree failed
    # 13s later).  Only a *captured and proven* blank frame delays readiness.
    BLANK_WINDOW_LUMA_THRESHOLD = 24
    BLANK_WINDOW_LIT_FRACTION = 0.02
    # How many times one launch may re-place a parked game window before it is
    # treated as a real gate instead of a placement problem.
    MAX_OFF_SCREEN_RESTORES = 3
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
    WW_LAUNCHER_MAX_UPDATE_ACTIONS = 3
    # Outcomes that describe a technical read fault rather than a proven human
    # decision.  The launcher HWND exists before its WebView content and UIA
    # providers settle, so these can occur on an early poll of a healthy
    # launcher.  They stay retryable inside the UI-ready window and only
    # escalate through the normal bounded gate if the launcher never settles.
    WW_LAUNCHER_TRANSIENT_PROBE_OUTCOMES = frozenset(
        {"human:uia-probe-failed", "human:invalid-probe-result"}
    )
    WW_LAUNCHER_PROCESS_NAMES = frozenset({"launcher.exe", "launcher_main.exe", "launcher_updater.exe"})
    # A leftover official launcher whose WebView never produced the audited
    # action is unusable but not dead: the process stays alive and
    # message-responsive inside a blank, uncomposited window.  Every later run
    # then re-observes the same wedge (the Manager only ever observes a
    # pre-existing WW process, it never replaces one) and parks the whole queue
    # on a human gate that no amount of waiting clears.  Recycle a
    # launcher-only leftover a bounded number of times so one bad launcher
    # instance cannot stop the daily.  A running game client is never
    # recyclable and suppresses recycling entirely.
    WW_LAUNCHER_MAX_RECYCLE_RESTARTS = 2
    # Launch gates that prove the launcher surface is unusable rather than that
    # a human decision is pending.  Login/consent/modal gates are deliberately
    # excluded: those must stay human.
    WW_LAUNCHER_RECYCLABLE_GATES = frozenset(
        {
            "ww_launcher_ui_unknown",
            "ww_launcher_uia_probe_failed",
            "ww_launcher_observation_failed",
            "ww_launcher_process_not_observed",
            # The launcher can also wedge *after* its start action: it keeps a
            # blank WebView while the client it spawned dies as a shell with no
            # window (measured on this installation 2026-09-21: launcher_main
            # minimized with a black surface, Wuthering Waves.exe 7.5MB and
            # Client-Win64-Shipping.exe 33MB, no window for 304s).  Reclaiming
            # that debris is the only way the next attempt starts clean; the
            # recycle targets stay empty while a live client exists.
            "ww_launcher_game_window_missing",
            # Same debris, seen one gate earlier: those shells make the loop
            # believe a client is already running, so it stops at 30s instead.
            "ww_launcher_existing_client_unready",
        }
    )
    # Client shells the Manager itself started and that never produced a game
    # window.  A live client holds gigabytes (and a signed-in session), so the
    # memory floor keeps this from ever matching a real running game.
    WW_CLIENT_SHELL_PROCESS_NAMES = frozenset(
        {"wuthering waves.exe", "client-win64-shipping.exe"}
    )
    WW_DEAD_CLIENT_MAX_BYTES = 320 * 1024 * 1024
    # Official status-btn labels may be invoked: 进入游戏, 更新/修复, 重试.
    # Login, consent, and modal confirmations stay human. No coordinate fallback.
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
        ($Kind -eq 'ControlType.Button' -and $Name -match '^(确认|确定|取消)$')) { return 'human:modal-dialog' }
    $mainAction = $Kind -eq 'ControlType.Button' -and $ClassName -cmatch '(^|\s)launcher-button(\s|$)' -and
        $ClassName -cmatch '(^|\s)status-btn(\s|$)'
    if ($mainAction) {
        if ($Name -ceq '进入游戏') { return 'entry' }
        # The official launcher reports the same progress slot as either a
        # "正在X" phrase or a bare "X中…"/"等待中" label (a client update shows
        # 下载中… / 校验中, and 等待中 appears while the client is coming up).
        # All of them mean "keep waiting"; escalating to human here would stop an
        # unattended run while the client is still updating itself.
        if ($Name -match '^(进入中|正在检查|正在更新|正在下载|正在安装|正在校验|正在修复)' -or
            $Name -match '^(下载中|更新中|安装中|校验中|修复中|检查中|进入中|等待中)' -or
            $Name -match '检查游戏版本') { return 'busy' }
        if ($Name -match '^(更新|更新游戏|下载|安装|修复|修复游戏)$') { return 'update' }
        if ($Name -ceq '重试') { return 'retry' }
        if ($Name -match '(失败|异常)') { return 'error' }
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
        # The launcher creates its HWND before the WebView content and the UIA
        # providers settle, so a black transitional window can fail any single
        # property read.  Those reads are best-effort: an unstable window is
        # retried on the next poll instead of being escalated to a human gate.
        # Only a proven identity mismatch stays fatal.
        try {
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
        } catch {
            continue
        }
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
    $actions = @()
    $sawBusy = $false
    $sawError = $false
    foreach ($element in $all) {
        # Read every element property defensively.  While the WebView renders,
        # the tree mutates under the enumeration: an element can vanish between
        # two reads, an unnamed container returns a null Name, and a stale
        # provider raises.  A single unstable element must not become a human
        # gate, so it is skipped and re-read on the next poll.
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = $element.Current.Name
            if ($null -eq $name) { $name = '' }
            $name = $name.Trim()
            $kind = $element.Current.ControlType
            if ($null -eq $kind) { continue }
            $className = $element.Current.ClassName
            $localizedKind = $element.Current.LocalizedControlType
            $isPassword = [bool]$element.Current.IsPassword
            $ownerPid = [int]$element.Current.ProcessId
            $windowPattern = $null
            $isModal = $false
            if ($element.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern, [ref]$windowPattern)) { $isModal = $windowPattern.Current.IsModal }
        } catch {
            continue
        }
        $disposition = Get-YeYuWwElementDisposition -Name $name -Kind $kind.ProgrammaticName `
            -ClassName $className -LocalizedKind $localizedKind `
            -IsPassword $isPassword -IsModal $isModal
        if ($disposition -eq 'ignore') { continue }
        $ownerVerified = $false
        try {
            $ownerVerified = Test-YeYuWwElementOwner -OwnerPid $ownerPid
        } catch {
            continue
        }
        if (-not $ownerVerified) { Write-Output 'human:untrusted-element-owner'; exit 6 }
        if ($disposition.StartsWith('human:')) { Write-Output $disposition; exit 6 }
        if ($disposition -eq 'busy') { $sawBusy = $true; continue }
        if ($disposition -eq 'error') { $sawError = $true; continue }
        if ($disposition -in @('entry','update','retry')) {
            $actions += [pscustomobject]@{ Element = $element; Kind = $disposition; Name = $name }
        }
    }
    if ($actions.Count -eq 0) {
        if ($sawBusy) { Write-Output 'waiting:ww-busy'; exit 3 }
        if ($sawError) { Write-Output 'waiting:ww-error-state'; exit 3 }
        Write-Output 'waiting:unrecognized-launcher-ui'; exit 3
    }
    if ($actions.Count -ne 1) { Write-Output 'human:ambiguous-enter-game-button'; exit 6 }
    $action = $actions[0]
    $button = $action.Element
    $expectedName = @{ entry = '进入游戏'; update = $action.Name; retry = '重试' }[$action.Kind]
    $buttonReady = $false
    try {
        $buttonReady = (Test-YeYuWwElementOwner -OwnerPid $button.Current.ProcessId) -and
            -not $button.Current.IsOffscreen -and $button.Current.IsEnabled -and
            ($button.Current.Name -ceq $expectedName)
    } catch {
        # The audited button is re-read immediately before dispatch.  A repaint
        # can invalidate it mid-read; never invoke a stale element, and let the
        # next poll settle instead of requesting human intervention.
        Write-Output 'waiting:button-state-unstable'; exit 3
    }
    if (-not $buttonReady) {
        Write-Output 'human:button-owner-or-state-changed'; exit 6
    }
    $pattern = $null
    if (-not $button.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        Write-Output 'human:invoke-pattern-unavailable'; exit 6
    }
    if ($env:YEYU_WW_ALLOW_INVOKE -ne '1') {
        Write-Output (@{ entry = 'ready:ww-enter-game'; update = 'ready:ww-update'; retry = 'ready:ww-retry' }[$action.Kind])
        exit 3
    }
    $pattern.Invoke()
    Write-Output (@{ entry = 'invoked:ww-enter-game'; update = 'invoked:ww-update'; retry = 'invoked:ww-retry' }[$action.Kind])
    exit 0
} catch {
    # Diagnosis only: the stdout classification stays byte-identical so the
    # Manager's probe contract is unchanged, while stderr names the exact
    # UIA/COM read that failed.  _launcher_probe_summary already folds the last
    # stderr line into lastLauncherProbe, so a transient fault that exhausts the
    # UI-ready window can be localised instead of only being reported as
    # 'human:uia-probe-failed'.
    try {
        $faultMessage = ([string]$_.Exception.Message) -replace '\s+', ' '
        [Console]::Error.WriteLine(
            'uia-probe-exception: ' + [string]$_.Exception.GetType().Name + ': ' + $faultMessage)
    } catch {
    }
    Write-Output 'human:uia-probe-failed'; exit 6
}
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
    # Budget for the launched surface to expose its registered process.  NIKKE
    # starts through the installed WeGame bootstrap, which routinely needs far
    # more than the 45s default before the client appears (observed
    # 2026-09-18: game_start_failed at 46.2s).
    START_TIMEOUT_OVERRIDES: dict[str, float] = {"NIKKE": 600.0}
    STARTED_GAME_HANDOFF_SECONDS: dict[str, float] = {
        # These clients expose a large visible window before the account has
        # entered the playable scene.  Only a client created by this Manager
        # run receives a bounded startup delay before the Adapter starts.
        "PGR": 75.0,
        "StarRail": 75.0,
        "Endfield": 75.0,
    }
    ENDFIELD_LAUNCHER_POLL_SECONDS = 5.0
    ENDFIELD_MAX_NO_EFFECT_ACTIONS = 3
    # A total ceiling on audited launcher actions, independent of the no-effect
    # counter.  Measured 2026-09-21: the launcher reported "acted" every 5s while
    # the client sat as a 6MB shell with no window; because "acted" resets the
    # no-effect counter and the wait-timeout check lives in the same elif chain,
    # the ladder never tripped and it dispatched audited *foreground* clicks
    # (stealing focus each time) for 23 minutes, making the desktop unusable.
    ENDFIELD_MAX_LAUNCHER_ACTIONS = 12
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
    # Exit-code probes inform readiness only. An enumerated residual with an
    # unusual exit state does not establish why it remains or free its resources.
    STILL_ACTIVE = 259
    # Current official public-desktop shortcut targets the root Launcher.exe.
    # Games.exe may also omit --region (observed with signed launcher 1.5.0).
    # Preserve its official default; reject conflicting explicit selectors.
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
    $regions = [regex]::Matches($entry.CommandLine, '(?i)(?:^|\s)--region=([^\s]+)')
    if ($regions.Count -gt 1 -or ($regions.Count -eq 1 -and $regions[0].Groups[1].Value -ine 'CN')) { exit 6 }
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
    _ENDFIELD_UIA_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$signature = @'
using System;
using System.Runtime.InteropServices;
public static class YeYuEndfieldLauncherInput {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
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
    $before = [YeYuEndfieldLauncherInput]::GetForegroundWindow()
    [YeYuEndfieldLauncherInput]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [YeYuEndfieldLauncherInput]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
    $shown = [YeYuEndfieldLauncherInput]::ShowWindowAsync($Handle, 9)
    $raised = [YeYuEndfieldLauncherInput]::BringWindowToTop($Handle)
    $activated = [YeYuEndfieldLauncherInput]::SetForegroundWindow($Handle)
    Start-Sleep -Milliseconds 400
    $after = [YeYuEndfieldLauncherInput]::GetForegroundWindow()
    $script:lastForegroundProbe = "requester=$PID;target=$Handle;before=$before;after=$after;show=$shown;raise=$raised;activate=$activated"
    return ($after -eq $Handle)
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
$observedProcesses = @(Get-CimInstance Win32_Process -Filter "Name='Games.exe'" -ErrorAction SilentlyContinue)
$pathCandidates = @($observedProcesses | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_.ExecutablePath) -and
    [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)
})
$gameCandidates = @($pathCandidates | Where-Object {
    $selectors = [regex]::Matches($_.CommandLine, '(?i)(?:^|\s)--game=([^\s]+)')
    $selectors.Count -eq 1 -and $selectors[0].Groups[1].Value -ieq 'Endfield'
})
$candidates = @($gameCandidates | Where-Object {
    $regions = [regex]::Matches($_.CommandLine, '(?i)(?:^|\s)--region=([^\s]+)')
    $regions.Count -eq 0 -or ($regions.Count -eq 1 -and $regions[0].Groups[1].Value -ieq 'CN')
})
if ($candidates.Count -ne 1) {
    Write-Output ('not-ready:games-exe-candidates=' + $candidates.Count +
        ';observed=' + $observedProcesses.Count + ';pathMatched=' + $pathCandidates.Count +
        ';gameMatched=' + $gameCandidates.Count + ';regionCompatible=' + $candidates.Count)
    exit 3
}
$process = Get-Process -Id $candidates[0].ProcessId -ErrorAction SilentlyContinue
if ($null -eq $process -or $process.MainWindowHandle -eq 0) { Write-Output 'not-ready:launcher-window-missing'; exit 3 }
$window = $process.MainWindowHandle
if ([YeYuEndfieldLauncherInput]::IsIconic($window)) {
    $requested = [YeYuEndfieldLauncherInput]::ShowWindowAsync($window, 9)
    Write-Output ('not-ready:launcher-restore-requested;pid=' + $process.Id + ';accepted=' + $requested)
    exit 3
}
$root = [Windows.Automation.AutomationElement]::FromHandle($window)
# The launcher renders through QtWebEngine. An empty accessibility tree does
# not establish whether its page is loaded. The existing fixed-point fallback
# is enabled only after its delay; exact window and pixel-owner guards remain.
$descendants = $root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
if ($descendants.Count -eq 0 -and $env:YEYU_ENDFIELD_ALLOW_FALLBACK -ne '1') { Write-Output 'not-ready:accessibility-tree-empty'; exit 3 }
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
    if ($null -eq $clicked) { Write-Output ('not-ready:foreground-not-acquired;' + $script:lastForegroundProbe); exit 3 }
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
if ($null -eq $clicked) { Write-Output ('not-ready:foreground-not-acquired;' + $script:lastForegroundProbe); exit 3 }
Write-Output 'no-effect:launcher-primary-action'
exit 4
"""

    @classmethod
    def _resolve_nikke_launcher(cls, configured_executable: Path) -> Path:
        """Use the installed WeGame bootstrap for its NIKKE distribution.

        The client checks its Rail launch environment and cannot be started
        directly in this layout. Keep the configured client as the identity
        used for readiness and cleanup; do not copy launcher credentials.
        """
        directory = configured_executable.parent / "WeGameLauncher"
        if configured_executable.name.casefold() != "nikke.exe" or not directory.exists():
            return configured_executable
        candidate = directory / "launcher.exe"
        try:
            cls._require_queue_local_path(candidate)
        except GameLaunchError as error:
            raise GameLaunchError("NIKKE WeGame launcher could not be verified in the configured installation") from error
        try:
            candidate.resolve().relative_to(configured_executable.parent.resolve())
        except ValueError as error:
            raise GameLaunchError("NIKKE WeGame launcher escapes the configured installation") from error
        if not candidate.is_file():
            raise GameLaunchError("NIKKE WeGame launcher is missing from the configured installation")
        return candidate

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

    def _reject_leftover_client_instance(self, game_id: str) -> None:
        """Refuse a launch whose own leftover client still owns the guard mutex.

        Measured on this installation 2026-09-22: a client that never completes
        process teardown stays enumerated by ``tasklist``/``CreateToolhelp32``
        while ``GetExitCodeProcess`` already reports its final code, and the
        kernel has *not* released the game's single-instance mutex.  ``PGR``'s
        ``comkurogameharukuro`` and ``GF2``'s single-instance mutex were both
        still held hours after their owners stopped responding, which is why
        every later launch self-terminates (PGR: exit 0 within seconds, no
        window, zero bytes written to its own log) or reports the client's own
        "Another instance is already running" (GF2).

        That leftover cannot be removed from user mode -- ``TerminateProcess``
        fails with ``ERROR_ACCESS_DENIED`` (the real status is
        ``STATUS_PROCESS_IS_TERMINATING``), ``TerminateThread`` on its remaining
        threads returns success without releasing the mutex, and it survives a
        Manager restart -- so only a machine restart clears it.  Launching
        anyway spends the whole readiness timeout, produces no evidence beyond
        the same timeout, and leaves one more unreapable leftover, so the launch
        is refused with a reason that names the mechanism and the process ids.
        """

        mutex_name = self.CLIENT_SINGLE_INSTANCE_MUTEXES.get(game_id)
        if not mutex_name or not named_mutex_is_held(mutex_name):
            return
        expected_names = set(registered_game_process_names(game_id))
        if not expected_names:
            return
        leftovers = self._list_zombies(expected_names)
        if not leftovers:
            # The mutex is held by something that is not an exited leftover --
            # a live client is handled by the ordinary already-running path.
            return
        raise GameLaunchError(
            "a previous %s client never finished terminating and still holds the "
            "single-instance mutex %s, so a new launch can only exit immediately. "
            "Leftover process ids: %s. This cannot be cleared from user mode; the "
            "machine has to be restarted."
            % (
                game_id,
                mutex_name,
                ", ".join(str(pid) for pid in sorted(leftovers)),
            )
        )

    def _start_launch_executable(
        self, game_id: str, executable: Path, launch_executable: Path, ww_launcher: Path | None,
    ) -> subprocess.Popen:
        """Start the configured launch executable for one game.

        Extracted so the launch path can start a *fresh* launcher after it
        reclaims a wedged one, instead of only working when a launcher already
        existed before the run.
        """

        self._reject_leftover_client_instance(game_id)
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
                    # _start_endfield_launcher reaps its own child.
                    return self._start_endfield_launcher(launch_executable)
                else:
                    process = subprocess.Popen(
                        [str(launch_executable)],
                        cwd=str(launch_executable.parent),
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
        self._reap_spawned_process(process)
        return process

    @staticmethod
    def _reap_spawned_process(process: subprocess.Popen) -> None:
        """Wait for a spawned child so its process handle stops being held.

        CPython keeps a ``Popen`` whose child is still running alive in
        ``subprocess._active`` (see ``Popen.__del__``: "Not reading subprocess
        exit status creates a zombie process which is only destroyed at the
        parent python process exit").  The launch path only reads ``.pid``, so
        the object dies milliseconds after the spawn -- always while the child
        is still running -- and the Manager therefore held that child's handle
        for its whole lifetime.  Releasing it here is correct hygiene and keeps
        the Manager from being one of the holders.

        It is *not*, however, the reason the clients stayed enumerable.  Measured
        2026-09-22 with Sysinternals ``handle64``: the processes holding the dead
        ``PGR.exe``/``GF2_Exilium.exe`` entries were Windows' own subsystems
        (``RpcSs``/``Themes``/``Audiosrv``, one ``Process`` handle each), GF2's
        own crash handlers, and the clients' own hundreds of ``Thread`` handles.
        Those clients never complete teardown, so nothing in user mode can free
        them -- see ``_reject_leftover_client_instance`` for what follows from
        that.
        """

        wait = getattr(process, "wait", None)
        if not callable(wait):
            return
        try:
            threading.Thread(target=wait, name="yeyu-launch-reaper", daemon=True).start()
        except RuntimeError:  # pragma: no cover - interpreter shutdown
            pass

    def _start_endfield_launcher(self, executable: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            [str(executable), *self.ENDFIELD_LAUNCHER_ARGUMENTS],
            cwd=str(executable.parent), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            env=self._external_process_environment(),
        )
        self._reap_spawned_process(process)
        return process

    # ── NTE launcher upgrade prompt ──────────────────────────────────────────
    # The NTE launcher shows a modal ("全新启动器现已推出！") that covers its
    # 开始游戏 button.  The official LauncherTask only searches for the start
    # button, so an unattended run stalls behind the prompt (observed
    # 2026-09-18).  Before handing the launch to the tool the Manager starts the
    # launcher, clicks the audited upgrade action and leaves the launcher
    # running.  Only this single label is clicked: login, consent and notice
    # dialogs stay human.
    NTE_LAUNCHER_UPGRADE_LABEL = "立即体验"
    NTE_LAUNCHER_UPGRADE_PROBE_SECONDS = 5.0
    NTE_LAUNCHER_UPGRADE_TIMEOUT_SECONDS = 900.0
    _NTE_LAUNCHER_PROMPT_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch { Write-Output 'error:nte-launcher-uia-unavailable'; exit 2 }
$rootPath = [IO.Path]::GetFullPath($env:YEYU_NTE_LAUNCHER_ROOT).TrimEnd('\') + '\'
$candidates = @()
foreach ($process in Get-Process -ErrorAction SilentlyContinue) {
    if ($process.ProcessName -cnotin @('NTEGame', 'NTELauncher')) { continue }
    if ($process.MainWindowHandle -eq 0) { continue }
    try { $image = [IO.Path]::GetFullPath($process.MainModule.FileName) } catch { continue }
    if (-not $image.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) { continue }
    $candidates += $process
}
if ($candidates.Count -eq 0) { Write-Output 'none:nte-launcher-window'; exit 3 }
$buttonCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::Button)
$label = $env:YEYU_NTE_UPGRADE_LABEL
foreach ($process in $candidates) {
    $window = $null
    try { $window = [Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle) } catch { continue }
    if ($null -eq $window) { continue }
    foreach ($button in @($window.FindAll([Windows.Automation.TreeScope]::Descendants, $buttonCondition))) {
        if ($button.Current.Name -cne $label) { continue }
        if (-not $button.Current.IsEnabled -or $button.Current.IsOffscreen) { Write-Output 'waiting:nte-launcher-upgrade-disabled'; exit 3 }
        $rect = $button.Current.BoundingRectangle
        if ($rect.Width -lt 40 -or $rect.Height -lt 20) { Write-Output 'waiting:nte-launcher-upgrade-not-actionable'; exit 3 }
        if ($env:YEYU_NTE_ALLOW_INVOKE -ne '1') { Write-Output 'ready:nte-launcher-upgrade'; exit 3 }
        $pattern = $null
        try { $pattern = $button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern) } catch { $pattern = $null }
        if ($null -eq $pattern) { Write-Output 'no-effect:nte-launcher-upgrade:no-invoke-pattern'; exit 4 }
        $pattern.Invoke()
        $deadline = [DateTime]::UtcNow.AddSeconds(8)
        while ([DateTime]::UtcNow -lt $deadline) {
            $remaining = @($window.FindAll([Windows.Automation.TreeScope]::Descendants, $buttonCondition) |
                Where-Object { $_.Current.Name -ceq $label })
            if ($remaining.Count -eq 0) { Write-Output 'invoked:nte-launcher-upgrade:removed'; exit 0 }
            try {
                if (-not $remaining[0].Current.IsEnabled) { Write-Output 'invoked:nte-launcher-upgrade:state-changed'; exit 0 }
            } catch { Write-Output 'invoked:nte-launcher-upgrade:element-replaced'; exit 0 }
            Start-Sleep -Milliseconds 500
        }
        Write-Output 'no-effect:nte-launcher-upgrade:unchanged'
        exit 4
    }
}
Write-Output 'none:nte-launcher-upgrade-prompt'
exit 3
'''

    @classmethod
    def _probe_nte_launcher_prompt(
        cls, launcher_root: Path, *, allow_invoke: bool,
        cancel_requested: LaunchCancellationCheck | None,
    ) -> str:
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        environment = cls._external_process_environment()
        environment["YEYU_NTE_LAUNCHER_ROOT"] = str(launcher_root)
        environment["YEYU_NTE_UPGRADE_LABEL"] = cls.NTE_LAUNCHER_UPGRADE_LABEL
        environment["YEYU_NTE_ALLOW_INVOKE"] = "1" if allow_invoke else "0"
        try:
            result = cls._run_launcher_probe(
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cls._NTE_LAUNCHER_PROMPT_SCRIPT],
                environment=environment, cancel_requested=cancel_requested,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "error:nte-launcher-probe-failed"
        return result.stdout.strip()

    def _start_nte_launcher_upgrade_watcher(
        self, launcher: Path, *, cancel_requested: LaunchCancellationCheck | None = None,
    ) -> None:
        """Clear the NTE launcher's "new launcher" modal once the tool starts it.

        The launcher belongs to the official LauncherTask, so this service must
        not start it: a bounded daemon watcher waits for that window to appear,
        clicks the audited upgrade action once, and exits.  It never touches
        Manager state and never raises, so a launcher problem cannot turn into a
        new failure mode for the daily.
        """
        launcher_root = launcher.parent
        probe_seconds = self.NTE_LAUNCHER_UPGRADE_PROBE_SECONDS
        deadline_seconds = self.NTE_LAUNCHER_UPGRADE_TIMEOUT_SECONDS

        def watch() -> None:
            deadline = time.monotonic() + deadline_seconds
            while time.monotonic() < deadline:
                if cancel_requested is not None:
                    try:
                        if cancel_requested():
                            return
                    except Exception:  # noqa: BLE001 - a failing probe must not kill the watcher
                        return
                outcome = self._probe_nte_launcher_prompt(
                    launcher_root, allow_invoke=True, cancel_requested=None,
                )
                if outcome.startswith("invoked:"):
                    return
                time.sleep(probe_seconds)

        threading.Thread(target=watch, name="nte-launcher-upgrade-watch", daemon=True).start()

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
            official_launch = {
                "NTE": ({"ntelauncher.exe", "ntegame.exe"}, "LauncherTask.run",
                        "official_enable_after_start",
                        "official LauncherTask launch and game capture, then DailyRoutineTask"),
                # OneDragon's existing-window branch skips OpenAndEnterGame.
                # Starting the EXE here bypasses its login and launch arguments.
                "ZZZ": ({"zenlesszonezero.exe"}, "OpenAndEnterGame.execute",
                        "official_no_game_window_branch",
                        "official OpenGame and EnterGame completion, then selected applications"),
            }.get(game_id)
            if official_launch and Path(game_path).name.casefold() in official_launch[0]:
                self._raise_if_cancelled(cancel_requested)
                executable = self.validate_configured_executable(game_path)
                names = self._launch_surface_names(game_id, str(executable))
                if game_id == "NTE":
                    # Clear the launcher's "new launcher" modal once the official
                    # tool brings the launcher up; otherwise its LauncherTask
                    # never finds the start button and the queue stalls behind
                    # the prompt.  The watcher is bounded and never blocks the
                    # handoff.
                    self._start_nte_launcher_upgrade_watcher(executable, cancel_requested=cancel_requested)
                baseline = self._list_running(set(names))
                receipt = GameLaunchReceipt(
                    "official-tool-pending", None, executable.name,
                    tuple(sorted(baseline)), tuple(sorted(names)),
                )
                self._notify(
                    observer, game_id, "official-launch-pending", started_at, names,
                    {"operation": official_launch[1], "trigger": official_launch[2],
                     "waitingFor": official_launch[3],
                     "launchState": receipt.state}, process_ids=frozenset(baseline),
                )
                return receipt
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
        if result.returncode == 0 and allow_invoke and outcome in {
            "invoked:ww-enter-game", "invoked:ww-update", "invoked:ww-retry",
        }:
            return outcome
        if result.returncode == 3 and outcome in {
            "waiting:launcher-window", "waiting:game-window", "waiting:unrecognized-launcher-ui",
            "waiting:ww-busy", "waiting:ww-error-state", "waiting:button-state-unstable",
            "ready:ww-enter-game", "ready:ww-update", "ready:ww-retry",
        }:
            return outcome
        if result.returncode == 6 and outcome in {
            "human:untrusted-window-owner", "human:ambiguous-launcher-window",
            "human:login-or-consent",
            "human:ambiguous-enter-game-button", "human:invoke-pattern-unavailable",
            "human:uia-probe-failed", "human:button-owner-or-state-changed",
            "human:modal-dialog", "human:unrecognized-primary-action", "human:untrusted-element-owner",
        }:
            return outcome
        return "human:invalid-probe-result"

    # The installed WeGame surface is the only supported entry point for this
    # NIKKE distribution.  Its bootstrap launcher starts WeGame and hands the
    # request over, but WeGame itself still owns the final start/update
    # decision, so a run that only starts the bootstrap waits forever for a
    # client nobody asked WeGame to start.  The Manager therefore observes the
    # WeGame surface and performs the audited primary action itself.
    # Login, consent, purchase and account prompts stay human.
    NIKKE_WEGAME_PROCESS_NAMES = frozenset({"wegame.exe", "wegame_env.exe"})
    NIKKE_WEGAME_POLL_SECONDS = 8.0
    # A healthy WeGame surface answers within a page load, and a cold WeGame
    # start needs about two minutes; anything longer means WeGame is waiting for
    # a decision the Manager must not make.  This stays well below the NIKKE
    # client-start budget so a stuck surface cannot consume the whole run.
    NIKKE_WEGAME_UI_READY_SECONDS = 240.0
    NIKKE_WEGAME_MAX_ACTION_ACTIONS = 3
    # Measured on this installation's WeGame NIKKE player page (2026-09-20,
    # 1280x800 frame): the primary action sits at the bottom-right of the
    # player page.  Only used when the Chromium host exposes no accessibility
    # button, and only through the same foreground/ownership guards.
    NIKKE_WEGAME_ACTION_POSITION = (0.807, 0.934)
    _NIKKE_WEGAME_ACTION_LABELS = ("\u542f\u52a8", "\u66f4\u65b0", "\u91cd\u8bd5", "\u7ee7\u7eed")
    _NIKKE_WEGAME_ACTION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch { Write-Output 'error:wegame-uia-unavailable'; exit 2 }
$signature = @'
using System;
using System.Runtime.InteropServices;
public static class YeYuWeGameSurfaceInput {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(YeYuWeGamePoint point);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr processId);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, IntPtr arg);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out YeYuWeGameRect rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int count);
    public delegate bool EnumProc(IntPtr hWnd, IntPtr arg);
}
[StructLayout(LayoutKind.Sequential)]
public struct YeYuWeGamePoint { public int X; public int Y; }
[StructLayout(LayoutKind.Sequential)]
public struct YeYuWeGameRect { public int Left; public int Top; public int Right; public int Bottom; }
'@
Add-Type -TypeDefinition $signature
function Get-YeYuWeGameOwnerPid {
    param([IntPtr]$Handle)
    $ptr = [Runtime.InteropServices.Marshal]::AllocHGlobal(4)
    try {
        [void][YeYuWeGameSurfaceInput]::GetWindowThreadProcessId($Handle, $ptr)
        return [uint32][Runtime.InteropServices.Marshal]::ReadInt32($ptr)
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($ptr) }
}
$roots = @()
foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
    if ($process.ProcessName -ine 'wegame') { continue }
    try { $image = [IO.Path]::GetFullPath($process.MainModule.FileName) } catch { continue }
    if ([IO.Path]::GetFileName($image) -ine 'wegame.exe') { continue }
    $parent = Split-Path -Parent $image
    if ($parent) { $roots += ($parent.TrimEnd('\') + '\') }
}
if ($roots.Count -eq 0) { Write-Output 'none:wegame-not-running'; exit 3 }
$windows = New-Object 'System.Collections.Generic.List[object]'
$callback = [YeYuWeGameSurfaceInput+EnumProc]{
    param([IntPtr]$handle, [IntPtr]$arg)
    if (-not [YeYuWeGameSurfaceInput]::IsWindowVisible($handle)) { return $true }
    $length = [YeYuWeGameSurfaceInput]::GetWindowTextLength($handle)
    if ($length -le 0) { return $true }
    $buffer = New-Object System.Text.StringBuilder ($length + 1)
    [void][YeYuWeGameSurfaceInput]::GetWindowText($handle, $buffer, $buffer.Capacity)
    if ($buffer.ToString() -cne 'WeGame') { return $true }
    $ownerPid = Get-YeYuWeGameOwnerPid -Handle $handle
    $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if ($null -eq $owner) { return $true }
    try { $ownerImage = [IO.Path]::GetFullPath($owner.MainModule.FileName) } catch { return $true }
    foreach ($root in $roots) {
        if ($ownerImage.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
            $windows.Add([pscustomobject]@{ Handle = $handle; ProcessId = $ownerPid })
            break
        }
    }
    return $true
}
[void][YeYuWeGameSurfaceInput]::EnumWindows($callback, [IntPtr]::Zero)
if ($windows.Count -eq 0) { Write-Output 'waiting:wegame-window-missing'; exit 3 }
if ($windows.Count -gt 1) { Write-Output ('human:ambiguous-wegame-window:' + $windows.Count); exit 6 }
$target = $windows[0]
$window = $null
try { $window = [Windows.Automation.AutomationElement]::FromHandle($target.Handle) } catch { $window = $null }
if ($null -eq $window) { Write-Output 'waiting:wegame-uia-root-missing'; exit 3 }
$buttonCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::Button)
$labels = $env:YEYU_NIKKE_WEGAME_LABELS -split '\|'
# WeGame's player page is rendered by a Chromium host that does not publish an
# accessibility tree at all (the window exposes a single "Chrome Legacy Window"
# pane), so UIA can only ever confirm or rule out an exposed button.  Prefer an
# exposed labelled button when one exists, otherwise fall back to one audited
# proportional action on the bottom-right primary-action area of the player
# page, exactly like the Endfield launcher's bounded fixed action.  The caller
# bounds how many times this may happen.
$match = $null
foreach ($button in @($window.FindAll([Windows.Automation.TreeScope]::Descendants, $buttonCondition))) {
    try { $name = $button.Current.Name } catch { continue }
    if ([string]::IsNullOrWhiteSpace($name)) { continue }
    $hit = $false
    foreach ($label in $labels) { if ($name.StartsWith($label, [StringComparison]::Ordinal)) { $hit = $true; break } }
    if (-not $hit) { continue }
    if (-not $button.Current.IsEnabled -or $button.Current.IsOffscreen) {
        Write-Output ('waiting:wegame-action-disabled:' + $name); exit 3
    }
    $rect = $button.Current.BoundingRectangle
    if ($rect.Width -lt 60 -or $rect.Height -lt 24) {
        Write-Output ('waiting:wegame-action-not-actionable:' + $name); exit 3
    }
    $match = [pscustomobject]@{ Name = $name; X = [int]($rect.Left + ($rect.Width / 2)); Y = [int]($rect.Top + ($rect.Height / 2)) }
    break
}
if ($null -eq $match) {
    if ([YeYuWeGameSurfaceInput]::IsIconic($target.Handle)) {
        Write-Output 'waiting:wegame-restore-requested'; exit 3
    }
    $frame = New-Object YeYuWeGameRect
    if (-not [YeYuWeGameSurfaceInput]::GetWindowRect($target.Handle, [ref]$frame)) {
        Write-Output 'waiting:wegame-window-rect-unavailable'; exit 3
    }
    $width = $frame.Right - $frame.Left
    $height = $frame.Bottom - $frame.Top
    if ($width -lt 640 -or $height -lt 360) { Write-Output 'waiting:wegame-window-too-small'; exit 3 }
    $match = [pscustomobject]@{
        Name = 'audited-primary-action'
        X = [int]($frame.Left + ($width * $env:YEYU_NIKKE_WEGAME_ACTION_X))
        Y = [int]($frame.Top + ($height * $env:YEYU_NIKKE_WEGAME_ACTION_Y))
    }
}
if ($env:YEYU_NIKKE_WEGAME_ALLOW_ACTION -ne '1') { Write-Output ('ready:wegame-primary:' + $match.Name); exit 3 }
$handle = $target.Handle
if ([YeYuWeGameSurfaceInput]::IsIconic($handle)) {
    [void][YeYuWeGameSurfaceInput]::ShowWindowAsync($handle, 9)
    Write-Output 'waiting:wegame-restore-requested'; exit 3
}
[YeYuWeGameSurfaceInput]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[YeYuWeGameSurfaceInput]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)
[void][YeYuWeGameSurfaceInput]::BringWindowToTop($handle)
[void][YeYuWeGameSurfaceInput]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 400
if ([YeYuWeGameSurfaceInput]::GetForegroundWindow() -ne $handle) {
    Write-Output 'blocked:wegame-foreground-not-acquired'; exit 5
}
$x = [int]$match.X
$y = [int]$match.Y
$point = New-Object YeYuWeGamePoint
$point.X = $x; $point.Y = $y
$hit = [YeYuWeGameSurfaceInput]::WindowFromPoint($point)
if ($hit -eq [IntPtr]::Zero) { Write-Output 'blocked:wegame-point-unowned'; exit 5 }
if ((Get-YeYuWeGameOwnerPid -Handle $hit) -ne [uint32]$target.ProcessId) {
    Write-Output 'blocked:wegame-foreign-window'; exit 5
}
[void][YeYuWeGameSurfaceInput]::SetCursorPos($x, $y)
Start-Sleep -Milliseconds 150
# WeGame is Chromium: InvokePattern is a no-op there, so dispatch one real
# foreground click on the audited action instead.
[YeYuWeGameSurfaceInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
[YeYuWeGameSurfaceInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
$verifyDeadline = [DateTime]::UtcNow.AddSeconds(12)
while ([DateTime]::UtcNow -lt $verifyDeadline) {
    if (Get-Process -Name 'nikke' -ErrorAction SilentlyContinue) {
        Write-Output ('clicked:wegame-primary:' + $match.Name + ':game-started'); exit 0
    }
    Start-Sleep -Milliseconds 400
}
Write-Output ('no-effect:wegame-primary:' + $match.Name)
exit 4
"""

    def _drive_nikke_wegame_surface(
        self,
        *,
        observer: LaunchObserver | None,
        game_id: str,
        started_at: float,
        expected_names: set[str],
        actions: int,
        wait_started_at: float,
        cancel_requested: LaunchCancellationCheck | None,
    ) -> tuple[int, str | None]:
        """Probe the WeGame surface once and perform one bounded audited action.

        The NIKKE client only appears after WeGame itself runs the start/update
        action, so this runs while no registered game process exists yet.
        Returns the updated action count and the last probe outcome; raises the
        human gate when WeGame waits for a decision the automation must not make.
        """

        wegame_running = self._list_running(set(self.NIKKE_WEGAME_PROCESS_NAMES))
        outcome = self._probe_nikke_wegame_primary_action(
            allow_action=actions < self.NIKKE_WEGAME_MAX_ACTION_ACTIONS,
            cancel_requested=cancel_requested,
        )
        surface_names = set(expected_names) | self.NIKKE_WEGAME_PROCESS_NAMES
        surface_pids = frozenset(wegame_running)
        if outcome.startswith(("clicked:", "no-effect:")):
            actions += 1
            self._notify(
                observer, game_id, "launcher-action", started_at, surface_names, {
                    "action": "nikke-wegame-primary-action",
                    "outcome": outcome,
                    "dispatched": True,
                    "gameReady": False,
                    "actions": actions,
                    "waitingFor": "registered NIKKE client window started by the audited WeGame action",
                },
                process_ids=surface_pids,
            )
        elif outcome.startswith("human:"):
            raise GameLaunchHumanRequired(
                "nikke_wegame_" + outcome.removeprefix("human:").split(":")[0].replace("-", "_"),
                "WeGame requires operator inspection before it can start NIKKE: "
                + outcome.removeprefix("human:"),
                detail={"launcherOutcome": outcome}, process_ids=surface_pids,
            )
        elif time.monotonic() - wait_started_at >= self.NIKKE_WEGAME_UI_READY_SECONDS:
            # Never park the queue on the whole client-start budget: a WeGame
            # surface that has not started the client inside the UI-ready window
            # is waiting for a decision the automation must not make alone.
            raise GameLaunchHumanRequired(
                "nikke_wegame_launch_required",
                "The installed WeGame surface did not start the NIKKE client from its audited action; "
                "confirm the client is fully updated and the WeGame account is signed in, then resume.",
                detail={"launcherOutcome": outcome, "actions": actions},
                process_ids=surface_pids,
            )
        return actions, outcome

    @classmethod
    def _probe_nikke_wegame_primary_action(
        cls, *, allow_action: bool,
        cancel_requested: LaunchCancellationCheck | None,
    ) -> str:
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        environment = cls._external_process_environment()
        environment["YEYU_NIKKE_WEGAME_LABELS"] = "|".join(cls._NIKKE_WEGAME_ACTION_LABELS)
        environment["YEYU_NIKKE_WEGAME_ALLOW_ACTION"] = "1" if allow_action else "0"
        environment["YEYU_NIKKE_WEGAME_ACTION_X"] = "%.4f" % cls.NIKKE_WEGAME_ACTION_POSITION[0]
        environment["YEYU_NIKKE_WEGAME_ACTION_Y"] = "%.4f" % cls.NIKKE_WEGAME_ACTION_POSITION[1]
        try:
            result = cls._run_launcher_probe(
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cls._NIKKE_WEGAME_ACTION_SCRIPT],
                environment=environment, cancel_requested=cancel_requested,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "error:wegame-probe-failed"
        outcome = result.stdout.strip()
        if outcome.startswith(("clicked:wegame-primary:", "ready:wegame-primary:", "waiting:", "none:", "blocked:", "human:", "error:")):
            return outcome
        return "error:wegame-invalid-probe-result"

    @classmethod
    def _capture_window_frame(cls, hwnd: int) -> tuple[int, int, bytes] | None:
        """Capture one window's pixels for a blank-screen check. Never reads game memory."""

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        # Handles are pointers on 64-bit Windows: declare every signature or
        # ctypes truncates them and every call silently fails.
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.BitBlt.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HANDLE, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 64 or height < 64:
            return None
        window_dc = user32.GetWindowDC(hwnd)
        if not window_dc:
            return None
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)

        class _BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        try:
            gdi32.SelectObject(memory_dc, bitmap)
            if not user32.PrintWindow(hwnd, memory_dc, 2):  # PW_RENDERFULLCONTENT
                gdi32.BitBlt(memory_dc, 0, 0, width, height, window_dc, 0, 0, 0x00CC0020)
            header = _BitmapInfoHeader()
            header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            header.biWidth = width
            header.biHeight = -height
            header.biPlanes = 1
            header.biBitCount = 32
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                return None
            return width, height, buffer.raw
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)

    @classmethod
    def _window_is_proven_blank(cls, hwnd: int) -> bool:
        """True only when a frame was captured and is essentially dark.

        Fail-open on purpose: a frame that cannot be captured never blocks a
        launch, so this can only ever delay readiness for a proven black window.
        """

        try:
            frame = cls._capture_window_frame(hwnd)
        except OSError:
            return False
        if frame is None:
            return False
        width, height, pixels = frame
        pixel_count = width * height
        step = max(1, pixel_count // 4096)
        sampled = 0
        lit = 0
        for index in range(0, pixel_count, step):
            offset = index * 4
            blue = pixels[offset]
            green = pixels[offset + 1]
            red = pixels[offset + 2]
            sampled += 1
            if (red + green + blue) >= 3 * cls.BLANK_WINDOW_LUMA_THRESHOLD:
                lit += 1
        if sampled == 0:
            return False
        return (lit / sampled) < cls.BLANK_WINDOW_LIT_FRACTION

    @classmethod
    def _window_is_off_screen(cls, hwnd: int) -> bool:
        """True only when a captured frame proves the window sits outside the desktop.

        Windows parks minimized/hidden windows at -32000,-32000 while
        ``IsWindowVisible`` still reports true, so visibility alone cannot tell
        whether the official tool can actually see the game.  Measured
        2026-09-22: the ZZZ client sat at (-32000,-32000) and the official tool
        looped on "enter game" for 35 minutes taking screenshots it could never
        act on; restoring the window let it continue immediately.
        """

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        if rect.left <= -30000 or rect.top <= -30000:
            return True
        virtual_left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        virtual_top = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        virtual_width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        virtual_height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if virtual_width <= 0 or virtual_height <= 0:
            return False
        return (
            rect.right <= virtual_left
            or rect.bottom <= virtual_top
            or rect.left >= virtual_left + virtual_width
            or rect.top >= virtual_top + virtual_height
        )

    @classmethod
    def _restore_window_on_screen(cls, hwnd: int) -> bool:
        """Bring a parked game window back onto the desktop; report success."""

        user32 = ctypes.WinDLL("user32", set_last_error=True)
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        rect = wintypes.RECT()
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(640, rect.right - rect.left)
        height = max(360, rect.bottom - rect.top)
        user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        # SWP_SHOWWINDOW: the window is ours to place, so a fixed on-screen
        # position is enough and keeps the move deterministic.
        return bool(user32.SetWindowPos(hwnd, None, 40, 40, width, height, 0x0040))

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

    @classmethod
    def _ww_launcher_recycle_targets(cls, running: dict[int, str]) -> set[int]:
        """Processes the Manager may replace so a wedged WW launch self-heals.

        A launcher is disposable because the Manager starts its own.  A *live*
        game client is never recyclable -- it may hold a live, signed-in
        session -- so any live client suppresses recycling entirely.  Client
        shells that never produced a game window and hold almost no memory are
        the Manager's own debris: leaving them in place wedges the launcher
        permanently, because recycling is what would clear the wedge.
        """

        if not running:
            return set()
        targets: set[int] = set()
        for pid, name in running.items():
            if name.casefold() in cls.WW_LAUNCHER_PROCESS_NAMES:
                targets.add(pid)
                continue
            if cls._is_proven_dead_ww_client(pid, name):
                targets.add(pid)
                continue
            return set()
        return targets

    @classmethod
    def _is_proven_dead_ww_client(cls, process_id: int, name: str) -> bool:
        """True only for WW client debris, never for a live game client."""

        if name.casefold() not in cls.WW_CLIENT_SHELL_PROCESS_NAMES:
            return False
        if cls._find_window_handle(process_id) is not None:
            return False
        working_set = cls._process_working_set_bytes(process_id)
        if working_set is None:
            return False
        return working_set < cls.WW_DEAD_CLIENT_MAX_BYTES

    @staticmethod
    def _process_working_set_bytes(process_id: int) -> int | None:
        """Working set of one process; ``None`` when it cannot be read."""

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0400 | 0x0010, False, process_id)
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            if not kernel32.K32GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)

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

    def _notify_launch_executable_started(
        self,
        observer: LaunchObserver | None,
        game_id: str,
        started_at: float,
        process_names: set[str],
        process: subprocess.Popen,
        executable: Path,
    ) -> None:
        """Record the exact process this attempt started, with its pid.

        Measured 2026-09-22: `PGR.exe` was spawned around 08:20:24 and exited
        within the same second, yet every following ``launch.launcher-waiting``
        sample reported ``pids=[]`` for the whole 300s window.  The spawn could
        only be proven by enumerating processes outside the Manager, so a client
        that dies before it can write a single log line must still leave an
        attributable launch record.
        """

        self._notify(
            observer, game_id, "launcher-action", started_at, process_names,
            {
                "operation": "launch-executable-started",
                "processId": process.pid,
                "executable": executable.name,
                "waitingFor": "registered game client window",
            },
            process_ids=frozenset({process.pid}),
        )

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
            else self._resolve_nikke_launcher(executable) if game_id == "NIKKE" else executable
        )
        nikke_wegame = game_id == "NIKKE" and launch_executable != executable
        readiness_root = executable.parent if nikke_wegame else launch_executable.parent
        ww_launcher = (
            launch_executable.resolve()
            if game_id == "WW" and launch_executable.name.casefold() == "launcher.exe"
            else None
        )
        if ww_launcher is not None:
            self._verify_ww_launcher(ww_launcher, cancel_requested=cancel_requested)

        expected_names = set(registered_game_process_names(game_id))
        if not nikke_wegame:
            expected_names.add(launch_executable.name.casefold())
        if game_id == "Endfield":
            expected_names.add("games.exe")
        if ww_launcher is not None:
            expected_names.update(self.WW_LAUNCHER_PROCESS_NAMES)
        baseline = self._list_ww_running(ww_launcher, expected_names) if ww_launcher else self._list_running(expected_names)
        existing = next(iter(baseline.values()), None)
        ww_launcher_recycles = 0
        while existing:
            reactivation_pid = (
                self._reactivate_headless_endfield_launcher(
                    launch_executable, baseline, cancel_requested=cancel_requested,
                ) if game_id == "Endfield" else None
            )
            reactivation_scope = frozenset((*baseline, reactivation_pid)) if reactivation_pid else None
            # Only an official launcher leftover may be replaced.  Reusing a
            # healthy pre-existing launcher stays the fast path; recycling is a
            # last resort applied only after the probe has already failed.
            recycle_targets = (
                self._ww_launcher_recycle_targets(baseline)
                if ww_launcher is not None and reactivation_scope is None
                else set()
            )
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
                    None if reactivation_scope is not None else readiness_root,
                    observer=observer,
                    started_at=started_at,
                    cancel_requested=cancel_requested,
                    **({"ww_launcher": ww_launcher} if ww_launcher else {}),
                    **({"endfield_reactivation": (executable.parent, reactivation_scope)} if reactivation_scope is not None else {}),
                    nikke_wegame=nikke_wegame,
                )
            except GameLaunchCancelled:
                raise
            except GameLaunchHumanRequired as gate:
                if not (
                    recycle_targets
                    and ww_launcher_recycles < self.WW_LAUNCHER_MAX_RECYCLE_RESTARTS
                    and gate.reason_code in self.WW_LAUNCHER_RECYCLABLE_GATES
                ):
                    raise
                # The pre-existing launcher never exposed a usable surface.
                # Replace it so the next iteration starts a fresh one instead
                # of re-observing the same wedge forever.
                ww_launcher_recycles += 1
                self._notify(
                    observer, game_id, "launcher-action", started_at, expected_names,
                    {
                        "action": "ww-launcher-recycle",
                        "dispatched": True,
                        "gameReady": False,
                        "reasonCode": gate.reason_code,
                        "recycleAttempt": ww_launcher_recycles,
                        "recycledProcessIds": sorted(recycle_targets),
                    },
                    process_ids=frozenset(recycle_targets),
                )
                survivors = self._recycle_ww_launcher(recycle_targets)
                if survivors:
                    # The wedge could not be removed; keep the typed human gate
                    # rather than launching a second launcher beside it.
                    raise GameLaunchHumanRequired(
                        gate.reason_code,
                        str(gate),
                        detail={**gate.detail, "recycleSurvivors": sorted(survivors)},
                        process_ids=gate.process_ids,
                    ) from gate
                baseline = self._list_ww_running(ww_launcher, expected_names)
                existing = next(iter(baseline.values()), None)
                continue
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

        process = self._start_launch_executable(game_id, executable, launch_executable, ww_launcher)
        self._notify_launch_executable_started(
            observer, game_id, started_at, expected_names, process, launch_executable,
        )

        if nikke_wegame:
            self._notify(
                observer, game_id, "launcher-action", started_at, expected_names,
                {"action": "nikke-wegame-bootstrap", "dispatched": True, "gameReady": False,
                 "waitingFor": "registered NIKKE client window from the installed WeGame launcher"},
                process_ids=frozenset({process.pid}),
            )

        start_timeout = self.START_TIMEOUT_OVERRIDES.get(game_id, self.START_TIMEOUT_SECONDS)
        deadline = time.monotonic() + start_timeout
        # The NIKKE client only appears after WeGame itself runs the audited
        # start/update action, so the surface must be driven here -- before any
        # registered game process exists to hand over to _wait_until_ready.
        nikke_wegame_actions = 0
        nikke_wegame_wait_started_at = time.monotonic()
        next_nikke_surface_at = 0.0
        next_nikke_observation_at = (
            nikke_wegame_wait_started_at + self.LAUNCH_OBSERVATION_INTERVAL_SECONDS
        )
        last_nikke_outcome: str | None = None
        # Venue for the single renewal that the WW recycle branch performs.
        # A blocking `_wait_until_ready` call burns the whole readiness budget
        # *inside* one loop iteration while this loop condition still compares
        # against the start deadline computed before it, so a wedged launcher is
        # only ever reported near/after that deadline.  Without a renewal the
        # `continue` below re-tests an expired deadline and the loop exits
        # immediately, discarding the launcher the recycle branch just started
        # -- the recovery branch could never work.
        restart_deadlines: list[float] = []
        while True:
            active_deadline = restart_deadlines[-1] if restart_deadlines else deadline
            if time.monotonic() >= active_deadline:
                break
            self._raise_if_cancelled(cancel_requested)
            observed = self._list_ww_running(ww_launcher, expected_names) if ww_launcher else self._list_running(expected_names)
            if nikke_wegame and not observed:
                now = time.monotonic()
                wegame_running = self._list_running(set(self.NIKKE_WEGAME_PROCESS_NAMES))
                surface_names = set(expected_names) | self.NIKKE_WEGAME_PROCESS_NAMES
                surface_pids = frozenset(set(observed) | set(wegame_running))
                if now >= next_nikke_surface_at:
                    nikke_wegame_actions, last_nikke_outcome = self._drive_nikke_wegame_surface(
                        observer=observer,
                        game_id=game_id,
                        started_at=started_at,
                        expected_names=expected_names,
                        actions=nikke_wegame_actions,
                        wait_started_at=nikke_wegame_wait_started_at,
                        cancel_requested=cancel_requested,
                    )
                    next_nikke_surface_at = now + self.NIKKE_WEGAME_POLL_SECONDS
                if now >= next_nikke_observation_at:
                    # Keep a readable WeGame capture in the run log; without the
                    # surface in scope every failure artifact was a black frame.
                    next_nikke_observation_at = now + self.LAUNCH_OBSERVATION_INTERVAL_SECONDS
                    self._notify(
                        observer, game_id, "launcher-waiting", started_at,
                        surface_names, {
                            "running": {str(pid): name for pid, name in sorted(wegame_running.items())},
                            "lastLauncherOutcome": last_nikke_outcome,
                            "actions": nikke_wegame_actions,
                            "waitingFor": "audited WeGame action, then a registered NIKKE client window",
                            "readyProcessNames": sorted(expected_names),
                        },
                        process_ids=surface_pids,
                    )
            if observed:
                try:
                    ready = self._wait_until_ready(
                        game_id,
                        expected_names,
                        readiness_root,
                        observer=observer,
                        started_at=started_at,
                        cancel_requested=cancel_requested,
                        **({"ww_launcher": ww_launcher} if ww_launcher else {}),
                        nikke_wegame=nikke_wegame,
                    )
                except GameLaunchCancelled:
                    # Cancellation deliberately preserves the launcher/client
                    # surface.  It is not a launch failure and must not invoke
                    # the normal Manager-owned process cleanup path.
                    raise
                except GameLaunchHumanRequired as gate:
                    # A launcher the Manager started itself can wedge too (blank
                    # WebView plus dead client shells).  The pre-existing-launcher
                    # branch already replaces those leftovers; without the same
                    # handling here the gate is terminal even though recycling is
                    # exactly what fixes it (measured 2026-09-22:
                    # ww_launcher_game_window_missing at 302s produced no recycle
                    # attempt because this path had no handler at all).
                    #
                    # Scope note: the recycle candidates here are the processes
                    # observed *after* this attempt started (`observed`, i.e. the
                    # debris this very attempt produced), not the pre-launch
                    # leftovers the `baseline` branch above handles.  Reading an
                    # undefined `running` here raised NameError and downgraded
                    # every recyclable WW gate into `adapter_start_failed`
                    # (measured 2026-09-22 05:17 and 05:25 on this host).
                    #
                    # `observed` is enumerated at the top of the loop and the
                    # blocking ready-wait then runs for up to READY_TIMEOUT_SECONDS
                    # inside the same iteration, so that snapshot predates every
                    # shell the wedged launcher started while the wait was
                    # running.  For a recyclable gate, enumerate again here and
                    # recycle the union: measured 2026-09-24 21:37 the gate fired
                    # with `running={26752,31972,44620}` visible to the waiter, yet
                    # only the stale `{26752}` was replaced, so the surviving
                    # shells made the freshly started launcher fail immediately
                    # with `ww_launcher_existing_client_unready`.  Gates that must
                    # stay human (login, consent, modals) are absent from the
                    # recyclable set and are re-raised without any extra probe.
                    recyclable = (
                        ww_launcher is not None
                        and gate.reason_code in self.WW_LAUNCHER_RECYCLABLE_GATES
                    )
                    if recyclable:
                        try:
                            observed = {
                                **observed,
                                **self._list_ww_running(ww_launcher, expected_names),
                            }
                        except GameLaunchError:
                            pass
                    targets = (
                        self._ww_launcher_recycle_targets(observed) if recyclable else set()
                    )
                    if (
                        not targets
                        or ww_launcher_recycles >= self.WW_LAUNCHER_MAX_RECYCLE_RESTARTS
                    ):
                        raise
                    ww_launcher_recycles += 1
                    self._notify(
                        observer, game_id, "launcher-action", started_at, expected_names,
                        {
                            "operation": "recycle-wedged-launcher",
                            "trigger": gate.reason_code,
                            "processIds": sorted(targets),
                            "recycleAttempt": ww_launcher_recycles,
                            "waitingFor": "a fresh launcher start after the wedged leftovers were replaced",
                        },
                        process_ids=frozenset(targets),
                    )
                    survivors = self._recycle_ww_launcher(targets)
                    if survivors:
                        raise GameLaunchHumanRequired(
                            gate.reason_code,
                            str(gate),
                            detail={**gate.detail, "recycleSurvivors": sorted(survivors)},
                            process_ids=gate.process_ids,
                        ) from gate
                    process = self._start_launch_executable(
                        game_id, executable, launch_executable, ww_launcher,
                    )
                    self._notify_launch_executable_started(
                        observer, game_id, started_at, expected_names, process, launch_executable,
                    )
                    # The fresh launcher needs its own readiness budget: the
                    # deadline above was measured from the *previous* start and
                    # the blocking ready-wait just consumed it (measured
                    # 2026-09-24: the restart at 370s was followed one
                    # millisecond later by `ww_launcher_process_not_observed`
                    # with `pids=[]`, which parked the whole game day on a
                    # human gate for 16 hours).
                    restart_deadlines.append(time.monotonic() + start_timeout)
                    continue
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
                    self._notify(
                        observer, game_id, "client-startup-wait", started_at, expected_names,
                        {
                            "requiredWaitSeconds": self.STARTED_GAME_HANDOFF_SECONDS[game_id],
                            "trigger": "Manager started this client; configured STARTED_GAME_HANDOFF_SECONDS applies",
                            "waitingFor": "configured startup delay expires while the owned client remains running, then official Adapter dispatch",
                        },
                        process_ids=frozenset({ready[0]}),
                    )
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
        nikke_wegame: bool = False,
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
        blank_window_seen = False
        off_screen_restores = 0
        next_launcher_action_at = 0.0
        endfield_no_effect_actions = 0
        endfield_launcher_actions = 0
        launcher_wait_started_at = time.monotonic()
        next_observation_at = wait_started_at + self.LAUNCH_OBSERVATION_INTERVAL_SECONDS
        last_launcher_outcome: str | None = None
        ww_invoked = False
        ww_update_actions = 0
        nikke_wegame_actions = 0
        running: dict[int, str] = {}
        surface_names: set[str] = set(expected_names)
        surface_pids: set[int] = set()
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
            # The WeGame surface owns the final NIKKE start/update decision, so
            # it must be part of the observed and captured scene even though it
            # is not a registered game client.
            surface_names: set[str] = set(expected_names)
            surface_pids: set[int] = set(running)
            if nikke_wegame:
                surface_names |= self.NIKKE_WEGAME_PROCESS_NAMES
                surface_pids |= set(self._list_running(set(self.NIKKE_WEGAME_PROCESS_NAMES)))
            # I/O activity is diagnostic only. A launcher can write cache/log
            # data while its verified Resume Download button awaits input.
            drive_launcher = launcher_root is not None and ready is None and endfield_reactivation is None
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
                elif outcome in {"invoked:ww-update", "invoked:ww-retry"}:
                    ww_update_actions += 1
                    if ww_update_actions > self.WW_LAUNCHER_MAX_UPDATE_ACTIONS:
                        raise GameLaunchError(
                            "WW launcher update/retry was dispatched too many times without a game window"
                        )
                    launcher_wait_started_at = now
                    self._notify(
                        observer, game_id, "launcher-action", observation_origin, expected_names,
                        {
                            "action": outcome.removeprefix("invoked:"),
                            "dispatched": True,
                            "gameReady": False,
                            "updateActions": ww_update_actions,
                        },
                        process_ids=frozenset(running),
                    )
                elif outcome in {
                    "waiting:ww-busy", "waiting:ww-error-state",
                    "ready:ww-update", "ready:ww-retry",
                }:
                    launcher_wait_started_at = now
                    deadline = min(max(deadline, now + ready_timeout), hard_cap)
                elif (
                    outcome in self.WW_LAUNCHER_TRANSIENT_PROBE_OUTCOMES
                    and not ww_invoked
                    and now - launcher_wait_started_at < self.WW_LAUNCHER_UI_READY_SECONDS
                ):
                    # A UIA/COM read that failed while the launcher window was
                    # still being built is a technical fault, not a human
                    # decision.  Keep polling inside the same UI-ready window;
                    # if the launcher never settles this falls through to the
                    # bounded gate below on a later poll.
                    pass
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
                if outcome == "restore-requested":
                    self._notify(
                        observer, game_id, "launcher-action", observation_origin,
                        expected_names, {
                            "operation": "restore-launcher-window",
                            "trigger": "verified Endfield launcher window is minimized",
                            "waitingFor": "next official launcher probe confirms restored window and button state",
                            "lastLauncherProbe": self._last_launcher_probe,
                            "outcome": outcome,
                        }, process_ids=frozenset(running),
                    )
                if outcome == "acted":
                    endfield_no_effect_actions = 0
                    endfield_launcher_actions += 1
                    if endfield_launcher_actions > self.ENDFIELD_MAX_LAUNCHER_ACTIONS:
                        # Bound the total number of audited foreground actions.
                        # A launcher that keeps reporting "acted" while the client
                        # never appears must fail into the normal cleanup path
                        # (close what this launch started) instead of clicking
                        # forever and stealing the operator's foreground.
                        raise GameLaunchError(
                            "Endfield launcher dispatched "
                            f"{endfield_launcher_actions} audited actions without yielding a game window"
                        )
                    self._notify(
                        observer, game_id, "launcher-action", observation_origin,
                        expected_names, {"outcome": outcome, "launcherActions": endfield_launcher_actions},
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
                        "Endfield launcher start-action wait expired; "
                        f"last probe: {self._last_launcher_probe or 'not reported'}"
                    )
                next_launcher_action_at = (
                    now + self.ENDFIELD_LAUNCHER_POLL_SECONDS
                )
            if ready is None and now >= next_observation_at:
                next_observation_at = now + self.LAUNCH_OBSERVATION_INTERVAL_SECONDS
                observation_deadline = deadline
                deadline_trigger = "registered game client window readiness timeout"
                waiting_for = "registered game client window with required dimensions, then stability interval"
                if game_id == "Endfield" and drive_launcher and last_launcher_outcome == "not-ready":
                    action_deadline = launcher_wait_started_at + self.LAUNCHER_UI_READY_TIMEOUT_SECONDS
                    if action_deadline < observation_deadline:
                        observation_deadline = action_deadline
                        deadline_trigger = "Endfield launcher start-action wait timeout"
                    waiting_for = "verified launcher start/update action, followed by a registered game client window"
                self._notify(
                    observer,
                    game_id,
                    "launcher-waiting",
                    observation_origin,
                    surface_names,
                    {
                        "running": {str(pid): name for pid, name in sorted(running.items())},
                        "writeActivity": updating,
                        "bytesWrittenDelta": progress.last_delta,
                        "lastLauncherOutcome": last_launcher_outcome,
                        "lastLauncherProbe": self._last_launcher_probe,
                        "secondsUntilDeadline": max(0.0, round(observation_deadline - now, 1)),
                        "deadlineTrigger": deadline_trigger,
                        "waitingFor": waiting_for,
                        "readyProcessNames": sorted(ready_names),
                    },
                    process_ids=frozenset(surface_pids),
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
                    handle = self._find_window_handle(ready[0])
                    if handle is not None and self._window_is_proven_blank(handle):
                        # The window exists but renders nothing yet; the official
                        # tool must not be started against that black screen.
                        blank_window_seen = True
                        stable_key = None
                        stable_since = 0.0
                        next_observation_at = now
                    elif (
                        handle is not None
                        and off_screen_restores < self.MAX_OFF_SCREEN_RESTORES
                        and self._window_is_off_screen(handle)
                    ):
                        # A parked (off-screen) window cannot be acted on: the
                        # official tool screenshots/clicks it and loops forever
                        # (measured 2026-09-22: ZZZ looped on "enter game" for 35
                        # minutes at -32000,-32000 and continued immediately once
                        # the window was placed back on the desktop).
                        off_screen_restores += 1
                        self._restore_window_on_screen(handle)
                        self._notify(
                            observer, game_id, "launcher-action", observation_origin,
                            expected_names, {
                                "operation": "restore-off-screen-game-window",
                                "processId": ready[0],
                                "restoreAttempt": off_screen_restores,
                                "waitingFor": "an on-screen game window the official tool can act on",
                            },
                            process_ids=frozenset(running),
                        )
                        stable_key = None
                        stable_since = 0.0
                        next_observation_at = now
                    else:
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
        if blank_window_seen:
            raise GameLaunchHumanRequired(
                "game_window_blank",
                "The configured game client created its window but never rendered content before the launch "
                "timeout; the client is still loading or waiting for an in-game prompt (for example a notice "
                "dialog), so the official tool was not started.",
                detail={"gameId": game_id, "blankWindow": True},
                process_ids=frozenset(running),
            )
        if nikke_wegame:
            raise GameLaunchHumanRequired(
                "nikke_wegame_game_window_missing",
                "WeGame was started but the NIKKE client never exposed a stable game window; "
                "inspect the preserved WeGame surface before resuming.",
                detail={
                    "actions": nikke_wegame_actions,
                    "lastLauncherOutcome": last_launcher_outcome,
                },
                process_ids=frozenset(surface_pids),
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
        if returncode == 3 and text.startswith("not-ready:launcher-restore-requested;"):
            return "restore-requested"
        if returncode == 0 and text.startswith(("invoked:", "clicked:")):
            return "acted"
        if returncode == 5 or text.startswith("blocked-by-foreign-window:"):
            return "foreground-interference"
        if returncode == 4 or text.startswith("no-effect:"):
            return "no-effect"
        return "not-ready"

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
        """Keep the existing new-client startup delay before official dispatch."""

        self._raise_if_cancelled(cancel_requested)
        deadline = time.monotonic() + self.STARTED_GAME_HANDOFF_SECONDS[game_id]
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel_requested)
            if process_id not in self._list_running(set(registered_game_process_names(game_id))):
                raise GameLaunchError(f"{game_id} exited during the configured startup delay")
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
        """Window size belongs to PGR and MPA. YeYu does not rewrite PlayerPrefs."""

        return {}

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
        on_close: Callable[[dict[str, object]], None] | None = None,
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
        observations: list[dict[str, object]] = []
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
            observations.extend(self._close_verified_queue_processes(
                targets, force=force, cancel_requested=cancel_requested, on_close=on_close,
            ))
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
            process_close_observations=tuple(observations),
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
            # A process that has already exited needs no action and is not a
            # blocker.  Windows keeps such an entry enumerable while another
            # process still holds its handle (measured 2026-09-22: PGR.exe stayed
            # listed with exit code 0 and could never be "closed"), and reporting
            # it as remaining stopped the whole queue with
            # queue_game_cleanup_incomplete.  A process whose state cannot be
            # read is still treated as live by _process_is_live, so genuinely
            # unverifiable processes stay blockers.
            if not cls._process_is_live(pid):
                continue
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
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
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
        on_close: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[dict[str, object], ...]:
        kernel32 = self._queue_process_api()
        observations: list[dict[str, object]] = []
        for pid, identity in sorted(targets.items()):
            self._raise_if_cancelled(cancel_requested)
            # SYNCHRONIZE permits a zero-time observation on the very handle
            # used for identity verification and termination; it adds no wait.
            access = 0x1000 | (0x100001 if force else 0)
            ctypes.set_last_error(0)
            handle = kernel32.OpenProcess(access, False, pid)
            open_error = ctypes.get_last_error() if not handle else None
            observation: dict[str, object] = {
                "processId": pid,
                "phase": "terminate" if force else "wm-close",
                "expectedCreationFiletime": identity.created_at,
                "openProcess": {"accessMask": access, "succeeded": bool(handle), "winError": open_error},
                "outcome": "open-process-failed",
            }
            observations.append(observation)
            if not handle:
                continue
            audited = False
            try:
                observed_identity = self._queue_identity_from_handle(kernel32, handle)
                if observed_identity != identity:
                    observation["outcome"] = "identity-unavailable" if observed_identity is None else "identity-mismatch"
                    continue
                self._require_queue_local_path(identity.executable)
                observation["identityVerified"] = True
                self._raise_if_cancelled(cancel_requested)
                if on_close is not None:
                    observation["outcome"] = "request-not-yet-issued"
                    on_close({**observation, "auditPhase": "requested", "outcome": "request-not-yet-issued"})
                    audited = True
                if force:
                    observation["waitBefore"] = self._queue_wait_observation(kernel32, handle)
                self._raise_if_cancelled(cancel_requested)
                if force:
                    # Act on the same handle that supplied the verified image
                    # and creation time, so PID reuse cannot retarget the kill.
                    ctypes.set_last_error(0)
                    returned = int(kernel32.TerminateProcess(handle, 1))
                    accepted = bool(returned)
                    terminate_error = ctypes.get_last_error() if not accepted else None
                    observation["terminateProcess"] = {"apiReturn": returned, "succeeded": accepted, "winError": terminate_error}
                    observation["outcome"] = "termination-request-accepted" if accepted else "termination-request-failed"
                    observation["waitAfter"] = self._queue_wait_observation(kernel32, handle)
                else:
                    self._request_graceful_close({pid})
                    # The legacy WM_CLOSE helper does not return dispatch ACKs.
                    # Do not claim a window existed or accepted its message.
                    observation["outcome"] = "graceful-close-helper-returned"
            except Exception as error:
                observation["outcome"] = "close-interrupted"
                observation["errorType"] = type(error).__name__
                raise
            finally:
                kernel32.CloseHandle(handle)
                if audited and on_close is not None:
                    on_close({**observation, "auditPhase": "result"})
        return tuple(observations)

    @staticmethod
    def _queue_wait_observation(kernel32: Any, handle: Any) -> dict[str, object]:
        ctypes.set_last_error(0)
        value = int(kernel32.WaitForSingleObject(handle, 0))
        error = ctypes.get_last_error() if value == 0xFFFFFFFF else None
        return {
            "value": value,
            "state": {0: "signaled", 258: "not-signaled", 0xFFFFFFFF: "failed"}.get(value, "unexpected"),
            "winError": error,
        }

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

        if receipt.state not in {"started", "official-tool-pending"}:
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
        # An entry that has already exited is not a residual.  Windows keeps
        # listing a terminated process while another process still holds its
        # handle (measured on this installation 2026-09-21: PGR.exe stayed
        # enumerable with exit code 0 and could not be terminated, which made
        # every following game stop at queue_game_cleanup_incomplete).  Only a
        # process that is still live -- or whose state cannot be read at all --
        # remains an unresolved residual.
        return {
            pid for pid in listed
            if pid not in baseline and self._process_is_live(pid)
        }

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

    def _recycle_ww_launcher(self, process_ids: set[int]) -> set[int]:
        """Replace a wedged launcher-only leftover and report survivors.

        Mirrors the Manager-owned close ladder (normal close, then bounded
        termination) but is scoped strictly to the launcher pids handed in, so
        no game client can ever be affected.
        """

        if not process_ids:
            return set()
        self._request_graceful_close(process_ids)
        remaining = self._wait_pids_exit(process_ids, self.GRACEFUL_CLOSE_SECONDS)
        if remaining:
            self._terminate_owned(remaining, force=False)
            remaining = self._wait_pids_exit(remaining, self.TERMINATE_CLOSE_SECONDS)
        if remaining:
            self._terminate_owned(remaining, force=True)
            remaining = self._wait_pids_exit(remaining, self.TERMINATE_CLOSE_SECONDS)
        return remaining

    @staticmethod
    def _wait_pids_exit(process_ids: set[int], timeout_seconds: float) -> set[int]:
        """Return the subset of ``process_ids`` still alive when the wait ends."""

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        synchronize = 0x00100000
        wait_timeout = 0x00000102

        def alive(process_id: int) -> bool:
            handle = kernel32.OpenProcess(synchronize, False, process_id)
            if not handle:
                # A process that cannot be opened for a zero-time wait either
                # exited or is otherwise not observable; treat as gone so the
                # recycle never spins on an unreadable handle.
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)

        deadline = time.monotonic() + timeout_seconds
        remaining = {pid for pid in process_ids if alive(pid)}
        while remaining and time.monotonic() < deadline:
            time.sleep(0.5)
            remaining = {pid for pid in remaining if alive(pid)}
        return remaining

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
