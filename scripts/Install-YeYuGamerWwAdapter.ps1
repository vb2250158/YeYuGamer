[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\local-daily-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidenceDirectory = '',
    [switch]$PromoteForDailyBatchValidation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$source = Assert-YeYuGamerLocalTarget -Path $SourceRoot -Purpose 'OpenKuro installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-OpenKuroCandidate([string]$Root) {
    $resolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose 'OpenKuro candidate'
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw 'OpenKuro candidate is missing.' }
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'OpenKuro candidate' | Out-Null
    $manifestPath = Join-Path $resolved 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'OpenKuro candidate manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $expected = [ordered]@{
        WW = @('attach-world','inspect-daily-progress','farm-nightmare-daily-echo','spend-waveplates','claim-daily-reward','claim-mail','claim-battle-pass')
        Endfield = @('attach-world','mail','spend-sanity','delivery-commission','collect-credit','dijiang-harvest','claim-daily-reward')
        GF2 = @('attach-home','mail','public-area-dispatch','spend-stamina','squad-tasks','claim-daily-missions','battle-pass-free-track')
    }
    if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
        [string]$manifest.entryPoint -cne 'runner.exe' -or [string]$manifest.promotion.status -cne 'candidate' -or
        [bool]$manifest.executionReady -or
        ((@($manifest.supportedGameIds | Sort-Object) -join ',') -cne (@($expected.Keys | Sort-Object) -join ','))) {
        throw 'OpenKuro package must be an exact-scope unpromoted candidate.'
    }
    foreach ($gameId in $expected.Keys) {
        $actual = @($manifest.operationBindings.$gameId.PSObject.Properties.Name | Sort-Object)
        if (($actual -join ',') -cne (@($expected[$gameId] | Sort-Object) -join ',')) {
            throw "$gameId operation allowlist differs from the audited contract."
        }
    }
    if ((Get-YeYuGamerPayloadDigest -Files @($manifest.files)) -cne [string]$manifest.promotion.payloadDigest) {
        throw 'OpenKuro candidate payload digest is invalid.'
    }
    $declared = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.path
        if (-not $relative -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains('\') -or -not $declared.Add($relative)) {
            throw "OpenKuro candidate contains an invalid file path: $relative"
        }
        $path = Assert-YeYuGamerChildPath -Parent $resolved -Child (Join-Path $resolved $relative.Replace('/','\')) -Purpose 'OpenKuro candidate file'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne [int64]$file.sizeBytes -or
            (Get-FileSha256 $path) -cne ([string]$file.sha256).ToLowerInvariant()) {
            throw "OpenKuro candidate integrity mismatch: $relative"
        }
    }
    if (-not $declared.Contains('runner.exe') -or -not $declared.Contains('tool-binding.json')) { throw 'OpenKuro candidate payload is incomplete.' }
    if (@(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force).Count -ne $declared.Count + 1) { throw 'OpenKuro candidate contains undeclared files.' }
    return [pscustomobject]@{ Root=$resolved; Manifest=$manifest }
}

if ($PromoteForDailyBatchValidation) { throw 'OpenKuro promotion is Manager-owned; this installer accepts candidates only.' }
if ([string]::IsNullOrWhiteSpace($CandidateTestEvidenceDirectory)) { throw 'OpenKuro candidate installation requires payload-bound test evidence.' }
$evidenceDirectory = Assert-YeYuGamerLocalTarget -Path $CandidateTestEvidenceDirectory -Purpose 'OpenKuro candidate test evidence directory'
if (-not (Test-Path -LiteralPath $evidenceDirectory -PathType Container)) { throw 'OpenKuro candidate test evidence directory is missing.' }

$candidate = Assert-OpenKuroCandidate -Root $CandidateRoot
& (Join-Path $candidate.Root 'runner.exe') --probe-binding | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'OpenKuro candidate binding probe failed.' }

$scoped = @{}
$testEvidence = @{}
foreach ($gameId in @('WW','Endfield','GF2')) {
    $manifest = (($candidate.Manifest | ConvertTo-Json -Depth 12 -Compress) | ConvertFrom-Json -ErrorAction Stop)
    $bindings = $manifest.operationBindings.$gameId
    $manifest.supportedGameIds = @($gameId)
    $manifest.operationBindings = [pscustomobject]@{ $gameId = $bindings }
    $scoped[$gameId] = $manifest
    $path = Join-Path $evidenceDirectory ($gameId.ToLowerInvariant() + '.json')
    $testEvidence[$gameId] = Assert-YeYuGamerCandidateTestEvidence -EvidencePath $path -Manifest $manifest -ExpectedGameIds @($gameId)
}

$runtime = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$modules = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters\game-modules') -Purpose 'game module root'
New-Item -ItemType Directory -Path $modules -Force | Out-Null
Assert-YeYuGamerNoReparseTree -Path $modules -Purpose 'game module root' | Out-Null
$targets = @('ww','endfield','gf2') | ForEach-Object { Join-Path $modules $_ }
$active = @(Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $executablePath = $_.ExecutablePath
    $executablePath -and @($targets | Where-Object { ([IO.Path]::GetFullPath($executablePath)).StartsWith(($_.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
})
if ($active.Count -gt 0) { throw 'An OpenKuro Adapter runner is active; installation is refused.' }

$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
$results = @()
try {
    foreach ($gameId in @('WW','Endfield','GF2')) {
        $leaf = $gameId.ToLowerInvariant()
        $incoming = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('.' + $leaf + '.incoming-' + [guid]::NewGuid().ToString('N'))) -Purpose "incoming $gameId Adapter"
        Copy-Item -LiteralPath $candidate.Root -Destination $incoming -Recurse
        Assert-YeYuGamerNoReparseTree -Path $incoming -Purpose "incoming $gameId Adapter" | Out-Null
        $manifest = $scoped[$gameId]
        $manifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
        [IO.File]::WriteAllText((Join-Path $incoming 'install-manifest.json'), ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
        foreach ($file in @($manifest.files)) {
            $path = Assert-YeYuGamerChildPath -Parent $incoming -Child (Join-Path $incoming ([string]$file.path).Replace('/','\')) -Purpose "$gameId installed module file"
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-FileSha256 $path) -cne ([string]$file.sha256).ToLowerInvariant()) {
                throw "$gameId installed module integrity mismatch: $($file.path)"
            }
        }
        $target = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules $leaf) -Purpose "$gameId Adapter target"
        if (Test-Path -LiteralPath $target) {
            $previous = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ($leaf + '.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose "previous $gameId Adapter"
            Move-Item -LiteralPath $target -Destination $previous
        }
        Move-Item -LiteralPath $incoming -Destination $target
        $incoming = $null
        Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
        Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName $leaf | Out-Null
        $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId $gameId -EvidencePath $testEvidence[$gameId].Path
        $results += [pscustomobject]@{
            gameId=$gameId; moduleRoot=$target; packageVersion=[string]$manifest.packageVersion; status='installed-unpromoted';
            executionReady=$false; gameStarted=$false; candidateTestEvidencePath=$installedEvidence;
            candidateTestEvidenceSha256=$testEvidence[$gameId].Sha256
        }
    }
    [pscustomobject]@{
        status='modules-installed-unpromoted'; packageId='legacy-night-rain-gamer'; gameIds=@('WW','Endfield','GF2');
        executionReady=$false; promotionOwner='manager'; gameStarted=$false; modules=$results
    } | ConvertTo-Json -Depth 7 -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) {
        Remove-Item -LiteralPath (Assert-YeYuGamerChildPath -Parent $modules -Child $incoming -Purpose 'failed OpenKuro incoming') -Recurse -Force
    }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
