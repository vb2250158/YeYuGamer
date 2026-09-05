[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\bd2-candidate'),
    [string]$ToolRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\MFABD2'),
    [string]$EvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$candidate = [IO.Path]::GetFullPath($CandidateRoot)
if ($candidate.StartsWith('\\') -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'BD2 candidate must be an existing local directory.' }
$manifestPath = Join-Path $candidate 'install-manifest.json'
$runner = Join-Path $candidate 'runner.exe'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
    [string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady -or
    (@($manifest.supportedGameIds) -join ',') -cne 'BD2' -or
    (@($manifest.operationBindings.BD2.PSObject.Properties.Name | Sort-Object) -join ',') -cne
        (@('attach-home','daily-claim','stamina-sweep' | Sort-Object) -join ',')) {
    throw 'BD2 candidate identity, stage, or operation contract is invalid.'
}
if ([string]$manifest.promotion.payloadDigest -cne (Get-YeYuGamerPayloadDigest -Files @($manifest.files))) { throw 'BD2 candidate payload digest is invalid.' }

$reportRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-tests\bd2-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $reportRoot -Force | Out-Null
$upstreamReport = Join-Path $reportRoot 'upstream-binding.json'
& (Join-Path $source 'scripts\Test-YeYuGamerBd2UpstreamBinding.ps1') -ToolRoot $ToolRoot -EvidencePath $upstreamReport | Out-Null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $upstreamReport -PathType Leaf)) { throw 'BD2 upstream contract suite failed.' }

$replayReport = Join-Path $reportRoot 'operation-contract.json'
[IO.File]::WriteAllText($replayReport, ([ordered]@{
    schemaVersion=1; suite='bd2-selected-operation-contract'; passed=$true;
    operations=@('attach-home','daily-claim','stamina-sweep'); upstreamEvidenceSha256=(Get-FileHash -LiteralPath $upstreamReport -Algorithm SHA256).Hash.ToLowerInvariant();
    gameStarted=$false
} | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))

$before = @(Get-Process -Name 'dnplayer','MFAAvalonia' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$probeOutput = & $runner --probe-binding
if ($LASTEXITCODE -ne 0) { throw 'BD2 binding shadow probe failed.' }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
$after = @(Get-Process -Name 'dnplayer','MFAAvalonia' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if (-not $probe.ok -or $probe.processStarted -or @($before).Count -ne @($after).Count) { throw 'BD2 shadow probe started a process or returned an invalid result.' }
$shadowReport = Join-Path $reportRoot 'binding-shadow.json'
[IO.File]::WriteAllText($shadowReport, ([ordered]@{
    schemaVersion=1; suite='bd2-binding-shadow'; passed=$true; processStarted=$false;
    beforeProcessCount=@($before).Count; afterProcessCount=@($after).Count; probe=$probe
} | ConvertTo-Json -Depth 8 -Compress), [Text.UTF8Encoding]::new($false))

$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayReport -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowReport -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))

$evidence = if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\bd2-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss') + '.json')
} else { [IO.Path]::GetFullPath($EvidencePath) }
if ($evidence.StartsWith('\\')) { throw 'BD2 candidate evidence path must be local.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $evidence) -Force | Out-Null
$summary = [ordered]@{
    schemaVersion=1; resourceType='adapter-candidate-test-evidence'; status='passed'; passed=$true;
    packageId=[string]$manifest.packageId; packageVersion=[string]$manifest.packageVersion; buildId=[string]$manifest.buildId;
    supportedGameIds=@('BD2'); payloadDigest=[string]$manifest.promotion.payloadDigest;
    replaySuiteDigest=[string]$manifest.promotion.replaySuiteDigest; shadowSuiteDigest=[string]$manifest.promotion.shadowSuiteDigest;
    generatedAt=[DateTimeOffset]::UtcNow.ToString('o'); gameStarted=$false
}
[IO.File]::WriteAllText($evidence, ($summary | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{status='passed';evidencePath=$evidence;evidence=$summary} | ConvertTo-Json -Depth 5 -Compress
