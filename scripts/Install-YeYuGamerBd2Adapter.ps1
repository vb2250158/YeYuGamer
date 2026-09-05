[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\bd2-candidate'),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [string]$CandidateTestEvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$source = Assert-YeYuGamerLocalTarget -Path $SourceRoot -Purpose 'BD2 installer source root'
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')

function Get-Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

if ([string]::IsNullOrWhiteSpace($CandidateTestEvidencePath)) { throw 'BD2 candidate installation requires payload-bound test evidence.' }

$candidate = Assert-YeYuGamerLocalTarget -Path $CandidateRoot -Purpose 'BD2 Adapter candidate'
Assert-YeYuGamerNoReparseTree -Path $candidate -Purpose 'BD2 Adapter candidate' | Out-Null
$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([int]$manifest.schemaVersion -ne 2 -or [string]$manifest.packageId -cne 'legacy-night-rain-gamer' -or
    [string]$manifest.entryPoint -cne 'runner.exe' -or [string]$manifest.promotion.status -cne 'candidate' -or
    [bool]$manifest.executionReady -or @($manifest.supportedGameIds).Count -ne 1 -or [string]$manifest.supportedGameIds[0] -cne 'BD2') {
    throw 'BD2 candidate identity or stage is invalid.'
}
$expected = @('attach-home','daily-claim','stamina-sweep')
if (@(Compare-Object $expected @($manifest.operationBindings.BD2.PSObject.Properties.Name)).Count -ne 0) { throw 'BD2 operation allowlist differs from the audited safe set.' }
foreach ($file in @($manifest.files)) {
    $relative = [string]$file.path
    if (-not $relative -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains('\')) { throw "Unsafe BD2 candidate file: $relative" }
    $path = Assert-YeYuGamerChildPath -Parent $candidate -Child (Join-Path $candidate $relative) -Purpose 'BD2 candidate file'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne [int64]$file.sizeBytes -or (Get-Hash $path) -cne [string]$file.sha256) { throw "BD2 candidate integrity mismatch: $relative" }
}
& (Join-Path $candidate 'runner.exe') --probe-binding | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'BD2 fixed binding probe failed.' }

$runtime = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$testEvidence = Assert-YeYuGamerCandidateTestEvidence -EvidencePath $CandidateTestEvidencePath -Manifest $manifest -ExpectedGameIds @('BD2')
$modules = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters\game-modules') -Purpose 'game module root'
New-Item -ItemType Directory -Path $modules -Force | Out-Null
$target = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules 'bd2') -Purpose 'BD2 Adapter target'
if (@(Get-CimInstance Win32_Process -Filter "Name='runner.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(($target.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase) }).Count) { throw 'An active BD2 runner prevents replacement.' }

$lock = Enter-YeYuGamerInstallLifecycleLock
$incoming = $null
try {
    $incoming = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('.bd2.incoming-' + [guid]::NewGuid().ToString('N'))) -Purpose 'incoming BD2 Adapter'
    Copy-Item -LiteralPath $candidate -Destination $incoming -Recurse
    $incomingManifestPath = Join-Path $incoming 'install-manifest.json'
    $installedManifest = Get-Content -LiteralPath $incomingManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $installedManifest.installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    [IO.File]::WriteAllText($incomingManifestPath, ($installedManifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $target) {
        $previous = Assert-YeYuGamerChildPath -Parent $modules -Child (Join-Path $modules ('bd2.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous BD2 Adapter'
        Move-Item -LiteralPath $target -Destination $previous
    }
    Move-Item -LiteralPath $incoming -Destination $target
    $incoming = $null
    Protect-YeYuGamerRuntimePackageDirectory -Path $target | Out-Null
    Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName 'bd2' | Out-Null
    $installedEvidence = Install-YeYuGamerCandidateTestEvidence -RuntimeRoot $runtime -GameId 'BD2' -EvidencePath $testEvidence.Path
    [pscustomobject]@{status='installed-unpromoted';packageVersion=[string]$installedManifest.packageVersion;gameId='BD2';executionReady=$false;promotionOwner='manager';gameStarted=$false;candidateTestEvidencePath=$installedEvidence;candidateTestEvidenceSha256=$testEvidence.Sha256}|ConvertTo-Json -Compress
}
finally {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) { Remove-Item -LiteralPath (Assert-YeYuGamerChildPath -Parent $modules -Child $incoming -Purpose 'failed BD2 incoming') -Recurse -Force }
    Exit-YeYuGamerInstallLifecycleLock -Mutex $lock
}
