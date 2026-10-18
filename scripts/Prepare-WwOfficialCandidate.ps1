[CmdletBinding()]
param(
    [string]$OfficialSource = (Join-Path (Split-Path -Parent $PSScriptRoot) 'vendor-baselines\ok-wuthering-waves'),
    [string]$CandidateRoot = 'D:\Game\Tools\Candidates\YeYuGamerCandidates\ww-official-c3fef9a',
    [ValidateSet('c3fef9a06f61f96abeb53571fb47dd578b6f7063', '07a578842b34dda3428535eb46f8b98f76fddb0d', '36c557437de745cdef506bcfa148569d3e09cfcf', 'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d', '20a2d40d0eaa0cfa053ceafecd9a4d04395291f4', '737c89ef00e36248baa0ab92df248af935528b56', '4a1b39d002c5754557bdc7b30782fad0048cd106', '567e71c93c32f03a7bd3ad013205438c0adcba11', '2243d465d58c74f52765b658174bff76165656ee', '6a454cce9e830cd0ecee3fa495e9b8d40c534f28', '1a779fbf3a2211342c6c83f06011b534cbb80466', 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939', '274346512f904b2e2bc2dea78a45982fa0012e83', '93e19b4f455390f50b8e126c23cf4f4be1595adb', 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb', '81edb5af888688be64e879f346b0f2440bacd958', '85e54379b3a6859f30f2a1df6326091f3a51929d')]
    [string]$SourceRevision = 'c3fef9a06f61f96abeb53571fb47dd578b6f7063',
    [string]$PythonPath = 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe',
    [string]$TemplateSource
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ExpectedRepository = 'https://github.com/ok-oldking/ok-wuthering-waves.git'
$ExpectedCommit = $SourceRevision
$LocalPatch = $SourceRevision -cin @('20a2d40d0eaa0cfa053ceafecd9a4d04395291f4', '737c89ef00e36248baa0ab92df248af935528b56', '4a1b39d002c5754557bdc7b30782fad0048cd106', '567e71c93c32f03a7bd3ad013205438c0adcba11', '6a454cce9e830cd0ecee3fa495e9b8d40c534f28', '1a779fbf3a2211342c6c83f06011b534cbb80466', 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939', '274346512f904b2e2bc2dea78a45982fa0012e83', '93e19b4f455390f50b8e126c23cf4f4be1595adb', 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb', '81edb5af888688be64e879f346b0f2440bacd958', '85e54379b3a6859f30f2a1df6326091f3a51929d')
$OfficialBaseCommit = if ($SourceRevision -cin @('6a454cce9e830cd0ecee3fa495e9b8d40c534f28', '1a779fbf3a2211342c6c83f06011b534cbb80466', 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939', '274346512f904b2e2bc2dea78a45982fa0012e83', '93e19b4f455390f50b8e126c23cf4f4be1595adb', 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb', '81edb5af888688be64e879f346b0f2440bacd958', '85e54379b3a6859f30f2a1df6326091f3a51929d')) { '6ffaa7f0b0b7f3140367de96c8eacba533c96894' } elseif ($LocalPatch) { 'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d' } else { $SourceRevision }
$ExpectedTemplateRepository = 'https://github.com/ok-oldking/ok-wuthering-waves-coco-labeling.git'
$ExpectedTemplateCommit = switch ($SourceRevision) {
    '85e54379b3a6859f30f2a1df6326091f3a51929d' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '1a779fbf3a2211342c6c83f06011b534cbb80466' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    'da98151e6bcd2c4f0c191e80c059f84c5e6c4939' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '274346512f904b2e2bc2dea78a45982fa0012e83' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '93e19b4f455390f50b8e126c23cf4f4be1595adb' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '81edb5af888688be64e879f346b0f2440bacd958' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '6a454cce9e830cd0ecee3fa495e9b8d40c534f28' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '2243d465d58c74f52765b658174bff76165656ee' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '4a1b39d002c5754557bdc7b30782fad0048cd106' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '567e71c93c32f03a7bd3ad013205438c0adcba11' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '737c89ef00e36248baa0ab92df248af935528b56' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '20a2d40d0eaa0cfa053ceafecd9a4d04395291f4' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '36c557437de745cdef506bcfa148569d3e09cfcf' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
    '07a578842b34dda3428535eb46f8b98f76fddb0d' { 'd3c70f3c1a941abe99dec2d6921c79c8810ab832' }
    'c3fef9a06f61f96abeb53571fb47dd578b6f7063' { 'd1b4ed8c1ca9e145c514853c14030a7358afe12c' }
}

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
function Write-Utf8Json([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 14), [Text.UTF8Encoding]::new($false))
}
function Write-PreparationEvent([string]$Status, [string]$Detail) {
    Add-Content -LiteralPath $statusLog -Value (([ordered]@{ at=[DateTimeOffset]::UtcNow.ToString('o'); status=$Status; detail=$Detail } | ConvertTo-Json -Compress)) -Encoding utf8
}
function Invoke-LoggedNative([string]$FileName, [string[]]$Arguments, [string]$Description) {
    Add-Content -LiteralPath $stdoutLog -Value ([Environment]::NewLine + '# ' + $Description) -Encoding utf8
    Add-Content -LiteralPath $stderrLog -Value ([Environment]::NewLine + '# ' + $Description) -Encoding utf8
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
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
function Get-GitBlobManifest([string]$Repository, [string]$Revision, [string]$Label) {
    $lines = Read-GitText @('-C', $Repository, 'ls-tree', '-r', $Revision) "Read blob tree $Label@$Revision"
    $entries = @()
    foreach ($line in ($lines -split "\r?\n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $match = [regex]::Match($line, '^(?<mode>\d+)\s+blob\s+(?<hash>[0-9a-f]{40})\t(?<path>.+)$')
        if ($match.Success) { $entries += [ordered]@{ path=$match.Groups['path'].Value; gitBlobSha1=$match.Groups['hash'].Value } }
    }
    if ($entries.Count -eq 0) { throw "No blob entries found for $Label@$Revision" }
    return @($entries | Sort-Object path)
}

$official = Assert-LocalDirectory $OfficialSource 'Audited official WW source'
$python = Assert-LocalFile $PythonPath 'Python 3.12 interpreter'
if ($TemplateSource) {
    $TemplateSource = Assert-LocalDirectory $TemplateSource 'Audited local WW template cache'
    $templateCacheCommit = & git -C $TemplateSource rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $templateCacheCommit -cne $ExpectedTemplateCommit) { throw 'Local WW template cache has a different commit.' }
    $templateCacheRemote = & git -C $TemplateSource remote get-url origin
    if ($LASTEXITCODE -ne 0 -or $templateCacheRemote -cne $ExpectedTemplateRepository) { throw 'Local WW template cache has a different official source.' }
    $templateCacheDirty = & git -C $TemplateSource status --porcelain
    if ($LASTEXITCODE -ne 0 -or $templateCacheDirty) { throw 'Local WW template cache must be clean.' }
}
$target = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
$projectCandidates = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSScriptRoot) '.cache\upstream-candidates')).TrimEnd('\') + '\'
if ($target.StartsWith('\\') -or -not (
        ($target + '\').StartsWith($projectCandidates, [StringComparison]::OrdinalIgnoreCase) -or
        ($target + '\').StartsWith('D:\Game\Tools\Candidates\YeYuGamerCandidates\', [StringComparison]::OrdinalIgnoreCase))) {
    throw 'Candidate root must be a new local child of the project upstream-candidates cache or the existing legacy candidate directory.'
}
if (Test-Path -LiteralPath $target) { throw "Candidate target already exists; preparation never deletes or replaces it: $target" }

New-Item -ItemType Directory -Path $target -Force | Out-Null
$logs = Join-Path $target 'logs'; New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stdoutLog = Join-Path $logs 'prepare.stdout.log'; $stderrLog = Join-Path $logs 'prepare.stderr.log'; $statusLog = Join-Path $logs 'prepare.status.jsonl'
Set-Content -LiteralPath $stdoutLog -Value '' -Encoding utf8; Set-Content -LiteralPath $stderrLog -Value '' -Encoding utf8; Set-Content -LiteralPath $statusLog -Value '' -Encoding utf8

try {
    Write-PreparationEvent 'started' 'Validating audited WW source and creating an isolated source candidate.'
    $head = Read-GitText @('-C', $official, 'rev-parse', 'HEAD') 'Read official source commit'
    $remote = Read-GitText @('-C', $official, 'remote', 'get-url', 'origin') 'Read official source remote'
    $dirty = Read-GitText @('-C', $official, 'status', '--porcelain') 'Read official source worktree status'
    if ($head -cne $ExpectedCommit -or $remote -cne $ExpectedRepository -or -not [string]::IsNullOrEmpty($dirty)) { throw "Audited official source must be clean ok-wuthering-waves at $ExpectedCommit." }
    if ($LocalPatch) { Invoke-LoggedNative 'git' @('-C', $official, 'merge-base', '--is-ancestor', $OfficialBaseCommit, $ExpectedCommit) 'Verify fixed official base of local source patch' }
    $templateGitlink = Read-GitText @('-C', $official, 'ls-tree', 'HEAD', 'ok_templates') 'Read official ok_templates gitlink'
    if ($templateGitlink -notmatch "^160000 commit $ExpectedTemplateCommit\s+ok_templates$") { throw 'Official WW ok_templates gitlink does not match the audited commit.' }
    $sourceBlobs = Get-GitBlobManifest $official $ExpectedCommit 'ok-wuthering-waves'

    $source = Join-Path $target 'WW\source'
    $archive = Join-Path $target 'official-source.zip'
    Invoke-LoggedNative 'git' @('-C', $official, 'archive', '--format=zip', ("--output=$archive"), $ExpectedCommit) 'Archive audited official WW source'
    Expand-Archive -LiteralPath $archive -DestinationPath $source -Force
    foreach ($required in @('main.py', 'config.py', 'pyproject.toml', 'requirements.txt', 'src\task\DailyTask.py', 'src\task\MultiAccountDailyTask.py')) {
        Assert-LocalFile (Join-Path $source $required) "Archived official WW source $required" | Out-Null
    }
    if (rg -n 'YEYU_GAMER|NightRain|nightrain' $source -g '*.py') { throw 'Official WW source archive unexpectedly contains a YeYu bridge.' }

    $templates = Join-Path $source 'ok_templates'
    Invoke-LoggedNative 'git' @('init', $templates) 'Initialize isolated WW ok_templates repository'
    Invoke-LoggedNative 'git' @('-C', $templates, 'config', 'core.longpaths', 'true') 'Enable long paths only in the isolated official template checkout'
    Invoke-LoggedNative 'git' @('-C', $templates, 'remote', 'add', 'origin', $ExpectedTemplateRepository) 'Bind official WW ok_templates repository'
    $templateFetchSource = if ($TemplateSource) { $TemplateSource } else { 'origin' }
    Invoke-LoggedNative 'git' @('-C', $templates, 'fetch', '--depth', '1', $templateFetchSource, $ExpectedTemplateCommit) 'Fetch audited WW ok_templates commit'
    Invoke-LoggedNative 'git' @('-C', $templates, 'checkout', '--detach', $ExpectedTemplateCommit) 'Checkout audited WW ok_templates commit'
    $resolvedTemplateCommit = Read-GitText @('-C', $templates, 'rev-parse', 'HEAD') 'Read isolated WW ok_templates commit'
    $resolvedTemplateRemote = Read-GitText @('-C', $templates, 'remote', 'get-url', 'origin') 'Read isolated WW ok_templates remote'
    if ($resolvedTemplateCommit -cne $ExpectedTemplateCommit -or $resolvedTemplateRemote -cne $ExpectedTemplateRepository) { throw 'Isolated WW ok_templates repository does not match the audited source and commit.' }
    $templateBlobs = Get-GitBlobManifest $templates $ExpectedTemplateCommit 'ok_templates'

    $venv = Join-Path $target 'r'
    Invoke-LoggedNative $python @('-m', 'venv', $venv) 'Create independent candidate virtual environment'
    $candidatePython = Assert-LocalFile (Join-Path $venv 'Scripts\python.exe') 'Candidate virtual environment Python'
    $candidatePythonw = Assert-LocalFile (Join-Path $venv 'Scripts\pythonw.exe') 'Candidate virtual environment Pythonw'
    Invoke-LoggedNative $candidatePython @('-m', 'pip', '--disable-pip-version-check', '--no-input', 'install', '-r', (Join-Path $source 'requirements.txt')) 'Install official pinned requirements'
    Invoke-LoggedNative $candidatePython @('-m', 'pip', 'check') 'Verify candidate dependency graph'

    $previousPythonPath = $env:PYTHONPATH; $previousBytecode = $env:PYTHONDONTWRITEBYTECODE
    try {
        $env:PYTHONPATH = $source; $env:PYTHONDONTWRITEBYTECODE = '1'
        Push-Location -LiteralPath $source
        try {
            Invoke-LoggedNative $candidatePython @('-c', 'import ok; import config; import src.task.DailyTask; import src.task.MultiAccountDailyTask; print("official_imports_ok")') 'Run import-only official module probe'
        } finally { Pop-Location }
    } finally {
        $env:PYTHONPATH = $previousPythonPath; $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
    }

    $manifest = [ordered]@{
        schemaVersion=1; status='read-only-import-ready'; candidateKind=$(if ($LocalPatch) {'isolated-local-ww-source-patch'} else {'isolated-official-ww-source'}); preparedAt=[DateTimeOffset]::UtcNow.ToString('o')
        source=[ordered]@{repository=$ExpectedRepository;commit=$ExpectedCommit;officialBaseCommit=$OfficialBaseCommit;archive=[ordered]@{file='official-source.zip';sha256=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant();sizeBytes=(Get-Item -LiteralPath $archive).Length};sourceDirectory='WW/source';blobs=$sourceBlobs}
        templates=[ordered]@{repository=$ExpectedTemplateRepository;commit=$ExpectedTemplateCommit;directory='WW/source/ok_templates';blobs=$templateBlobs}
        entry=[ordered]@{pythonwRelativePath='r\Scripts\pythonw.exe';workingDirectoryRelativePath='WW/source';sourceEntry='main.py';arguments='main.py --task 1 --exit';taskClass='src.task.DailyTask.DailyTask'}
        configurationMigration=[ordered]@{copied=$false;reason='No installed tasks, configs, account data, device paths, logs, screenshots, or source files were copied.'}
        multiAccount=[ordered]@{bound=$false;reason='This candidate is single-client DailyTask only. Official MultiAccountDailyTask lacks a safe Manager-specified account selector.'}
        validation=[ordered]@{requirementsInstalled=$true;pipCheck=$true;imports=@('ok','config','src.task.DailyTask','src.task.MultiAccountDailyTask');toolGuiStarted=$false;gameStarted=$false}
    }
    $manifestPath = Join-Path $target 'WW\runtime-preparation.json'
    Write-Utf8Json $manifestPath $manifest
    Write-PreparationEvent 'prepared' 'Official source, pinned template submodule, independent venv, pip check, and import-only probe completed.'
    [ordered]@{status='read-only-import-ready';candidateRoot=$target;manifest=$manifestPath;stdoutLog=$stdoutLog;stderrLog=$stderrLog;toolGuiStarted=$false;gameStarted=$false} | ConvertTo-Json -Compress
} catch {
    $message=$_.Exception.Message; Add-Content -LiteralPath $stderrLog -Value ('PREPARATION FAILURE: '+$message) -Encoding utf8; Write-PreparationEvent 'failed' $message; throw
}
