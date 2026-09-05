[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\build'),
    [string]$InstallRoot,
    [string]$RuntimeRoot,
    [string]$PythonPath,
    [string]$NodePath,
    [string]$PnpmPath,
    [string]$CSharpCompilerPath,
    [switch]$SkipBuild,
    [switch]$SkipWebBuild,
    [switch]$VerifyOnly,
    [switch]$RequireTray,
    [switch]$StartAfterInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PYTHONDONTWRITEBYTECODE = '1'
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

function Get-YeYuGamerExistingDesktopShortcuts {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$DesktopPath)

    # Keep the localized legacy names independent of Windows PowerShell's
    # source-file encoding. Only these existing product icons are migrated.
    $knownNames = @(
        'YeYu Gamer.lnk',
        ([string][char]0x591c + [char]0x96e8 + 'Gamer.lnk'),
        ([string][char]0x6bcf + [char]0x65e5 + '-' + [char]0x6e38 + [char]0x620f + [char]0x4e00 + [char]0x6761 + [char]0x9f99 + 'GUI.lnk')
    )
    foreach ($name in $knownNames) {
        $path = Join-Path $DesktopPath $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Assert-YeYuGamerNoReparseAncestors -Path $path -Purpose 'existing desktop shortcut'
        }
    }
}

function Set-YeYuGamerHostShortcut {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$HostExecutable,
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $HostExecutable
    $shortcut.Arguments = "--config `"$ConfigPath`""
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = 'YeYu Gamer'
    $shortcut.Save()
}

function Test-YeYuGamerInstallManifestTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object[]]$Entries,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $rootResolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose $Purpose
    if (-not (Test-Path -LiteralPath $rootResolved -PathType Container)) {
        throw "$Purpose is missing: $rootResolved"
    }
    Assert-YeYuGamerNoReparseTree -Path $rootResolved -Purpose $Purpose | Out-Null

    $declared = [System.Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($entry in @($Entries)) {
        foreach ($propertyName in @('path', 'sha256', 'bytes')) {
            if ($null -eq $entry.PSObject.Properties[$propertyName]) {
                throw "$Purpose manifest entry is missing $propertyName."
            }
        }
        $relativeText = [string]$entry.path
        if (-not $relativeText -or
            $relativeText.Contains('\') -or
            [System.IO.Path]::IsPathRooted($relativeText)) {
            throw "$Purpose manifest contains a rooted, empty or non-canonical path: $relativeText"
        }
        $segments = $relativeText -split '/'
        if (@($segments | Where-Object { -not $_ -or $_ -in @('.', '..') }).Count -gt 0) {
            throw "$Purpose manifest contains a non-canonical path: $relativeText"
        }
        if ($declared.ContainsKey($relativeText)) {
            throw "$Purpose manifest contains a duplicate path: $relativeText"
        }
        $sha256 = [string]$entry.sha256
        if ($sha256 -notmatch '\A[0-9a-fA-F]{64}\z') {
            throw "$Purpose manifest contains an invalid SHA-256: $relativeText"
        }
        try {
            $expectedBytes = [int64]$entry.bytes
        } catch {
            throw "$Purpose manifest contains an invalid size: $relativeText"
        }
        if ($expectedBytes -lt 0) {
            throw "$Purpose manifest contains a negative size: $relativeText"
        }

        $relativeNative = $relativeText.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        $filePath = Assert-YeYuGamerChildPath `
            -Parent $rootResolved `
            -Child (Join-Path $rootResolved $relativeNative) `
            -Purpose "$Purpose manifest file"
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "$Purpose manifest-declared file is missing: $relativeText"
        }
        $file = Get-Item -LiteralPath $filePath -Force
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Purpose contains a reparse-point file: $relativeText"
        }
        if ($file.Length -ne $expectedBytes) {
            throw "$Purpose manifest size mismatch: $relativeText"
        }
        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $sha256.ToLowerInvariant()) {
            throw "$Purpose manifest SHA-256 mismatch: $relativeText"
        }
        $declared.Add($relativeText, $entry)
    }

    $actualFiles = @(Get-ChildItem -LiteralPath $rootResolved -Recurse -File -Force)
    if ($actualFiles.Count -ne $declared.Count) {
        throw "$Purpose file count differs from its manifest: actual=$($actualFiles.Count), declared=$($declared.Count)"
    }
    foreach ($file in $actualFiles) {
        $relative = $file.FullName.Substring($rootResolved.Length + 1).Replace('\', '/')
        if (-not $declared.ContainsKey($relative)) {
            throw "$Purpose contains an undeclared file: $relative"
        }
    }
    return $declared
}

function Test-YeYuGamerInstallBuildManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$WheelhouseRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [switch]$AllowRelocatedAppRoot
    )

    $buildResolvedForManifest = Assert-YeYuGamerFixedProductPath -Path $BuildRoot -Kind Build
    $manifestResolved = Assert-YeYuGamerChildPath `
        -Parent $buildResolvedForManifest `
        -Child $ManifestPath `
        -Purpose 'build manifest'
    if (-not (Test-Path -LiteralPath $manifestResolved -PathType Leaf)) {
        throw "Build manifest is missing: $manifestResolved"
    }
    $manifestItem = Get-Item -LiteralPath $manifestResolved -Force
    if (($manifestItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Build manifest must not be a reparse point.'
    }
    $manifestText = Get-Content -LiteralPath $manifestResolved -Raw -Encoding UTF8
    $manifest = $manifestText | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 3) {
        throw "Unsupported build manifest schema: $($manifest.schemaVersion)"
    }
    foreach ($propertyName in @(
        'layout',
        'dependencyLock',
        'appFiles',
        'wheelhouseFiles',
        'wheelhouse',
        'runtimeAllowlist',
        'packagePrivacy'
    )) {
        if ($null -eq $manifest.PSObject.Properties[$propertyName]) {
            throw "Build manifest v3 is missing $propertyName."
        }
    }
    foreach ($forbiddenProperty in @('sourceRoot', 'buildRoot', 'stagedRoot', 'buildHost', 'userName', 'hostName')) {
        if ($null -ne $manifest.PSObject.Properties[$forbiddenProperty]) {
            throw "Build manifest contains private machine metadata: $forbiddenProperty"
        }
    }
    if ($manifestText -match '(?i)[a-z]:\\\\' -or $manifestText -match '\\\\\\\\[^\\]') {
        throw 'Build manifest contains an absolute drive or UNC path.'
    }
    if ([string]$manifest.layout.application -cne 'staged-app' -or
        [string]$manifest.layout.wheelhouse -cne 'wheelhouse') {
        throw 'Build manifest does not declare the fixed relative package layout.'
    }
    $canonicalStage = [System.IO.Path]::GetFullPath((Join-Path $buildResolvedForManifest 'staged-app'))
    if (-not $AllowRelocatedAppRoot -and
        -not ([System.IO.Path]::GetFullPath($AppRoot)).Equals($canonicalStage, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Selected app root is not the manifest-bound staged-app directory.'
    }

    foreach ($wheelhousePropertyName in @('relativePath', 'lockFile', 'installMode', 'requireHashes')) {
        if ($null -eq $manifest.wheelhouse.PSObject.Properties[$wheelhousePropertyName]) {
            throw "Build manifest wheelhouse policy is missing $wheelhousePropertyName."
        }
    }
    if ([string]$manifest.wheelhouse.relativePath -cne 'wheelhouse' -or
        [string]$manifest.wheelhouse.lockFile -cne 'requirements.lock' -or
        [string]$manifest.wheelhouse.installMode -cne 'no-index' -or
        $manifest.wheelhouse.requireHashes -ne $true) {
        throw 'Build manifest wheelhouse policy is not the required offline/hash-locked policy.'
    }
    $canonicalWheelhouse = [System.IO.Path]::GetFullPath((Join-Path $buildResolvedForManifest 'wheelhouse'))
    if (-not ([System.IO.Path]::GetFullPath($WheelhouseRoot)).Equals(
        $canonicalWheelhouse,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Selected wheelhouse is not the fixed build wheelhouse directory.'
    }

    Test-YeYuGamerInstallManifestTree `
        -Root $AppRoot `
        -Entries @($manifest.appFiles) `
        -Purpose 'staged application' | Out-Null
    Test-YeYuGamerInstallManifestTree `
        -Root $canonicalWheelhouse `
        -Entries @($manifest.wheelhouseFiles) `
        -Purpose 'offline wheelhouse' | Out-Null

    if ([string]$manifest.dependencyLock.sourcePath -cne 'packaging/requirements.cpython312-win_amd64.lock' -or
        [string]$manifest.dependencyLock.packagedPath -cne 'provenance/requirements.lock' -or
        [string]$manifest.dependencyLock.wheelhousePath -cne 'requirements.lock' -or
        [string]$manifest.dependencyLock.provenanceSourcePath -cne 'packaging/dependency-provenance.cpython312-win_amd64.json' -or
        [string]$manifest.dependencyLock.provenancePackagedPath -cne 'provenance/dependency-provenance.json' -or
        [string]$manifest.dependencyLock.target -cne 'cpython312-win_amd64' -or
        [int]$manifest.dependencyLock.packageCount -le 0 -or
        [string]$manifest.dependencyLock.sha256 -cnotmatch '\A[0-9a-f]{64}\z' -or
        [string]$manifest.dependencyLock.provenanceSha256 -cnotmatch '\A[0-9a-f]{64}\z') {
        throw 'Build manifest contains an invalid source-derived dependency trust anchor.'
    }
    $packagedLockPath = Join-Path $AppRoot 'provenance\requirements.lock'
    $packagedProvenancePath = Join-Path $AppRoot 'provenance\dependency-provenance.json'
    $wheelhouseLockPath = Join-Path $canonicalWheelhouse 'requirements.lock'
    foreach ($dependencyPath in @($packagedLockPath, $packagedProvenancePath, $wheelhouseLockPath)) {
        if (-not (Test-Path -LiteralPath $dependencyPath -PathType Leaf) -or
            (Get-Item -LiteralPath $dependencyPath).Length -le 0) {
            throw "A manifest-bound dependency trust-anchor file is missing or empty: $(Split-Path -Leaf $dependencyPath)"
        }
    }
    $packagedLockHash = (Get-FileHash -LiteralPath $packagedLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $wheelhouseLockHash = (Get-FileHash -LiteralPath $wheelhouseLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $packagedProvenanceHash = (Get-FileHash -LiteralPath $packagedProvenancePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($packagedLockHash -cne [string]$manifest.dependencyLock.sha256 -or
        $wheelhouseLockHash -cne $packagedLockHash -or
        $packagedProvenanceHash -cne [string]$manifest.dependencyLock.provenanceSha256) {
        throw 'The packaged lock, wheelhouse lock and build-manifest trust anchor do not match.'
    }
    $dependencyProvenance = Get-Content -LiteralPath $packagedProvenancePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$dependencyProvenance.schemaVersion -ne 1 -or
        [string]$dependencyProvenance.lockFile -cne 'requirements.cpython312-win_amd64.lock' -or
        [string]$dependencyProvenance.lockSha256 -cne $packagedLockHash -or
        [string]$dependencyProvenance.target.implementation -cne 'CPython' -or
        [string]$dependencyProvenance.target.pythonVersion -cne '3.12' -or
        [string]$dependencyProvenance.target.abi -cne 'cp312' -or
        [string]$dependencyProvenance.target.platform -cne 'win_amd64' -or
        @($dependencyProvenance.artifacts).Count -ne [int]$manifest.dependencyLock.packageCount) {
        throw 'The packaged dependency provenance does not match the build-manifest trust anchor.'
    }

    $wheelhouseEntries = @($manifest.wheelhouseFiles)
    $lockEntries = @($wheelhouseEntries | Where-Object { [string]$_.path -ceq 'requirements.lock' })
    if ($lockEntries.Count -ne 1 -or [int64]$lockEntries[0].bytes -le 0) {
        throw "The wheelhouse must declare exactly one non-empty requirements.lock; found $($lockEntries.Count)."
    }
    $requirementsLockText = [System.IO.File]::ReadAllText($wheelhouseLockPath, [System.Text.Encoding]::UTF8)
    if ($requirementsLockText.Contains("`r") -or
        -not $requirementsLockText.EndsWith("`n", [StringComparison]::Ordinal)) {
        throw 'requirements.lock must use canonical LF line endings and one final LF.'
    }
    $lockLines = @($requirementsLockText.Substring(0, $requirementsLockText.Length - 1).Split("`n"))
    if ($lockLines.Count -lt 2 -or $lockLines[0] -cne '--only-binary=:all:') {
        throw 'requirements.lock must start with the unique wheel-only directive.'
    }
    $canonicalPackages = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $fixedPySideFound = $false
    foreach ($lockLine in $lockLines[1..($lockLines.Count - 1)]) {
        if ($lockLine -notmatch '\A([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*) --hash=sha256:([0-9a-f]{64})\z') {
            throw "requirements.lock contains a non-canonical pin, option, path or direct URL: $lockLine"
        }
        $packageName = $Matches[1]
        $canonicalPackage = ([regex]::Replace($packageName, '[-_.]+', '-')).ToLowerInvariant()
        if (-not $canonicalPackages.Add($canonicalPackage)) {
            throw "requirements.lock contains a duplicate package pin: $packageName"
        }
        if ($packageName -ceq 'PySide6' -and $Matches[2] -ceq '6.11.2') {
            $fixedPySideFound = $true
        }
    }
    if (-not $fixedPySideFound) {
        throw 'The verified requirements.lock does not contain the fixed PySide6==6.11.2 dependency.'
    }
    if ($canonicalPackages.Count -ne [int]$manifest.dependencyLock.packageCount) {
        throw 'The verified requirements.lock package count differs from the manifest trust anchor.'
    }
    $unexpectedWheelhouseEntries = @($wheelhouseEntries | Where-Object {
        [string]$_.path -cne 'requirements.lock' -and
        ([string]$_.path -notmatch '\A[^/]+\.whl\z' -or [int64]$_.bytes -le 0)
    })
    if ($unexpectedWheelhouseEntries.Count -gt 0) {
        throw "The wheelhouse contains an unsupported or empty entry: $($unexpectedWheelhouseEntries[0].path)"
    }
    $expectedRuntimeTopLevel = @('adapter-host', 'backend', 'contracts', 'desktop-host', 'packages', 'provenance', 'scripts', 'webgui')
    if ((@($manifest.runtimeAllowlist.topLevel) -join '|') -cne ($expectedRuntimeTopLevel -join '|') -or
        [string]$manifest.packagePrivacy.exactMachineTokenScan -cne 'passed' -or
        [bool]$manifest.packagePrivacy.absolutePathsInManifest) {
        throw 'The runtime allowlist or package privacy contract is invalid.'
    }
    $forbiddenRuntimeSegments = @('tests', 'docs', '__pycache__', '.pytest_cache', 'node_modules')
    foreach ($appEntry in @($manifest.appFiles)) {
        $appRelative = [string]$appEntry.path
        $appSegments = $appRelative -split '/'
        if (@($appSegments | Where-Object { $_ -in $forbiddenRuntimeSegments }).Count -gt 0 -or
            $appRelative -match '(?i)\.(?:py|pyc|pyo|cs|ts|tsx|vue|map|md)$' -or
            (Split-Path -Leaf $appRelative) -match '^(?:Build|Install|Test)-') {
            throw "The runtime app manifest contains a development-only file: $appRelative"
        }
    }
    $productContracts = @(
        [pscustomobject]@{
            Parent = 'backend/dist'
            Pattern = 'yeyu_gamer_manager-*.whl'
        },
        [pscustomobject]@{
            Parent = 'packages/platform'
            Pattern = 'yeyu_gamer_platform-*.whl'
        }
    )
    foreach ($productContract in $productContracts) {
        $productEntries = @($manifest.appFiles | Where-Object {
            $path = [string]$_.path
            (Split-Path -Parent $path).Replace('\', '/') -ceq $productContract.Parent -and
            (Split-Path -Leaf $path) -like $productContract.Pattern -and
            [int64]$_.bytes -gt 0
        })
        if ($productEntries.Count -ne 1) {
            throw "The app manifest must declare exactly one $($productContract.Pattern) product wheel under $($productContract.Parent); found $($productEntries.Count)."
        }
    }
    return $manifest
}

function Get-YeYuGamerAutoStartRegistrySnapshot {
    $keyPath = 'Software\Microsoft\Windows\CurrentVersion\Run'
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath, $false)
    try {
        $valueNames = if ($null -ne $key) { @($key.GetValueNames()) } else { @() }
        return @('YeYuGamer', 'YeYuGamerTray') | ForEach-Object {
            $name = $_
            $exists = $name -in $valueNames
            [pscustomobject]@{
                Name = $name
                Exists = $exists
                Value = if ($exists) {
                    $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                } else { $null }
                Kind = if ($exists) { $key.GetValueKind($name) } else { $null }
            }
        }
    } finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Restore-YeYuGamerAutoStartRegistrySnapshot {
    param([Parameter(Mandatory = $true)][object[]]$Snapshot)

    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey(
        'Software\Microsoft\Windows\CurrentVersion\Run',
        $true
    )
    try {
        foreach ($entry in $Snapshot) {
            if ($entry.Exists) {
                $key.SetValue([string]$entry.Name, $entry.Value, $entry.Kind)
            } else {
                $key.DeleteValue([string]$entry.Name, $false)
            }
        }
    } finally {
        $key.Dispose()
    }
}

$sourceResolved = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not $InstallRoot) { $InstallRoot = Get-YeYuGamerDefaultInstallRoot }
if (-not $RuntimeRoot) { $RuntimeRoot = Get-YeYuGamerDefaultRuntimeRoot }
$installResolved = Assert-YeYuGamerInstallRoot -Path $InstallRoot
$runtimeResolved = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$buildResolved = Assert-YeYuGamerFixedProductPath -Path $BuildRoot -Kind Build
$managerBaseUrl = $script:YeYuGamerCanonicalManagerBaseUrl
$webUrl = 'http://127.0.0.1:8877/'
$installLifecycleLock = Enter-YeYuGamerInstallLifecycleLock

try {
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'Build-YeYuGamer.ps1') `
        -SourceRoot $SourceRoot `
        -BuildRoot $buildResolved `
        -PythonPath $PythonPath `
        -NodePath $NodePath `
        -PnpmPath $PnpmPath `
        -CSharpCompilerPath $CSharpCompilerPath `
        -SkipWebBuild:$SkipWebBuild
}

$stagedRoot = Join-Path $buildResolved 'staged-app'
$wheelhouseRoot = Join-Path $buildResolved 'wheelhouse'
$manifestPath = Join-Path $buildResolved 'build-manifest.json'
if (-not (Test-Path -LiteralPath $stagedRoot -PathType Container) -or
    -not (Test-Path -LiteralPath $wheelhouseRoot -PathType Container) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Build output is incomplete: $buildResolved"
}
$sourceManifest = Test-YeYuGamerInstallBuildManifest `
    -BuildRoot $buildResolved `
    -AppRoot $stagedRoot `
    -WheelhouseRoot $wheelhouseRoot `
    -ManifestPath $manifestPath
if ($VerifyOnly) {
    Write-Host "YeYu Gamer build verified; no installation changes were made: $buildResolved"
    return
}

Assert-YeYuGamerInstallLifecycleStopped `
    -RuntimeRoot $runtimeResolved

$incoming = Assert-YeYuGamerChildPath -Parent $installResolved -Child (Join-Path $installResolved 'app.incoming') -Purpose 'incoming app directory'
$current = Assert-YeYuGamerChildPath -Parent $installResolved -Child (Join-Path $installResolved 'app') -Purpose 'installed app directory'
$previous = Assert-YeYuGamerChildPath -Parent $installResolved -Child (Join-Path $installResolved 'app.previous') -Purpose 'previous app directory'
$venvRoot = Assert-YeYuGamerChildPath -Parent $installResolved -Child (Join-Path $installResolved '.venv') -Purpose 'installed Python environment'
$venvPrevious = Assert-YeYuGamerChildPath -Parent $installResolved -Child (Join-Path $installResolved '.venv.previous') -Purpose 'previous Python environment'
$configPath = Join-Path $runtimeResolved 'config\platform.json'
$actorTokensDirectory = Join-Path $runtimeResolved 'secrets\actors'
$notificationSecretsDirectory = Join-Path $runtimeResolved 'secrets\notifications'
$legacyRoot = Join-Path $runtimeResolved 'import\legacy-config'
$legacySeedSource = Split-Path -Parent $sourceResolved
$adaptersRuntimeRoot = Assert-YeYuGamerChildPath `
    -Parent $runtimeResolved `
    -Child (Join-Path $runtimeResolved 'adapters') `
    -Purpose 'runtime adapters directory'
$adapterRuntimeRoot = Assert-YeYuGamerChildPath `
    -Parent $adaptersRuntimeRoot `
    -Child (Join-Path $adaptersRuntimeRoot 'manager-adapter-host') `
    -Purpose 'Manager Adapter Host runtime directory'
$adapterRuntimePrevious = Assert-YeYuGamerChildPath `
    -Parent $adaptersRuntimeRoot `
    -Child (Join-Path $adaptersRuntimeRoot 'manager-adapter-host.previous') `
    -Purpose 'previous Manager Adapter Host runtime directory'
$installManifestPath = Join-Path $installResolved 'install-manifest.json'
$cliWrapperPath = Join-Path $installResolved 'YeYuGamer.cmd'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'YeYu Gamer'
$shortcutPath = Join-Path $startMenu 'YeYu Gamer.lnk'
$existingDesktopShortcuts = @(Get-YeYuGamerExistingDesktopShortcuts `
    -DesktopPath ([Environment]::GetFolderPath('DesktopDirectory')))
$startupDirectory = [Environment]::GetFolderPath('Startup')
$legacyStartupShortcuts = @(
    (Join-Path $startupDirectory 'YeYu Gamer.lnk'),
    (Join-Path $startupDirectory 'YeYu Gamer Tray.lnk')
)
$retiredTrayToken = Assert-YeYuGamerChildPath `
    -Parent $actorTokensDirectory `
    -Child (Join-Path $actorTokensDirectory 'tray.token') `
    -Purpose 'retired persisted tray token'

foreach ($rotationPath in @($incoming, $current, $previous)) {
    if (Test-Path -LiteralPath $rotationPath) {
        Assert-YeYuGamerNoReparseTree `
            -Path $rotationPath `
            -Purpose 'installed app rotation directory' | Out-Null
    }
}

$directoryTargets = [System.Collections.Generic.List[object]]::new()
foreach ($target in @(
    [pscustomobject]@{ Path = $current; PreserveAs = $previous },
    [pscustomobject]@{ Path = $incoming },
    [pscustomobject]@{ Path = $previous },
    [pscustomobject]@{ Path = $venvRoot; PreserveAs = $venvPrevious },
    [pscustomobject]@{ Path = $venvPrevious },
    [pscustomobject]@{ Path = $adapterRuntimeRoot; PreserveAs = $adapterRuntimePrevious },
    [pscustomobject]@{ Path = $adapterRuntimePrevious }
)) {
    $directoryTargets.Add($target)
}

# Directories that did not exist before this install are reversible side
# effects. Register both roots and nested runtime/start-menu paths so a failed
# candidate leaves no empty scaffolding behind. Existing data roots are never
# rotated wholesale.
foreach ($candidateDirectory in @(
    $installResolved,
    $runtimeResolved,
    (Split-Path -Parent $configPath),
    (Join-Path $runtimeResolved 'logs'),
    (Join-Path $runtimeResolved 'state'),
    (Join-Path $runtimeResolved 'artifacts'),
    (Join-Path $runtimeResolved 'secrets'),
    $actorTokensDirectory,
    $notificationSecretsDirectory,
    (Join-Path $runtimeResolved 'import'),
    $legacyRoot,
    $adaptersRuntimeRoot,
    $startMenu
)) {
    if (-not (Test-Path -LiteralPath $candidateDirectory)) {
        $directoryTargets.Add([pscustomobject]@{ Path = $candidateDirectory })
    }
}

$transactionFileTargets = @(
    $configPath,
    $installManifestPath,
    $cliWrapperPath,
    $shortcutPath,
    $retiredTrayToken
) + $legacyStartupShortcuts + $existingDesktopShortcuts
$autoStartRegistrySnapshot = @(Get-YeYuGamerAutoStartRegistrySnapshot)

try {
Invoke-YeYuGamerInstallTransaction `
    -DirectoryTargets @($directoryTargets) `
    -FileTargets $transactionFileTargets `
    -Action {
New-Item -ItemType Directory -Path $installResolved -Force | Out-Null
New-Item -ItemType Directory -Path $runtimeResolved -Force | Out-Null
New-Item -ItemType Directory -Path $incoming -Force | Out-Null
Get-ChildItem -LiteralPath $stagedRoot -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $incoming -Recurse -Force
}
Test-YeYuGamerInstallBuildManifest `
    -BuildRoot $buildResolved `
    -AppRoot $incoming `
    -WheelhouseRoot $wheelhouseRoot `
    -ManifestPath $manifestPath `
    -AllowRelocatedAppRoot | Out-Null

$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $bootstrapPython = Get-YeYuGamerPython -ExplicitPath $PythonPath
    Invoke-YeYuGamerNative `
        -FilePath $bootstrapPython `
        -Arguments @('-I', '-B', '-m', 'venv', $venvRoot)
}
$venvPythonw = Join-Path $venvRoot 'Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $venvPythonw -PathType Leaf)) {
    throw "The installed Python runtime has no windowless launcher: $venvPythonw"
}
$installPythonTargetJson = & $venvPython -I -B -c "import json,platform,struct,sys; print(json.dumps({'implementation':sys.implementation.name,'major':sys.version_info.major,'minor':sys.version_info.minor,'bits':struct.calcsize('P')*8,'platform':sys.platform,'machine':platform.machine()}))"
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the candidate install Python target.' }
$installPythonTarget = $installPythonTargetJson | ConvertFrom-Json -ErrorAction Stop
if ([string]$installPythonTarget.implementation -cne 'cpython' -or
    [int]$installPythonTarget.major -ne 3 -or [int]$installPythonTarget.minor -ne 12 -or
    [int]$installPythonTarget.bits -ne 64 -or [string]$installPythonTarget.platform -cne 'win32' -or
    [string]$installPythonTarget.machine -notin @('AMD64', 'x86_64')) {
    throw 'The candidate install runtime does not match the cpython312-win_amd64 dependency lock.'
}
$requirementsLock = Join-Path $wheelhouseRoot 'requirements.lock'
$managerWheelRoot = Join-Path $incoming 'backend\dist'
$platformWheelRoot = Join-Path $incoming 'packages\platform'
$managerWheels = @(Get-ChildItem `
    -LiteralPath $managerWheelRoot `
    -File `
    -Filter 'yeyu_gamer_manager-*.whl')
$platformWheels = @(Get-ChildItem `
    -LiteralPath $platformWheelRoot `
    -File `
    -Filter 'yeyu_gamer_platform-*.whl')
if ($managerWheels.Count -ne 1 -or $platformWheels.Count -ne 1) {
    throw 'The verified app must contain exactly one Manager wheel and one platform wheel.'
}

# Both runtime dependency closure and product wheels come exclusively from the
# manifest-bound local wheelhouse. --isolated ignores user/global pip config,
# --no-index prevents package-index access, and --only-binary rejects sdists.
Invoke-YeYuGamerNative `
    -FilePath $venvPython `
    -Arguments @(
        '-I',
        '-B',
        '-m',
        'pip',
        '--isolated',
        'install',
        '--disable-pip-version-check',
        '--no-input',
        '--no-cache-dir',
        '--no-index',
        '--find-links',
        $wheelhouseRoot,
        '--only-binary=:all:',
        '--require-hashes',
        '-r',
        $requirementsLock
    )
Invoke-YeYuGamerNative `
    -FilePath $venvPython `
    -Arguments @(
        '-I',
        '-B',
        '-m',
        'pip',
        '--isolated',
        'install',
        '--disable-pip-version-check',
        '--no-input',
        '--no-cache-dir',
        '--no-index',
        '--find-links',
        $wheelhouseRoot,
        '--only-binary=:all:',
        '--no-deps',
        '--force-reinstall',
        $managerWheels[0].FullName,
        $platformWheels[0].FullName
    )

if ($RequireTray) {
    # This is the candidate venv inside the still-uncommitted transaction. A
    # missing Qt DLL/module or broken platform wheel aborts and restores the old
    # app, venv, Host and launch files before the candidate app is promoted.
    Invoke-YeYuGamerNative `
        -FilePath $venvPython `
        -Arguments @(
            '-I',
            '-B',
            '-c',
            'from PySide6 import QtCore, QtGui, QtWidgets; from PySide6.QtWidgets import QSystemTrayIcon; import yeyu_gamer_platform.tray; assert QtCore.qVersion(); assert QSystemTrayIcon is not None'
        )
}

# Dependency installation must not mutate the copied application payload.  Run
# the same exact-file, size, hash, extras and reparse checks against its relocated
# install root before writing runtime configuration or shortcuts.
Test-YeYuGamerInstallBuildManifest `
    -BuildRoot $buildResolved `
    -AppRoot $incoming `
    -WheelhouseRoot $wheelhouseRoot `
    -ManifestPath $manifestPath `
    -AllowRelocatedAppRoot | Out-Null

$config = [ordered]@{
    schemaVersion = 1
    installRoot = $installResolved
    runtimeRoot = $runtimeResolved
    managerBaseUrl = $managerBaseUrl
    webUrl = $webUrl
    legacyRoot = $legacyRoot
    webDist = (Join-Path $current 'webgui\dist')
    actorTokenFile = $actorTokensDirectory
    requestTimeoutSeconds = 3
    startupTimeoutSeconds = 150
    healthRefreshSeconds = 10
    launchAtWindowsLogon = $false
}
foreach ($directory in @('logs', 'state', 'artifacts', 'secrets', 'import')) {
    New-Item -ItemType Directory -Path (Join-Path $runtimeResolved $directory) -Force | Out-Null
}
New-Item -ItemType Directory -Path $legacyRoot -Force | Out-Null

# Install the build-manifest-bound Manager Adapter Host into its own fixed local
# runtime boundary.  It never occupies the separately promoted execution-package
# slot at `legacy-night-rain-gamer\runner.exe`.
$adapterHostSourceRelative = 'adapter-host/host.exe'
$adapterHostSourceEntries = @($sourceManifest.appFiles | Where-Object {
    [string]$_.path -ceq $adapterHostSourceRelative
})
if ($adapterHostSourceEntries.Count -ne 1) {
    throw "The verified build must declare exactly one Manager Adapter Host; found $($adapterHostSourceEntries.Count)."
}
$adapterHostSourceEntry = $adapterHostSourceEntries[0]
$adapterHostSource = Join-Path $incoming 'adapter-host\host.exe'
if (-not (Test-Path -LiteralPath $adapterHostSource -PathType Leaf)) {
    throw 'The verified build is missing the Manager Adapter Host host.exe.'
}
$adapterHostSourceItem = Get-Item -LiteralPath $adapterHostSource -Force
$adapterHostSourceHash = (Get-FileHash -LiteralPath $adapterHostSource -Algorithm SHA256).Hash.ToLowerInvariant()
if ($adapterHostSourceItem.Length -ne [int64]$adapterHostSourceEntry.bytes -or
    $adapterHostSourceHash -ne ([string]$adapterHostSourceEntry.sha256).ToLowerInvariant()) {
    throw 'The Manager Adapter Host no longer matches its verified build-manifest entry.'
}

New-Item -ItemType Directory -Path $adaptersRuntimeRoot -Force | Out-Null
Assert-YeYuGamerNoReparseTree -Path $adaptersRuntimeRoot -Purpose 'runtime adapters directory' | Out-Null
if (Test-Path -LiteralPath $adapterRuntimeRoot -PathType Container) {
    Assert-YeYuGamerNoReparseTree -Path $adapterRuntimeRoot -Purpose 'Manager Adapter Host runtime directory' | Out-Null
    $unexpectedExistingItems = @(Get-ChildItem -LiteralPath $adapterRuntimeRoot -Force | Where-Object {
        $_.PSIsContainer -or $_.Name -notin @('host.exe', 'install-manifest.json')
    })
    if ($unexpectedExistingItems.Count -gt 0) {
        throw "Manager Adapter Host runtime contains an unexpected item: $($unexpectedExistingItems[0].FullName)"
    }
} else {
    New-Item -ItemType Directory -Path $adapterRuntimeRoot -Force | Out-Null
}
$adapterHostTarget = Join-Path $adapterRuntimeRoot 'host.exe'
$adapterHostIncoming = Assert-YeYuGamerChildPath `
    -Parent $adapterRuntimeRoot `
    -Child (Join-Path $adapterRuntimeRoot ('.host.incoming.{0}.exe' -f [Guid]::NewGuid().ToString('N'))) `
    -Purpose 'Manager Adapter Host incoming file'
try {
    Copy-Item -LiteralPath $adapterHostSource -Destination $adapterHostIncoming -Force
    $adapterHostIncomingItem = Get-Item -LiteralPath $adapterHostIncoming -Force
    $adapterHostIncomingHash = (Get-FileHash -LiteralPath $adapterHostIncoming -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($adapterHostIncomingItem.Length -ne [int64]$adapterHostSourceEntry.bytes -or
        $adapterHostIncomingHash -ne ([string]$adapterHostSourceEntry.sha256).ToLowerInvariant()) {
        throw 'The Manager Adapter Host size or hash changed while copying to the runtime.'
    }
    Move-Item -LiteralPath $adapterHostIncoming -Destination $adapterHostTarget -Force
} finally {
    if (Test-Path -LiteralPath $adapterHostIncoming -PathType Leaf) {
        Remove-Item -LiteralPath $adapterHostIncoming -Force
    }
}
$adapterHostHash = (Get-FileHash -LiteralPath $adapterHostTarget -Algorithm SHA256).Hash.ToLowerInvariant()

# host.exe is built as a Windows-subsystem executable so it never creates a
# console window. PowerShell cannot reliably consume stdout from such an
# executable through a direct pipeline, so every installer probe uses explicit
# redirected files and checks the process exit code before parsing JSON.
function Invoke-YeYuGamerInstalledAdapterHostCheck {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    $checkId = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Assert-YeYuGamerChildPath `
        -Parent $adapterRuntimeRoot `
        -Child (Join-Path $adapterRuntimeRoot "host-check-$checkId.stdout") `
        -Purpose 'installed Adapter Host stdout'
    $stderrPath = Assert-YeYuGamerChildPath `
        -Parent $adapterRuntimeRoot `
        -Child (Join-Path $adapterRuntimeRoot "host-check-$checkId.stderr") `
        -Purpose 'installed Adapter Host stderr'
    try {
        $process = Start-Process `
            -FilePath $adapterHostTarget `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru
        $stdout = [System.IO.File]::ReadAllText($stdoutPath)
        $stderr = [System.IO.File]::ReadAllText($stderrPath)
        if ($stderr) {
            throw "The installed Adapter Host wrote stderr: $stderr"
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

$installedAdapterProbeCheck = Invoke-YeYuGamerInstalledAdapterHostCheck `
    -Arguments @('--operation', 'probe', '--protocol-version', '1.1')
$installedAdapterProbe = $installedAdapterProbeCheck.Document
if ($installedAdapterProbeCheck.ExitCode -ne 0 -or
    -not $installedAdapterProbe.success -or
    -not $installedAdapterProbe.hostReady -or
    $installedAdapterProbe.executionReady -or
    $installedAdapterProbe.entryPointSha256 -ne $adapterHostHash) {
    throw 'The installed Adapter Host failed its fixed protocol/hash probe.'
}
$installedAdapterCanaryCheck = Invoke-YeYuGamerInstalledAdapterHostCheck `
    -Arguments @(
        '--operation', 'canary',
        '--protocol-version', '1.1',
        '--run-id', ([Guid]::NewGuid().ToString('D')),
        '--game-id', 'StarRail'
    )
$installedAdapterCanary = $installedAdapterCanaryCheck.Document
if ($installedAdapterCanaryCheck.ExitCode -ne 0 -or
    -not $installedAdapterCanary.success -or
    $installedAdapterCanary.code -ne 'canary_passed' -or
    $installedAdapterCanary.adapterProcessStarted -or
    $installedAdapterCanary.gameProcessStarted) {
    throw 'The installed Adapter Host failed its no-input canary.'
}
$adapterHostManifestPath = Join-Path $adapterRuntimeRoot 'install-manifest.json'
$adapterHostManifest = [ordered]@{
    schemaVersion = 1
    packageId = 'manager-adapter-host'
    hostVersion = [string]$installedAdapterProbe.hostVersion
    protocolVersion = [string]$installedAdapterProbe.protocolVersion
    entryPoint = 'host.exe'
    sha256 = $adapterHostHash
    sizeBytes = (Get-Item -LiteralPath $adapterHostTarget).Length
    hostReady = $true
    executionReady = $false
    supportedGameIds = @($installedAdapterProbe.supportedGameIds)
    installedAt = [DateTimeOffset]::UtcNow.ToString('o')
}
Write-YeYuGamerJson -Path $adapterHostManifestPath -Value $adapterHostManifest
Protect-YeYuGamerRuntimePackageDirectory -Path $adapterRuntimeRoot | Out-Null
$verifiedAdapterHostManifest = Test-YeYuGamerRuntimePackage `
    -PackageRoot $adapterRuntimeRoot `
    -ManifestPath $adapterHostManifestPath `
    -ExpectedPackageId 'manager-adapter-host' `
    -ExpectedEntryPoint 'host.exe'

$executionPackageRoot = Assert-YeYuGamerChildPath `
    -Parent $adaptersRuntimeRoot `
    -Child (Join-Path $adaptersRuntimeRoot 'legacy-night-rain-gamer') `
    -Purpose 'legacy execution package directory'
if (Test-Path -LiteralPath $executionPackageRoot) {
    Assert-YeYuGamerNoReparseTree -Path $executionPackageRoot -Purpose 'legacy execution package directory' | Out-Null
}
$executionPackagePath = Assert-YeYuGamerChildPath `
    -Parent $executionPackageRoot `
    -Child (Join-Path $executionPackageRoot 'runner.exe') `
    -Purpose 'legacy execution package entry point'
$executionPackageStatus = if (Test-Path -LiteralPath $executionPackagePath -PathType Leaf) {
    Write-Warning 'A separately managed legacy execution package is present; the base installer did not install or validate it.'
    'present-external'
} else {
    'missing'
}
Protect-YeYuGamerActorTokensDirectory -Path $actorTokensDirectory | Out-Null
Protect-YeYuGamerNotificationSecretsDirectory -Path $notificationSecretsDirectory | Out-Null
if (Test-Path -LiteralPath $retiredTrayToken -PathType Leaf) {
    Remove-Item -LiteralPath $retiredTrayToken -Force
} elseif (Test-Path -LiteralPath $retiredTrayToken) {
    throw "Retired persisted tray token path is not a regular file: $retiredTrayToken"
}
Invoke-YeYuGamerLegacySeed `
    -PythonPath $venvPython `
    -CandidateSourceRoot $legacySeedSource `
    -DestinationRoot $legacyRoot
Move-Item -LiteralPath $incoming -Destination $current
Test-YeYuGamerInstallBuildManifest `
    -BuildRoot $buildResolved `
    -AppRoot $current `
    -WheelhouseRoot $wheelhouseRoot `
    -ManifestPath $manifestPath `
    -AllowRelocatedAppRoot | Out-Null
Write-YeYuGamerJson -Path $configPath -Value $config

$installManifest = [ordered]@{
    schemaVersion = 2
    installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    installLayout = [ordered]@{
        application = 'app'
        environment = '.venv'
        configuration = 'config\platform.json'
    }
    buildProvenance = [ordered]@{
        manifestSchemaVersion = [int]$sourceManifest.schemaVersion
        dependencyLockSha256 = [string]$sourceManifest.dependencyLock.sha256
        dependencyProvenanceSha256 = [string]$sourceManifest.dependencyLock.provenanceSha256
        appFileCount = @($sourceManifest.appFiles).Count
        wheelhouseFileCount = @($sourceManifest.wheelhouseFiles).Count
    }
    launchAtWindowsLogon = $false
    adapterHost = [ordered]@{
        packageId = [string]$verifiedAdapterHostManifest.packageId
        protocolVersion = [string]$verifiedAdapterHostManifest.protocolVersion
        sha256 = [string]$verifiedAdapterHostManifest.sha256
        sizeBytes = [int64]$verifiedAdapterHostManifest.sizeBytes
        executionReady = $false
    }
    executionPackage = [ordered]@{
        packageId = 'legacy-night-rain-gamer'
        status = $executionPackageStatus
        installedByBaseInstaller = $false
    }
}
Write-YeYuGamerJson -Path $installManifestPath -Value $installManifest

$cliWrapper = @"
@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
"$venvPython" -I -B -X utf8 -m yeyu_gamer_platform.cli --config "$configPath" %*
exit /b %errorlevel%
"@
[System.IO.File]::WriteAllText($cliWrapperPath, $cliWrapper, [System.Text.UTF8Encoding]::new($false))

New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
$desktopHostExecutable = Join-Path $current 'desktop-host\YeYuGamer.exe'
if (-not (Test-Path -LiteralPath $desktopHostExecutable -PathType Leaf)) {
    throw "The installed single-process desktop host is missing: $desktopHostExecutable"
}
foreach ($hostShortcutPath in @($shortcutPath) + $existingDesktopShortcuts) {
    Set-YeYuGamerHostShortcut -Path $hostShortcutPath -HostExecutable $desktopHostExecutable `
        -ConfigPath $configPath -WorkingDirectory $current
}

# Keep login-startup cleanup as the last fallible action. Startup shortcut files
# participate in the file transaction and HKCU Run values were snapshotted
# before the candidate transaction began.
& (Join-Path $PSScriptRoot 'Disable-YeYuGamerAutoStart.ps1')
}
} catch {
    $installFailure = $_
    try {
        Restore-YeYuGamerAutoStartRegistrySnapshot -Snapshot $autoStartRegistrySnapshot
    } catch {
        throw "Installation failed ($($installFailure.Exception.Message)) and HKCU Run rollback also failed: $($_.Exception.Message)"
    }
    throw $installFailure
}
} finally {
    Exit-YeYuGamerInstallLifecycleLock -Mutex $installLifecycleLock
}

Write-Host "YeYu Gamer installed at: $installResolved"
Write-Host "Runtime and logs: $runtimeResolved"
Write-Host 'Login startup remains disabled.'
if ($StartAfterInstall) {
    & (Join-Path $current 'scripts\Start-YeYuGamer.ps1') -InstallRoot $installResolved -RuntimeRoot $runtimeResolved
}
