[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\local-daily-candidate'),
    [string]$PythonPath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\YeYuGamer\.venv\Scripts\python.exe'),
    [string]$EvidenceDirectory = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PYTHONDONTWRITEBYTECODE = '1'
$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$candidate = [IO.Path]::GetFullPath($CandidateRoot)
if ($candidate.StartsWith('\\') -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'OpenKuro candidate must be an existing local directory.' }
$runner = Join-Path $candidate 'runner.exe'
$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
$expected = [ordered]@{
    WW=@('attach-world','inspect-daily-progress','farm-nightmare-daily-echo','spend-waveplates','claim-daily-reward','claim-mail','claim-battle-pass')
    Endfield=@('attach-world','mail','spend-sanity','delivery-commission','collect-credit','dijiang-harvest','claim-daily-reward')
    GF2=@('attach-home','mail','public-area-dispatch','spend-stamina','squad-tasks','claim-daily-missions','battle-pass-free-track')
}
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady -or
    ((@($manifest.supportedGameIds | Sort-Object) -join ',') -cne (@($expected.Keys | Sort-Object) -join ','))) {
    throw 'OpenKuro tests may update only the exact-scope unpromoted candidate.'
}
foreach ($gameId in $expected.Keys) {
    if ((@($manifest.operationBindings.$gameId.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($expected[$gameId] | Sort-Object) -join ',')) {
        throw "$gameId candidate operation contract is invalid."
    }
}
if ([string]$manifest.promotion.payloadDigest -cne (Get-YeYuGamerPayloadDigest -Files @($manifest.files))) { throw 'OpenKuro candidate payload digest is invalid.' }

$evidenceRoot = if ([string]::IsNullOrWhiteSpace($EvidenceDirectory)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\openkuro-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))
} else { [IO.Path]::GetFullPath($EvidenceDirectory) }
if ($evidenceRoot.StartsWith('\\')) { throw 'OpenKuro evidence directory must be local.' }
New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null

$validator = Join-Path $source 'adapter-host\tests\validate_openkuro_selected_daily.py'
$staticOutput = & $PythonPath -X utf8 $validator 2>&1
if ($LASTEXITCODE -ne 0) { throw "OpenKuro static selected-Todo suite failed: $($staticOutput -join ' ')" }
$patchReplay = Join-Path $evidenceRoot 'ReplayWwConditionalPatch.exe'
$patchReplaySource = Join-Path $source 'adapter-host\tests\ReplayWwConditionalPatch.cs'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:exe /out:$patchReplay $patchReplaySource
if ($LASTEXITCODE -ne 0) { throw 'WW conditional-stage patch replay compilation failed.' }
$wwBinding = Get-Content -LiteralPath (Join-Path $candidate 'tool-binding.json') -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
$wwDailySource = Join-Path ([string]$wwBinding.tools.WW.root) 'data\apps\ok-ww\working\src\task\DailyTask.py'
$patchedDaily = Join-Path $evidenceRoot 'WW-DailyTask-conditional-replay.py'
& $patchReplay $runner $wwDailySource $patchedDaily
if ($LASTEXITCODE -ne 0) { throw 'WW conditional-stage patch replay failed.' }
$conditionalOutput = & $PythonPath -X utf8 (Join-Path $source 'adapter-host\tests\validate_ww_conditional_stages.py') $patchedDaily 2>&1
if ($LASTEXITCODE -ne 0) { throw "WW conditional-stage behavior suite failed: $($conditionalOutput -join ' ')" }
$replayReport = Join-Path $evidenceRoot 'selected-todo-contract.json'
[IO.File]::WriteAllText($replayReport, ([ordered]@{
    schemaVersion=1; suite='openkuro-selected-todo-contract'; passed=$true; games=@($expected.Keys);
    operationCount=($expected.Values | ForEach-Object { @($_).Count } | Measure-Object -Sum).Sum;
    validatorOutput=($staticOutput -join "`n"); conditionalStageOutput=($conditionalOutput -join "`n");
    conditionalReplaySha256=(Get-FileHash -LiteralPath $patchedDaily -Algorithm SHA256).Hash.ToLowerInvariant(); gameStarted=$false
} | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))

$before = @(Get-Process -Name 'Wuthering Waves','Endfield','GF2','ok-ww','ok-ef','ok-gf2' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$probeOutput = & $runner --probe-binding
if ($LASTEXITCODE -ne 0) { throw 'OpenKuro fixed binding shadow probe failed.' }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
$after = @(Get-Process -Name 'Wuthering Waves','Endfield','GF2','ok-ww','ok-ef','ok-gf2' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if (-not $probe.ok -or $probe.processStarted -or @($before).Count -ne @($after).Count) { throw 'OpenKuro shadow probe started a process or returned an invalid result.' }
$shadowReport = Join-Path $evidenceRoot 'binding-shadow.json'
[IO.File]::WriteAllText($shadowReport, ([ordered]@{
    schemaVersion=1; suite='openkuro-binding-shadow'; passed=$true; processStarted=$false;
    beforeProcessCount=@($before).Count; afterProcessCount=@($after).Count; probe=$probe
} | ConvertTo-Json -Depth 8 -Compress), [Text.UTF8Encoding]::new($false))

$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayReport -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowReport -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
foreach ($gameId in $expected.Keys) {
    $summary = [ordered]@{
        schemaVersion=1; resourceType='adapter-candidate-test-evidence'; status='passed'; passed=$true;
        packageId=[string]$manifest.packageId; packageVersion=[string]$manifest.packageVersion; buildId=[string]$manifest.buildId;
        supportedGameIds=@($gameId); payloadDigest=[string]$manifest.promotion.payloadDigest;
        replaySuiteDigest=[string]$manifest.promotion.replaySuiteDigest; shadowSuiteDigest=[string]$manifest.promotion.shadowSuiteDigest;
        generatedAt=[DateTimeOffset]::UtcNow.ToString('o'); gameStarted=$false
    }
    [IO.File]::WriteAllText((Join-Path $evidenceRoot ($gameId.ToLowerInvariant() + '.json')), ($summary | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}
[pscustomobject]@{status='passed';evidenceDirectory=$evidenceRoot;gameIds=@($expected.Keys);gameStarted=$false} | ConvertTo-Json -Depth 4 -Compress
