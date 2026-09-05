[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\fgo-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidencePath = '',
    [switch]$PromoteForDailyBatchValidation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$source = Assert-YeYuGamerLocalTarget -Path $SourceRoot -Purpose 'FGO installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

function Assert-FgoCandidate([string]$Root) {
    $resolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose 'FGO Adapter candidate'
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'FGO Adapter candidate' | Out-Null
    $manifestPath = Join-Path $resolved 'install-manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
        [string]$manifest.entryPoint -cne 'runner.exe' -or [string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) {
        throw 'FGO package must be a schema-v2 candidate before promotion.'
    }
    if (@($manifest.supportedGameIds).Count -ne 1 -or [string]$manifest.supportedGameIds[0] -cne 'FGO') { throw 'FGO candidate scope is invalid.' }
    foreach ($operation in @('attach-home','three-10ap-quests')) {
        if ($null -eq $manifest.operationBindings.FGO.PSObject.Properties[$operation]) { throw "FGO candidate is missing $operation." }
    }
    if ($manifest.security.allowsArbitraryCommand -ne $false -or $manifest.security.allowsArbitraryPath -ne $false -or $manifest.security.allowsArbitraryInput -ne $false) { throw 'FGO candidate has an unsafe execution field.' }
    $declared = @{}
    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.path
        if (-not $relative -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains('\') -or $declared.ContainsKey($relative)) { throw "Invalid FGO file path: $relative" }
        $path = Assert-YeYuGamerChildPath -Parent $resolved -Child (Join-Path $resolved $relative) -Purpose 'FGO candidate file'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne [int64]$file.sizeBytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne ([string]$file.sha256).ToLowerInvariant()) { throw "FGO candidate file mismatch: $relative" }
        $declared[$relative] = $true
    }
    if (-not $declared.ContainsKey('runner.exe') -or -not $declared.ContainsKey('tool-binding.json')) { throw 'FGO candidate files are incomplete.' }
    if (@(Get-ChildItem -LiteralPath $resolved -Recurse -File).Count -ne $declared.Count + 1) { throw 'FGO candidate has undeclared files.' }
    return [pscustomobject]@{ Root=$resolved; Manifest=$manifest }
}

if ($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -match '^(?i:true|1|yes|on)$') { throw 'Refusing FGO install while the caller execution gate is enabled.' }
if ($PromoteForDailyBatchValidation) { throw 'FGO promotion is Manager-owned; this installer accepts candidates only.' }
if ([string]::IsNullOrWhiteSpace($CandidateTestEvidencePath)) { throw 'FGO candidate installation requires payload-bound test evidence.' }
$candidate = Assert-FgoCandidate $CandidateRoot
& (Join-Path $candidate.Root 'runner.exe') --probe-binding | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'FGO formal MaaFgo binding probe failed.' }
$runtime = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$testEvidence = Assert-YeYuGamerCandidateTestEvidence -EvidencePath $CandidateTestEvidencePath -Manifest $candidate.Manifest -ExpectedGameIds @('FGO')
$modules = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters\game-modules') -Purpose 'game module root'
New-Item -ItemType Directory -Path $modules -Force | Out-Null
$target = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules 'fgo') -Purpose 'FGO Adapter target'
if (@(Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(($target.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) { throw 'An FGO runner is active.' }
$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
try {
    $incoming = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('.fgo.incoming-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'incoming FGO Adapter'
    Copy-Item -LiteralPath $candidate.Root -Destination $incoming -Recurse
    $verified = Assert-FgoCandidate $incoming
    $verified.Manifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    [IO.File]::WriteAllText((Join-Path $incoming 'install-manifest.json'), ($verified.Manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $target) {
        $previous = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('fgo.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous FGO Adapter'
        Move-Item -LiteralPath $target -Destination $previous
    }
    Move-Item -LiteralPath $incoming -Destination $target; $incoming = $null
    Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
    Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName 'fgo' | Out-Null
    $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId 'FGO' -EvidencePath $testEvidence.Path
    [pscustomobject]@{ status='installed-unpromoted'; packageVersion=[string]$verified.Manifest.packageVersion; gameId='FGO'; executionReady=$false; promotionOwner='manager'; managerRestarted=$false; gameStarted=$false; candidateTestEvidencePath=$installedEvidence; candidateTestEvidenceSha256=$testEvidence.Sha256 } | ConvertTo-Json -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) { Remove-Item -LiteralPath (Assert-YeYuGamerChildPath -Parent $modules -Child $incoming -Purpose 'failed FGO incoming') -Recurse -Force }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
