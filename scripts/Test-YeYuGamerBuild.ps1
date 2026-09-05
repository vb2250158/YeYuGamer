[CmdletBinding()]
param(
    [string]$BuildScriptPath = (Join-Path $PSScriptRoot 'Build-YeYuGamer.ps1'),
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\build'),
    [string]$PythonPath,
    [switch]$RequireBuiltPackage
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-BuildContract {
    param([Parameter(Mandatory)][bool]$Condition, [Parameter(Mandatory)][string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-Sha256Text {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-ManifestPrivacyNode {
    param([Parameter(Mandatory)]$Value, [string]$Path = '$')
    if ($null -eq $Value) { return }
    if ($Value -is [string]) {
        if ($Value -match '^[A-Za-z]:[\\/]' -or $Value.StartsWith('\\')) {
            throw "Manifest contains an absolute drive or UNC value at ${Path}."
        }
        return
    }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            if ([string]$key -in @('sourceRoot', 'buildRoot', 'stagedRoot', 'buildHost', 'userName', 'hostName')) {
                throw "Manifest contains private machine property at ${Path}: $key"
            }
            Test-ManifestPrivacyNode -Value $Value[$key] -Path "${Path}.$key"
        }
        return
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [pscustomobject]) {
        $index = 0
        foreach ($item in $Value) {
            Test-ManifestPrivacyNode -Value $item -Path "${Path}[$index]"
            $index++
        }
        return
    }
    if ($Value -is [pscustomobject]) {
        foreach ($property in $Value.PSObject.Properties) {
            if ($property.Name -in @('sourceRoot', 'buildRoot', 'stagedRoot', 'buildHost', 'userName', 'hostName')) {
                throw "Manifest contains private machine property at ${Path}: $($property.Name)"
            }
            Test-ManifestPrivacyNode -Value $property.Value -Path "${Path}.$($property.Name)"
        }
    }
}

function Assert-ManifestTree {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][object[]]$Entries,
        [Parameter(Mandatory)][string]$Label
    )
    $rootResolved = [System.IO.Path]::GetFullPath($Root)
    $declared = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $Entries) {
        $relative = [string]$entry.path
        Assert-BuildContract (-not [System.IO.Path]::IsPathRooted($relative)) "$Label manifest contains a rooted path: $relative"
        Assert-BuildContract ($declared.Add($relative)) "$Label manifest contains a duplicate path: $relative"
        $filePath = [System.IO.Path]::GetFullPath((Join-Path $rootResolved $relative.Replace('/', '\')))
        Assert-BuildContract ($filePath.StartsWith($rootResolved.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) `
            "$Label manifest path escapes its root: $relative"
        Assert-BuildContract (Test-Path -LiteralPath $filePath -PathType Leaf) "$Label file is missing: $relative"
        $file = Get-Item -LiteralPath $filePath -Force
        Assert-BuildContract ([int64]$file.Length -eq [int64]$entry.bytes) "$Label size mismatch: $relative"
        Assert-BuildContract ((Get-Sha256Text -Path $filePath) -ceq [string]$entry.sha256) "$Label hash mismatch: $relative"
    }
    $actual = @(Get-ChildItem -LiteralPath $rootResolved -Recurse -File -Force)
    Assert-BuildContract ($actual.Count -eq $declared.Count) "$Label file count differs from its manifest."
}

$sourceResolved = (Resolve-Path -LiteralPath $SourceRoot -ErrorAction Stop).Path
$resolved = (Resolve-Path -LiteralPath $BuildScriptPath -ErrorAction Stop).Path
$installScriptPath = Join-Path $PSScriptRoot 'Install-YeYuGamer.ps1'
$commonScriptPath = Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1'
$scriptSources = @{}
foreach ($scriptPath in @($resolved, $installScriptPath, $commonScriptPath)) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
    Assert-BuildContract ($parseErrors.Count -eq 0) (
        "PowerShell parser errors in $(Split-Path -Leaf $scriptPath): " + (($parseErrors | ForEach-Object Message) -join '; ')
    )
    $scriptSources[$scriptPath] = [System.IO.File]::ReadAllText($scriptPath)
}

$source = [string]$scriptSources[$resolved]
$installSource = [string]$scriptSources[$installScriptPath]
$commonSource = [string]$scriptSources[$commonScriptPath]
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($resolved, [ref]$tokens, [ref]$parseErrors)
$nativeInvocationCount = 0
foreach ($nativeScriptPath in @($resolved, $installScriptPath, $commonScriptPath)) {
    $nativeTokens = $null
    $nativeParseErrors = $null
    $nativeAst = [System.Management.Automation.Language.Parser]::ParseFile(
        $nativeScriptPath,
        [ref]$nativeTokens,
        [ref]$nativeParseErrors
    )
    foreach ($invocation in @($nativeAst.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
                $node.GetCommandName() -eq 'Invoke-YeYuGamerNative'
        },
        $true
    ))) {
        $nativeInvocationCount++
        $parameterNames = @($invocation.CommandElements |
            Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } |
            ForEach-Object ParameterName)
        Assert-BuildContract (
            'FilePath' -in $parameterNames -and 'Arguments' -in $parameterNames
        ) (
            "Native invocation must use exact named parameters: " +
            "$(Split-Path -Leaf $nativeScriptPath):$($invocation.Extent.StartLineNumber)"
        )
    }
}
Assert-BuildContract ($nativeInvocationCount -gt 0) 'No build/install native-wrapper invocation was found.'
$functionNames = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true
) | ForEach-Object Name)
foreach ($requiredFunction in @(
    'Get-YeYuGamerStableSourceSnapshot', 'Test-YeYuGamerSourceSnapshotsEqual',
    'Test-YeYuGamerSourceEntryUnchanged', 'Remove-YeYuGamerBuildSiblingSafely',
    'Test-YeYuGamerCandidateBuildManifest', 'Add-YeYuGamerRuntimeFileSpec'
)) {
    Assert-BuildContract ($requiredFunction -in $functionNames) "Missing build hardening function: $requiredFunction"
}
$commandNames = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true
) | ForEach-Object GetCommandName)
Assert-BuildContract ('Reset-YeYuGamerOwnedDirectory' -notin $commandNames) `
    'Release build may not delete the final BuildRoot before candidate verification.'
Assert-BuildContract ($source.Contains("throw '-SkipWebBuild is not supported for release builds.")) `
    '-SkipWebBuild must fail closed before creating a candidate.'
Assert-BuildContract ($source.Contains('Get-YeYuGamerStableSourceSnapshot') -and
    $source.Contains('MaximumAttempts = 3') -and $source.Contains('Single-file copy verification failed')) `
    'Bounded stable NAS snapshot and per-file SHA-256 verification are required.'
Assert-BuildContract ($source.Contains("'build.incoming-'") -and $source.Contains("'build.last-known-good'") -and
    $source.Contains('Move-Item -LiteralPath $incomingRoot -Destination $finalBuildRoot')) `
    'Unique incoming build and atomic same-volume promotion are required.'
Assert-BuildContract ($source.Contains("@('.yeyu-gamer-owned-directory', 'build-manifest.json', 'staged-app', 'wheelhouse', 'desktop-host-output')")) `
    'Candidate top-level allowlisting is required.'
Assert-BuildContract ($source.Contains('$wheelhouseRoot = Join-Path $buildResolved ''wheelhouse''')) `
    'The dependency wheelhouse must be a top-level BuildRoot child.'
Assert-BuildContract (-not $source.Contains('$wheelhouseRoot = Join-Path $stagedRoot ''wheelhouse''')) `
    'The dependency wheelhouse may not be embedded in staged-app.'

$lockPath = Join-Path $sourceResolved 'packaging\requirements.cpython312-win_amd64.lock'
$provenancePath = Join-Path $sourceResolved 'packaging\dependency-provenance.cpython312-win_amd64.json'
Assert-BuildContract (Test-Path -LiteralPath $lockPath -PathType Leaf) 'The pre-committed dependency lock is missing.'
Assert-BuildContract (Test-Path -LiteralPath $provenancePath -PathType Leaf) 'The dependency provenance file is missing.'
$lockText = [System.IO.File]::ReadAllText($lockPath, [System.Text.Encoding]::UTF8)
Assert-BuildContract (-not $lockText.Contains("`r") -and $lockText.EndsWith("`n", [StringComparison]::Ordinal)) `
    'The committed lock must use canonical LF line endings and one final LF.'
$lockLines = @($lockText.Substring(0, $lockText.Length - 1).Split("`n"))
Assert-BuildContract ($lockLines.Count -gt 1 -and $lockLines[0] -ceq '--only-binary=:all:') `
    'The committed lock must begin with the unique wheel-only directive.'
$lockRecords = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($lockLine in $lockLines[1..($lockLines.Count - 1)]) {
    Assert-BuildContract ($lockLine -match '\A([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*) --hash=sha256:([0-9a-f]{64})\z') `
        "The committed lock contains a non-canonical record: $lockLine"
    $canonicalName = ([regex]::Replace($Matches[1], '[-_.]+', '-')).ToLowerInvariant()
    Assert-BuildContract (-not $lockRecords.ContainsKey($canonicalName)) "The committed lock duplicates package $canonicalName."
    $lockRecords.Add($canonicalName, [pscustomobject]@{ Name = $Matches[1]; Version = $Matches[2]; Sha256 = $Matches[3] })
}
$lockHash = Get-Sha256Text -Path $lockPath
$provenance = Get-Content -LiteralPath $provenancePath -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-BuildContract ([int]$provenance.schemaVersion -eq 1) 'Dependency provenance schema is not 1.'
Assert-BuildContract ([string]$provenance.lockSha256 -ceq $lockHash) 'Dependency provenance lock digest differs from the committed lock.'
Assert-BuildContract ([string]$provenance.target.implementation -ceq 'CPython' -and
    [string]$provenance.target.pythonVersion -ceq '3.12' -and [string]$provenance.target.abi -ceq 'cp312' -and
    [string]$provenance.target.platform -ceq 'win_amd64') 'Dependency provenance target is not CPython 3.12 win_amd64.'
Assert-BuildContract (@($provenance.artifacts).Count -eq $lockRecords.Count) `
    'Dependency provenance artifact count differs from the committed lock.'
foreach ($artifact in @($provenance.artifacts)) {
    $canonicalName = ([regex]::Replace([string]$artifact.name, '[-_.]+', '-')).ToLowerInvariant()
    Assert-BuildContract ($lockRecords.ContainsKey($canonicalName)) "Provenance artifact is absent from lock: $($artifact.filename)"
    $locked = $lockRecords[$canonicalName]
    Assert-BuildContract ([string]$artifact.version -ceq [string]$locked.Version -and
        [string]$artifact.sha256 -ceq [string]$locked.Sha256 -and
        [string]$artifact.filename -match '\.whl\z' -and [int64]$artifact.bytes -gt 0) `
        "Provenance artifact differs from lock: $($artifact.filename)"
}
foreach ($input in @($provenance.inputFiles)) {
    $inputPath = Join-Path $sourceResolved ([string]$input.path).Replace('/', '\')
    Assert-BuildContract (Test-Path -LiteralPath $inputPath -PathType Leaf) "Dependency input is missing: $($input.path)"
    Assert-BuildContract ((Get-Sha256Text -Path $inputPath) -ceq [string]$input.sha256) `
        "Dependency input changed without a reviewed lock update: $($input.path)"
}
Assert-BuildContract ($lockRecords.Count -eq 32) "Expected 32 transitive dependency pins; found $($lockRecords.Count)."
Assert-BuildContract ($lockRecords.ContainsKey('pyside6') -and [string]$lockRecords['pyside6'].Version -ceq '6.11.2') `
    'The committed lock does not contain PySide6==6.11.2.'
Assert-BuildContract ($lockRecords.ContainsKey('pyinstaller') -and [string]$lockRecords['pyinstaller'].Version -ceq '6.22.2') `
    'The committed lock does not contain PyInstaller==6.22.2.'

foreach ($requiredText in @(
    "'pip', 'download'", "'--only-binary=:all:'", "'--require-hashes'", '$dependencyLockSourcePath',
    "Copy-Item -LiteralPath `$dependencyLockSourcePath -Destination `$wheelhouseLockPath",
    "target = 'cpython312-win_amd64'", 'schemaVersion = 3', 'runtimePackageRoot',
    "exactMachineTokenScan = 'passed'"
)) {
    Assert-BuildContract ($source.Contains($requiredText)) "Missing source-locked build contract text: $requiredText"
}
Assert-BuildContract (-not $source.Contains('$lockGenerator') -and
    -not $source.Contains('records.append(f"{name}=={version}') -and -not $source.Contains('lock_path.write_text')) `
    'Release build still generates dependency hashes from downloaded artifacts.'
Assert-BuildContract ($source.Contains("'adapter-host', 'backend', 'contracts', 'desktop-host', 'packages', 'provenance', 'scripts', 'webgui'") -and
    $source.Contains("'tests', 'docs', '__pycache__', '.pytest_cache', 'node_modules'") -and
    $source.Contains("'^(?:Build|Install|Test)-'")) `
    'The positive runtime allowlist or development-file exclusion is missing.'

$manifestStart = $source.LastIndexOf('$manifest = [ordered]@{', [StringComparison]::Ordinal)
$manifestEnd = $source.IndexOf('Write-YeYuGamerJson -Path (Join-Path $buildResolved ''build-manifest.json'')', $manifestStart, [StringComparison]::Ordinal)
Assert-BuildContract ($manifestStart -ge 0 -and $manifestEnd -gt $manifestStart) 'Could not isolate the build manifest declaration.'
$manifestDeclaration = $source.Substring($manifestStart, $manifestEnd - $manifestStart)
foreach ($privateManifestField in @('sourceRoot =', 'buildRoot =', 'stagedRoot =', 'buildHost =', '$env:USERNAME', '$env:COMPUTERNAME')) {
    Assert-BuildContract (-not $manifestDeclaration.Contains($privateManifestField)) `
        "Build manifest declaration leaks machine metadata: $privateManifestField"
}
Assert-BuildContract ($manifestDeclaration.Contains("application = 'staged-app'") -and
    $manifestDeclaration.Contains("wheelhouse = 'wheelhouse'")) 'Build manifest does not use the fixed relative layout.'

$installManifestStart = $installSource.IndexOf('$installManifest = [ordered]@{', [StringComparison]::Ordinal)
$installManifestEnd = $installSource.IndexOf('Write-YeYuGamerJson -Path $installManifestPath', $installManifestStart, [StringComparison]::Ordinal)
Assert-BuildContract ($installManifestStart -ge 0 -and $installManifestEnd -gt $installManifestStart) `
    'Could not isolate the install manifest declaration.'
$installManifestDeclaration = $installSource.Substring($installManifestStart, $installManifestEnd - $installManifestStart)
foreach ($privateInstallField in @('installRoot =', 'runtimeRoot =', 'sourceBuild =', 'path = $', 'manifestPath = $')) {
    Assert-BuildContract (-not $installManifestDeclaration.Contains($privateInstallField)) `
        "Install manifest inherits an absolute or machine-private field: $privateInstallField"
}
Assert-BuildContract ($installManifestDeclaration.Contains('buildProvenance = [ordered]@{') -and
    $installManifestDeclaration.Contains('dependencyLockSha256')) `
    'Install manifest does not retain the privacy-safe source-lock digest.'
Assert-BuildContract ($installSource.Contains('Unsupported build manifest schema') -and $installSource.Contains('-ne 3')) `
    'Installer does not require build-manifest schema v3.'
Assert-BuildContract ($commonSource.Contains('Unsupported build manifest schema') -and $commonSource.Contains('-ne 3')) `
    'Common build-manifest validation does not require schema v3.'
foreach ($processFenceContract in @(
    'ConvertFrom-YeYuGamerWindowsCommandLine',
    'CommandLineToArgvW',
    'Get-YeYuGamerPythonModuleProcesses',
    'Get-CimInstance Win32_Process -ErrorAction Stop',
    "'yeyu_gamer_platform.manager_host'",
    "'yeyu_gamer_manager'",
    "'yeyu_gamer_platform.tray'",
    'the operation must fail closed'
)) {
    Assert-BuildContract ($commonSource.Contains($processFenceContract)) `
        "Common lifecycle process fence is missing: $processFenceContract"
}
$installLifecycleFenceIndex = $installSource.IndexOf('Assert-YeYuGamerInstallLifecycleStopped')
$installRotationIndex = $installSource.IndexOf("`$incoming = Assert-YeYuGamerChildPath")
Assert-BuildContract ($installLifecycleFenceIndex -ge 0 -and
    $installRotationIndex -gt $installLifecycleFenceIndex) `
    'Installer must prove the endpoint, record, mutex, and module process set stopped before app rotation paths are prepared.'

$skipFailure = $null
try { & $resolved -SkipWebBuild } catch { $skipFailure = $_.Exception.Message }
Assert-BuildContract ($skipFailure -like '*-SkipWebBuild is not supported for release builds*') `
    '-SkipWebBuild did not fail closed with the expected reason.'

$builtPackageState = 'not-run'
$alternateWheelHashRejection = 'not-run'
$packagePrivacyScan = 'not-run'
$runtimeFileCount = 0
$manifestPath = Join-Path $BuildRoot 'build-manifest.json'
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$manifest.schemaVersion -eq 3) {
        Test-ManifestPrivacyNode -Value $manifest
        $appRoot = Join-Path $BuildRoot 'staged-app'
        $wheelhouseRoot = Join-Path $BuildRoot 'wheelhouse'
        Assert-ManifestTree -Root $appRoot -Entries @($manifest.appFiles) -Label 'application'
        Assert-ManifestTree -Root $wheelhouseRoot -Entries @($manifest.wheelhouseFiles) -Label 'wheelhouse'
        $sourceLockHash = Get-Sha256Text -Path $lockPath
        $packagedLockHash = Get-Sha256Text -Path (Join-Path $appRoot 'provenance\requirements.lock')
        $wheelhouseLockHash = Get-Sha256Text -Path (Join-Path $wheelhouseRoot 'requirements.lock')
        Assert-BuildContract ($sourceLockHash -ceq $packagedLockHash -and $packagedLockHash -ceq $wheelhouseLockHash -and
            $wheelhouseLockHash -ceq [string]$manifest.dependencyLock.sha256) `
            'Source, packaged, wheelhouse and manifest lock digests are not identical.'
        $forbiddenSegments = @('tests', 'docs', '__pycache__', '.pytest_cache', 'node_modules')
        foreach ($entry in @($manifest.appFiles)) {
            $relative = [string]$entry.path
            $segments = $relative -split '/'
            Assert-BuildContract (@($segments | Where-Object { $_ -in $forbiddenSegments }).Count -eq 0) `
                "Runtime package includes a forbidden segment: $relative"
            Assert-BuildContract ($relative -notmatch '(?i)\.(?:py|pyc|pyo|cs|ts|tsx|vue|map|md)$') `
                "Runtime package includes development source: $relative"
            Assert-BuildContract ((Split-Path -Leaf $relative) -notmatch '^(?:Build|Install|Test)-') `
                "Runtime package includes a development script: $relative"
        }
        $runtimeFileCount = @($manifest.appFiles).Count
        $privacyTokens = @($sourceResolved, [System.IO.Path]::GetFullPath($BuildRoot), $env:USERPROFILE) | Where-Object { $_ }
        foreach ($file in @(Get-ChildItem -LiteralPath $appRoot -Recurse -File -Force)) {
            $payload = [System.IO.File]::ReadAllBytes($file.FullName)
            foreach ($privacyToken in $privacyTokens) {
                foreach ($encoding in @([System.Text.Encoding]::UTF8, [System.Text.Encoding]::Unicode)) {
                    $needle = $encoding.GetBytes([string]$privacyToken)
                    if ($needle.Length -eq 0 -or $payload.Length -lt $needle.Length) { continue }
                    $matched = $false
                    for ($offset = 0; $offset -le $payload.Length - $needle.Length; $offset++) {
                        $same = $true
                        for ($index = 0; $index -lt $needle.Length; $index++) {
                            if ($payload[$offset + $index] -ne $needle[$index]) { $same = $false; break }
                        }
                        if ($same) { $matched = $true; break }
                    }
                    Assert-BuildContract (-not $matched) "Runtime package leaks a private machine token: $($file.Name)"
                }
            }
        }
        $packagePrivacyScan = 'passed'
        if (-not $PythonPath) {
            $defaultBuildPython = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\build-tools\.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $defaultBuildPython -PathType Leaf) { $PythonPath = $defaultBuildPython }
        }
        Assert-BuildContract ($PythonPath -and (Test-Path -LiteralPath $PythonPath -PathType Leaf)) `
            'A PythonPath is required to prove substituted-wheel hash rejection.'
        $probeArtifact = @($provenance.artifacts | Sort-Object { [int64]$_.bytes })[0]
        $originalWheel = Join-Path $wheelhouseRoot ([string]$probeArtifact.filename)
        Assert-BuildContract (Test-Path -LiteralPath $originalWheel -PathType Leaf) 'Hash-rejection probe wheel is missing.'
        $probeRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\build-contract-test-' + [Guid]::NewGuid().ToString('N'))
        $probeLinks = Join-Path $probeRoot 'links'
        $probeOutput = Join-Path $probeRoot 'output'
        New-Item -ItemType Directory -Path $probeLinks, $probeOutput -Force | Out-Null
        try {
            $substituteWheel = Join-Path $probeLinks ([string]$probeArtifact.filename)
            Copy-Item -LiteralPath $originalWheel -Destination $substituteWheel -Force
            $substituteBytes = [System.IO.File]::ReadAllBytes($substituteWheel)
            Assert-BuildContract ($substituteBytes.Length -gt 0) 'Hash-rejection probe wheel is empty.'
            $substituteBytes[$substituteBytes.Length - 1] = $substituteBytes[$substituteBytes.Length - 1] -bxor 0x01
            [System.IO.File]::WriteAllBytes($substituteWheel, $substituteBytes)
            $probeLock = Join-Path $probeRoot 'probe.lock'
            [System.IO.File]::WriteAllText(
                $probeLock,
                "--only-binary=:all:`n$($probeArtifact.name)==$($probeArtifact.version) --hash=sha256:$($probeArtifact.sha256)`n",
                [System.Text.UTF8Encoding]::new($false)
            )
            $probePipOutput = (& $PythonPath -I -B -m pip download --disable-pip-version-check --no-index --find-links $probeLinks --only-binary=:all: --require-hashes --no-deps --dest $probeOutput --requirement $probeLock 2>&1 | Out-String)
            $probePipExitCode = $LASTEXITCODE
            Assert-BuildContract ($probePipExitCode -ne 0) 'pip accepted a substituted wheel whose hash differs from the committed lock.'
            Assert-BuildContract ($probePipOutput -match '(?i)(do not match the hashes|hash mismatch|expected sha256)') `
                'The substituted-wheel probe failed for a reason other than the committed SHA-256 mismatch.'
            Assert-BuildContract (@(Get-ChildItem -LiteralPath $probeOutput -File -Force).Count -eq 0) `
                'pip left a downloaded artifact after substituted-wheel rejection.'
            $alternateWheelHashRejection = 'passed'
        } finally {
            $localDataRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
            $probeResolved = [System.IO.Path]::GetFullPath($probeRoot)
            if ($probeResolved.StartsWith($localDataRoot, [StringComparison]::OrdinalIgnoreCase) -and
                (Test-Path -LiteralPath $probeResolved -PathType Container)) {
                Remove-Item -LiteralPath $probeResolved -Recurse -Force
            }
        }
        $builtPackageState = 'passed'
    } elseif ($RequireBuiltPackage) {
        throw "BuildRoot does not contain a schema v3 package: $BuildRoot"
    }
} elseif ($RequireBuiltPackage) {
    throw "BuildRoot does not contain build-manifest.json: $BuildRoot"
}

[pscustomobject]@{
    parser = 'passed'
    skipWebBuildGate = 'passed'
    stableSnapshotContract = 'passed'
    committedDependencyLock = 'passed'
    dependencyPackageCount = $lockRecords.Count
    dependencyLockSha256 = $lockHash
    sourceLockDownloadContract = 'passed'
    dynamicHashGenerationAbsent = 'passed'
    manifestSchema = 3
    manifestPrivacyContract = 'passed'
    runtimeAllowlistContract = 'passed'
    builtPackage = $builtPackageState
    runtimeFileCount = $runtimeFileCount
    identicalLockDigest = if ($builtPackageState -eq 'passed') { 'passed' } else { 'not-run' }
    substitutedWheelHashRejection = $alternateWheelHashRejection
    packagePrivacyScan = $packagePrivacyScan
    atomicPromotionContract = 'passed'
}
