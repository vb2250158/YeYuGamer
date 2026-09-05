[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\nte-candidate'),
    [string]$PythonPath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\YeYuGamer\.venv\Scripts\python.exe'),
    [string]$EvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PYTHONDONTWRITEBYTECODE = '1'

$localBase = [System.IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-tests')).TrimEnd('\')
if ($localBase.StartsWith('\')) { throw 'Adapter test output must be local.' }
New-Item -ItemType Directory -Path $localBase -Force | Out-Null
$runRoot = [System.IO.Path]::GetFullPath((Join-Path $localBase ([Guid]::NewGuid().ToString('N'))))
if (-not ($runRoot + '\').StartsWith($localBase + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'NTE test root escaped LocalAppData.' }
$reports = Join-Path $runRoot 'reports'
New-Item -ItemType Directory -Path $reports -Force | Out-Null

$candidate = [System.IO.Path]::GetFullPath($CandidateRoot)
if ($candidate.StartsWith('\') -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'Real NTE candidate is unavailable or non-local.' }
$bridge = Join-Path $candidate 'NteYeYuBridge.py'
$replayReport = Join-Path $reports 'replay.json'
& $PythonPath (Join-Path $SourceRoot 'adapter-host\tests\validate_nte_formal_bridge.py') --bridge $bridge --report $replayReport
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $replayReport -PathType Leaf)) { throw 'NTE run-scoped bridge suite failed.' }

$before = @(Get-Process -Name 'NTEGame','NTE','ok-nte' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$probeOutput = & (Join-Path $candidate 'runner.exe') --probe-binding 2>&1
$probeExit = $LASTEXITCODE
$after = @(Get-Process -Name 'NTEGame','NTE','ok-nte' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if ($probeExit -ne 0) { throw "NTE real-binding shadow probe failed: $($probeOutput -join ' ')" }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json
if (-not $probe.ok -or $probe.processStarted -or @($after).Count -ne @($before).Count) { throw 'NTE shadow probe started a process or returned an invalid result.' }
$shadowReport = Join-Path $reports 'shadow.json'
$shadow = [ordered]@{
    schemaVersion = 1
    suite = 'nte-formal-binding-shadow'
    passed = $true
    processStarted = $false
    beforeProcessCount = @($before).Count
    afterProcessCount = @($after).Count
    dailyTaskSha256 = [string]$probe.dailyTaskSha256
    guiEntrySha256 = [string]$probe.guiEntrySha256
}
[System.IO.File]::WriteAllText($shadowReport, ($shadow | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))

$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) { throw 'NTE tests may update only an unpromoted candidate manifest.' }
$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayReport -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowReport -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))

$evidence = if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\nte-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss') + '.json')
} else { [System.IO.Path]::GetFullPath($EvidencePath) }
if ($evidence.StartsWith('\\')) { throw 'NTE evidence path must be local.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $evidence) -Force | Out-Null
$summary = [ordered]@{
    schemaVersion = 1
    resourceType = 'adapter-candidate-test-evidence'
    status = 'passed'
    passed = $true
    packageId = [string]$manifest.packageId
    packageVersion = [string]$manifest.packageVersion
    buildId = [string]$manifest.buildId
    supportedGameIds = @('NTE')
    payloadDigest = [string]$manifest.promotion.payloadDigest
    replaySuiteDigest = [string]$manifest.promotion.replaySuiteDigest
    shadowSuiteDigest = [string]$manifest.promotion.shadowSuiteDigest
    generatedAt = [DateTimeOffset]::UtcNow.ToString('o')
    gameStarted = $false
}
[System.IO.File]::WriteAllText($evidence, ($summary | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
[pscustomobject]@{ status='passed'; evidencePath=$evidence; evidence=$summary } | ConvertTo-Json -Depth 5 -Compress
