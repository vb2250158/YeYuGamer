[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [ValidateSet('Classic','OpenKuro','NTE','FGO','StarRail','CZN','BD2')]
    [string[]]$AdapterGroups = @('Classic','OpenKuro','NTE','FGO','StarRail','CZN','BD2'),
    [string]$PythonPath,
    [string]$NodePath,
    [string]$PnpmPath,
    [string]$CSharpCompilerPath,
    [ValidateRange(15, 300)][int]$ManagerStartupTimeoutSeconds = 150
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

# This is the only release path that composes the product package and Adapter
# packages.  It deliberately has no daily/batch execution call.  Promotion is
# owned by Manager and its no-process diagnostic canary.

function Assert-YeYuGamerReleaseSourceRoot {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose 'Release source'
    $driveRoot = [System.IO.Path]::GetPathRoot($resolved)
    $driveName = $driveRoot.TrimEnd('\').TrimEnd(':')
    $drive = Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue
    if (($drive -and (([string]$drive.DisplayRoot).StartsWith('\\') -or
            ([string]$drive.Root).StartsWith('\\'))) -or
        [System.IO.DriveInfo]::new($driveRoot).DriveType -eq [System.IO.DriveType]::Network) {
        throw "Release source must be on a local disk, not a mapped network drive: $resolved"
    }
    return Assert-YeYuGamerNoReparseAncestors -Path $resolved -Purpose 'Release source'
}

# Reject remote sources before changing process environment or creating any
# release snapshot/evidence/tool directory.
$SourceRoot = Assert-YeYuGamerReleaseSourceRoot -Path $SourceRoot
$env:PYTHONDONTWRITEBYTECODE = '1'

function Copy-YeYuGamerReleaseSource {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Destination
    )

    $source = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "Release source is missing: $source"
    }
    $destinationResolved = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $localAppData = [System.IO.Path]::GetFullPath(
        [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    ).TrimEnd('\') + '\'
    if (-not ($destinationResolved + '\').StartsWith($localAppData, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Release source staging must remain below LocalApplicationData.'
    }
    if (Test-Path -LiteralPath $destinationResolved) {
        throw "The unique release source staging directory already exists: $destinationResolved"
    }
    $excludedSegments = @(
        '.git', '.venv', '__pycache__', 'node_modules', 'dist', '.pytest_cache',
        'logs', 'runs'
    )
    $sourcePrefixLength = $source.Length + 1
    $pendingDirectories = [System.Collections.Generic.Stack[string]]::new()
    $sourceFiles = [System.Collections.Generic.List[object]]::new()
    $pendingDirectories.Push($source)
    while ($pendingDirectories.Count -gt 0) {
        $directory = Get-Item -LiteralPath $pendingDirectories.Pop() -Force -ErrorAction Stop
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Release source contains a reparse point: $($directory.FullName)"
        }
        # Prune before descent: NAS dependency/cache trees may be very large or
        # contain links, and none of their descendants belong in this snapshot.
        foreach ($entry in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            if ($entry.Name -in $excludedSegments) { continue }
            if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Release source contains a reparse point: $($entry.FullName)"
            }
            if ($entry.PSIsContainer) {
                $pendingDirectories.Push($entry.FullName)
            } else {
                $sourceFiles.Add($entry)
            }
        }
    }
    $files = @($sourceFiles | Sort-Object FullName)
    if ($files.Count -eq 0) { throw 'Release source snapshot is empty.' }
    New-Item -ItemType Directory -Path $destinationResolved | Out-Null

    foreach ($file in $files) {
        $file = Get-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Release source contains a reparse-point file: $($file.FullName)"
        }
        $relative = $file.FullName.Substring($sourcePrefixLength)
        $target = [System.IO.Path]::GetFullPath((Join-Path $destinationResolved $relative))
        if (-not $target.StartsWith($destinationResolved + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Release source path escaped staging: $relative"
        }
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination $target
        $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($sourceHash -cne $targetHash) {
            throw "Release source changed or copied incorrectly: $relative"
        }
    }
    return $destinationResolved
}

function Invoke-YeYuGamerCandidateInstallation {
    [CmdletBinding()]
    param([Parameter(Mandatory)][scriptblock]$Install)

    $gateName = 'YEYU_GAMER_LEGACY_EXECUTION_ENABLED'
    $gatePath = 'Env:\' + $gateName
    $gateWasSet = Test-Path -LiteralPath $gatePath
    $previousGate = [Environment]::GetEnvironmentVariable($gateName, 'Process')
    try {
        # Candidate installers require a disabled caller gate. This scope must
        # end before the installed Host starts with its configured execution gate.
        [Environment]::SetEnvironmentVariable($gateName, 'false', 'Process')
        & $Install
    } finally {
        if ($gateWasSet) {
            [Environment]::SetEnvironmentVariable($gateName, $previousGate, 'Process')
        } else {
            Remove-Item -LiteralPath $gatePath -ErrorAction SilentlyContinue
        }
    }
}

function Get-CandidateVersion {
    param([Parameter(Mandatory)][string]$CandidateRoot)
    $manifestPath = Join-Path $CandidateRoot 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Adapter candidate manifest is missing: $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) {
        throw "Only an unpromoted candidate may enter release installation: $CandidateRoot"
    }
    if ([string]$manifest.packageVersion -notmatch '\A[0-9A-Za-z][0-9A-Za-z._+-]{0,127}\z') {
        throw "Adapter candidate has an invalid package version: $CandidateRoot"
    }
    return [string]$manifest.packageVersion
}

function Get-ManagerHeaders {
    param(
        [Parameter(Mandatory)][string]$RuntimeRoot,
        [string]$IdempotencyKey,
        [int64]$StateVersion = -1
    )
    $tokenPath = Join-Path $RuntimeRoot 'secrets\actors\cli.token'
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf) -or
        ((Get-Item -LiteralPath $tokenPath -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Manager CLI credential is missing or unsafe.'
    }
    $token = (Get-Content -LiteralPath $tokenPath -Raw -Encoding ASCII).Trim()
    if ($token.Length -lt 32 -or $token -notmatch '\A[A-Za-z0-9_-]+\z') {
        throw 'Manager CLI credential is malformed.'
    }
    $headers = @{
        Authorization = 'Bearer ' + $token
        'X-YeYu-Gamer-Actor' = 'cli'
    }
    if ($IdempotencyKey) {
        if ($StateVersion -lt 0) { throw 'A Manager mutation requires a non-negative state version.' }
        $headers['Idempotency-Key'] = $IdempotencyKey
        $headers['If-Match'] = '"' + [string]$StateVersion + '"'
        $headers['X-Expected-State-Version'] = [string]$StateVersion
    }
    return $headers
}

function Invoke-ManagerGet {
    param(
        [Parameter(Mandatory)][string]$RuntimeRoot,
        [Parameter(Mandatory)][string]$RelativePath
    )
    $headers = Get-ManagerHeaders -RuntimeRoot $RuntimeRoot
    return Invoke-RestMethod `
        -Uri ('http://127.0.0.1:8877/api/v1/' + $RelativePath.TrimStart('/')) `
        -Method Get `
        -Headers $headers `
        -TimeoutSec 15 `
        -ErrorAction Stop
}

function Invoke-ManagerPromotion {
    param(
        [Parameter(Mandatory)][string]$RuntimeRoot,
        [Parameter(Mandatory)][string]$ReleaseId,
        [Parameter(Mandatory)][string]$GameId,
        [Parameter(Mandatory)][string]$PackageVersion
    )
    $adapterId = 'legacy-' + $GameId.ToLowerInvariant()
    $idempotencyKey = "local-release-$ReleaseId-$($GameId.ToLowerInvariant())"
    $body = [ordered]@{
        adapterId = $adapterId
        targetStage = 'promoted'
        reason = "payload-bound local release $ReleaseId"
        requestedBy = 'cli'
    }
    $escapedVersion = [Uri]::EscapeDataString($PackageVersion)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $snapshot = Invoke-ManagerGet -RuntimeRoot $RuntimeRoot -RelativePath 'snapshot'
        $stateVersion = [int64]$snapshot.stateVersion
        $headers = Get-ManagerHeaders `
            -RuntimeRoot $RuntimeRoot `
            -IdempotencyKey $idempotencyKey `
            -StateVersion $stateVersion
        try {
            $receipt = Invoke-RestMethod `
                -Uri "http://127.0.0.1:8877/api/v1/adapter-versions/$escapedVersion/promotion-requests" `
                -Method Post `
                -Headers $headers `
                -ContentType 'application/json; charset=utf-8' `
                -Body ($body | ConvertTo-Json -Compress) `
                -TimeoutSec 60 `
                -ErrorAction Stop
            if (-not [bool]$receipt.result.executionReady -or [bool]$receipt.result.gameStarted) {
                throw "Manager promotion did not return no-process execution readiness for $GameId."
            }
            return $receipt
        } catch {
            $statusCode = 0
            if ($null -ne $_.Exception.Response) {
                try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { $statusCode = 0 }
            }
            if ($statusCode -ne 412 -or $attempt -eq 3) { throw }
        }
    }
    throw "Manager promotion did not converge for $GameId."
}

$releaseId = [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$productWorkRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer'
$localSourceParent = Join-Path $productWorkRoot 'release-source'
$localSourceRoot = Join-Path $localSourceParent $releaseId
$evidenceRoot = Join-Path (Join-Path $productWorkRoot 'release-evidence') $releaseId
New-Item -ItemType Directory -Path $localSourceParent,$evidenceRoot -Force | Out-Null
$localSourceRoot = Copy-YeYuGamerReleaseSource -Root $SourceRoot -Destination $localSourceRoot

$localScripts = Join-Path $localSourceRoot 'scripts'
# Execute the isolated snapshot regression test from the local copy before
# preparing tools or changing any installed state.
& (Join-Path $localScripts 'Test-YeYuGamerReleaseSource.ps1') |
    Set-Content -LiteralPath (Join-Path $evidenceRoot 'release-source.json') -Encoding UTF8
. (Join-Path $localScripts 'YeYuGamer.Common.ps1')
$buildRoot = Get-YeYuGamerDefaultBuildRoot
$adapterBuildRoot = Join-Path (Get-YeYuGamerDefaultProductWorkRoot) 'adapter-build'
$installRoot = Get-YeYuGamerDefaultInstallRoot
$runtimeRoot = Get-YeYuGamerDefaultRuntimeRoot
$seedPython = Get-YeYuGamerPython -InstallRoot $installRoot -ExplicitPath $PythonPath
$bootstrapPythonJson = & $seedPython -I -B -c 'import json, os, sys; print(json.dumps({"executable": os.path.realpath(sys._base_executable)}))'
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the release Python base interpreter.' }
$bootstrapPythonRecord = $bootstrapPythonJson | ConvertFrom-Json -ErrorAction Stop
$bootstrapPython = Assert-YeYuGamerLocalTarget `
    -Path ([string]$bootstrapPythonRecord.executable) `
    -Purpose 'release bootstrap Python'
$installPrefix = [System.IO.Path]::GetFullPath($installRoot).TrimEnd('\') + '\'
if (($bootstrapPython + '\').StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The release bootstrap Python must remain outside the install tree that is rotated transactionally.'
}
$releaseToolsParent = Assert-YeYuGamerLocalTarget `
    -Path (Join-Path $productWorkRoot 'release-tools') `
    -Purpose 'release tools parent'
$releaseToolsRoot = Assert-YeYuGamerChildPath `
    -Parent $releaseToolsParent `
    -Child (Join-Path $releaseToolsParent $releaseId) `
    -Purpose 'isolated release tools environment'
if (Test-Path -LiteralPath $releaseToolsRoot) {
    throw "The unique release tools directory already exists: $releaseToolsRoot"
}
New-Item -ItemType Directory -Path $releaseToolsParent -Force | Out-Null
& $bootstrapPython -I -B -m venv $releaseToolsRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated release tools environment.' }
$python = Join-Path $releaseToolsRoot 'Scripts\python.exe'
$dependencyLock = Join-Path $localSourceRoot 'packaging\requirements.cpython312-win_amd64.lock'
& $python -I -B -m pip install `
    --disable-pip-version-check `
    --only-binary=:all: `
    --require-hashes `
    --requirement $dependencyLock
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the hash-locked release tools environment.' }
$compiler = if ($CSharpCompilerPath) { $CSharpCompilerPath } else { Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe' }
foreach ($localTool in @(
    [pscustomobject]@{Path=$python;Purpose='release Python'},
    [pscustomobject]@{Path=$bootstrapPython;Purpose='release bootstrap Python'},
    [pscustomobject]@{Path=$compiler;Purpose='release C# compiler'}
)) {
    $toolPath = Assert-YeYuGamerLocalTarget -Path ([string]$localTool.Path) -Purpose ([string]$localTool.Purpose)
    if (-not (Test-Path -LiteralPath $toolPath -PathType Leaf) -or
        ((Get-Item -LiteralPath $toolPath -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$($localTool.Purpose) is missing or unsafe."
    }
}
if (@($AdapterGroups).Count -eq 0) { throw 'At least one Adapter group is required for a unified release.' }

# The JSON contract is the release-wide operation source checked against every
# build script, installer and runner before any installed state changes.
& $python -X utf8 (Join-Path $localSourceRoot 'adapter-host\tests\validate_adapter_distribution_contracts.py')
if ($LASTEXITCODE -ne 0) { throw 'Adapter distribution contract validation failed.' }

$mainBuildArguments = @{
    SourceRoot = $localSourceRoot
    BuildRoot = $buildRoot
    PythonPath = $python
}
if ($NodePath) { $mainBuildArguments.NodePath = $NodePath }
if ($PnpmPath) { $mainBuildArguments.PnpmPath = $PnpmPath }
if ($CSharpCompilerPath) { $mainBuildArguments.CSharpCompilerPath = $CSharpCompilerPath }
& (Join-Path $localScripts 'Build-YeYuGamer.ps1') @mainBuildArguments

$promotionTargets = [System.Collections.Generic.List[object]]::new()
if ('Classic' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'classic-selected-daily-candidate'
    $evidence = Join-Path $evidenceRoot 'classic'
    & (Join-Path $localScripts 'Build-YeYuGamerClassicAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerClassicAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot $candidate -Python $python -CSharpCompilerPath $compiler -EvidenceDirectory $evidence
    $version = Get-CandidateVersion -CandidateRoot $candidate
    foreach ($gameId in @('PGR','ZZZ','NIKKE')) { $promotionTargets.Add([pscustomobject]@{GameId=$gameId;PackageVersion=$version}) }
}
if ('OpenKuro' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'local-daily-candidate'
    $evidence = Join-Path $evidenceRoot 'openkuro'
    & (Join-Path $localScripts 'Build-YeYuGamerWwAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerWwAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot $candidate -PythonPath $python -EvidenceDirectory $evidence
    $version = Get-CandidateVersion -CandidateRoot $candidate
    foreach ($gameId in @('WW','Endfield','GF2')) { $promotionTargets.Add([pscustomobject]@{GameId=$gameId;PackageVersion=$version}) }
}
if ('NTE' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'nte-candidate'
    $evidence = Join-Path $evidenceRoot 'nte.json'
    & (Join-Path $localScripts 'Build-YeYuGamerNteAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerNteAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot $candidate -PythonPath $python -EvidencePath $evidence
    $promotionTargets.Add([pscustomobject]@{GameId='NTE';PackageVersion=(Get-CandidateVersion -CandidateRoot $candidate)})
}
if ('FGO' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'fgo-candidate'
    $evidence = Join-Path $evidenceRoot 'fgo'
    New-Item -ItemType Directory -Path $evidence -Force | Out-Null
    & (Join-Path $localScripts 'Build-YeYuGamerFgoAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerFgoAdapter.ps1') -CandidateRoot $candidate -PythonPath $python -EvidencePath (Join-Path $evidence 'protocol.json') -UpstreamEvidencePath (Join-Path $evidence 'upstream.json') -CandidateTestEvidencePath (Join-Path $evidence 'candidate.json')
    $promotionTargets.Add([pscustomobject]@{GameId='FGO';PackageVersion=(Get-CandidateVersion -CandidateRoot $candidate)})
}
if ('StarRail' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'starrail-candidate'
    $evidence = Join-Path $evidenceRoot 'starrail.json'
    & (Join-Path $localScripts 'Build-YeYuGamerStarRailAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerStarRailAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot $candidate -PythonPath $python -CSharpCompilerPath $compiler -EvidencePath $evidence
    $promotionTargets.Add([pscustomobject]@{GameId='StarRail';PackageVersion=(Get-CandidateVersion -CandidateRoot $candidate)})
}
if ('CZN' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'czn-candidate'
    $evidence = Join-Path $evidenceRoot 'czn.json'
    & (Join-Path $localScripts 'Build-YeYuGamerCznAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -GameId CZN -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerCznAdapter.ps1') -CandidateRoot $candidate -EvidencePath $evidence
    $promotionTargets.Add([pscustomobject]@{GameId='CZN';PackageVersion=(Get-CandidateVersion -CandidateRoot $candidate)})
}
if ('BD2' -in $AdapterGroups) {
    $candidate = Join-Path $adapterBuildRoot 'bd2-candidate'
    $evidence = Join-Path $evidenceRoot 'bd2.json'
    & (Join-Path $localScripts 'Build-YeYuGamerCznAdapter.ps1') -SourceRoot $localSourceRoot -BuildRoot $adapterBuildRoot -GameId BD2 -CSharpCompilerPath $compiler
    & (Join-Path $localScripts 'Test-YeYuGamerBd2Adapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot $candidate -EvidencePath $evidence
    $promotionTargets.Add([pscustomobject]@{GameId='BD2';PackageVersion=(Get-CandidateVersion -CandidateRoot $candidate)})
}

# Do not stop a running product until every release package and no-process
# candidate test has passed.  Safe stop refuses active work rather than killing
# a Manager/game process.
$installedStop = Join-Path $installRoot 'app\scripts\Stop-YeYuGamer.ps1'
$installedConfig = Join-Path $runtimeRoot 'config\platform.json'
if ((Test-Path -LiteralPath $installedStop -PathType Leaf) -and (Test-Path -LiteralPath $installedConfig -PathType Leaf)) {
    # Bootstrap lifecycle fixes from the tested snapshot before replacing the
    # old installation; all stop requests still use the installed Manager API.
    & (Join-Path $localScripts 'Stop-YeYuGamer.ps1') -InstallRoot $installRoot -RuntimeRoot $runtimeRoot -UseSourceClient
} else {
    Assert-YeYuGamerInstallLifecycleStopped -RuntimeRoot $runtimeRoot
}

& (Join-Path $localScripts 'Install-YeYuGamer.ps1') `
    -SourceRoot $localSourceRoot `
    -BuildRoot $buildRoot `
    -InstallRoot $installRoot `
    -RuntimeRoot $runtimeRoot `
    -PythonPath $bootstrapPython `
    -SkipBuild

Invoke-YeYuGamerCandidateInstallation -Install {
    if ('Classic' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerClassicAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot (Join-Path $adapterBuildRoot 'classic-selected-daily-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidenceDirectory (Join-Path $evidenceRoot 'classic')
    }
    if ('OpenKuro' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerWwAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot (Join-Path $adapterBuildRoot 'local-daily-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidenceDirectory (Join-Path $evidenceRoot 'openkuro')
    }
    if ('NTE' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerNteAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot (Join-Path $adapterBuildRoot 'nte-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidencePath (Join-Path $evidenceRoot 'nte.json')
    }
    if ('FGO' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerFgoAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot (Join-Path $adapterBuildRoot 'fgo-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidencePath (Join-Path $evidenceRoot 'fgo\candidate.json')
    }
    if ('StarRail' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerStarRailAdapter.ps1') -CandidateRoot (Join-Path $adapterBuildRoot 'starrail-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidencePath (Join-Path $evidenceRoot 'starrail.json')
    }
    if ('CZN' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerCznAdapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot (Join-Path $adapterBuildRoot 'czn-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidencePath (Join-Path $evidenceRoot 'czn.json')
    }
    if ('BD2' -in $AdapterGroups) {
        & (Join-Path $localScripts 'Install-YeYuGamerBd2Adapter.ps1') -SourceRoot $localSourceRoot -CandidateRoot (Join-Path $adapterBuildRoot 'bd2-candidate') -RuntimeRoot $runtimeRoot -CandidateTestEvidencePath (Join-Path $evidenceRoot 'bd2.json')
    }
}

$installedStart = Join-Path $installRoot 'app\scripts\Start-YeYuGamer.ps1'
& $installedStart -InstallRoot $installRoot -RuntimeRoot $runtimeRoot -NoOpenWebGui -TimeoutSeconds $ManagerStartupTimeoutSeconds

foreach ($target in $promotionTargets) {
    Invoke-ManagerPromotion `
        -RuntimeRoot $runtimeRoot `
        -ReleaseId $releaseId `
        -GameId ([string]$target.GameId) `
        -PackageVersion ([string]$target.PackageVersion) | Out-Null
}

$adapterPage = Invoke-ManagerGet -RuntimeRoot $runtimeRoot -RelativePath 'adapters'
foreach ($target in $promotionTargets) {
    $adapterId = 'legacy-' + ([string]$target.GameId).ToLowerInvariant()
    $matches = @($adapterPage.items | Where-Object { [string]$_.adapterId -ceq $adapterId })
    if ($matches.Count -ne 1) { throw "Manager did not expose exactly one Adapter projection for $adapterId." }
    $adapter = $matches[0]
    if ([string]$adapter.activeVersion -cne [string]$target.PackageVersion -or
        [string]$adapter.stage -cne 'production' -or
        [string]$adapter.health -cne 'healthy' -or
        -not [bool]$adapter.hostHealthy -or
        -not [bool]$adapter.executionReady -or
        [string]$adapter.executionPackageStatus -cne 'promoted') {
        throw "Manager readiness verification failed for $adapterId."
    }
}

[pscustomobject]@{
    status = 'released'
    releaseId = $releaseId
    sourceStagedLocally = $true
    adapterGroups = @($AdapterGroups)
    promotedGames = @($promotionTargets | ForEach-Object { $_.GameId })
    managerHealthy = $true
    gameStarted = $false
    dailyBatchStarted = $false
    evidenceRoot = $evidenceRoot
} | ConvertTo-Json -Depth 5 -Compress
