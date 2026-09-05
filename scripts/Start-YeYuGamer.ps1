[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$RuntimeRoot,
    [switch]$NoOpenWebGui,
    [ValidateRange(5, 300)][int]$TimeoutSeconds = 150
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
if (-not $InstallRoot) { $InstallRoot = Get-YeYuGamerDefaultInstallRoot }
if (-not $RuntimeRoot) { $RuntimeRoot = Get-YeYuGamerDefaultRuntimeRoot }
$installResolved = Assert-YeYuGamerInstallRoot -Path $InstallRoot
$runtimeResolved = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$configPath = Join-Path $runtimeResolved 'config\platform.json'
$desktopHost = Join-Path $installResolved 'app\desktop-host\YeYuGamer.exe'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'YeYu Gamer\YeYu Gamer.lnk'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "YeYu Gamer is not installed or its configuration is missing: $configPath"
}
if (-not (Test-Path -LiteralPath $desktopHost -PathType Leaf)) {
    throw 'The installed YeYu Gamer desktop host is missing. Reinstall the current package.'
}

if ($NoOpenWebGui) {
    # Manager-only recovery keeps its explicit no-browser contract. Normal
    # interactive startup below always goes through the installed shortcut.
    Start-Process `
        -FilePath $desktopHost `
        -ArgumentList @('--config', $configPath, '--no-browser') `
        -WorkingDirectory (Split-Path -Parent $desktopHost) `
        -WindowStyle Hidden
} else {
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw 'The YeYu Gamer Start Menu shortcut is missing. Reinstall the current package.'
    }
    $explorer = Join-Path ([Environment]::GetFolderPath('Windows')) 'explorer.exe'
    if (-not (Test-Path -LiteralPath $explorer -PathType Leaf)) {
        throw 'Windows Explorer is unavailable.'
    }
    # Explorer owns the desktop application just as it does after a real user
    # double-click. This prevents a short-lived installer or CLI job from
    # reclaiming the host after the launch command returns.
    Start-Process `
        -FilePath $explorer `
        -ArgumentList ('"' + $shortcutPath + '"') `
        -WindowStyle Hidden
}

$managerDeadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/v1/health' -TimeoutSec 2 -ErrorAction Stop
        if ($health.status -eq 'ok') {
            Write-Host 'YeYu Gamer installed desktop host is healthy.'
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 250
} while ([DateTimeOffset]::UtcNow -lt $managerDeadline)
throw 'YeYu Gamer desktop host did not become healthy before the startup timeout.'
