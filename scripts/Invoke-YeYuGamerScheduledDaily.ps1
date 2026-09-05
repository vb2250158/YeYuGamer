[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$RuntimeRoot,
    [ValidateRange(30, 600)][int]$ManagerStartupTimeoutSeconds = 180,
    [switch]$DryRun,
    # Recognize retired switches only to reject old callers explicitly.
    [Parameter(DontShow = $true)][switch]$SkipOnStuckZombies,
    [Parameter(DontShow = $true)][switch]$RebootOnStuckZombies
)

# Scheduled-task action for the unattended daily round.
#
# It only does what a person would do from the WebGUI: make sure the installed
# YeYuGamer.exe is healthy, then ask the Manager for one daily batch through
# the typed CLI.  The Manager owns the queue, every Todo/evidence contract and
# the round e-mail. Process cleanup, memory observations and incident records
# also belong to the Manager; this script never controls game processes or
# restarts Windows.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSBoundParameters.ContainsKey('SkipOnStuckZombies') -or $PSBoundParameters.ContainsKey('RebootOnStuckZombies')) {
    throw 'SkipOnStuckZombies and RebootOnStuckZombies are no longer supported. Remove these switches: the Manager owns queue cleanup and residual-process gates; the scheduled entry never restarts Windows.'
}
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
if (-not $InstallRoot) { $InstallRoot = Get-YeYuGamerDefaultInstallRoot }
if (-not $RuntimeRoot) { $RuntimeRoot = Get-YeYuGamerDefaultRuntimeRoot }
$installResolved = Assert-YeYuGamerInstallRoot -Path $InstallRoot
$runtimeResolved = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$cli = Join-Path $installResolved 'YeYuGamer.cmd'
$configPath = Join-Path $runtimeResolved 'config\platform.json'
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
    throw "The installed YeYu Gamer CLI wrapper is missing: $cli"
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "YeYu Gamer is not installed or its configuration is missing: $configPath"
}

$logRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\logs\scheduled-daily'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$logPath = Join-Path $logRoot ((Get-Date).ToString('yyyy-MM-dd') + '.log')
function Write-DailyLog {
    param([string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss zzz} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-ManagerHealth {
    try {
        return Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/v1/health' -TimeoutSec 3 -ErrorAction Stop
    } catch {
        return $null
    }
}

function Get-BatchIdFromCliResult {
    param([string]$JsonText)

    try {
        $payload = $JsonText | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
    $raw = if ($payload.PSObject.Properties['raw']) { $payload.raw } else { $payload }
    if ($raw.PSObject.Properties['result'] -and $raw.result) {
        $result = $raw.result
        if ($result.PSObject.Properties['batchId']) { return [string]$result.batchId }
        if ($result.PSObject.Properties['batch'] -and $result.batch -and $result.batch.PSObject.Properties['batchId']) {
            return [string]$result.batch.batchId
        }
    }
    return $null
}

Write-DailyLog 'scheduled daily run: begin'

$health = Get-ManagerHealth
if ($null -eq $health -or $health.status -ne 'ok') {
    Write-DailyLog 'Manager is not healthy; starting the installed desktop host without a browser.'
    & (Join-Path $PSScriptRoot 'Start-YeYuGamer.ps1') -InstallRoot $installResolved -RuntimeRoot $runtimeResolved -NoOpenWebGui -TimeoutSeconds $ManagerStartupTimeoutSeconds
    $health = Get-ManagerHealth
    if ($null -eq $health -or $health.status -ne 'ok') {
        Write-DailyLog 'Manager did not become healthy; the daily batch was not requested.'
        exit 2
    }
}
Write-DailyLog ('Manager healthy: manager={0} storage={1}' -f $health.manager, $health.storage)

# Never stack a second queue on a running one; the Manager would reject it,
# but a clear log line is more useful than a receipt error.
$snapshotJson = & $cli --json snapshot 2>&1 | Out-String
try {
    # Windows PowerShell 5.1 (the scheduled host) has no -Depth on ConvertFrom-Json.
    $snapshot = $snapshotJson | ConvertFrom-Json
} catch {
    Write-DailyLog ('snapshot could not be parsed: ' + $_.Exception.Message)
    exit 3
}
$activeBatch = if ($snapshot.PSObject.Properties['activeBatch']) { $snapshot.activeBatch } else { $null }
if ($null -ne $activeBatch) {
    $activeId = if ($activeBatch.PSObject.Properties['batchId']) { $activeBatch.batchId } else { '<unknown>' }
    Write-DailyLog ('an active batch already exists ({0}); not requesting another daily run.' -f $activeId)
    exit 0
}

$idempotencyKey = 'scheduled-daily-' + (Get-Date).ToString('yyyyMMdd-HHmm')
if ($DryRun) {
    Write-DailyLog ('dry run: Manager healthy and no active batch; would request start-daily with key {0}.' -f $idempotencyKey)
    exit 0
}
$result = & $cli --json start-daily --idempotency-key $idempotencyKey 2>&1 | Out-String
$exitCode = $LASTEXITCODE
Write-DailyLog ('start-daily exit={0} result={1}' -f $exitCode, $result.Trim())
if ($exitCode -eq 0) {
    $batchId = Get-BatchIdFromCliResult -JsonText $result
    if ($batchId) {
        Write-DailyLog ('scheduled daily run requested: batchId={0}' -f $batchId)
    }
}
exit $exitCode
