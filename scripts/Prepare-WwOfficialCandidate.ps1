[CmdletBinding()]
param(
    [string]$OfficialSource = (Join-Path (Split-Path -Parent $PSScriptRoot) 'vendor-baselines\ok-wuthering-waves'),
    [string]$CandidateRoot = 'D:\Game\Tools\Candidates\YeYuGamerCandidates\ww-official-c3fef9a',
    [ValidateSet('c3fef9a06f61f96abeb53571fb47dd578b6f7063', '07a578842b34dda3428535eb46f8b98f76fddb0d', '36c557437de745cdef506bcfa148569d3e09cfcf', 'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d', '20a2d40d0eaa0cfa053ceafecd9a4d04395291f4', '737c89ef00e36248baa0ab92df248af935528b56', '4a1b39d002c5754557bdc7b30782fad0048cd106', '567e71c93c32f03a7bd3ad013205438c0adcba11', '2243d465d58c74f52765b658174bff76165656ee', '6a454cce9e830cd0ecee3fa495e9b8d40c534f28', '1a779fbf3a2211342c6c83f06011b534cbb80466', 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939', '274346512f904b2e2bc2dea78a45982fa0012e83', '93e19b4f455390f50b8e126c23cf4f4be1595adb', 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb', '81edb5af888688be64e879f346b0f2440bacd958', '85e54379b3a6859f30f2a1df6326091f3a51929d', '63186ed17abab2fb808f9f98ff310bdf3144c57b')]
    [string]$SourceRevision = 'c3fef9a06f61f96abeb53571fb47dd578b6f7063',
    [string]$PythonPath = 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe',
    [string]$TemplateSource,
    [switch]$RevalidatePreparedFork
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ExpectedRepository = 'https://github.com/ok-oldking/ok-wuthering-waves.git'
$ExpectedCommit = $SourceRevision
$EmbeddedFork = $SourceRevision -ceq '63186ed17abab2fb808f9f98ff310bdf3144c57b'
$ExpectedSourceRepository = if ($EmbeddedFork) { 'https://github.com/vb2250158/ok-wuthering-waves.git' } else { $ExpectedRepository }
$LocalPatch = $SourceRevision -cin @('20a2d40d0eaa0cfa053ceafecd9a4d04395291f4', '737c89ef00e36248baa0ab92df248af935528b56', '4a1b39d002c5754557bdc7b30782fad0048cd106', '567e71c93c32f03a7bd3ad013205438c0adcba11', '6a454cce9e830cd0ecee3fa495e9b8d40c534f28', '1a779fbf3a2211342c6c83f06011b534cbb80466', 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939', '274346512f904b2e2bc2dea78a45982fa0012e83', '93e19b4f455390f50b8e126c23cf4f4be1595adb', 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb', '81edb5af888688be64e879f346b0f2440bacd958', '85e54379b3a6859f30f2a1df6326091f3a51929d', '63186ed17abab2fb808f9f98ff310bdf3144c57b')
$OfficialBaseCommit = if ($SourceRevision -cin @('6a454cce9e830cd0ecee3fa495e9b8d40c534f28', '1a779fbf3a2211342c6c83f06011b534cbb80466', 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939', '274346512f904b2e2bc2dea78a45982fa0012e83', '93e19b4f455390f50b8e126c23cf4f4be1595adb', 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb', '81edb5af888688be64e879f346b0f2440bacd958', '85e54379b3a6859f30f2a1df6326091f3a51929d', '63186ed17abab2fb808f9f98ff310bdf3144c57b')) { '6ffaa7f0b0b7f3140367de96c8eacba533c96894' } elseif ($LocalPatch) { 'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d' } else { $SourceRevision }
$ExpectedTemplateRepository = 'https://github.com/ok-oldking/ok-wuthering-waves-coco-labeling.git'
$ExpectedTemplateCommit = switch ($SourceRevision) {
    '63186ed17abab2fb808f9f98ff310bdf3144c57b' { '438526eafcf4b1caf78c1dea68ef36b42e81e3c4' }
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
    $lines = Read-GitText @('-c', 'core.quotePath=false', '-C', $Repository, 'ls-tree', '-r', $Revision) "Read blob tree $Label@$Revision"
    $entries = @()
    foreach ($line in ($lines -split "\r?\n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $match = [regex]::Match($line, '^(?<mode>\d+)\s+blob\s+(?<hash>[0-9a-f]{40})\t(?<path>.+)$')
        if ($match.Success) { $entries += [ordered]@{ path=$match.Groups['path'].Value; gitBlobSha1=$match.Groups['hash'].Value } }
    }
    if ($entries.Count -eq 0) { throw "No blob entries found for $Label@$Revision" }
    return @($entries | Sort-Object path)
}

function Assert-ArchivedBlobTree([string]$Root, $Rows) {
    foreach ($row in $Rows) {
        $path = Assert-LocalFile (Join-Path $Root $row.path) 'Archived source blob'
        $stream = [IO.File]::OpenRead($path)
        $hash = [Security.Cryptography.SHA1]::Create()
        try {
            $header = [Text.Encoding]::UTF8.GetBytes("blob $($stream.Length)`0")
            $null = $hash.TransformBlock($header, 0, $header.Length, $header, 0)
            $buffer = [byte[]]::new(1MB)
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) { $null = $hash.TransformBlock($buffer, 0, $read, $buffer, 0) }
            $null = $hash.TransformFinalBlock([byte[]]::new(0), 0, 0)
            if ([Convert]::ToHexString($hash.Hash).ToLowerInvariant() -cne $row.gitBlobSha1) { throw "Archived blob differs from the fixed commit: $($row.path)" }
        } finally { $stream.Dispose(); $hash.Dispose() }
    }
}

$official = Assert-LocalDirectory $OfficialSource 'Audited official WW source'
$python = Assert-LocalFile $PythonPath 'Python 3.12 interpreter'
if ($EmbeddedFork -and $official -cne 'D:\Game\Tools\forks\ok-ww') { throw 'The audited WW fork must use its fixed local source directory.' }
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
if ($EmbeddedFork -and $target -cne 'D:\Game\Automation\YeYuGamer\.cache\upstream-candidates\ww127') { throw 'The audited fork preparation is restricted to the WW127 candidate.' }
$projectCandidates = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSScriptRoot) '.cache\upstream-candidates')).TrimEnd('\') + '\'
if ($target.StartsWith('\\') -or -not (
        ($target + '\').StartsWith($projectCandidates, [StringComparison]::OrdinalIgnoreCase) -or
        ($target + '\').StartsWith('D:\Game\Tools\Candidates\YeYuGamerCandidates\', [StringComparison]::OrdinalIgnoreCase))) {
    throw 'Candidate root must be a new local child of the project upstream-candidates cache or the existing legacy candidate directory.'
}
if ($RevalidatePreparedFork -and (-not $EmbeddedFork -or -not (Test-Path -LiteralPath (Join-Path $target 'WW/runtime-preparation.json')))) { throw 'Revalidation requires a previously prepared fixed WW127 candidate.' }
if ((Test-Path -LiteralPath $target) -and -not $RevalidatePreparedFork) { throw "Candidate target already exists; preparation never deletes or replaces it: $target" }

New-Item -ItemType Directory -Path $target -Force | Out-Null
$logs = Join-Path $target 'logs'; New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stdoutLog = Join-Path $logs 'prepare.stdout.log'; $stderrLog = Join-Path $logs 'prepare.stderr.log'; $statusLog = Join-Path $logs 'prepare.status.jsonl'
Set-Content -LiteralPath $stdoutLog -Value '' -Encoding utf8; Set-Content -LiteralPath $stderrLog -Value '' -Encoding utf8; Set-Content -LiteralPath $statusLog -Value '' -Encoding utf8

try {
    Write-PreparationEvent 'started' 'Validating audited WW source and creating an isolated source candidate.'
    $head = Read-GitText @('-C', $official, 'rev-parse', 'HEAD') 'Read official source commit'
    $remote = Read-GitText @('-C', $official, 'remote', 'get-url', 'origin') 'Read official source remote'
    $dirty = Read-GitText @('-C', $official, 'status', '--porcelain') 'Read official source worktree status'
    if ($head -cne $ExpectedCommit -or $remote -cne $ExpectedSourceRepository -or -not [string]::IsNullOrEmpty($dirty)) { throw "Audited source must be clean $ExpectedSourceRepository at $ExpectedCommit." }
    if ($LocalPatch) { Invoke-LoggedNative 'git' @('-C', $official, 'merge-base', '--is-ancestor', $OfficialBaseCommit, $ExpectedCommit) 'Verify fixed official base of local source patch' }
    $templateGitlink = Read-GitText @('-C', $official, 'ls-tree', 'HEAD', 'ok_templates') 'Read official ok_templates gitlink'
    if ($EmbeddedFork) {
        if ($templateGitlink -cne "040000 tree e3402f3d1f88fd23296e68ad20e108f8397cd7ae`tok_templates") { throw 'Embedded WW template tree differs from the audited tree.' }
    } elseif ($templateGitlink -notmatch "^160000 commit $ExpectedTemplateCommit\s+ok_templates$") { throw 'Official WW ok_templates gitlink does not match the audited commit.' }
    $sourceBlobs = Get-GitBlobManifest $official $ExpectedCommit 'ok-wuthering-waves'
    if ($EmbeddedFork) {
        $templateBlobs = @($sourceBlobs | Where-Object { $_.path.StartsWith('ok_templates/', [StringComparison]::Ordinal) } | ForEach-Object {
            [ordered]@{path=$_.path.Substring(13);gitBlobSha1=$_.gitBlobSha1}
        })
        $sourceBlobs = @($sourceBlobs | Where-Object { -not $_.path.StartsWith('ok_templates/', [StringComparison]::Ordinal) })
        if ($sourceBlobs.Count -ne 318 -or $templateBlobs.Count -ne 165) { throw 'Audited fork source/template counts differ.' }
    }

    $source = Join-Path $target 'WW\source'
    $archive = Join-Path $target 'official-source.zip'
    $archiveArguments = if ($EmbeddedFork) { @('-c', 'core.autocrlf=false', '-C', $official, 'archive', '--format=zip', ("--output=$archive"), $ExpectedCommit) } else { @('-C', $official, 'archive', '--format=zip', ("--output=$archive"), $ExpectedCommit) }
    Invoke-LoggedNative 'git' $archiveArguments 'Archive audited official WW source'
    if (-not $RevalidatePreparedFork) { Expand-Archive -LiteralPath $archive -DestinationPath $source -Force }
    foreach ($required in @('main.py', 'config.py', 'pyproject.toml', 'requirements.txt', 'src\task\DailyTask.py', 'src\task\MultiAccountDailyTask.py')) {
        Assert-LocalFile (Join-Path $source $required) "Archived official WW source $required" | Out-Null
    }
    if (rg -n 'YEYU_GAMER|NightRain|nightrain' $source -g '*.py') { throw 'Official WW source archive unexpectedly contains a YeYu bridge.' }

    $templates = Join-Path $source 'ok_templates'
    if ($EmbeddedFork) {
        Assert-ArchivedBlobTree $source $sourceBlobs
        Assert-ArchivedBlobTree $templates $templateBlobs
    }
    if (-not $EmbeddedFork) {
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
    }

    $venv = Join-Path $target 'r'
    if ($EmbeddedFork) {
        $donor = Assert-LocalDirectory (Join-Path (Split-Path -Parent $target) 'ww126') 'Audited WW126 dependency donor'
        $donorManifestPath = Assert-LocalFile (Join-Path $donor 'WW/runtime-preparation.json') 'WW126 preparation manifest'
        if ((Get-FileHash $donorManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne 'cc72d6422410d4dae22aeff5515af4de38bb27230d1c61dc45aa4e5453da7e93') { throw 'WW126 dependency provenance changed.' }
        $donorManifest = Get-Content $donorManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ($donorManifest.source.commit -cne '88a67e6a1719315954f5120aaf9d6a7819b28d78') { throw 'Unexpected dependency donor source.' }
        Invoke-LoggedNative 'git' @('-C', $official, 'diff', '--exit-code', $donorManifest.source.commit, $ExpectedCommit, '--', 'requirements.txt', 'pyproject.toml', 'uv.lock') 'Verify unchanged audited dependency inputs'
        $copyRows = [Collections.Generic.List[object]]::new()
        if ($RevalidatePreparedFork) {
            $previous = Get-Content (Join-Path $target 'WW/runtime-preparation.json') -Raw -Encoding utf8 | ConvertFrom-Json
            $copyReceiptPath = Assert-LocalFile (Join-Path $logs 'dependency-copy.json') 'Independent dependency copy receipt'
            if ($previous.source.commit -cne $ExpectedCommit -or (Get-FileHash $copyReceiptPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $previous.dependencyReuse.receiptSha256) { throw 'Prepared dependency receipt changed.' }
            $copyReceipt = Get-Content $copyReceiptPath -Raw -Encoding utf8 | ConvertFrom-Json
            if ($copyReceipt.sourceRoot -cne $donor -or $copyReceipt.targetRoot -cne $target -or -not $copyReceipt.independentByteCopy -or $copyReceipt.files.Count -gt 20000) { throw 'Prepared dependency copy ownership differs.' }
            foreach ($row in $copyReceipt.files) {
                if ($row.path -notmatch '^(r/|source-patches/|WW/source/configs/)' -or $row.path -match '(^|/)\.\.(/|$)|[:\\]') { throw 'Unsafe dependency receipt path.' }
                foreach ($parent in @($donor, $target)) {
                    $file = Assert-LocalFile (Join-Path $parent $row.path) 'Copied dependency file'
                    if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $row.sha256) { throw 'Prepared dependency bytes changed.' }
                }
            }
        } else {
        foreach ($relativeRoot in @('r', 'source-patches', 'WW/source/configs')) {
            $from = Join-Path $donor $relativeRoot
            if (-not (Test-Path -LiteralPath $from)) { continue }
            $from = Assert-LocalDirectory $from 'Dependency/configuration donor subtree'
            $items = @(Get-ChildItem -LiteralPath $from -Recurse -Force)
            if (@($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Reparse entries in donor are refused.' }
            foreach ($file in @($items | Where-Object { -not $_.PSIsContainer })) {
                $relative = [IO.Path]::GetRelativePath($donor, $file.FullName)
                $destination = Join-Path $target $relative
                $before = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                if (Test-Path -LiteralPath $destination) { throw 'Dependency copy cannot overwrite archived source.' }
                Copy-Item -LiteralPath $file.FullName -Destination $destination
                if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -cne $before -or
                    (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne $before) { throw 'Dependency bytes changed during independent copy.' }
                $copyRows.Add([ordered]@{path=$relative.Replace('\','/');sizeBytes=$file.Length;sha256=$before})
            }
            if (@(Get-ChildItem -LiteralPath $from -Recurse -Force -File).Count -ne @($items | Where-Object { -not $_.PSIsContainer }).Count) { throw 'Donor inventory changed during copy.' }
        }
        Write-Utf8Json (Join-Path $logs 'dependency-copy.json') ([ordered]@{
            sourceRoot=$donor;targetRoot=$target;sourceManifestSha256='cc72d6422410d4dae22aeff5515af4de38bb27230d1c61dc45aa4e5453da7e93';
            independentByteCopy=$true;files=@($copyRows.ToArray());gameStarted=$false
        })
        }
    } else {
        Invoke-LoggedNative $python @('-m', 'venv', $venv) 'Create independent candidate virtual environment'
    }
    $candidatePython = Assert-LocalFile (Join-Path $venv 'Scripts\python.exe') 'Candidate virtual environment Python'
    $candidatePythonw = Assert-LocalFile (Join-Path $venv 'Scripts\pythonw.exe') 'Candidate virtual environment Pythonw'
    if (-not $EmbeddedFork) { Invoke-LoggedNative $candidatePython @('-m', 'pip', '--disable-pip-version-check', '--no-input', 'install', '-r', (Join-Path $source 'requirements.txt')) 'Install official pinned requirements' }
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
    if ($EmbeddedFork) {
        $manifest.source.forkRepository = $remote
        $manifest.source.forkDirectory = $official
        $manifest.templates.embeddedTree = 'e3402f3d1f88fd23296e68ad20e108f8397cd7ae'
        $manifest.sdkSourcePatch = $donorManifest.sdkSourcePatch
        $manifest.configurationMigration = [ordered]@{copied=$true;sourceRoot=$donor;byteExact=$true;absencePreserved=$true}
        $manifest.validation.requirementsInstalled = $false
        $manifest.validation.dependenciesReused = $true
        $manifest.dependencyReuse = [ordered]@{mode='independent-byte-copy';sourceRoot=$donor;targetRoot=$target;receipt='logs/dependency-copy.json';receiptSha256=(Get-FileHash (Join-Path $logs 'dependency-copy.json') -Algorithm SHA256).Hash.ToLowerInvariant()}
        foreach ($spec in @(
            @('source-patches/ok_script-2.0.8+local.wgcclose.20261007-py3-none-any.whl','bfb4a4f000d41df8ad8e4ed96144c2c8a2895ae6587f9161e4d55c34605ffe07'),
            @('r/Lib/site-packages/ok/device/capture_methods/windows_graphics.py','d6f0d75442b0dc627a5bcfa7b7ae874083826b5a2047de88bc6b7296d4e9a047'),
            @('r/Lib/site-packages/onnxocr/predict_base.py','cf80ceec7f6cb6c116811321a820c37aba012118deb29601bf99ddc61db2b2ec'),
            @('r/Lib/site-packages/onnxocr_ppocrv5-0.0.22.dist-info/RECORD','7b4d4e3c72f1ce5a90491408e92b0ce53f6298953f6d69800c82a25e5f4fb6c5'))) {
            if ((Get-FileHash (Assert-LocalFile (Join-Path $target $spec[0]) 'Audited runtime input') -Algorithm SHA256).Hash.ToLowerInvariant() -cne $spec[1]) { throw 'Copied runtime differs from the audited SDK/OCR inputs.' }
        }
    }
    Write-Utf8Json $manifestPath $manifest
    if ($EmbeddedFork) {
        New-Item -ItemType Directory -Path (Join-Path $target 'mutable/onnxocr') -Force | Out-Null
        Write-Utf8Json (Join-Path $target 'WW/runtime-requalification-ocr-cache.json') ([ordered]@{
            schemaVersion=1;status='runtime-files-requalified';runtimeRoot=$target;parentManifestSha256=(Get-FileHash $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant();
            upstreamRepository='https://github.com/ok-oldking/OnnxOCR.git';officialBaseCommit='1888fd16e3ac3c663e2b25739cb21c388fb8a6fa';upstreamPatchCommit='9fd82161a575a994f70ef5a45e759b66dc7c7224';
            predictBaseSha256='cf80ceec7f6cb6c116811321a820c37aba012118deb29601bf99ddc61db2b2ec';recordSha256='7b4d4e3c72f1ce5a90491408e92b0ce53f6298953f6d69800c82a25e5f4fb6c5';
            cacheDirectoryRelativePath='mutable/onnxocr';qualificationMethod='Independent byte-copy and SHA256 verification; new empty owned cache; no model initialization or warm-hit claim';gameStarted=$false
        })
    }
    Write-PreparationEvent 'prepared' 'Audited source and templates, independent runtime, pip check, and import-only probe completed.'
    [ordered]@{status='read-only-import-ready';candidateRoot=$target;manifest=$manifestPath;stdoutLog=$stdoutLog;stderrLog=$stderrLog;toolGuiStarted=$false;gameStarted=$false} | ConvertTo-Json -Compress
} catch {
    $message=$_.Exception.Message; Add-Content -LiteralPath $stderrLog -Value ('PREPARATION FAILURE: '+$message) -Encoding utf8; Write-PreparationEvent 'failed' $message; throw
}
