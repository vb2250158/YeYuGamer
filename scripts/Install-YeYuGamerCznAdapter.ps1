[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\czn-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidencePath = '',
    [string]$CanaryEvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$source = Assert-YeYuGamerLocalTarget -Path $SourceRoot -Purpose 'CZN installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

function Assert-CznCandidate([string]$Root) {
    $resolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose 'CZN Adapter candidate'
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw 'The CZN Adapter candidate is missing.' }
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'CZN Adapter candidate' | Out-Null
    $manifestPath = Join-Path $resolved 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'The CZN candidate manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
        [string]$manifest.entryPoint -cne 'runner.exe' -or [string]$manifest.promotion.status -cne 'candidate' -or
        [bool]$manifest.executionReady -or @($manifest.supportedGameIds).Count -ne 1 -or [string]$manifest.supportedGameIds[0] -cne 'CZN') {
        throw 'CZN installer accepts only an unpromoted candidate.'
    }
    $expected = @('login-bonus','achievement-schedule','arkhianon-supply','simulation-stamina')
    if (@(Compare-Object $expected @($manifest.operationBindings.CZN.PSObject.Properties.Name)).Count -ne 0) { throw 'CZN candidate operation scope differs from the audited minimal set.' }
    foreach ($operationClass in @('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','story_progression','roguelike','save_deletion','shop','arbitrary_command','arbitrary_path','arbitrary_input')) {
        if ($operationClass -cnotin @($manifest.forbiddenOperationClasses)) { throw "CZN package is missing denied class: $operationClass" }
    }
    if ($manifest.security.allowsArbitraryCommand -ne $false -or $manifest.security.allowsArbitraryPath -ne $false -or $manifest.security.allowsArbitraryInput -ne $false) {
        throw 'CZN package exposes an arbitrary execution field.'
    }
    $declared = @{}
    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.path
        if (-not $relative -or [System.IO.Path]::IsPathRooted($relative) -or $relative.Contains('\') -or $declared.ContainsKey($relative)) { throw "CZN package has an invalid file path: $relative" }
        $path = Assert-YeYuGamerChildPath -Parent $resolved -Child (Join-Path $resolved $relative) -Purpose 'CZN candidate file'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "CZN candidate file is missing: $relative" }
        $item = Get-Item -LiteralPath $path -Force
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($item.Length -ne [int64]$file.sizeBytes -or $hash -cne ([string]$file.sha256).ToLowerInvariant()) { throw "CZN candidate file differs from its manifest: $relative" }
        $declared[$relative] = $true
    }
    if (-not $declared.ContainsKey('runner.exe') -or -not $declared.ContainsKey('tool-binding.json')) { throw 'CZN candidate files are incomplete.' }
    if (@(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force).Count -ne $declared.Count + 1) { throw 'CZN candidate contains undeclared files.' }
    return [pscustomobject]@{ Root=$resolved; Manifest=$manifest }
}

if ($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -match '^(?i:true|1|yes|on)$') { throw 'Refusing CZN installation while caller execution gate is enabled.' }
if (-not [string]::IsNullOrWhiteSpace($CanaryEvidencePath)) { throw 'CZN promotion is Manager-owned; the installer accepts only candidate test evidence.' }
if ([string]::IsNullOrWhiteSpace($CandidateTestEvidencePath)) { throw 'CZN candidate installation requires payload-bound test evidence.' }
$candidate = Assert-CznCandidate $CandidateRoot
$runtime = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$testEvidence = Assert-YeYuGamerCandidateTestEvidence -EvidencePath $CandidateTestEvidencePath -Manifest $candidate.Manifest -ExpectedGameIds @('CZN')
$adapters = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters') -Purpose 'Adapter runtime root'
$modules = Assert-YeYuGamerChildPath -Parent $adapters -Child (Join-Path $adapters 'game-modules') -Purpose 'game module root'
New-Item -ItemType Directory -Path $modules -Force | Out-Null
$target = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules 'czn') -Purpose 'CZN Adapter target'
$active = @(Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ([System.IO.Path]::GetFullPath($_.ExecutablePath)).StartsWith(($target.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)
})
if ($active.Count -gt 0) { throw 'A CZN Adapter runner is active; installation is refused.' }

$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
try {
    $incoming = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('.czn.incoming-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'incoming CZN Adapter'
    Copy-Item -LiteralPath $candidate.Root -Destination $incoming -Recurse
    $verified = Assert-CznCandidate $incoming
    $verified.Manifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    [System.IO.File]::WriteAllText((Join-Path $incoming 'install-manifest.json'), ($verified.Manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))
    Assert-CznCandidate $incoming | Out-Null
    if (Test-Path -LiteralPath $target) {
        $previous = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('czn.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous CZN Adapter'
        Move-Item -LiteralPath $target -Destination $previous
    }
    Move-Item -LiteralPath $incoming -Destination $target
    $incoming = $null
    Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
    Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName 'czn' | Out-Null
    $installed = Assert-CznCandidate $target
    $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId 'CZN' -EvidencePath $testEvidence.Path
    [pscustomobject]@{
        status='installed-unpromoted'; packageVersion=[string]$installed.Manifest.packageVersion; gameId='CZN';
        executionReady=$false; promotionOwner='manager'; gameStarted=$false; managerRestarted=$false;
        candidateTestEvidencePath=$installedEvidence; candidateTestEvidenceSha256=$testEvidence.Sha256
    } | ConvertTo-Json -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) { Remove-Item -LiteralPath (Assert-YeYuGamerChildPath -Parent $modules -Child $incoming -Purpose 'failed incoming CZN Adapter') -Recurse -Force }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
