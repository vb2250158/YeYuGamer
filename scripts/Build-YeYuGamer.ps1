[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\build'),
    [string]$PythonPath,
    [string]$NodePath,
    [string]$PnpmPath,
    [string]$CSharpCompilerPath,
    [switch]$SkipWebBuild,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

# Trap declarations apply to the entire script scope, including failures that
# occur before the incoming directory exists.
$incomingPromoted = $false
$incomingRoot = $null
$incomingLeaf = $null
$productWorkRoot = $null

if ($SkipWebBuild) {
    throw '-SkipWebBuild is not supported for release builds. A complete WebGUI must be rebuilt and verified.'
}

function Get-YeYuGamerSourceFileSnapshotOnce {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string[]]$ExcludedSegments
    )

    $prefix = $Root.TrimEnd('\') + '\'
    $entries = foreach ($file in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction Stop)) {
        $relative = $file.FullName.Substring($prefix.Length)
        $segments = $relative -split '[\\/]'
        if (@($segments | Where-Object { $ExcludedSegments -contains $_ }).Count -gt 0) { continue }
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Source snapshot contains a reparse-point file: $relative"
        }
        [pscustomobject]@{
            RelativePath = $relative
            FullName = $file.FullName
            Length = [int64]$file.Length
            LastWriteUtcTicks = [int64]$file.LastWriteTimeUtc.Ticks
        }
    }
    return @($entries | Sort-Object RelativePath)
}

function Test-YeYuGamerSourceSnapshotsEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object[]]$Left,
        [Parameter(Mandatory)][object[]]$Right
    )

    if ($Left.Count -ne $Right.Count) { return $false }
    for ($index = 0; $index -lt $Left.Count; $index++) {
        if ([string]$Left[$index].RelativePath -cne [string]$Right[$index].RelativePath -or
            [int64]$Left[$index].Length -ne [int64]$Right[$index].Length -or
            [int64]$Left[$index].LastWriteUtcTicks -ne [int64]$Right[$index].LastWriteUtcTicks) {
            return $false
        }
    }
    return $true
}

function Get-YeYuGamerStableSourceSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string[]]$ExcludedSegments,
        [ValidateRange(1, 5)][int]$MaximumAttempts = 3
    )

    $lastFailure = $null
    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        try {
            $first = @(Get-YeYuGamerSourceFileSnapshotOnce -Root $Root -ExcludedSegments $ExcludedSegments)
            $second = @(Get-YeYuGamerSourceFileSnapshotOnce -Root $Root -ExcludedSegments $ExcludedSegments)
            if (Test-YeYuGamerSourceSnapshotsEqual -Left $first -Right $second) { return $second }
            $lastFailure = 'the source tree changed between two consecutive enumerations'
        } catch {
            $lastFailure = $_.Exception.Message
        }
        if ($attempt -lt $MaximumAttempts) { Start-Sleep -Milliseconds (200 * $attempt) }
    }
    throw "Could not obtain a stable, bounded source snapshot after $MaximumAttempts attempts: $lastFailure"
}

function Test-YeYuGamerSourceEntryUnchanged {
    [CmdletBinding()]
    param([Parameter(Mandatory)][object]$Entry)

    $current = Get-Item -LiteralPath $Entry.FullName -Force -ErrorAction Stop
    return (-not $current.PSIsContainer -and
        [int64]$current.Length -eq [int64]$Entry.Length -and
        [int64]$current.LastWriteTimeUtc.Ticks -eq [int64]$Entry.LastWriteUtcTicks)
}

function Remove-YeYuGamerBuildSiblingSafely {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$ProductWorkRoot,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$AllowedLeafNames
    )

    $resolved = Assert-YeYuGamerChildPath -Parent $ProductWorkRoot -Child $Path -Purpose 'build sibling cleanup'
    $leaf = Split-Path -Leaf $resolved
    if ($leaf -notin $AllowedLeafNames) { throw "Build sibling cleanup rejected an unexpected leaf: $leaf" }
    if (-not (Test-Path -LiteralPath $resolved)) { return }
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Build sibling cleanup target is not a directory: $resolved"
    }
    $sentinelPath = Join-Path $resolved '.yeyu-gamer-owned-directory'
    if (-not (Test-Path -LiteralPath $sentinelPath -PathType Leaf) -or
        [System.IO.File]::ReadAllText($sentinelPath) -cne 'YeYuGamer:Build:v1') {
        throw "Build sibling cleanup target has no valid ownership sentinel: $resolved"
    }
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'build sibling cleanup root' | Out-Null
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Test-YeYuGamerCandidateBuildManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$CandidateRoot,
        [Parameter(Mandatory)][string]$FinalBuildRoot
    )

    $manifestPath = Join-Path $CandidateRoot 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'The candidate build manifest is missing.'
    }
    $manifestText = Get-Content -LiteralPath $manifestPath -Raw
    $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
    if ([int]$manifest.schemaVersion -ne 3) { throw 'The candidate build manifest must use schemaVersion 3.' }
    foreach ($forbiddenProperty in @('sourceRoot', 'buildRoot', 'stagedRoot', 'buildHost', 'userName', 'hostName')) {
        if ($null -ne $manifest.PSObject.Properties[$forbiddenProperty]) {
            throw "The candidate build manifest contains private machine metadata: $forbiddenProperty"
        }
    }
    if ([string]$manifest.layout.application -cne 'staged-app' -or
        [string]$manifest.layout.wheelhouse -cne 'wheelhouse') {
        throw 'The candidate build manifest must use the fixed relative package layout.'
    }
    if ($manifestText -match '(?i)[a-z]:\\\\' -or $manifestText -match '\\\\\\\\[^\\]') {
        throw 'The candidate build manifest contains an absolute drive or UNC path.'
    }
    if ([string]$manifest.wheelhouse.relativePath -cne 'wheelhouse' -or
        [string]$manifest.wheelhouse.lockFile -cne 'requirements.lock' -or
        [string]$manifest.wheelhouse.installMode -cne 'no-index' -or
        -not [bool]$manifest.wheelhouse.requireHashes) {
        throw 'The candidate wheelhouse contract is invalid.'
    }
    $topLevelItems = @(Get-ChildItem -LiteralPath $CandidateRoot -Force)
    # desktop-host-output is a transient PyInstaller work tree. It is checked
    # separately before selected files are copied into the explicit runtime
    # allowlist, so it must not be mistaken for an undeclared source payload.
    $allowedTopLevel = @('.yeyu-gamer-owned-directory', 'build-manifest.json', 'staged-app', 'wheelhouse', 'desktop-host-output')
    $unexpectedTopLevel = @($topLevelItems | Where-Object Name -notin $allowedTopLevel)
    if ($unexpectedTopLevel.Count -gt 0) {
        throw "The candidate build contains an undeclared top-level item: $($unexpectedTopLevel[0].Name)"
    }
    $packagedLock = Join-Path $CandidateRoot 'staged-app\provenance\requirements.lock'
    $wheelhouseLock = Join-Path $CandidateRoot 'wheelhouse\requirements.lock'
    $packagedProvenance = Join-Path $CandidateRoot 'staged-app\provenance\dependency-provenance.json'
    foreach ($dependencyFile in @($packagedLock, $wheelhouseLock, $packagedProvenance)) {
        if (-not (Test-Path -LiteralPath $dependencyFile -PathType Leaf) -or
            (Get-Item -LiteralPath $dependencyFile).Length -le 0) {
            throw "The candidate dependency provenance file is missing or empty: $(Split-Path -Leaf $dependencyFile)"
        }
    }
    $lockHash = (Get-FileHash -LiteralPath $packagedLock -Algorithm SHA256).Hash.ToLowerInvariant()
    $wheelhouseLockHash = (Get-FileHash -LiteralPath $wheelhouseLock -Algorithm SHA256).Hash.ToLowerInvariant()
    $provenanceHash = (Get-FileHash -LiteralPath $packagedProvenance -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([string]$manifest.dependencyLock.sourcePath -cne 'packaging/requirements.cpython312-win_amd64.lock' -or
        [string]$manifest.dependencyLock.packagedPath -cne 'provenance/requirements.lock' -or
        [string]$manifest.dependencyLock.wheelhousePath -cne 'requirements.lock' -or
        [string]$manifest.dependencyLock.target -cne 'cpython312-win_amd64' -or
        [string]$manifest.dependencyLock.sha256 -cne $lockHash -or
        $wheelhouseLockHash -cne $lockHash -or
        [string]$manifest.dependencyLock.provenanceSha256 -cne $provenanceHash) {
        throw 'The candidate dependency lock does not match its source-derived manifest trust anchor.'
    }

    foreach ($treeSpec in @(
        [pscustomobject]@{ Root = (Join-Path $CandidateRoot 'staged-app'); Entries = @($manifest.appFiles); Label = 'app' },
        [pscustomobject]@{ Root = (Join-Path $CandidateRoot 'wheelhouse'); Entries = @($manifest.wheelhouseFiles); Label = 'wheelhouse' }
    )) {
        if (-not (Test-Path -LiteralPath $treeSpec.Root -PathType Container)) {
            throw "The candidate $($treeSpec.Label) root is missing."
        }
        Assert-YeYuGamerNoReparseTree -Path $treeSpec.Root -Purpose "candidate $($treeSpec.Label) tree" | Out-Null
        $declared = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $treeSpec.Entries) {
            $relative = [string]$entry.path
            $segments = $relative -split '[\\/]'
            if (-not $relative -or [System.IO.Path]::IsPathRooted($relative) -or
                @($segments | Where-Object { -not $_ -or $_ -in @('.', '..') }).Count -gt 0 -or
                $declared.ContainsKey($relative)) {
                throw "The candidate $($treeSpec.Label) manifest contains an invalid or duplicate path: $relative"
            }
            $path = Assert-YeYuGamerChildPath -Parent $treeSpec.Root -Child (Join-Path $treeSpec.Root $relative.Replace('/', '\')) -Purpose "candidate $($treeSpec.Label) file"
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Candidate file is missing: $relative" }
            $item = Get-Item -LiteralPath $path -Force
            if ([int64]$entry.bytes -ne [int64]$item.Length) { throw "Candidate file size mismatch: $relative" }
            $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualHash -cne ([string]$entry.sha256).ToLowerInvariant()) { throw "Candidate file hash mismatch: $relative" }
            $declared.Add($relative, $entry)
        }
        $actualFiles = @(Get-ChildItem -LiteralPath $treeSpec.Root -Recurse -File -Force)
        if ($actualFiles.Count -ne $declared.Count) { throw "Candidate $($treeSpec.Label) file count mismatch." }
        foreach ($actualFile in $actualFiles) {
            $relative = $actualFile.FullName.Substring($treeSpec.Root.Length + 1).Replace('\', '/')
            if (-not $declared.ContainsKey($relative)) { throw "Candidate tree contains an undeclared file: $relative" }
        }
    }

    $webIndex = Join-Path $CandidateRoot 'staged-app\webgui\dist\index.html'
    $webAssets = Join-Path $CandidateRoot 'staged-app\webgui\dist\assets'
    if (-not (Test-Path -LiteralPath $webIndex -PathType Leaf) -or (Get-Item -LiteralPath $webIndex).Length -le 0 -or
        -not (Test-Path -LiteralPath $webAssets -PathType Container) -or
        @(Get-ChildItem -LiteralPath $webAssets -Recurse -File | Where-Object Length -gt 0).Count -eq 0) {
        throw 'The candidate WebGUI is incomplete; dist/index.html and non-empty assets are required.'
    }
    $forbiddenRuntimeSegments = @('tests', 'docs', '__pycache__', '.pytest_cache', 'node_modules')
    $forbiddenRuntimeFiles = @(
        'Build-YeYuGamer.ps1',
        'Install-YeYuGamer.ps1',
        'Test-YeYuGamerBuild.ps1',
        'Test-YeYuGamerLifecycle.ps1',
        'Test-YeYuGamerPlatform.ps1',
        'Disable-YeYuGamerLegacyBypassTasks.ps1'
    )
    foreach ($entry in @($manifest.appFiles)) {
        $runtimePath = [string]$entry.path
        $runtimeSegments = $runtimePath -split '/'
        if (@($runtimeSegments | Where-Object { $_ -in $forbiddenRuntimeSegments }).Count -gt 0 -or
            (Split-Path -Leaf $runtimePath) -in $forbiddenRuntimeFiles -or
            $runtimePath -match '(?i)\.(?:py|pyc|pyo|cs|ts|tsx|vue|map|md)$') {
            throw "The candidate runtime allowlist contains a development-only file: $runtimePath"
        }
    }
    return $manifest
}

$sourceResolved = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not (Test-Path -LiteralPath $sourceResolved -PathType Container)) {
    throw "SourceRoot does not exist: $sourceResolved"
}
$finalBuildRoot = Assert-YeYuGamerFixedProductPath -Path $BuildRoot -Kind Build
$sourcePrefixForSafety = $sourceResolved.TrimEnd('\') + '\'
$buildPrefixForSafety = $finalBuildRoot.TrimEnd('\') + '\'
if ($sourceResolved.StartsWith($buildPrefixForSafety, [StringComparison]::OrdinalIgnoreCase) -or
    $finalBuildRoot.StartsWith($sourcePrefixForSafety, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'SourceRoot and BuildRoot may not contain one another.'
}

$productWorkRoot = Split-Path -Parent $finalBuildRoot
$incomingLeaf = 'build.incoming-' + [Guid]::NewGuid().ToString('N')
$incomingRoot = Assert-YeYuGamerChildPath `
    -Parent $productWorkRoot `
    -Child (Join-Path $productWorkRoot $incomingLeaf) `
    -Purpose 'unique incoming build root'
$incomingRoot = Assert-YeYuGamerLocalTarget -Path $incomingRoot -Purpose 'unique incoming build root'
Assert-YeYuGamerNoReparseAncestors -Path $incomingRoot -Purpose 'unique incoming build root' | Out-Null
if (Test-Path -LiteralPath $incomingRoot) { throw "Unique incoming build root already exists: $incomingRoot" }
New-Item -ItemType Directory -Path $incomingRoot -Force | Out-Null
[System.IO.File]::WriteAllText(
    (Join-Path $incomingRoot '.yeyu-gamer-owned-directory'),
    'YeYuGamer:Build:v1',
    [System.Text.UTF8Encoding]::new($false)
)
$buildResolved = $incomingRoot
$incomingPromoted = $false

trap {
    $failure = $_
    if (-not $incomingPromoted -and $incomingRoot -and $productWorkRoot -and
        (Test-Path -LiteralPath $incomingRoot)) {
        Remove-YeYuGamerBuildSiblingSafely `
            -ProductWorkRoot $productWorkRoot `
            -Path $incomingRoot `
            -AllowedLeafNames @($incomingLeaf)
    }
    throw $failure
}

$stagedRoot = Join-Path $buildResolved 'staged-app'
New-Item -ItemType Directory -Path $stagedRoot -Force | Out-Null
$excludedSegments = @('.git', '.venv', '__pycache__', 'node_modules', 'dist', '.pytest_cache')
$sourceSnapshot = @(Get-YeYuGamerStableSourceSnapshot -Root $sourceResolved -ExcludedSegments $excludedSegments)
foreach ($sourceEntry in $sourceSnapshot) {
    $relative = [string]$sourceEntry.RelativePath
    $destination = Join-Path $stagedRoot $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    $copied = $false
    for ($attempt = 1; $attempt -le 3 -and -not $copied; $attempt++) {
        try {
            if (-not (Test-YeYuGamerSourceEntryUnchanged -Entry $sourceEntry)) {
                throw "Source file changed before copy: $relative"
            }
            $sourceHash = (Get-FileHash -LiteralPath $sourceEntry.FullName -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            if (-not (Test-YeYuGamerSourceEntryUnchanged -Entry $sourceEntry)) {
                throw "Source file changed while hashing: $relative"
            }
            Copy-Item -LiteralPath $sourceEntry.FullName -Destination $destination -Force -ErrorAction Stop
            if (-not (Test-YeYuGamerSourceEntryUnchanged -Entry $sourceEntry)) {
                throw "Source file changed during copy: $relative"
            }
            $destinationItem = Get-Item -LiteralPath $destination -Force -ErrorAction Stop
            $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            if ([int64]$destinationItem.Length -ne [int64]$sourceEntry.Length -or $destinationHash -cne $sourceHash) {
                throw "Single-file copy verification failed: $relative"
            }
            $copied = $true
        } catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Milliseconds (150 * $attempt)
        }
    }
}
$sourceSnapshotAfterCopy = @(Get-YeYuGamerStableSourceSnapshot -Root $sourceResolved -ExcludedSegments $excludedSegments)
if (-not (Test-YeYuGamerSourceSnapshotsEqual -Left $sourceSnapshot -Right $sourceSnapshotAfterCopy)) {
    throw 'The source tree changed while the local build snapshot was being copied.'
}

# Build the fixed Manager-owned Adapter Host from the already-local staged
# source.  The host has a typed probe/canary contract and deliberately fails
# execute requests closed until a per-game package has passed promotion.
$adapterHostRoot = Join-Path $stagedRoot 'adapter-host'
$adapterHostSource = Join-Path $adapterHostRoot 'YeYuGamerAdapterHost.cs'
$adapterHostOutput = Join-Path $adapterHostRoot 'host.exe'
if (-not (Test-Path -LiteralPath $adapterHostSource -PathType Leaf)) {
    throw 'The staged Adapter Host source is missing.'
}
if (-not $CSharpCompilerPath) {
    $compilerCandidates = @(
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    )
    $CSharpCompilerPath = $compilerCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}
if (-not $CSharpCompilerPath -or
    -not (Test-Path -LiteralPath $CSharpCompilerPath -PathType Leaf)) {
    throw 'The Windows .NET Framework C# compiler is required to build the Adapter Host.'
}
Invoke-YeYuGamerNative `
    -FilePath $CSharpCompilerPath `
    -Arguments @(
        '/nologo',
        '/optimize+',
        # The Host is a short-lived Manager child and must never allocate a
        # console window. Redirected stdout remains available to the fixed
        # Manager protocol when built as a Windows subsystem executable.
        '/target:winexe',
        '/platform:anycpu',
        '/reference:System.Web.Extensions.dll',
        "/out:$adapterHostOutput",
        $adapterHostSource
    )
if (-not (Test-Path -LiteralPath $adapterHostOutput -PathType Leaf) -or
    (Get-Item -LiteralPath $adapterHostOutput).Length -le 0) {
    throw 'The Adapter Host build did not produce a non-empty host.exe.'
}

function Invoke-YeYuGamerAdapterHostBuildCheck {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    $checkId = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $buildResolved "adapter-host-check-$checkId.stdout"
    $stderrPath = Join-Path $buildResolved "adapter-host-check-$checkId.stderr"
    try {
        $process = Start-Process `
            -FilePath $adapterHostOutput `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru
        $stdout = [System.IO.File]::ReadAllText($stdoutPath)
        $stderr = [System.IO.File]::ReadAllText($stderrPath)
        if ($stderr) {
            throw "The Adapter Host build check wrote stderr: $stderr"
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Document = ($stdout | ConvertFrom-Json)
        }
    } finally {
        foreach ($temporaryPath in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryPath -Force
            }
        }
    }
}

$adapterProbeCheck = Invoke-YeYuGamerAdapterHostBuildCheck `
    -Arguments @('--operation', 'probe', '--protocol-version', '1.1')
$adapterProbe = $adapterProbeCheck.Document
if ($adapterProbeCheck.ExitCode -ne 0 -or
    -not $adapterProbe.success -or
    -not $adapterProbe.hostReady -or
    $adapterProbe.executionReady -or
    $adapterProbe.adapterProcessStarted -or
    $adapterProbe.gameProcessStarted) {
    throw 'The staged Adapter Host failed its no-input probe contract.'
}
$adapterCanaryRunId = [Guid]::NewGuid().ToString('D')
$adapterCanaryCheck = Invoke-YeYuGamerAdapterHostBuildCheck `
    -Arguments @(
        '--operation', 'canary',
        '--protocol-version', '1.1',
        '--run-id', $adapterCanaryRunId,
        '--game-id', 'StarRail'
    )
$adapterCanary = $adapterCanaryCheck.Document
if ($adapterCanaryCheck.ExitCode -ne 0 -or
    -not $adapterCanary.success -or
    $adapterCanary.code -ne 'canary_passed' -or
    $adapterCanary.adapterProcessStarted -or
    $adapterCanary.gameProcessStarted) {
    throw 'The staged Adapter Host failed its no-input canary contract.'
}
if (-not $SkipTests) {
    $adapterProtocolTest = Join-Path $adapterHostRoot 'tests\Test-YeYuGamerAdapterHostProtocol.ps1'
    if (-not (Test-Path -LiteralPath $adapterProtocolTest -PathType Leaf)) {
        throw 'The Adapter Host protocol v1.1 e2e test is missing.'
    }
    $adapterProtocolWork = Join-Path $buildResolved 'adapter-host-protocol-test'
    try {
        & $adapterProtocolTest `
            -SourceRoot $adapterHostRoot `
            -WorkRoot $adapterProtocolWork `
            -CSharpCompilerPath $CSharpCompilerPath | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'The Adapter Host protocol v1.1 fake-runner e2e test failed.'
        }
    } finally {
        if (Test-Path -LiteralPath $adapterProtocolWork -PathType Container) {
            $safeAdapterProtocolWork = Assert-YeYuGamerChildPath `
                -Parent $buildResolved `
                -Child $adapterProtocolWork `
                -Purpose 'Adapter Host protocol build-test cleanup'
            Assert-YeYuGamerNoReparseTree -Path $safeAdapterProtocolWork -Purpose 'Adapter Host protocol build-test cleanup' | Out-Null
            Remove-Item -LiteralPath $safeAdapterProtocolWork -Recurse -Force
        }
    }
}

$python = Get-YeYuGamerPython -ExplicitPath $PythonPath
$pythonTargetJson = & $python -c "import json,platform,struct,sys; print(json.dumps({'implementation':sys.implementation.name,'major':sys.version_info.major,'minor':sys.version_info.minor,'bits':struct.calcsize('P')*8,'platform':sys.platform,'machine':platform.machine()}))"
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the release Python target.' }
$pythonTarget = $pythonTargetJson | ConvertFrom-Json -ErrorAction Stop
if ([string]$pythonTarget.implementation -cne 'cpython' -or
    [int]$pythonTarget.major -ne 3 -or [int]$pythonTarget.minor -ne 12 -or
    [int]$pythonTarget.bits -ne 64 -or [string]$pythonTarget.platform -cne 'win32' -or
    [string]$pythonTarget.machine -notin @('AMD64', 'x86_64')) {
    throw 'Release dependencies are locked for 64-bit CPython 3.12 on Windows (win_amd64).'
}
$platformRoot = Join-Path $stagedRoot 'platform'
if (-not (Test-Path -LiteralPath (Join-Path $platformRoot 'pyproject.toml'))) {
    throw 'The staged platform package is missing pyproject.toml.'
}
$wheelRoot = Join-Path $stagedRoot 'packages\platform'
New-Item -ItemType Directory -Path $wheelRoot -Force | Out-Null
$hasWheelBackend = $false
& $python -c "import setuptools.build_meta, wheel" *> $null
if ($LASTEXITCODE -eq 0) { $hasWheelBackend = $true }
if (-not $hasWheelBackend) {
    throw 'setuptools and wheel are required for a complete installable build.'
}
Push-Location $platformRoot
try {
    Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $wheelRoot, '.')
} finally { Pop-Location }
$platformWheels = @(Get-ChildItem -LiteralPath $wheelRoot -File -Filter 'yeyu_gamer_platform-*.whl')
if ($platformWheels.Count -ne 1 -or $platformWheels[0].Length -le 0) {
    throw "The staged platform build must produce exactly one non-empty wheel; found $($platformWheels.Count)."
}
$generatedPaths = @((Join-Path $platformRoot 'build'))
$generatedPaths += @(Get-ChildItem -LiteralPath $platformRoot -Directory -Filter '*.egg-info' |
    Select-Object -ExpandProperty FullName)
foreach ($generatedPath in $generatedPaths) {
    if ($generatedPath -and (Test-Path -LiteralPath $generatedPath)) {
        $safeGeneratedPath = Assert-YeYuGamerChildPath -Parent $buildResolved -Child $generatedPath -Purpose 'generated Python packaging directory'
        Remove-Item -LiteralPath $safeGeneratedPath -Recurse -Force
    }
}

$webRoot = Join-Path $stagedRoot 'webgui'
if (-not (Test-Path -LiteralPath (Join-Path $webRoot 'package.json') -PathType Leaf)) {
    throw 'The staged WebGUI package.json is missing; a release build may not omit the WebGUI.'
}
if (Test-Path -LiteralPath (Join-Path $webRoot 'package.json') -PathType Leaf) {
    if (-not $NodePath) {
        $node = Get-Command node.exe -ErrorAction SilentlyContinue
        if ($node) { $NodePath = $node.Source }
    }
    if (-not $NodePath -and $env:USERPROFILE) {
        $candidate = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $NodePath = $candidate }
    }
    if (-not $PnpmPath) {
        $pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
        if ($pnpm) { $PnpmPath = $pnpm.Source }
    }
    if (-not $PnpmPath -and $env:USERPROFILE) {
        $candidate = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $PnpmPath = $candidate }
    }
    if (-not $NodePath -or -not (Test-Path -LiteralPath $NodePath -PathType Leaf)) {
        throw 'node.exe is required to build the WebGUI. Pass -NodePath when it is not on PATH.'
    }
    if (-not $PnpmPath -or -not (Test-Path -LiteralPath $PnpmPath -PathType Leaf)) {
        throw 'pnpm.cmd is required to build the WebGUI. Pass -PnpmPath when it is not on PATH.'
    }

    # The Node bundled with the packaged Codex desktop app is subject to
    # Windows package file-system virtualization. It resolves paths below
    # LOCALAPPDATA into the package's LocalCache\Local tree. The ordinary and
    # virtual paths are two views of the same redirected build content, so the
    # BuildRoot cleanup above is the single cleanup authority. Discover the
    # virtual view for Node tool execution without deleting either view here.
    $codexVirtualWebRoots = @()
    $usingCodexBundledNode = $false
    if ($env:USERPROFILE -and $env:LOCALAPPDATA) {
        $codexRuntimeRoot = [System.IO.Path]::GetFullPath(
            (Join-Path $env:USERPROFILE '.cache\codex-runtimes')
        ).TrimEnd('\') + '\'
        $nodeResolved = [System.IO.Path]::GetFullPath($NodePath)
        $usingCodexBundledNode = $nodeResolved.StartsWith(
            $codexRuntimeRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
        $localAppDataResolved = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\') + '\'
        if ($usingCodexBundledNode -and $webRoot.StartsWith(
            $localAppDataResolved,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            $relativeLocalWebRoot = $webRoot.Substring($localAppDataResolved.Length)
            $packagesRoot = Join-Path $env:LOCALAPPDATA 'Packages'
            if (Test-Path -LiteralPath $packagesRoot -PathType Container) {
                foreach ($packageRoot in @(Get-ChildItem -LiteralPath $packagesRoot -Directory -Filter 'OpenAI.Codex_*')) {
                    $virtualLocalRoot = Join-Path $packageRoot.FullName 'LocalCache\Local'
                    if (-not (Test-Path -LiteralPath $virtualLocalRoot -PathType Container)) { continue }
                    $virtualWebRoot = Join-Path $virtualLocalRoot $relativeLocalWebRoot
                    $virtualWebRoot = Assert-YeYuGamerChildPath `
                        -Parent $virtualLocalRoot `
                        -Child $virtualWebRoot `
                        -Purpose 'Codex virtualized WebGUI build root'
                    $virtualWebRoot = Assert-YeYuGamerLocalTarget `
                        -Path $virtualWebRoot `
                        -Purpose 'Codex virtualized WebGUI build root'
                    $codexVirtualWebRoots += $virtualWebRoot
                }
            }
        }
    }

    $oldPath = $env:PATH
    $env:PATH = (Split-Path -Parent $NodePath) + [System.IO.Path]::PathSeparator + $oldPath
    Push-Location $webRoot
    try {
        if (Test-Path -LiteralPath (Join-Path $webRoot 'pnpm-lock.yaml')) {
            Invoke-YeYuGamerNative `
                -FilePath $PnpmPath `
                -Arguments @('install', '--frozen-lockfile', '--config.node-linker=hoisted')
        } else {
            Invoke-YeYuGamerNative `
                -FilePath $PnpmPath `
                -Arguments @('install', '--config.node-linker=hoisted')
        }

        $nodeExecutionWebRoot = $webRoot
        if ($usingCodexBundledNode -and $codexVirtualWebRoots.Count -gt 0) {
            $virtualMatches = @($codexVirtualWebRoots | Where-Object {
                Test-Path -LiteralPath (Join-Path $_ 'node_modules\vite\bin\vite.js') -PathType Leaf
            })
            if ($virtualMatches.Count -gt 0) {
                $nodeExecutionWebRoot = $virtualMatches[0]
            } else {
                Write-Warning 'Codex Node virtualization was detected, but pnpm did not create a virtual dependency tree; using the ordinary local build path.'
            }
        }

        # The Codex-bundled pnpm wrapper performs a second default-linker
        # install before `pnpm run`, which replaces hoisted packages with
        # symlinks that the packaged Node runtime cannot follow. Invoke the
        # locally installed, typed build tools directly with the selected Node.
        $vueTsc = Join-Path $nodeExecutionWebRoot 'node_modules\vue-tsc\bin\vue-tsc.js'
        $vite = Join-Path $nodeExecutionWebRoot 'node_modules\vite\bin\vite.js'
        if (-not (Test-Path -LiteralPath $vueTsc -PathType Leaf) -or
            -not (Test-Path -LiteralPath $vite -PathType Leaf)) {
            throw 'pnpm install did not produce the expected local Vue build tools.'
        }
        Push-Location $nodeExecutionWebRoot
        try {
            Invoke-YeYuGamerNative -FilePath $NodePath -Arguments @(
                $vueTsc,
                '--noEmit',
                '-p',
                (Join-Path $nodeExecutionWebRoot 'tsconfig.app.json')
            )
            if (-not $SkipTests) {
                $vitest = Join-Path $nodeExecutionWebRoot 'node_modules\vitest\vitest.mjs'
                if (-not (Test-Path -LiteralPath $vitest -PathType Leaf)) {
                    throw 'pnpm install did not produce the expected Vitest runner.'
                }
                Invoke-YeYuGamerNative -FilePath $NodePath -Arguments @($vitest, 'run')
            }
            Invoke-YeYuGamerNative -FilePath $NodePath -Arguments @($vite, 'build')
        } finally {
            Pop-Location
        }

        $regularDist = Join-Path $webRoot 'dist'
        if (-not (Test-Path -LiteralPath $regularDist -PathType Container)) {
            throw 'The WebGUI build completed without producing dist in the staged build.'
        }
        $webIndex = Join-Path $regularDist 'index.html'
        $webAssets = Join-Path $regularDist 'assets'
        if (-not (Test-Path -LiteralPath $webIndex -PathType Leaf) -or
            (Get-Item -LiteralPath $webIndex).Length -le 0 -or
            -not (Test-Path -LiteralPath $webAssets -PathType Container) -or
            @(Get-ChildItem -LiteralPath $webAssets -Recurse -File | Where-Object Length -gt 0).Count -eq 0) {
            throw 'The WebGUI build is incomplete; non-empty dist/index.html and assets are required.'
        }
    } finally {
        Pop-Location
        $env:PATH = $oldPath
    }
    $nodeModules = Join-Path $webRoot 'node_modules'
    if (Test-Path -LiteralPath $nodeModules) {
        $safeNodeModules = Assert-YeYuGamerChildPath -Parent $buildResolved -Child $nodeModules -Purpose 'staged WebGUI dependencies'
        Remove-Item -LiteralPath $safeNodeModules -Recurse -Force
    }
}

$backendRoot = Join-Path $stagedRoot 'backend'
if (-not (Test-Path -LiteralPath $backendRoot -PathType Container)) {
    throw 'The staged Manager backend is missing.'
}
if (-not $SkipTests) {
    if (Test-Path -LiteralPath (Join-Path $backendRoot 'tests') -PathType Container) {
        Push-Location $backendRoot
        try {
            Invoke-YeYuGamerNative `
                -FilePath $python `
                -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py')
        } finally {
            Pop-Location
        }
    }
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $platformRoot
        Invoke-YeYuGamerNative `
            -FilePath $python `
            -Arguments @('-m', 'compileall', '-q', $platformRoot)
        Invoke-YeYuGamerNative `
            -FilePath $python `
            -Arguments @('-m', 'unittest', 'discover', '-s', (Join-Path $platformRoot 'tests'), '-v')
    } finally { $env:PYTHONPATH = $oldPythonPath }
}

# Build the Manager wheel from the already-local staged source.  It is included
# in the signed manifest so installation never runs a package build inside the
# active application tree.
$backendWheelRoot = Join-Path $backendRoot 'dist'
New-Item -ItemType Directory -Path $backendWheelRoot -Force | Out-Null
Push-Location $backendRoot
try {
    Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $backendWheelRoot, '.')
} finally {
    Pop-Location
}
$backendWheels = @(Get-ChildItem `
    -LiteralPath $backendWheelRoot `
    -File `
    -Filter 'yeyu_gamer_manager-*.whl')
if ($backendWheels.Count -ne 1 -or $backendWheels[0].Length -le 0) {
    throw "The staged Manager build must produce exactly one non-empty wheel; found $($backendWheels.Count)."
}
$backendGeneratedPaths = @((Join-Path $backendRoot 'build'))
$backendGeneratedPaths += @(Get-ChildItem -LiteralPath $backendRoot -Directory -Filter '*.egg-info' |
    Select-Object -ExpandProperty FullName)
foreach ($generatedPath in $backendGeneratedPaths) {
    if ($generatedPath -and (Test-Path -LiteralPath $generatedPath)) {
        $safeGeneratedPath = Assert-YeYuGamerChildPath `
            -Parent $buildResolved `
            -Child $generatedPath `
            -Purpose 'generated Manager packaging directory'
        Remove-Item -LiteralPath $safeGeneratedPath -Recurse -Force
    }
}

# Download the complete third-party closure only from the pre-committed source
# lock.  Hashes are never generated from whatever an index happens to resolve
# during this build: the reviewed lock is the release trust anchor.
$wheelhouseRoot = Join-Path $buildResolved 'wheelhouse'
New-Item -ItemType Directory -Path $wheelhouseRoot -Force | Out-Null
$backendRequirementsPath = Join-Path $backendRoot 'requirements.txt'
$platformRequirementsPath = Join-Path $platformRoot 'requirements.txt'
foreach ($requirementsPath in @($backendRequirementsPath, $platformRequirementsPath)) {
    if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
        throw "A wheelhouse requirements input is missing: $requirementsPath"
    }
}
$dependencyLockSourcePath = Join-Path $stagedRoot 'packaging\requirements.cpython312-win_amd64.lock'
$dependencyProvenanceSourcePath = Join-Path $stagedRoot 'packaging\dependency-provenance.cpython312-win_amd64.json'
foreach ($dependencySourcePath in @($dependencyLockSourcePath, $dependencyProvenanceSourcePath)) {
    if (-not (Test-Path -LiteralPath $dependencySourcePath -PathType Leaf) -or
        (Get-Item -LiteralPath $dependencySourcePath).Length -le 0) {
        throw "A pre-committed dependency trust-anchor file is missing or empty: $(Split-Path -Leaf $dependencySourcePath)"
    }
}
$dependencyLockText = [System.IO.File]::ReadAllText($dependencyLockSourcePath, [System.Text.Encoding]::UTF8)
if ($dependencyLockText.Contains("`r") -or
    -not $dependencyLockText.EndsWith("`n", [StringComparison]::Ordinal)) {
    throw 'The pre-committed dependency lock must use canonical LF line endings and one final LF.'
}
$dependencyLockLines = @($dependencyLockText.Substring(0, $dependencyLockText.Length - 1).Split("`n"))
if ($dependencyLockLines.Count -lt 2 -or $dependencyLockLines[0] -cne '--only-binary=:all:') {
    throw 'The pre-committed dependency lock must begin with the wheel-only directive.'
}
$dependencyPackages = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$dependencyLockRecords = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($dependencyLockLine in $dependencyLockLines[1..($dependencyLockLines.Count - 1)]) {
    if ($dependencyLockLine -notmatch '\A([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*) --hash=sha256:([0-9a-f]{64})\z') {
        throw "The pre-committed dependency lock contains a non-canonical record: $dependencyLockLine"
    }
    $canonicalDependency = ([regex]::Replace($Matches[1], '[-_.]+', '-')).ToLowerInvariant()
    if (-not $dependencyPackages.Add($canonicalDependency)) {
        throw "The pre-committed dependency lock contains a duplicate package: $($Matches[1])"
    }
    $dependencyLockRecords.Add($canonicalDependency, [pscustomobject]@{
        Name = $Matches[1]
        Version = $Matches[2]
        Sha256 = $Matches[3]
    })
}
$dependencyLockHash = (Get-FileHash -LiteralPath $dependencyLockSourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
$dependencyProvenanceHash = (Get-FileHash -LiteralPath $dependencyProvenanceSourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
$dependencyProvenance = Get-Content -LiteralPath $dependencyProvenanceSourcePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([int]$dependencyProvenance.schemaVersion -ne 1 -or
    [string]$dependencyProvenance.lockFile -cne 'requirements.cpython312-win_amd64.lock' -or
    [string]$dependencyProvenance.lockSha256 -cne $dependencyLockHash -or
    [string]$dependencyProvenance.target.implementation -cne 'CPython' -or
    [string]$dependencyProvenance.target.pythonVersion -cne '3.12' -or
    [string]$dependencyProvenance.target.abi -cne 'cp312' -or
    [string]$dependencyProvenance.target.platform -cne 'win_amd64' -or
    @($dependencyProvenance.artifacts).Count -ne $dependencyPackages.Count) {
    throw 'The committed dependency provenance does not match the CPython 3.12 Windows lock.'
}
foreach ($inputContract in @(
    [pscustomobject]@{ Path = 'backend/requirements.txt'; FullName = $backendRequirementsPath },
    [pscustomobject]@{ Path = 'platform/requirements.txt'; FullName = $platformRequirementsPath }
)) {
    $provenanceInputs = @($dependencyProvenance.inputFiles | Where-Object { [string]$_.path -ceq $inputContract.Path })
    $actualInputHash = (Get-FileHash -LiteralPath $inputContract.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($provenanceInputs.Count -ne 1 -or [string]$provenanceInputs[0].sha256 -cne $actualInputHash) {
        throw "The dependency root input changed without regenerating and reviewing the committed lock: $($inputContract.Path)"
    }
}
$wheelhouseLockPath = Join-Path $wheelhouseRoot 'requirements.lock'
Copy-Item -LiteralPath $dependencyLockSourcePath -Destination $wheelhouseLockPath -Force
Invoke-YeYuGamerNative `
    -FilePath $python `
    -Arguments @(
        '-m', 'pip', 'download',
        '--disable-pip-version-check',
        '--only-binary=:all:',
        '--require-hashes',
        '--dest', $wheelhouseRoot,
        '--requirement', $dependencyLockSourcePath
    )
$downloadedDependencyFiles = @(Get-ChildItem -LiteralPath $wheelhouseRoot -File -Force)
if (@($downloadedDependencyFiles | Where-Object {
    $_.Name -cne 'requirements.lock' -and $_.Extension -cne '.whl'
}).Count -gt 0) {
    throw 'The dependency wheelhouse may contain only the source-derived lock and binary wheels.'
}
$declaredArtifacts = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($artifact in @($dependencyProvenance.artifacts)) {
    $artifactName = [string]$artifact.filename
    if (-not $artifactName -or $artifactName -match '[\\/]' -or -not $artifactName.EndsWith('.whl', [StringComparison]::OrdinalIgnoreCase) -or
        $declaredArtifacts.ContainsKey($artifactName)) {
        throw "The committed dependency provenance contains an invalid artifact filename: $artifactName"
    }
    $canonicalArtifactPackage = ([regex]::Replace([string]$artifact.name, '[-_.]+', '-')).ToLowerInvariant()
    if (-not $dependencyLockRecords.ContainsKey($canonicalArtifactPackage)) {
        throw "The committed provenance artifact is not present in the dependency lock: $artifactName"
    }
    $lockedArtifact = $dependencyLockRecords[$canonicalArtifactPackage]
    if ([string]$artifact.version -cne [string]$lockedArtifact.Version -or
        [string]$artifact.sha256 -cne [string]$lockedArtifact.Sha256) {
        throw "The committed provenance artifact differs from its dependency lock record: $artifactName"
    }
    $artifactPath = Join-Path $wheelhouseRoot $artifactName
    if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
        throw "The hash-locked wheelhouse is missing an expected artifact: $artifactName"
    }
    $artifactItem = Get-Item -LiteralPath $artifactPath -Force
    $artifactHash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([int64]$artifact.bytes -ne [int64]$artifactItem.Length -or
        [string]$artifact.sha256 -cne $artifactHash) {
        throw "The hash-locked wheelhouse artifact differs from committed provenance: $artifactName"
    }
    $declaredArtifacts.Add($artifactName, $artifact)
}
$actualWheelFiles = @(Get-ChildItem -LiteralPath $wheelhouseRoot -File -Filter '*.whl' -Force)
if ($actualWheelFiles.Count -ne $declaredArtifacts.Count) {
    throw "The hash-locked wheelhouse file count differs from committed provenance: actual=$($actualWheelFiles.Count), declared=$($declaredArtifacts.Count)"
}
foreach ($actualWheelFile in $actualWheelFiles) {
    if (-not $declaredArtifacts.ContainsKey($actualWheelFile.Name)) {
        throw "The hash-locked wheelhouse contains an undeclared wheel: $($actualWheelFile.Name)"
    }
}

# Prove that the locked dependency set resolves without an index and without
# building source distributions.  --dry-run prevents mutation of the build venv.
Invoke-YeYuGamerNative `
    -FilePath $python `
    -Arguments @(
        '-m', 'pip', 'install',
        '--disable-pip-version-check',
        '--dry-run',
        '--ignore-installed',
        '--no-index',
        '--find-links', $wheelhouseRoot,
        '--require-hashes',
        '--requirement', $wheelhouseLockPath
    )

$openApiExporter = Join-Path $backendRoot 'scripts\export_openapi.py'
if (-not (Test-Path -LiteralPath $openApiExporter -PathType Leaf)) {
    throw 'The staged Manager OpenAPI exporter is missing.'
}
$contractsRoot = Join-Path $stagedRoot 'contracts'
$openApiPath = Join-Path $contractsRoot 'openapi.v1.json'
$openApiHashPath = "$openApiPath.sha256"
$oldBackendPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $backendRoot
    Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @($openApiExporter, '--output', $openApiPath)
} finally {
    $env:PYTHONPATH = $oldBackendPythonPath
}
if (-not (Test-Path -LiteralPath $openApiPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $openApiHashPath -PathType Leaf)) {
    throw 'The Manager OpenAPI exporter did not produce both contract files.'
}
$openApiHashLine = [System.IO.File]::ReadAllText($openApiHashPath).TrimEnd("`r", "`n")
if ($openApiHashLine -notmatch '^([0-9a-f]{64})  openapi\.v1\.json$') {
    throw 'The Manager OpenAPI digest file is not a canonical 64-character SHA-256 record.'
}
$declaredOpenApiHash = $Matches[1]
$actualOpenApiHash = (Get-FileHash -LiteralPath $openApiPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($declaredOpenApiHash -ne $actualOpenApiHash) {
    throw 'The exported Manager OpenAPI digest does not match openapi.v1.json.'
}

# The desktop host is the only user-facing application process. It embeds the
# Manager and the already-built WebGUI assets; wheels remain provenance and
# CLI compatibility material, never the normal UI launcher.
$desktopHostEntry = Join-Path $platformRoot 'desktop_host_entry.py'
if (-not (Test-Path -LiteralPath $desktopHostEntry -PathType Leaf)) {
    throw 'The single-process desktop host entry point is missing.'
}
$desktopHostRoot = Join-Path $buildResolved 'desktop-host-output'
if (Test-Path -LiteralPath $desktopHostRoot) {
    throw 'The temporary desktop-host output unexpectedly already exists.'
}
$desktopHostDist = Join-Path $desktopHostRoot 'dist'
$desktopHostWork = Join-Path $desktopHostRoot 'work'
$desktopHostSpec = Join-Path $desktopHostRoot 'spec'
# The staged build root can be redirected into a deep application-cache path.
# Qt's bundled qml objects then exceed the Windows default path limit while pip
# installs them.  Keep this disposable packaging venv at a deliberately short
# local path; the resulting executable is still copied from the staged output.
$desktopHostVenv = Join-Path ([System.IO.Path]::GetPathRoot([Environment]::SystemDirectory)) (
    'ygb-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
)
Invoke-YeYuGamerNative -FilePath $python -Arguments @('-m', 'venv', $desktopHostVenv)
$desktopHostPython = Join-Path $desktopHostVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $desktopHostPython -PathType Leaf)) {
    throw 'The isolated desktop-host build environment was not created.'
}

# PyInstaller is a release-time dependency, not an assumption about the
# developer Python.  Install the complete, already hash-locked wheelhouse into
# the disposable packaging venv before invoking it.
Invoke-YeYuGamerNative `
    -FilePath $desktopHostPython `
    -Arguments @(
        '-m', 'pip', 'install',
        '--disable-pip-version-check',
        '--no-index',
        '--find-links', $wheelhouseRoot,
        '--require-hashes',
        '--requirement', $wheelhouseLockPath
    )
$pyInstallerProbe = & $desktopHostPython -c 'import PyInstaller; print(PyInstaller.__version__)'
if ($LASTEXITCODE -ne 0 -or -not $pyInstallerProbe) {
    throw 'The hash-locked desktop-host build environment does not contain PyInstaller.'
}
Invoke-YeYuGamerNative `
    -FilePath $desktopHostPython `
    -Arguments @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--noconsole',
        '--name', 'YeYuGamer', '--distpath', $desktopHostDist,
        '--workpath', $desktopHostWork, '--specpath', $desktopHostSpec,
        '--paths', $platformRoot, '--paths', $backendRoot,
        '--add-data', ((Join-Path $webRoot 'dist') + ';webgui'),
        '--collect-submodules', 'yeyu_gamer_manager',
        '--collect-submodules', 'yeyu_gamer_platform',
        $desktopHostEntry
    )
$desktopHostPublishedRoot = Join-Path $desktopHostDist 'YeYuGamer'
$desktopHostExecutable = Join-Path $desktopHostPublishedRoot 'YeYuGamer.exe'
if (-not (Test-Path -LiteralPath $desktopHostExecutable -PathType Leaf) -or
    (Get-Item -LiteralPath $desktopHostExecutable).Length -le 0) {
    throw 'PyInstaller did not produce a non-empty YeYuGamer.exe desktop host.'
}
Assert-YeYuGamerNoReparseTree -Path $desktopHostPublishedRoot -Purpose 'desktop-host package' | Out-Null

foreach ($cacheName in @('__pycache__', '.pytest_cache')) {
    Get-ChildItem -LiteralPath $stagedRoot -Directory -Filter $cacheName -Recurse |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object {
            $safeCache = Assert-YeYuGamerChildPath -Parent $buildResolved -Child $_.FullName -Purpose 'staged test or Python cache'
            Remove-Item -LiteralPath $safeCache -Recurse -Force
    }
}

# Build the final runtime tree from an explicit positive allowlist.  The broad
# source snapshot exists only long enough to compile and test; it is never the
# released application payload.
$runtimePackageRoot = Join-Path $buildResolved 'runtime-package'
if (Test-Path -LiteralPath $runtimePackageRoot) {
    throw 'The temporary runtime package root unexpectedly already exists.'
}
New-Item -ItemType Directory -Path $runtimePackageRoot -Force | Out-Null
$runtimeFileSpecs = [System.Collections.Generic.List[object]]::new()
function Add-YeYuGamerRuntimeFileSpec {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$RelativePath
    )
    $runtimeFileSpecs.Add([pscustomobject]@{ Source = $Source; RelativePath = $RelativePath })
}

Add-YeYuGamerRuntimeFileSpec -Source $adapterHostOutput -RelativePath 'adapter-host/host.exe'
Add-YeYuGamerRuntimeFileSpec -Source $openApiPath -RelativePath 'contracts/openapi.v1.json'
Add-YeYuGamerRuntimeFileSpec -Source $openApiHashPath -RelativePath 'contracts/openapi.v1.json.sha256'
Add-YeYuGamerRuntimeFileSpec -Source $backendWheels[0].FullName -RelativePath ('backend/dist/' + $backendWheels[0].Name)
Add-YeYuGamerRuntimeFileSpec -Source $platformWheels[0].FullName -RelativePath ('packages/platform/' + $platformWheels[0].Name)
Add-YeYuGamerRuntimeFileSpec -Source $dependencyLockSourcePath -RelativePath 'provenance/requirements.lock'
Add-YeYuGamerRuntimeFileSpec -Source $dependencyProvenanceSourcePath -RelativePath 'provenance/dependency-provenance.json'
foreach ($desktopHostFile in @(Get-ChildItem -LiteralPath $desktopHostPublishedRoot -Recurse -File -Force)) {
    $desktopHostRelative = $desktopHostFile.FullName.Substring($desktopHostPublishedRoot.Length + 1).Replace('\', '/')
    Add-YeYuGamerRuntimeFileSpec -Source $desktopHostFile.FullName -RelativePath "desktop-host/$desktopHostRelative"
}

$productionScriptNames = @(
    'YeYuGamer.Common.ps1',
    'Start-YeYuGamer.ps1',
    'Stop-YeYuGamer.ps1',
    'Restart-YeYuGamer.ps1',
    'Disable-YeYuGamerAutoStart.ps1',
    'Set-YeYuGamerNotificationSecret.ps1',
    'Invoke-YeYuGamerScheduledDaily.ps1',
    'Register-YeYuGamerDailySchedule.ps1'
)
foreach ($productionScriptName in $productionScriptNames) {
    $productionScriptPath = Join-Path $stagedRoot "scripts\$productionScriptName"
    Add-YeYuGamerRuntimeFileSpec -Source $productionScriptPath -RelativePath "scripts/$productionScriptName"
}
$webDistRoot = Join-Path $webRoot 'dist'
foreach ($webFile in @(Get-ChildItem -LiteralPath $webDistRoot -Recurse -File -Force)) {
    $webRelative = $webFile.FullName.Substring($webDistRoot.Length + 1).Replace('\', '/')
    Add-YeYuGamerRuntimeFileSpec -Source $webFile.FullName -RelativePath "webgui/dist/$webRelative"
}

$runtimeDestinations = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($runtimeFileSpec in $runtimeFileSpecs) {
    $runtimeRelative = [string]$runtimeFileSpec.RelativePath
    $runtimeSegments = $runtimeRelative -split '/'
    if (-not $runtimeRelative -or [System.IO.Path]::IsPathRooted($runtimeRelative) -or
        @($runtimeSegments | Where-Object { -not $_ -or $_ -in @('.', '..') }).Count -gt 0 -or
        -not $runtimeDestinations.Add($runtimeRelative)) {
        throw "The runtime package allowlist contains an invalid or duplicate path: $runtimeRelative"
    }
    $runtimeSource = Assert-YeYuGamerChildPath -Parent $buildResolved -Child ([string]$runtimeFileSpec.Source) -Purpose 'runtime allowlist source'
    if (-not (Test-Path -LiteralPath $runtimeSource -PathType Leaf)) {
        throw "A runtime allowlist source is missing: $runtimeRelative"
    }
    $runtimeSourceItem = Get-Item -LiteralPath $runtimeSource -Force
    if (($runtimeSourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "A runtime allowlist source is a reparse point: $runtimeRelative"
    }
    $runtimeDestination = Assert-YeYuGamerChildPath `
        -Parent $runtimePackageRoot `
        -Child (Join-Path $runtimePackageRoot $runtimeRelative.Replace('/', '\')) `
        -Purpose 'runtime allowlist destination'
    New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeDestination) -Force | Out-Null
    Copy-Item -LiteralPath $runtimeSource -Destination $runtimeDestination -Force
    $runtimeDestinationItem = Get-Item -LiteralPath $runtimeDestination -Force
    if ([int64]$runtimeSourceItem.Length -ne [int64]$runtimeDestinationItem.Length -or
        (Get-FileHash -LiteralPath $runtimeSource -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath $runtimeDestination -Algorithm SHA256).Hash) {
        throw "A runtime allowlist file changed while being copied: $runtimeRelative"
    }
}

$runtimeActualFiles = @(Get-ChildItem -LiteralPath $runtimePackageRoot -Recurse -File -Force)
if ($runtimeActualFiles.Count -ne $runtimeDestinations.Count) {
    throw "The runtime package contains files outside the positive allowlist: actual=$($runtimeActualFiles.Count), allowlisted=$($runtimeDestinations.Count)"
}
$runtimeTopLevel = @($runtimeActualFiles | ForEach-Object {
    $_.FullName.Substring($runtimePackageRoot.Length + 1).Split('\')[0]
} | Sort-Object -Unique)
$expectedRuntimeTopLevel = @('adapter-host', 'backend', 'contracts', 'desktop-host', 'packages', 'provenance', 'scripts', 'webgui')
if (($runtimeTopLevel -join '|') -cne ($expectedRuntimeTopLevel -join '|')) {
    throw "The runtime package top-level allowlist differs from the release contract: $($runtimeTopLevel -join ', ')"
}
$forbiddenRuntimeSegments = @('tests', 'docs', '__pycache__', '.pytest_cache', 'node_modules')
foreach ($runtimeFile in $runtimeActualFiles) {
    $runtimeRelative = $runtimeFile.FullName.Substring($runtimePackageRoot.Length + 1).Replace('\', '/')
    $runtimeSegments = $runtimeRelative -split '/'
    if (@($runtimeSegments | Where-Object { $_ -in $forbiddenRuntimeSegments }).Count -gt 0 -or
        $runtimeRelative -match '(?i)\.(?:py|pyc|pyo|cs|ts|tsx|vue|map|md)$' -or
        (Split-Path -Leaf $runtimeRelative) -match '^(?:Build|Install|Test)-') {
        throw "A development-only file reached the runtime package: $runtimeRelative"
    }
}

# Search our runtime payload for exact local machine tokens in UTF-8 and
# UTF-16LE. Native DLL/EXE/PYD dependencies are deliberately excluded: their
# reproducible compiler metadata can contain the builder's Windows account
# path, while they cannot be a source/configuration channel for this product.
# Wheels, scripts, WebGUI assets and every non-native file remain covered.
$privacyTokens = [System.Collections.Generic.List[string]]::new()
foreach ($privacyToken in @($sourceResolved, $buildResolved, $finalBuildRoot, $env:USERPROFILE)) {
    if ($privacyToken -and -not $privacyTokens.Contains([string]$privacyToken)) {
        $privacyTokens.Add([string]$privacyToken)
    }
}
if ($env:USERNAME) {
    $privacyTokens.Add("\Users\$($env:USERNAME)\")
    $privacyTokens.Add("/Users/$($env:USERNAME)/")
}
if ($env:COMPUTERNAME) { $privacyTokens.Add("\\$($env:COMPUTERNAME)\") }
$privacyScanner = @'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
tokens = [token for token in sys.argv[2:] if token]
needles = []
for token in tokens:
    needles.extend((token.encode("utf-8"), token.encode("utf-16le")))
for path in root.rglob("*"):
    if not path.is_file():
        continue
    if path.suffix.lower() in {".dll", ".exe", ".pyd"}:
        continue
    payload = path.read_bytes()
    for needle in needles:
        if needle and needle in payload:
            raise SystemExit(f"runtime package contains a private machine token: {path.relative_to(root).as_posix()}")
'@
Invoke-YeYuGamerNative `
    -FilePath $python `
    -Arguments (@('-c', $privacyScanner, $runtimePackageRoot) + @($privacyTokens))

$oldStagedRoot = Assert-YeYuGamerChildPath -Parent $buildResolved -Child $stagedRoot -Purpose 'source-rich staged tree retirement'
Assert-YeYuGamerNoReparseTree -Path $oldStagedRoot -Purpose 'source-rich staged tree retirement' | Out-Null
Remove-Item -LiteralPath $oldStagedRoot -Recurse -Force
Move-Item -LiteralPath $runtimePackageRoot -Destination $stagedRoot
Assert-YeYuGamerNoReparseTree -Path $stagedRoot -Purpose 'final runtime package' | Out-Null

$appFiles = Get-ChildItem -LiteralPath $stagedRoot -Recurse -File -Force | Sort-Object FullName
$manifestAppFiles = foreach ($file in $appFiles) {
    [ordered]@{
        path = $file.FullName.Substring($stagedRoot.Length + 1).Replace('\', '/')
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        bytes = $file.Length
    }
}
$wheelhouseFiles = Get-ChildItem -LiteralPath $wheelhouseRoot -Recurse -File -Force | Sort-Object FullName
$manifestWheelhouseFiles = foreach ($file in $wheelhouseFiles) {
    [ordered]@{
        path = $file.FullName.Substring($wheelhouseRoot.Length + 1).Replace('\', '/')
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        bytes = $file.Length
    }
}
$manifest = [ordered]@{
    schemaVersion = 3
    builtAt = [DateTimeOffset]::UtcNow.ToString('o')
    layout = [ordered]@{
        application = 'staged-app'
        wheelhouse = 'wheelhouse'
    }
    dependencyLock = [ordered]@{
        sourcePath = 'packaging/requirements.cpython312-win_amd64.lock'
        packagedPath = 'provenance/requirements.lock'
        wheelhousePath = 'requirements.lock'
        provenanceSourcePath = 'packaging/dependency-provenance.cpython312-win_amd64.json'
        provenancePackagedPath = 'provenance/dependency-provenance.json'
        target = 'cpython312-win_amd64'
        packageCount = $dependencyPackages.Count
        sha256 = $dependencyLockHash
        provenanceSha256 = $dependencyProvenanceHash
    }
    wheelhouse = [ordered]@{
        relativePath = 'wheelhouse'
        lockFile = 'requirements.lock'
        installMode = 'no-index'
        requireHashes = $true
    }
    runtimeAllowlist = [ordered]@{
        topLevel = @($expectedRuntimeTopLevel)
        productionScripts = @($productionScriptNames)
        excludedSegments = @($forbiddenRuntimeSegments)
        excludedExtensions = @('.py', '.pyc', '.pyo', '.cs', '.ts', '.tsx', '.vue', '.map', '.md')
    }
    packagePrivacy = [ordered]@{
        exactMachineTokenScan = 'passed'
        absolutePathsInManifest = $false
    }
    appFiles = @($manifestAppFiles)
    wheelhouseFiles = @($manifestWheelhouseFiles)
}
Write-YeYuGamerJson -Path (Join-Path $buildResolved 'build-manifest.json') -Value $manifest
Test-YeYuGamerCandidateBuildManifest -CandidateRoot $buildResolved -FinalBuildRoot $finalBuildRoot | Out-Null

# Promote only a completely tested and manifest-verified candidate.  Directory
# renames stay on the same local volume, so readers see either the old build or
# the new build.  The former final build is retained as last-known-good.
$lastKnownGoodRoot = Assert-YeYuGamerChildPath `
    -Parent $productWorkRoot `
    -Child (Join-Path $productWorkRoot 'build.last-known-good') `
    -Purpose 'last-known-good build root'
$previousBuildRotated = $false
if (Test-Path -LiteralPath $finalBuildRoot) {
    $finalSentinel = Join-Path $finalBuildRoot '.yeyu-gamer-owned-directory'
    if (-not (Test-Path -LiteralPath $finalBuildRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $finalSentinel -PathType Leaf) -or
        [System.IO.File]::ReadAllText($finalSentinel) -cne 'YeYuGamer:Build:v1') {
        throw 'The existing final build has no valid ownership sentinel; refusing rotation.'
    }
    Assert-YeYuGamerNoReparseTree -Path $finalBuildRoot -Purpose 'existing final build rotation' | Out-Null
    if (Test-Path -LiteralPath $lastKnownGoodRoot) {
        Remove-YeYuGamerBuildSiblingSafely `
            -ProductWorkRoot $productWorkRoot `
            -Path $lastKnownGoodRoot `
            -AllowedLeafNames @('build.last-known-good')
    }
    # Explorer and endpoint scanning can keep a just-read build file open for a
    # moment.  Retrying the same-volume rename preserves atomic promotion
    # without ever falling back to a partial copy.
    $rotationError = $null
    foreach ($rotationAttempt in 1..12) {
        try {
            Move-Item -LiteralPath $finalBuildRoot -Destination $lastKnownGoodRoot -ErrorAction Stop
            $rotationError = $null
            break
        } catch {
            $rotationError = $_
            if ($rotationAttempt -lt 12) { Start-Sleep -Seconds 1 }
        }
    }
    if ($null -ne $rotationError) { throw $rotationError }
    $previousBuildRotated = $true
}
try {
    Move-Item -LiteralPath $incomingRoot -Destination $finalBuildRoot
    $incomingPromoted = $true
} catch {
    if ($previousBuildRotated -and
        -not (Test-Path -LiteralPath $finalBuildRoot) -and
        (Test-Path -LiteralPath $lastKnownGoodRoot -PathType Container)) {
        Move-Item -LiteralPath $lastKnownGoodRoot -Destination $finalBuildRoot
    }
    throw
}
Write-Host "YeYu Gamer local build is ready: $finalBuildRoot"
