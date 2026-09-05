[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\classic-selected-daily-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidenceDirectory = '',
    [switch]$PromoteForDailyBatchValidation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

function Resolve-Local([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\')) { throw "$Purpose must be local." }
    return $full
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Verify-Candidate([string]$Root) {
    $root = Resolve-Local $Root 'classic selected-daily candidate'
    $manifestPath = Join-Path $root 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Classic selected-daily candidate manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop
    $expectedGames = @('PGR','ZZZ','NIKKE')
    if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or [string]$manifest.entryPoint -cne 'runner.exe' -or
        ((@($manifest.supportedGameIds | Sort-Object) -join ',') -cne (@($expectedGames | Sort-Object) -join ','))) {
        throw 'Classic selected-daily candidate identity or scope is invalid.'
    }
    $expected = [ordered]@{
        PGR=@('attach-home','claim-serum','dorm','simulation-field','maintainer-action','claim-daily-tasks','battle-pass-free-track')
        ZZZ=@('attach-home','coffee','scratch-card','trigrams-collection','suibian-temple','random-play','charge-plan','city-fund-free-claim','engagement-reward')
        NIKKE=@('attach-lobby','outpost','dispatch-friend')
    }
    foreach ($gameId in $expected.Keys) {
        $actual = @($manifest.operationBindings.$gameId.psobject.Properties.Name | Sort-Object)
        $wanted = @($expected[$gameId] | Sort-Object)
        if (($actual -join ',') -cne ($wanted -join ',')) { throw "$gameId operation allowlist is not exact." }
    }
    foreach ($file in @($manifest.files)) {
        $path = Join-Path $root ([string]$file.path)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Hash $path) -cne [string]$file.sha256) { throw "Classic candidate integrity mismatch: $($file.path)" }
    }
    $binding = Get-Content -LiteralPath (Join-Path $root 'tool-binding.json') -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop
    foreach ($gameId in $expected.Keys) {
        $entry = $binding.bindings.$gameId
        if ($null -eq $entry) { throw "$gameId trusted binding is missing." }
        foreach ($path in @([string]$entry.toolRoot,[string]$entry.gamePath,[string]$entry.python)) {
            $full = Resolve-Local $path "$gameId binding"
            if (-not (Test-Path -LiteralPath $full)) { throw "$gameId bound path is missing: $full" }
        }
        foreach ($verified in @($entry.verifiedFiles)) {
            $path = Resolve-Local ([string]$verified.path) "$gameId verified file"
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Hash $path) -cne [string]$verified.sha256) { throw "$gameId upstream tool changed after candidate build: $path" }
        }
    }
    return @{Root=$root;Manifest=$manifest}
}

$source = Assert-YeYuGamerLocalTarget -Path $SourceRoot -Purpose 'Classic installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
if ($PromoteForDailyBatchValidation) { throw 'Classic Adapter promotion is Manager-owned; this installer accepts candidates only.' }
$candidate = Verify-Candidate $CandidateRoot
if ([string]$candidate.Manifest.promotion.status -cne 'candidate' -or [bool]$candidate.Manifest.executionReady -or
    (Get-YeYuGamerPayloadDigest -Files @($candidate.Manifest.files)) -cne [string]$candidate.Manifest.promotion.payloadDigest) {
    throw 'Classic package must remain an integrity-bound unpromoted candidate.'
}
& (Join-Path $candidate.Root 'runner.exe') --probe-binding | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Classic candidate trusted-binding probe failed.' }
$runCandidateTest = [string]::IsNullOrWhiteSpace($CandidateTestEvidenceDirectory)
$evidenceRoot = if ($runCandidateTest) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\classic-install-' + [guid]::NewGuid().ToString('N'))
} else { Assert-YeYuGamerLocalTarget -Path $CandidateTestEvidenceDirectory -Purpose 'Classic candidate test evidence directory' }
if ($runCandidateTest) {
    & (Join-Path $source 'scripts\Test-YeYuGamerClassicAdapter.ps1') -SourceRoot $source -CandidateRoot $candidate.Root -EvidenceDirectory $evidenceRoot | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Classic candidate replay suite failed.' }
}
$candidate = Verify-Candidate $candidate.Root

$runtime = Resolve-Local $RuntimeRoot 'runtime root'
$adapters = Join-Path $runtime 'adapters'
if (-not (Test-Path -LiteralPath $adapters -PathType Container)) { throw 'YeYu Gamer adapter runtime is missing.' }
$modulesRoot = Join-Path $adapters 'game-modules'
if (-not (Test-Path -LiteralPath $modulesRoot)) { New-Item -ItemType Directory -Path $modulesRoot -Force | Out-Null }
$modulesRoot = Resolve-Local $modulesRoot 'game module root'

$targets = @('pgr','zzz','nikke') | ForEach-Object { Join-Path $modulesRoot $_ }
$active = Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $executable = $_.ExecutablePath
    $executable -and @($targets | Where-Object { $executable.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
}
if (@($active).Count -gt 0) { throw 'An active PGR/ZZZ/NIKKE runner prevents adapter replacement.' }

$scoped = @{}
$testEvidence = @{}
foreach ($gameId in @('PGR','ZZZ','NIKKE')) {
    $manifest = (($candidate.Manifest | ConvertTo-Json -Depth 12 -Compress) | ConvertFrom-Json -ErrorAction Stop)
    $bindings = $manifest.operationBindings.$gameId
    $manifest.supportedGameIds = @($gameId)
    $manifest.operationBindings = [pscustomobject]@{$gameId=$bindings}
    $scoped[$gameId] = $manifest
    $testEvidence[$gameId] = Assert-YeYuGamerCandidateTestEvidence -EvidencePath (Join-Path $evidenceRoot ($gameId.ToLowerInvariant() + '.json')) -Manifest $manifest -ExpectedGameIds @($gameId)
}

$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
$results = @()
try {
    foreach ($gameId in @('PGR','ZZZ','NIKKE')) {
        $leaf = $gameId.ToLowerInvariant()
        $incoming = Assert-YeYuGamerChildPath -Parent $modulesRoot -Child (Join-Path $modulesRoot ('.' + $leaf + '.incoming-' + [guid]::NewGuid().ToString('N'))) -Purpose "incoming $gameId Adapter"
        Copy-Item -LiteralPath $candidate.Root -Destination $incoming -Recurse
        $manifestPath = Join-Path $incoming 'install-manifest.json'
        $manifest = $scoped[$gameId]
        $manifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
        [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
        foreach ($file in @($manifest.files)) {
            $path = Join-Path $incoming ([string]$file.path)
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Hash $path) -cne [string]$file.sha256) { throw "$gameId installed module file integrity mismatch: $($file.path)" }
        }
        $target = Assert-YeYuGamerChildPath -Parent $modulesRoot -Child (Join-Path $modulesRoot $leaf) -Purpose "$gameId Adapter target"
        if (Test-Path -LiteralPath $target) {
            $previous = Assert-YeYuGamerChildPath -Parent $modulesRoot -Child (Join-Path $modulesRoot ($leaf + '.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose "previous $gameId Adapter"
            Move-Item -LiteralPath $target -Destination $previous
        }
        Move-Item -LiteralPath $incoming -Destination $target
        $incoming = $null
        Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
        Remove-YeYuGamerAdapterBackups -AdapterRoot $modulesRoot -PackageName $leaf | Out-Null
        $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId $gameId -EvidencePath $testEvidence[$gameId].Path
        $results += [pscustomobject]@{
            gameId=$gameId;moduleRoot=$target;packageVersion=[string]$manifest.packageVersion;status='installed-unpromoted';executionReady=$false;gameStarted=$false;
            candidateTestEvidencePath=$installedEvidence;candidateTestEvidenceSha256=$testEvidence[$gameId].Sha256
        }
    }
    [pscustomobject]@{status='modules-installed-unpromoted';packageId='legacy-night-rain-gamer';executionReady=$false;promotionOwner='manager';gameStarted=$false;modules=$results} | ConvertTo-Json -Depth 7 -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) { Remove-Item -LiteralPath (Assert-YeYuGamerChildPath -Parent $modulesRoot -Child $incoming -Purpose 'failed Classic incoming') -Recurse -Force }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
