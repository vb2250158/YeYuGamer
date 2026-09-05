[CmdletBinding()]
param(
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\starrail-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidencePath = '',
    [string]$CanaryEvidencePath = '',
    [switch]$PromoteForDailyBatchValidation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$source = Assert-YeYuGamerLocalTarget -Path (Split-Path -Parent $PSScriptRoot) -Purpose 'StarRail installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

function Assert-CandidatePackage {
    param([Parameter(Mandatory)][string]$Root)
    $resolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose 'StarRail Adapter candidate'
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw 'The StarRail Adapter candidate is missing.' }
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'StarRail Adapter candidate' | Out-Null
    $manifestPath = Join-Path $resolved 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'The candidate manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    foreach ($property in @('schemaVersion','packageId','packageVersion','entryPoint','files','supportedGameIds','operationBindings','forbiddenOperationClasses','limits','artifactPolicy','security','promotion','executionReady')) {
        if ($null -eq $manifest.PSObject.Properties[$property]) { throw "Candidate manifest is missing $property." }
    }
    if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
        [string]$manifest.entryPoint -cne 'runner.exe' -or [string]$manifest.promotion.status -cne 'candidate' -or
        [bool]$manifest.executionReady) {
        throw 'The StarRail installer accepts only an unpromoted schema-v2 candidate.'
    }
    if (@($manifest.supportedGameIds).Count -ne 1 -or [string]$manifest.supportedGameIds[0] -cne 'StarRail') {
        throw 'The candidate game scope must be exactly StarRail.'
    }
    $requiredForbidden = @('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','arbitrary_command','arbitrary_path','arbitrary_input')
    foreach ($operationClass in $requiredForbidden) {
        if ($operationClass -cnotin @($manifest.forbiddenOperationClasses)) { throw "Candidate is missing hard-denied operation class: $operationClass" }
    }
    $bindings = $manifest.operationBindings.StarRail
    $requiredOperations = @('attach-home','spend-trailblaze-power','daily-training-objectives','claim-daily-training-rewards','verify-daily-task-list')
    if ((@($bindings.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($requiredOperations | Sort-Object) -join ',')) {
        throw 'StarRail candidate operation scope differs from the audited contract.'
    }
    if ([bool]$bindings.'spend-trailblaze-power'.supportsResume) { throw 'The non-idempotent Trailblaze Power Todo must not support replay.' }
    if ($manifest.security.allowsArbitraryCommand -ne $false -or $manifest.security.allowsArbitraryPath -ne $false -or $manifest.security.allowsArbitraryInput -ne $false) {
        throw 'Candidate arbitrary execution fields must all be false.'
    }
    $declared = [System.Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.path
        if (-not $relative -or [System.IO.Path]::IsPathRooted($relative) -or $relative.Contains('\') -or
            @($relative.Split('/') | Where-Object { -not $_ -or $_ -in @('.','..') }).Count -gt 0 -or $declared.ContainsKey($relative)) {
            throw "Candidate contains an invalid or duplicate file path: $relative"
        }
        $path = Assert-YeYuGamerChildPath -Parent $resolved -Child (Join-Path $resolved $relative.Replace('/','\')) -Purpose 'candidate file'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Candidate file is missing: $relative" }
        $item = Get-Item -LiteralPath $path -Force
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($item.Length -ne [int64]$file.sizeBytes -or $hash -cne ([string]$file.sha256).ToLowerInvariant()) {
            throw "Candidate file differs from its manifest: $relative"
        }
        $declared.Add($relative, $file)
    }
    if (-not $declared.ContainsKey('runner.exe') -or -not $declared.ContainsKey('tool-binding.json')) { throw 'Candidate must declare runner.exe and tool-binding.json.' }
    $actual = @(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force)
    if ($actual.Count -ne $declared.Count + 1) { throw 'Candidate file count differs from its manifest.' }
    foreach ($file in $actual) {
        $relative = $file.FullName.Substring($resolved.Length + 1).Replace('\','/')
        if ($relative -cne 'install-manifest.json' -and -not $declared.ContainsKey($relative)) { throw "Candidate has an undeclared file: $relative" }
    }
    return [pscustomobject]@{ Root = $resolved; Manifest = $manifest }
}

if ($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -match '^(?i:true|1|yes|on)$') {
    throw 'Refusing candidate installation while the caller execution gate is enabled.'
}
if ($PromoteForDailyBatchValidation -or -not [string]::IsNullOrWhiteSpace($CanaryEvidencePath)) {
    throw 'StarRail promotion is Manager-owned; the installer accepts only candidate test evidence.'
}
if ([string]::IsNullOrWhiteSpace($CandidateTestEvidencePath)) { throw 'StarRail candidate installation requires payload-bound test evidence.' }
$candidate = Assert-CandidatePackage -Root $CandidateRoot
$runtime = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$testEvidence = Assert-YeYuGamerCandidateTestEvidence -EvidencePath $CandidateTestEvidencePath -Manifest $candidate.Manifest -ExpectedGameIds @('StarRail')
$adapters = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters') -Purpose 'runtime Adapter root'
New-Item -ItemType Directory -Path $adapters -Force | Out-Null
Assert-YeYuGamerNoReparseTree -Path $adapters -Purpose 'runtime Adapter root' | Out-Null
$modules = Assert-YeYuGamerChildPath -Parent $adapters -Child (Join-Path $adapters 'game-modules') -Purpose 'game module root'
New-Item -ItemType Directory -Path $modules -Force | Out-Null
Assert-YeYuGamerNoReparseTree -Path $modules -Purpose 'game module root' | Out-Null
$target = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules 'starrail') -Purpose 'StarRail Adapter target'
$running = @(Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ([System.IO.Path]::GetFullPath($_.ExecutablePath)).StartsWith(($target.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)
})
if ($running.Count -gt 0) { throw 'A YeYu Gamer execution runner is active; candidate installation is refused.' }

$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
try {
    $incoming = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('.starrail.incoming-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'incoming StarRail Adapter'
    Copy-Item -LiteralPath $candidate.Root -Destination $incoming -Recurse
    Assert-YeYuGamerNoReparseTree -Path $incoming -Purpose 'incoming StarRail Adapter' | Out-Null
    $incomingVerified = Assert-CandidatePackage -Root $incoming
    $incomingManifestPath = Join-Path $incoming 'install-manifest.json'
    $incomingVerified.Manifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    [System.IO.File]::WriteAllText($incomingManifestPath, ($incomingVerified.Manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))
    Assert-CandidatePackage -Root $incoming | Out-Null
    if (Test-Path -LiteralPath $target) {
        $previous = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('starrail.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous StarRail Adapter'
        Move-Item -LiteralPath $target -Destination $previous
    }
    Move-Item -LiteralPath $incoming -Destination $target
    $incoming = $null
    Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
    Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName 'starrail' | Out-Null
    $installed = Assert-CandidatePackage -Root $target
    $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId 'StarRail' -EvidencePath $testEvidence.Path
    [pscustomobject]@{
        status = 'installed-unpromoted'
        packageVersion = [string]$installed.Manifest.packageVersion
        gameId = 'StarRail'
        executionReady = $false
        promotionOwner = 'manager'
        executionGateChanged = $false
        managerRestarted = $false
        gameStarted = $false
        candidateTestEvidencePath = $installedEvidence
        candidateTestEvidenceSha256 = $testEvidence.Sha256
    } | ConvertTo-Json -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) {
        $verifiedIncoming = Assert-YeYuGamerChildPath -Parent $modules -Child $incoming -Purpose 'failed incoming StarRail Adapter'
        Remove-Item -LiteralPath $verifiedIncoming -Recurse -Force
    }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
