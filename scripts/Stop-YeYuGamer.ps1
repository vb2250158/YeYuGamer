[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$RuntimeRoot,
    [ValidateRange(5, 300)][int]$TimeoutSeconds = 45
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
if (-not $InstallRoot) { $InstallRoot = Get-YeYuGamerDefaultInstallRoot }
if (-not $RuntimeRoot) { $RuntimeRoot = Get-YeYuGamerDefaultRuntimeRoot }
$installResolved = Assert-YeYuGamerInstallRoot -Path $InstallRoot
$runtimeResolved = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$configPath = Join-Path $runtimeResolved 'config\platform.json'
$python = Join-Path $installResolved '.venv\Scripts\python.exe'
$desktopHost = Join-Path $installResolved 'app\desktop-host\YeYuGamer.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'YeYu Gamer is not installed or its lifecycle configuration is missing.'
}

& $python -I -B -m yeyu_gamer_platform.cli --config $configPath health *> $null
if ($LASTEXITCODE -eq 0) {
    & $python -I -B -m yeyu_gamer_platform.cli --config $configPath manager-stop
    if ($LASTEXITCODE -ne 0) { throw 'YeYu Gamer rejected the safe stop request.' }
}

$trayProcesses = @(Get-YeYuGamerPythonModuleProcesses -ModuleName 'yeyu_gamer_platform.tray')
if ($trayProcesses.Count -gt 0) {
    & $python -I -B -m yeyu_gamer_platform.cli --config $configPath tray-exit
    if ($LASTEXITCODE -ne 0) { throw 'YeYu Gamer rejected the authenticated tray-exit request.' }
}

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $healthy = $false
    try { $healthy = (Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/v1/health' -TimeoutSec 2).status -eq 'ok' } catch {}
    $desktopHostProcesses = @(Get-CimInstance Win32_Process -Filter "Name='YeYuGamer.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $desktopHost })
    $moduleProcesses = @(Get-YeYuGamerPythonModuleProcesses -ModuleName @(
        'yeyu_gamer_platform.tray',
        'yeyu_gamer_platform.manager_host',
        'yeyu_gamer_manager'
    ))
    if (-not $healthy -and $desktopHostProcesses.Count -eq 0 -and $moduleProcesses.Count -eq 0) {
        Write-Host 'YeYu Gamer desktop host and all legacy lifecycle processes stopped safely.'
        exit 0
    }
    Start-Sleep -Milliseconds 250
} while ([DateTimeOffset]::UtcNow -lt $deadline)
throw 'YeYu Gamer accepted the safe-stop request but one or more lifecycle processes did not exit before timeout. No process was force-killed.'
