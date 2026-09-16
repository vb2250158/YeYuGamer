[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OfficialSource,
    [string]$CandidateRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) ('.cache\upstream-candidates\nte-release-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))),
    [string]$PythonPath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Python\Python312\python.exe'),
    [Parameter(Mandatory)][string]$Release,
    [string]$TemplateSource
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$baselines = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\adapter-host\nte-runner\official-baselines.json') -Raw -Encoding utf8 | ConvertFrom-Json
$releaseProperty = $baselines.releases.PSObject.Properties[$Release]
if ($null -eq $releaseProperty) { throw "NTE release has not been audited: $Release" }
$ExpectedRepository = $baselines.repository
$ExpectedCommit = $releaseProperty.Value.commit
$ExpectedTemplateRepository = $baselines.templateRepository
$ExpectedTemplateCommit = $releaseProperty.Value.templateCommit

function Assert-LocalDirectory([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\') -or -not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Purpose is missing or not local: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point: $full" }
    return $full
}
function Assert-LocalFile([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\') -or -not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "$Purpose is missing or not local: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point: $full" }
    return $full
}
function Write-PreparationEvent([string]$Status, [string]$Detail) {
    Add-Content -LiteralPath $statusLog -Value (([ordered]@{ at=[DateTimeOffset]::UtcNow.ToString('o'); status=$Status; detail=$Detail } | ConvertTo-Json -Compress)) -Encoding utf8
}
function Invoke-LoggedNative([string]$FileName, [string[]]$Arguments, [string]$Description) {
    Add-Content -LiteralPath $stdoutLog -Value ("`n# " + $Description) -Encoding utf8
    Add-Content -LiteralPath $stderrLog -Value ("`n# " + $Description) -Encoding utf8
    & $FileName @Arguments 1>> $stdoutLog 2>> $stderrLog
    if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE. See $stderrLog" }
}
function Read-GitText([string[]]$Arguments, [string]$Description) {
    $temporary = Join-Path $logs ('.git-read-' + [guid]::NewGuid().ToString('N') + '.txt')
    try {
        & git @Arguments 1> $temporary 2>> $stderrLog
        if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE. See $stderrLog" }
        $text = Get-Content -LiteralPath $temporary -Raw -Encoding utf8
        return $(if ($null -eq $text) { [string]::Empty } else { $text.Trim() })
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Get-GitBlobManifest([string]$Repository, [string]$Revision, [string]$Prefix) {
    $lines = Read-GitText @('-C', $Repository, 'ls-tree', '-r', $Revision) "Read blob tree $Prefix@$Revision"
    $entries = @()
    foreach ($line in ($lines -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $match = [regex]::Match($line, '^(?<mode>\d+)\s+(?<type>blob)\s+(?<hash>[0-9a-f]{40})\t(?<path>.+)$')
        if ($match.Success) { $entries += [ordered]@{ path=$match.Groups['path'].Value; gitBlobSha1=$match.Groups['hash'].Value } }
    }
    if ($entries.Count -eq 0) { throw "No blob entries found for $Prefix@$Revision" }
    return @($entries | Sort-Object path)
}

$target = [IO.Path]::GetFullPath($CandidateRoot)
$cachePrefix = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSScriptRoot) '.cache')).TrimEnd('\') + '\'
if (-not ($target + '\').StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Candidate output must remain below the project cache.' }
for ($cursor = $target; $cursor; $cursor = Split-Path -Parent $cursor) {
    if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Candidate output must not traverse a reparse point.' }
}
$official = Assert-LocalDirectory $OfficialSource 'Audited official NTE source'
$python = Assert-LocalFile $PythonPath 'Python 3.12 interpreter'
if (Test-Path -LiteralPath $target) { throw "Candidate target already exists; preparation never deletes or replaces it: $target" }

# Creation is intentional and singular. All later failures retain this target,
# its separate output streams, and a terminal failure event for inspection.
New-Item -ItemType Directory -Path $target -Force | Out-Null
$logs = Join-Path $target 'logs'; New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stdoutLog = Join-Path $logs 'prepare.stdout.log'; $stderrLog = Join-Path $logs 'prepare.stderr.log'; $statusLog = Join-Path $logs 'prepare.status.jsonl'
Set-Content -LiteralPath $stdoutLog -Value '' -Encoding utf8; Set-Content -LiteralPath $stderrLog -Value '' -Encoding utf8; Set-Content -LiteralPath $statusLog -Value '' -Encoding utf8

try {
    Write-PreparationEvent 'started' 'Validating audited source and preparing a new isolated candidate.'
    $head = Read-GitText @('-C', $official, 'rev-parse', 'HEAD') 'Read official source commit'
    $remote = Read-GitText @('-C', $official, 'remote', 'get-url', 'origin') 'Read official source remote'
    $dirty = Read-GitText @('-C', $official, 'status', '--porcelain') 'Read official source worktree status'
    if ($head -cne $ExpectedCommit -or $remote -cne $ExpectedRepository -or -not [string]::IsNullOrEmpty($dirty)) { throw "Audited official source must be clean $ExpectedRepository at $ExpectedCommit." }
    $templateGitlink = Read-GitText @('-C', $official, 'ls-tree', 'HEAD', 'ok_templates') 'Read official ok_templates gitlink'
    if ($templateGitlink -cne ("160000 commit $ExpectedTemplateCommit" + "`tok_templates")) { throw 'Official ok_templates gitlink does not match the audited commit.' }
    $sourceBlobs = Get-GitBlobManifest $official $ExpectedCommit 'ok-nte'

    $archive = Join-Path $target 'official-source.zip'
    Invoke-LoggedNative 'git' @('-C', $official, 'archive', '--format=zip', ("--output=$archive"), $ExpectedCommit) 'Archive audited official source'
    Expand-Archive -LiteralPath $archive -DestinationPath $target -Force
    if (-not (Test-Path -LiteralPath (Join-Path $target 'main.py') -PathType Leaf)) { throw 'Official archive does not contain main.py.' }

    $templates = Join-Path $target 'ok_templates'
    if ($TemplateSource) {
        $templateCache = Assert-LocalDirectory $TemplateSource 'Downloaded official template source'
        $templateOrigin = Read-GitText @('-C', $templateCache, 'remote', 'get-url', 'origin') 'Read cached template origin'
        $templateHead = Read-GitText @('-C', $templateCache, 'rev-parse', 'HEAD') 'Read cached template revision'
        $templateDirty = Read-GitText @('-C', $templateCache, 'status', '--porcelain') 'Read cached template status'
        if ($templateOrigin -cne $ExpectedTemplateRepository -or $templateHead -cne $ExpectedTemplateCommit -or $templateDirty) { throw 'Cached templates must be the clean audited official revision.' }
        Invoke-LoggedNative 'git' @('clone', '--no-hardlinks', '--no-checkout', $templateCache, $templates) 'Copy verified template objects into isolated candidate'
        Invoke-LoggedNative 'git' @('-C', $templates, 'remote', 'set-url', 'origin', $ExpectedTemplateRepository) 'Record official template origin'
    } else {
        Invoke-LoggedNative 'git' @('init', $templates) 'Initialize isolated ok_templates repository'
        Invoke-LoggedNative 'git' @('-C', $templates, 'remote', 'add', 'origin', $ExpectedTemplateRepository) 'Set official ok_templates origin'
        Invoke-LoggedNative 'git' @('-C', $templates, '-c', 'http.version=HTTP/1.1', 'fetch', '--depth=1', 'origin', $ExpectedTemplateCommit) 'Fetch pinned official ok_templates commit'
    }
    Invoke-LoggedNative 'git' @('-C', $templates, 'checkout', '--detach', $ExpectedTemplateCommit) 'Checkout audited ok_templates commit'
    $resolvedTemplateCommit = Read-GitText @('-C', $templates, 'rev-parse', 'HEAD') 'Read isolated ok_templates commit'
    $resolvedTemplateRemote = Read-GitText @('-C', $templates, 'remote', 'get-url', 'origin') 'Read isolated ok_templates remote'
    if ($resolvedTemplateCommit -cne $ExpectedTemplateCommit -or $resolvedTemplateRemote -cne $ExpectedTemplateRepository) { throw 'Isolated ok_templates repository does not match its audited source and commit.' }
    $templateBlobs = Get-GitBlobManifest $templates $ExpectedTemplateCommit 'ok_templates'

    $venv = Join-Path $target '.venv'
    Invoke-LoggedNative $python @('-m', 'venv', $venv) 'Create independent candidate virtual environment'
    $candidatePython = Assert-LocalFile (Join-Path $venv 'Scripts\python.exe') 'Candidate virtual environment Python'
    Invoke-LoggedNative $candidatePython @('-m', 'pip', '--disable-pip-version-check', '--no-input', 'install', '-r', (Join-Path $target 'requirements.txt')) 'Install official locked requirements'
    Invoke-LoggedNative $candidatePython @('-m', 'pip', 'check') 'Verify candidate dependency graph'
    Push-Location -LiteralPath $target
    try {
        Invoke-LoggedNative $candidatePython @('-c', 'import ok; import src.config; import src.tasks.daily.DailyRoutineTask; import src.tasks.daily.DailyClaimTask; print("official_imports_ok")') 'Run import-only official module probe'
    } finally {
        Pop-Location
    }

    $manifest = [ordered]@{
        schemaVersion=2; candidateKind='official-nte-source'; preparedAt=[DateTimeOffset]::UtcNow.ToString('o')
        source=[ordered]@{repository=$ExpectedRepository;commit=$ExpectedCommit;blobs=$sourceBlobs}
        templates=[ordered]@{repository=$ExpectedTemplateRepository;commit=$ExpectedTemplateCommit;blobs=$templateBlobs}
        # ok-script task indices are one-based. src.config registers
        # LauncherTask first and DailyRoutineTask second.
        entry=[ordered]@{pythonwRelativePath='.venv\Scripts\pythonw.exe';workingDirectoryRelativePath='.';sourceEntry='main.py';arguments='main.py --task 2 --exit'}
        configurationMigrationWhitelist=@('configs/DailyRoutineTask.json','configs/DailyRoutineTaskConfigs.json')
        validation=[ordered]@{requirementsInstalled=$true;pipCheck=$true;imports=@('ok','src.config','src.tasks.daily.DailyRoutineTask','src.tasks.daily.DailyClaimTask');toolGuiStarted=$false;gameStarted=$false}
    }
    $manifestPath = Join-Path $target 'yeyu-candidate-baseline.json'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
    Write-PreparationEvent 'prepared' 'Official archive, pinned template submodule, independent venv, pip check, and import-only probe completed.'
    [pscustomobject]@{status='prepared';candidateRoot=$target;manifest=$manifestPath;stdoutLog=$stdoutLog;stderrLog=$stderrLog;gameStarted=$false;toolGuiStarted=$false} | ConvertTo-Json -Compress
} catch {
    $message=$_.Exception.Message; Add-Content -LiteralPath $stderrLog -Value ('PREPARATION FAILURE: '+$message) -Encoding utf8; Write-PreparationEvent 'failed' $message; throw
}
