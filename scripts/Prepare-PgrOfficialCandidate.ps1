[CmdletBinding()]
param(
    [string]$CandidateRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) ('.cache\upstream-candidates\pgr-release-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))),
    [string]$DownloadRoot = (Join-Path 'C:\Projects\YeYuGamer' '.cache\upstream-downloads'),
    [Parameter(Mandatory)][string]$InstalledRoot,
    [ValidatePattern('^v[0-9]+\.[0-9]+\.[0-9]+$')]
    [Parameter(Mandatory)][string]$Release
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function LocalPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not [IO.Path]::IsPathFullyQualified($Path) -or $full.StartsWith('\\') -or
        [IO.DriveInfo]::new([IO.Path]::GetPathRoot($full)).DriveType -ne [IO.DriveType]::Fixed) {
        throw 'Official candidates and inputs must be on a local fixed disk.'
    }
    for ($cursor = $full; $cursor; $cursor = Split-Path -Parent $cursor) {
        if ((Test-Path -LiteralPath $cursor) -and
            ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Candidate paths must not traverse a reparse point.'
        }
    }
    return $full
}
function Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
$CandidateRoot = LocalPath $CandidateRoot
$DownloadRoot = LocalPath $DownloadRoot
$InstalledRoot = LocalPath $InstalledRoot
$cachePrefix = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSScriptRoot) '.cache')).TrimEnd('\') + '\'
foreach ($outputPath in @($CandidateRoot, $DownloadRoot)) {
    if (-not ($outputPath + '\').StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Candidate and download outputs must remain below the project cache.'
    }
}
if (Test-Path -LiteralPath $CandidateRoot) { throw 'A new candidate directory is required.' }
$tag = $Release
$assetName = "MPA-win-x86_64-$tag.zip"
$githubCli = Get-Command gh -ErrorAction SilentlyContinue
$githubCliPath = if ($githubCli) { $githubCli.Source } else { Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe' }
if (Test-Path -LiteralPath $githubCliPath -PathType Leaf) {
    # Use the user's existing GitHub authentication without reading or logging its token.
    $releaseJson = & $githubCliPath api "repos/overflow65537/MAA_Punish/releases/tags/$tag"
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the official release through GitHub CLI.' }
    $releaseMetadata = $releaseJson | ConvertFrom-Json -ErrorAction Stop
} else {
    $releaseMetadata = Invoke-RestMethod -Uri "https://api.github.com/repos/overflow65537/MAA_Punish/releases/tags/$tag" -Headers @{'User-Agent'='YeYuGamer-official-candidate'}
}
$assets = @($releaseMetadata.assets | Where-Object name -CEQ $assetName)
if ($releaseMetadata.tag_name -cne $tag -or $assets.Count -ne 1) { throw 'Official release identity mismatch.' }
$asset = $assets[0]
$url = "https://github.com/overflow65537/MAA_Punish/releases/download/$tag/$assetName"
if ($asset.browser_download_url -cne $url -or $asset.digest -cnotmatch '^sha256:[0-9a-f]{64}$') { throw 'Official release digest or URL missing.' }
New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
$archive = Join-Path $DownloadRoot $assetName
if (-not (Test-Path -LiteralPath $archive)) {
    $temporary = $archive + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
    Invoke-WebRequest -Uri $url -OutFile $temporary -TimeoutSec 300
    if ((Get-Item -LiteralPath $temporary).Length -ne $asset.size -or 'sha256:' + (Hash $temporary) -cne $asset.digest) { throw 'Downloaded release hash mismatch.' }
    Move-Item -LiteralPath $temporary -Destination $archive
}
if ((Get-Item -LiteralPath $archive).Length -ne $asset.size -or 'sha256:' + (Hash $archive) -cne $asset.digest) { throw 'Cached release hash mismatch.' }
New-Item -ItemType Directory -Path $CandidateRoot | Out-Null
$toolRoot = Join-Path $CandidateRoot 'tool-root'
[IO.Compression.ZipFile]::ExtractToDirectory($archive, $toolRoot)
foreach ($required in @('FOS.exe','interface.json','resource')) {
    if (-not (Test-Path -LiteralPath (Join-Path $toolRoot $required))) { throw "Official package entry is missing: $required" }
}
$files = @(Get-ChildItem -LiteralPath $toolRoot -File -Recurse | Sort-Object FullName | ForEach-Object {
    [ordered]@{path=$_.FullName.Substring($toolRoot.Length+1).Replace('\','/');sha256=(Hash $_.FullName);sizeBytes=$_.Length}
})
# Preserve private settings as a separate migration input. Never import old
# executable resources, backups or schedules into the official distribution.
$configInputs = @()
foreach ($name in @('config.json','maa_option.json','multi_config.json')) {
    $inputFile = Join-Path $InstalledRoot ('config\' + $name)
    if (-not (Test-Path -LiteralPath $inputFile -PathType Leaf)) { continue }
    $null = Get-Content -LiteralPath $inputFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $privateRoot = Join-Path $CandidateRoot 'private-config-input'
    New-Item -ItemType Directory -Path $privateRoot -Force | Out-Null
    Copy-Item -LiteralPath $inputFile -Destination (Join-Path $privateRoot $name)
    $configInputs += [ordered]@{name=$name;sha256=(Hash (Join-Path $privateRoot $name));applied=$false}
}
$registryPath = Join-Path $InstalledRoot 'config\multi_config.json'
$registry = Get-Content -LiteralPath $registryPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($registry.curr_config_id -notin $registry.config_list) { throw 'Official current profile is not registered.' }
# Relative resource roots already resolve against the new official tool. Rebase
# only the binding to this distribution, never game or controller parameters.
foreach ($bundle in $registry.bundle.PSObject.Properties) {
    $value = $bundle.Value
    $resolvedBundle = [IO.Path]::GetFullPath((Join-Path $InstalledRoot ([string]$value.path))).TrimEnd('\')
    if ($resolvedBundle -ine $InstalledRoot) { throw 'A non-default resource bundle requires an explicit migration mapping.' }
    $value.path = '.'
}
$configRoot = Join-Path $toolRoot 'config'
New-Item -ItemType Directory -Path (Join-Path $configRoot 'configs') -Force | Out-Null
$migrated = @()
foreach ($profileId in @($registry.config_list)) {
    if ($profileId -cnotmatch '^[A-Za-z0-9_-]{1,128}$') { throw 'Invalid official profile identifier.' }
    $relative = 'config\configs\' + $profileId + '.json'
    $profileSource = LocalPath (Join-Path $InstalledRoot $relative)
    $null = Get-Content -LiteralPath $profileSource -Raw -Encoding UTF8 | ConvertFrom-Json
    $destination = Join-Path $toolRoot $relative
    Copy-Item -LiteralPath $profileSource -Destination $destination
    if ((Hash $profileSource) -cne (Hash $destination)) { throw 'Profile copy verification failed.' }
    $migrated += [ordered]@{path=$relative.Replace('\','/');sha256=(Hash $destination)}
}
[IO.File]::WriteAllText((Join-Path $configRoot 'multi_config.json'), ($registry | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
# No schedules or global startup/notification settings are transferred. The
# official registered profiles contain the user's controller and task options.
$report = [ordered]@{
    schemaVersion=1;status='official-release-prepared';gameId='PGR';tag=$tag;
    sourceUrl=$url;archiveSha256=(Hash $archive);assetId=$asset.id;
    toolRoot=$toolRoot;formalEntry='FOS.exe';files=$files;privateConfigInputs=$configInputs;
    migratedProfiles=$migrated;configMigrationApplied=$true;toolStarted=$false;gameStarted=$false;installedToolChanged=$false
}
[IO.File]::WriteAllText((Join-Path $CandidateRoot 'official-release-manifest.json'), ($report | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{status=$report.status;candidateRoot=$CandidateRoot;files=$files.Count;privateConfigInputs=$configInputs.Count;toolStarted=$false;gameStarted=$false} | ConvertTo-Json -Compress
