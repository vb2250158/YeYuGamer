[CmdletBinding()]
param(
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\fgo-candidate'),
    [string]$EvidencePath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\fgo-blocked-validation.json'),
    [string]$UpstreamEvidencePath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\fgo-upstream-contract-validation.json'),
    [string]$FgaRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\upstream-audit\FGA'),
    [string]$MaaFgoRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\upstream-audit\MaaFgo'),
    [string]$BbchannelRoot = 'C:\Game\BBchannel',
    [string]$PythonPath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Python\Python312\python.exe'),
    [string]$CandidateTestEvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = Split-Path -Parent $PSScriptRoot
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Python is unavailable: $PythonPath" }
$validator = Join-Path (Split-Path -Parent $PSScriptRoot) 'adapter-host\tests\validate_fgo_runner.py'
if (-not (Test-Path -LiteralPath $validator -PathType Leaf)) { throw 'FGO runner validator is missing.' }
$upstreamValidator = Join-Path (Split-Path -Parent $PSScriptRoot) 'adapter-host\tests\validate_fgo_upstream_contract.py'
if (-not (Test-Path -LiteralPath $upstreamValidator -PathType Leaf)) { throw 'FGO upstream contract validator is missing.' }
$candidate = [IO.Path]::GetFullPath($CandidateRoot)
$evidence = [IO.Path]::GetFullPath($EvidencePath)
$upstreamEvidence = [IO.Path]::GetFullPath($UpstreamEvidencePath)
$local = [IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not ($candidate + '\').StartsWith($local, [StringComparison]::OrdinalIgnoreCase) -or
    -not ($evidence.StartsWith($local, [StringComparison]::OrdinalIgnoreCase)) -or
    -not ($upstreamEvidence.StartsWith($local, [StringComparison]::OrdinalIgnoreCase))) {
    throw 'FGO candidate and validation evidence must remain below LocalAppData.'
}

& $PythonPath -X utf8 $validator --candidate-root $candidate --evidence $evidence
if ($LASTEXITCODE -ne 0) { throw "FGO blocked runner validation failed with exit code $LASTEXITCODE." }
& $PythonPath -X utf8 $upstreamValidator --fga-root $FgaRoot --maafgo-root $MaaFgoRoot --bbchannel-root $BbchannelRoot --evidence $upstreamEvidence
if ($LASTEXITCODE -ne 0) { throw "FGO upstream contract validation failed with exit code $LASTEXITCODE." }

$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) { throw 'FGO tests may update only an unpromoted candidate manifest.' }
$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $evidence -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $upstreamEvidence -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]$manifest.promotion.payloadDigest -cne (Get-YeYuGamerPayloadDigest -Files @($manifest.files))) { throw 'FGO candidate payload digest is invalid.' }
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))

$candidateEvidence = if ([string]::IsNullOrWhiteSpace($CandidateTestEvidencePath)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\fgo-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss') + '.json')
} else { [IO.Path]::GetFullPath($CandidateTestEvidencePath) }
if ($candidateEvidence.StartsWith('\\')) { throw 'FGO candidate evidence path must be local.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $candidateEvidence) -Force | Out-Null
$summary = [ordered]@{
    schemaVersion=1; resourceType='adapter-candidate-test-evidence'; status='passed'; passed=$true;
    packageId=[string]$manifest.packageId; packageVersion=[string]$manifest.packageVersion; buildId=[string]$manifest.buildId;
    supportedGameIds=@('FGO'); payloadDigest=[string]$manifest.promotion.payloadDigest;
    replaySuiteDigest=[string]$manifest.promotion.replaySuiteDigest; shadowSuiteDigest=[string]$manifest.promotion.shadowSuiteDigest;
    generatedAt=[DateTimeOffset]::UtcNow.ToString('o'); gameStarted=$false
}
[IO.File]::WriteAllText($candidateEvidence, ($summary | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{status='passed';evidencePath=$candidateEvidence;evidence=$summary} | ConvertTo-Json -Depth 5 -Compress
