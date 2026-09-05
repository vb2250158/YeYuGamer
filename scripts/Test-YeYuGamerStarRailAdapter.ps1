[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\starrail-candidate'),
    [string]$PythonPath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\YeYuGamer\.venv\Scripts\python.exe'),
    [string]$CSharpCompilerPath = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    [string]$EvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$localBase = [System.IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-tests')).TrimEnd('\')
if ($localBase.StartsWith('\\')) { throw 'Adapter test output must be local.' }
New-Item -ItemType Directory -Path $localBase -Force | Out-Null
$runRoot = [System.IO.Path]::GetFullPath((Join-Path $localBase ([Guid]::NewGuid().ToString('N'))))
if (-not ($runRoot + '\').StartsWith($localBase + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Test root escaped LocalAppData.' }
$fakeRoot = Join-Path $runRoot 'fake-march7th'
$fakeBuildRoot = Join-Path $runRoot 'build'
$staging = Join-Path $runRoot 'staging'
$reports = Join-Path $runRoot 'reports'
New-Item -ItemType Directory -Path $fakeRoot, $staging, $reports -Force | Out-Null

$fakeSource = Join-Path $SourceRoot 'adapter-host\tests\FakeMarch7thTool.cs'
$fakeLauncher = Join-Path $fakeRoot 'March7th Assistant.exe'
& $CSharpCompilerPath /nologo /target:exe /optimize+ ('/out:' + $fakeLauncher) $fakeSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $fakeLauncher -PathType Leaf)) { throw 'Fake March7th compilation failed.' }
Copy-Item -LiteralPath $fakeLauncher -Destination (Join-Path $fakeRoot 'March7th Launcher.exe')
$fakeGame = Join-Path $fakeRoot 'StarRail.exe'
$fakeGameSource = Join-Path $SourceRoot 'adapter-host\tests\FakeStarRailTool.cs'
& $CSharpCompilerPath /nologo /target:exe /optimize+ ('/out:' + $fakeGame) $fakeGameSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $fakeGame -PathType Leaf)) { throw 'Fake StarRail compilation failed.' }
$fakeConfig = @"
exit_after_failure: false
after_finish: None
game_path: $fakeGame
power_enable: false
power_plan: []
instance_type: 拟造花萼（金）
use_reserved_trailblaze_power: false
daily_enable: true
auto_set_game_path_enable: false
scheduled_run_enable: false
scheduled_tasks: []
daily_memory_one_enable: false
use_background_screenshot: true
update_via_launcher: true
"@
[System.IO.File]::WriteAllText((Join-Path $fakeRoot 'config.yaml'), $fakeConfig, [System.Text.UTF8Encoding]::new($false))

& (Join-Path $PSScriptRoot 'Build-YeYuGamerStarRailAdapter.ps1') -SourceRoot $SourceRoot -BuildRoot $fakeBuildRoot -ToolRoot $fakeRoot -TestBuild | Out-Null
$fakeCandidate = Join-Path $fakeBuildRoot 'starrail-candidate'
$replayReport = Join-Path $reports 'replay.json'
$env:PYTHONPATH = Join-Path $SourceRoot 'backend'
$env:YEYU_STARRAIL_TEST_FAKE = '1'
try {
    & $PythonPath (Join-Path $SourceRoot 'adapter-host\tests\validate_starrail_runner.py') `
        --runner (Join-Path $fakeCandidate 'runner.exe') `
        --fake-root $fakeRoot `
        --staging-parent $staging `
        --report $replayReport
} finally {
    Remove-Item Env:YEYU_STARRAIL_TEST_FAKE -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $replayReport -PathType Leaf)) { throw 'StarRail replay suite failed.' }

$candidate = [System.IO.Path]::GetFullPath($CandidateRoot)
if ($candidate.StartsWith('\\') -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'Real candidate root is unavailable or non-local.' }
$before = @(Get-Process -Name 'StarRail','March7th Launcher','March7th Assistant','March7th Updater' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$shadowOutput = & (Join-Path $candidate 'runner.exe') --probe-binding 2>&1
$shadowExit = $LASTEXITCODE
$after = @(Get-Process -Name 'StarRail','March7th Launcher','March7th Assistant','March7th Updater' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if ($shadowExit -ne 0) { throw "Real binding shadow probe failed: $($shadowOutput -join ' ')" }
$shadow = ($shadowOutput -join "`n") | ConvertFrom-Json
if (-not $shadow.ok -or $shadow.processStarted -or @($after).Count -ne @($before).Count) { throw 'Shadow probe started a process or returned an invalid result.' }
$shadowReport = Join-Path $reports 'shadow.json'
$shadowDocument = [ordered]@{
    schemaVersion = 1
    suite = 'starrail-binding-shadow'
    passed = $true
    processStarted = $false
    beforeProcessCount = @($before).Count
    afterProcessCount = @($after).Count
    probe = $shadow
}
[System.IO.File]::WriteAllText($shadowReport, ($shadowDocument | ConvertTo-Json -Depth 8 -Compress), [System.Text.UTF8Encoding]::new($false))

$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) { throw 'Tests may update only an unpromoted candidate manifest.' }
$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayReport -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowReport -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))

$evidence = if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\starrail-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss') + '.json')
} else { [System.IO.Path]::GetFullPath($EvidencePath) }
if ($evidence.StartsWith('\\')) { throw 'StarRail evidence path must be local.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $evidence) -Force | Out-Null
$summary = [ordered]@{
    schemaVersion = 1
    resourceType = 'adapter-candidate-test-evidence'
    status = 'passed'
    passed = $true
    packageId = [string]$manifest.packageId
    packageVersion = [string]$manifest.packageVersion
    buildId = [string]$manifest.buildId
    supportedGameIds = @('StarRail')
    payloadDigest = [string]$manifest.promotion.payloadDigest
    replaySuiteDigest = $manifest.promotion.replaySuiteDigest
    shadowSuiteDigest = $manifest.promotion.shadowSuiteDigest
    generatedAt = [DateTimeOffset]::UtcNow.ToString('o')
    gameStarted = $false
}
[System.IO.File]::WriteAllText($evidence, ($summary | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
[pscustomobject]@{ status='passed'; evidencePath=$evidence; evidence=$summary } | ConvertTo-Json -Depth 5 -Compress
