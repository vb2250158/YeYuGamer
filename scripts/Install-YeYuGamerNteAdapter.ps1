[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\nte-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidencePath = '',
    [string]$CanaryEvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$source = Assert-YeYuGamerLocalTarget -Path $SourceRoot -Purpose 'NTE installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

function Assert-NteCandidate {
    param([Parameter(Mandatory)][string]$Root)
    $resolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose 'NTE Adapter candidate'
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw 'The NTE Adapter candidate is missing.' }
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'NTE Adapter candidate' | Out-Null
    $manifestPath = Join-Path $resolved 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'The NTE candidate manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
        [string]$manifest.entryPoint -cne 'runner.exe' -or [string]$manifest.promotion.status -cne 'candidate' -or
        [bool]$manifest.executionReady -or @($manifest.supportedGameIds).Count -ne 1 -or
        [string]$manifest.supportedGameIds[0] -cne 'NTE') { throw 'NTE installer accepts only an unpromoted candidate.' }
    foreach ($operation in @('attach-home','mail','daily-activity','spend-city-vitality','claim-activity-reward','claim-cycle-reward')) {
        if ($null -eq $manifest.operationBindings.NTE.PSObject.Properties[$operation]) { throw "NTE operation binding is missing: $operation" }
    }
    if ($null -ne $manifest.operationBindings.NTE.PSObject.Properties['cafe']) { throw 'The unsafe composite cafe route must not be present.' }
    foreach ($operationClass in @('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','arbitrary_command','arbitrary_path','arbitrary_input')) {
        if ($operationClass -cnotin @($manifest.forbiddenOperationClasses)) { throw "NTE package is missing denied class: $operationClass" }
    }
    if ($manifest.security.allowsArbitraryCommand -ne $false -or $manifest.security.allowsArbitraryPath -ne $false -or $manifest.security.allowsArbitraryInput -ne $false) {
        throw 'NTE package exposes an arbitrary execution field.'
    }
    $declared = @{}
    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.path
        if (-not $relative -or [System.IO.Path]::IsPathRooted($relative) -or $relative.Contains('\') -or $declared.ContainsKey($relative)) {
            throw "NTE package has an invalid file path: $relative"
        }
        $path = Assert-YeYuGamerChildPath -Parent $resolved -Child (Join-Path $resolved $relative.Replace('/','\')) -Purpose 'NTE candidate file'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "NTE candidate file is missing: $relative" }
        $item = Get-Item -LiteralPath $path -Force
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($item.Length -ne [int64]$file.sizeBytes -or $hash -cne ([string]$file.sha256).ToLowerInvariant()) {
            throw "NTE candidate file differs from its manifest: $relative"
        }
        $declared[$relative] = $true
    }
    if (-not $declared.ContainsKey('runner.exe') -or -not $declared.ContainsKey('tool-binding.json')) { throw 'NTE candidate files are incomplete.' }
    $actual = @(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force)
    if ($actual.Count -ne $declared.Count + 1) { throw 'NTE candidate contains undeclared files.' }
    return [pscustomobject]@{ Root = $resolved; Manifest = $manifest }
}

if ($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -match '^(?i:true|1|yes|on)$') { throw 'Refusing NTE installation while caller execution gate is enabled.' }
if (-not [string]::IsNullOrWhiteSpace($CanaryEvidencePath)) { throw 'NTE promotion is Manager-owned; the installer accepts only candidate test evidence.' }
if ([string]::IsNullOrWhiteSpace($CandidateTestEvidencePath)) { throw 'NTE candidate installation requires payload-bound test evidence.' }
$candidate = Assert-NteCandidate -Root $CandidateRoot
$runtime = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$testEvidence = Assert-YeYuGamerCandidateTestEvidence -EvidencePath $CandidateTestEvidencePath -Manifest $candidate.Manifest -ExpectedGameIds @('NTE')
$adapters = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters') -Purpose 'Adapter runtime root'
$modules = Assert-YeYuGamerChildPath -Parent $adapters -Child (Join-Path $adapters 'game-modules') -Purpose 'game module root'
New-Item -ItemType Directory -Path $modules -Force | Out-Null
$target = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules 'nte') -Purpose 'NTE Adapter target'
$active = @(Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ([System.IO.Path]::GetFullPath($_.ExecutablePath)).StartsWith(($target.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)
})
if ($active.Count -gt 0) { throw 'An NTE Adapter runner is active; installation is refused.' }

$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
try {
    $incoming = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('.nte.incoming-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'incoming NTE Adapter'
    Copy-Item -LiteralPath $candidate.Root -Destination $incoming -Recurse
    $verified = Assert-NteCandidate -Root $incoming
    $verified.Manifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    [System.IO.File]::WriteAllText((Join-Path $incoming 'install-manifest.json'), ($verified.Manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))
    Assert-NteCandidate -Root $incoming | Out-Null
    if (Test-Path -LiteralPath $target) {
        $previous = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('nte.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous NTE Adapter'
        Move-Item -LiteralPath $target -Destination $previous
    }
    Move-Item -LiteralPath $incoming -Destination $target
    $incoming = $null
    Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
    Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName 'nte' | Out-Null
    $installed = Assert-NteCandidate -Root $target
    $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId 'NTE' -EvidencePath $testEvidence.Path
    [pscustomobject]@{
        status = 'installed-unpromoted'
        packageVersion = [string]$installed.Manifest.packageVersion
        gameId = 'NTE'
        executionReady = $false
        promotionOwner = 'manager'
        gameStarted = $false
        managerRestarted = $false
        candidateTestEvidencePath = $installedEvidence
        candidateTestEvidenceSha256 = $testEvidence.Sha256
    } | ConvertTo-Json -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) {
        $safeIncoming = Assert-YeYuGamerChildPath -Parent $modules -Child $incoming -Purpose 'failed incoming NTE Adapter'
        Remove-Item -LiteralPath $safeIncoming -Recurse -Force
    }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
