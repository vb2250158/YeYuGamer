[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$RuntimeRoot,
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
$python = Join-Path $installResolved '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'YeYu Gamer is not installed or its lifecycle configuration is missing.'
}

& $python -I -B -m yeyu_gamer_platform.cli --config $configPath health *> $null
if ($LASTEXITCODE -ne 0) {
    & (Join-Path $PSScriptRoot 'Start-YeYuGamer.ps1') -InstallRoot $installResolved -RuntimeRoot $runtimeResolved -NoOpenWebGui -TimeoutSeconds $TimeoutSeconds
    exit $LASTEXITCODE
}
& $python -I -B -m yeyu_gamer_platform.cli --config $configPath manager-restart
if ($LASTEXITCODE -ne 0) { throw 'YeYu Gamer rejected the safe restart request.' }

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    try {
        if ((Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/v1/health' -TimeoutSec 2).status -eq 'ok') {
            Write-Host 'YeYu Gamer single-process desktop host restarted successfully.'
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 250
} while ([DateTimeOffset]::UtcNow -lt $deadline)
throw 'YeYu Gamer did not become healthy after the requested restart.'
