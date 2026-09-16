[CmdletBinding()]
param(
    [string]$PgrBaseline = 'C:\Projects\YeYuGamer\vendor-baselines\MAA_Punish-v3.10.30',
    [string]$PgrTarget = 'C:\Game\MPA-win-x86_64-v3.7.15',
    [string]$ZzzBaseline = 'C:\Projects\YeYuGamer\vendor-baselines\ZenlessZoneZero-OneDragon-v2.5.1',
    [ValidateSet('c16372ae5c9ba140a61e9c2b4e1c5e464e26a115','9f53b562ab2ab2877db455c0596a6d25f5721f64')]
    [string]$ZzzAuditedCommit = 'c16372ae5c9ba140a61e9c2b4e1c5e464e26a115',
    [string]$ZzzTarget = 'C:\Game\ZZZTool',
    [string]$ZzzTargetCommit = 'c16372ae5c9ba140a61e9c2b4e1c5e464e26a115',
    [string]$CandidateRoot = (Join-Path 'C:\Projects\YeYuGamer' ('.cache\upstream-candidates\classic-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))),
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Expected = @{ PGR = '68fb1e39f14613806a75df8e51969242e1138c4e'; ZZZ = $ZzzAuditedCommit }

function Resolve-LocalDirectory([string]$Path, [string]$Label, [switch]$MustNotExist) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not [IO.Path]::IsPathFullyQualified($full) -or $full.StartsWith('\\')) { throw "$Label must be an absolute local directory: $Path" }
    $drive = [IO.Path]::GetPathRoot($full)
    if ([IO.DriveInfo]::new($drive).DriveType -ne [IO.DriveType]::Fixed) { throw "$Label must be on a fixed local disk." }
    if ($MustNotExist) { if (Test-Path -LiteralPath $full) { throw "$Label already exists: $full" } }
    elseif (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Label is missing: $full" }
    return $full
}
function Assert-Baseline([string]$Root, [string]$ExpectedCommit, [string]$Label) {
    $actual = (& git -C $Root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -cne $ExpectedCommit) { throw "$Label baseline HEAD mismatch: expected $ExpectedCommit, got $actual" }
    if ((& git -C $Root status --porcelain)) { throw "$Label baseline has local modifications." }
}
function Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function File-Manifest([string]$Root) {
    $prefix = $Root.TrimEnd('\') + '\'
    @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force |
        Where-Object { $_.FullName -ne (Join-Path $Root '.git') -and -not $_.FullName.StartsWith(($prefix + '.git\'), [StringComparison]::OrdinalIgnoreCase) } |
        Sort-Object FullName |
        ForEach-Object { [ordered]@{ path=$_.FullName.Substring($prefix.Length).Replace('\','/'); sha256=(Hash $_.FullName); sizeBytes=[int64]$_.Length } })
}
function Compare-Manifest($Baseline, $Candidate, [string]$Label) {
    $left = @{}; foreach($entry in $Baseline) { $left[$entry.path] = $entry }; $right = @{}; foreach($entry in $Candidate) { $right[$entry.path] = $entry }
    $missing=@();$different=@();$extra=@()
    foreach($path in $left.Keys) { if(-not $right.ContainsKey($path)) { $missing += $path } elseif($left[$path].sha256 -cne $right[$path].sha256 -or $left[$path].sizeBytes -ne $right[$path].sizeBytes) { $different += $path } }
    foreach($path in $right.Keys) { if(-not $left.ContainsKey($path)) { $extra += $path } }
    if($missing.Count -or $different.Count -or $extra.Count) { throw "$Label candidate hash verification failed: missing=$($missing.Count), different=$($different.Count), extra=$($extra.Count)" }
}
function Copy-OfficialTree([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Where-Object { $_.Name -ne '.git' } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name) -Recurse -Force }
}
function Scope-Inventory([string]$BaselineRoot, [string]$TargetRoot, [string]$Scope) {
    $baselinePath = Join-Path $BaselineRoot $Scope; $targetPath = Join-Path $TargetRoot $Scope
    if (-not (Test-Path -LiteralPath $baselinePath -PathType Container) -or -not (Test-Path -LiteralPath $targetPath -PathType Container)) { return [ordered]@{scope=$Scope;available=$false;same=@();different=@();missing=@();extra=@()} }
    $baseline = @{}; Get-ChildItem -LiteralPath $baselinePath -File -Recurse -Force | ForEach-Object { $baseline[$_.FullName.Substring($baselinePath.Length + 1).Replace('\','/')] = Hash $_.FullName }
    $target = @{}; Get-ChildItem -LiteralPath $targetPath -File -Recurse -Force | ForEach-Object { $target[$_.FullName.Substring($targetPath.Length + 1).Replace('\','/')] = Hash $_.FullName }
    $same=@();$different=@();$missing=@();$extra=@()
    foreach($relative in $baseline.Keys | Sort-Object) { if(-not $target.ContainsKey($relative)){$missing += $relative}elseif($baseline[$relative] -eq $target[$relative]){$same += $relative}else{$different += [ordered]@{path=$relative;baselineSha256=$baseline[$relative];targetSha256=$target[$relative]}} }
    foreach($relative in $target.Keys | Sort-Object) { if(-not $baseline.ContainsKey($relative)){$extra += [ordered]@{path=$relative;targetSha256=$target[$relative]}} }
    [ordered]@{scope=$Scope;available=$true;same=$same;different=$different;missing=$missing;extra=$extra}
}
function Zzz-WorkingTreeInventory([string]$TargetRoot, [string]$ExpectedCommit) {
    $head = (& git -C $TargetRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -cne $ExpectedCommit) { throw "ZZZ target HEAD mismatch: expected $ExpectedCommit, got $head" }
    $tracked = @(& git -C $TargetRoot diff --name-only -- src | ForEach-Object { $_.Replace('\\','/') } | Sort-Object); $untracked = @(& git -C $TargetRoot ls-files --others --exclude-standard -- src | ForEach-Object { $_.Replace('\\','/') } | Sort-Object)
    [ordered]@{scope='src-git-working-tree';available=$true;same=@();different=@($tracked | ForEach-Object { [ordered]@{path=$_;baselineSha256='git:' + $ExpectedCommit;targetSha256='working-tree'} });missing=@();extra=@($untracked | ForEach-Object { [ordered]@{path=$_;targetSha256='untracked'} })}
}

if ($Apply) { throw 'Apply is intentionally unsupported. This command only creates a new isolated official candidate and never modifies C:\Game.' }
$PgrBaseline = Resolve-LocalDirectory $PgrBaseline 'PGR baseline'; $PgrTarget = Resolve-LocalDirectory $PgrTarget 'PGR installed tool'; $ZzzBaseline = Resolve-LocalDirectory $ZzzBaseline 'ZZZ baseline'; $ZzzTarget = Resolve-LocalDirectory $ZzzTarget 'ZZZ installed tool'; $CandidateRoot = Resolve-LocalDirectory $CandidateRoot 'isolated candidate root' -MustNotExist
if (-not $CandidateRoot.StartsWith('C:\Projects\YeYuGamer\.cache\upstream-candidates\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Official source candidates must remain below the project upstream-candidates cache.'
}
$candidateParent = Split-Path -Parent $CandidateRoot; if (-not (Test-Path -LiteralPath $candidateParent -PathType Container)) { New-Item -ItemType Directory -Path $candidateParent -Force | Out-Null }
Assert-Baseline $PgrBaseline $Expected.PGR 'PGR'; Assert-Baseline $ZzzBaseline $Expected.ZZZ 'ZZZ'

# Installed trees are read-only evidence sources. Only clean, separately cloned
# upstream baselines are copied; user config and unverified local files stay out.
$pgrObserved = @(Scope-Inventory $PgrBaseline $PgrTarget 'resource'); $zzzObserved = @(Zzz-WorkingTreeInventory $ZzzTarget $ZzzTargetCommit)
$work = $CandidateRoot + '.building-' + [Guid]::NewGuid().ToString('N')
try {
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    $pgrDestination = Join-Path $work 'PGR\official-source'; $zzzDestination = Join-Path $work 'ZZZ\official-source'
    Copy-OfficialTree $PgrBaseline $pgrDestination; Copy-OfficialTree $ZzzBaseline $zzzDestination
    $pgrFiles = File-Manifest $PgrBaseline; $zzzFiles = File-Manifest $ZzzBaseline
    Compare-Manifest $pgrFiles (File-Manifest $pgrDestination) 'PGR'; Compare-Manifest $zzzFiles (File-Manifest $zzzDestination) 'ZZZ'
    $manifest = [ordered]@{ schemaVersion=1; candidateKind='isolated-official-source'; createdAt=[DateTimeOffset]::UtcNow.ToString('o'); runnable=$false; deploymentAllowed=$false; sourceOnlyReason='No verified official release manifest exists for reusing installed executable/runtime dependencies; installed trees were not copied.'; tools=[ordered]@{ PGR=[ordered]@{repository='https://github.com/overflow65537/MAA_Punish';commit=$Expected.PGR;sourceDirectory='PGR/official-source';fileCount=$pgrFiles.Count;files=$pgrFiles}; ZZZ=[ordered]@{repository='https://github.com/DoctorReid/ZenlessZoneZero-OneDragon';commit=$Expected.ZZZ;sourceDirectory='ZZZ/official-source';fileCount=$zzzFiles.Count;files=$zzzFiles} }; installedTrees=[ordered]@{copied=$false;PGR=[ordered]@{path=$PgrTarget;observationOnly=$true;configCopied=$false;runtimeDependenciesCopied=$false};ZZZ=[ordered]@{path=$ZzzTarget;observationOnly=$true;configCopied=$false;runtimeDependenciesCopied=$false}} }
    [IO.File]::WriteAllText((Join-Path $work 'official-candidate-manifest.json'), ($manifest | ConvertTo-Json -Depth 16), [Text.UTF8Encoding]::new($false))
    $report = [ordered]@{schemaVersion=1;status='verified-isolated-candidate';candidateRoot=$CandidateRoot;gameStarted=$false;toolStarted=$false;installedTreesModified=$false;verification=[ordered]@{PGR=[ordered]@{commit=$Expected.PGR;fileCount=$pgrFiles.Count;hashesMatch=$true};ZZZ=[ordered]@{commit=$Expected.ZZZ;fileCount=$zzzFiles.Count;hashesMatch=$true}};installedObservations=[ordered]@{PGR=$pgrObserved;ZZZ=$zzzObserved};limitation='Candidate contains only cloned official source. It is deliberately non-runnable until an official release provides independently verifiable runtime dependency hashes.'}
    [IO.File]::WriteAllText((Join-Path $work 'verification-report.json'), ($report | ConvertTo-Json -Depth 16), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $work -Destination $CandidateRoot
} catch { if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }; throw }
[ordered]@{status='verified-isolated-candidate';candidateRoot=$CandidateRoot;manifest=(Join-Path $CandidateRoot 'official-candidate-manifest.json');report=(Join-Path $CandidateRoot 'verification-report.json');gameStarted=$false;toolStarted=$false;installedTreesModified=$false} | ConvertTo-Json -Compress
