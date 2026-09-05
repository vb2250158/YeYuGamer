[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TestRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\platform-test'),
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
$testResolved = Reset-YeYuGamerOwnedDirectory -Path $TestRoot -Kind PlatformTest
$platformSource = Join-Path $SourceRoot 'platform'
$stagedPlatform = Join-Path $testResolved 'platform'
New-Item -ItemType Directory -Path $stagedPlatform -Force | Out-Null
$platformSourcePrefix = [System.IO.Path]::GetFullPath($platformSource).TrimEnd('\') + '\'
Get-ChildItem -LiteralPath $platformSource -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($platformSourcePrefix.Length)
    $segments = $relative -split '[\\/]'
    if (@($segments | Where-Object {
        $_ -in @('__pycache__', '.pytest_cache', 'build') -or $_ -like '*.egg-info'
    }).Count -gt 0 -or $_.Extension -eq '.pyc') {
        return
    }
    $destination = Join-Path $stagedPlatform $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
}

$rejectedBuildRoots = @(
    [System.IO.Path]::GetPathRoot($env:WINDIR),
    $env:WINDIR,
    [Environment]::GetFolderPath('UserProfile'),
    (Get-YeYuGamerCurrentProgramData),
    (Join-Path $testResolved 'arbitrary-build')
)
foreach ($rejectedBuildRoot in $rejectedBuildRoots) {
    $rejected = $false
    try {
        Assert-YeYuGamerFixedProductPath -Path $rejectedBuildRoot -Kind Build | Out-Null
    } catch { $rejected = $true }
    if (-not $rejected) {
        throw "BuildRoot validator accepted a forbidden path: $rejectedBuildRoot"
    }
}

$arbitraryBuildRoot = Join-Path $testResolved 'arbitrary-build'
$arbitraryBuildMarker = Join-Path $arbitraryBuildRoot 'must-survive.txt'
New-Item -ItemType Directory -Path $arbitraryBuildRoot -Force | Out-Null
[System.IO.File]::WriteAllText(
    $arbitraryBuildMarker,
    'must-survive',
    [System.Text.UTF8Encoding]::new($false)
)
$buildScriptRejectedArbitraryRoot = $false
try {
    & (Join-Path $SourceRoot 'scripts\Build-YeYuGamer.ps1') `
        -SourceRoot $SourceRoot `
        -BuildRoot $arbitraryBuildRoot `
        -SkipWebBuild `
        -SkipTests
} catch { $buildScriptRejectedArbitraryRoot = $true }
if (-not $buildScriptRejectedArbitraryRoot -or
    -not (Test-Path -LiteralPath $arbitraryBuildMarker -PathType Leaf)) {
    throw 'Build script did not reject an arbitrary local BuildRoot before deletion.'
}

$boundaryFixture = Join-Path (Get-YeYuGamerDefaultProductWorkRoot) 'boundary-fixture'
$boundaryFixture = Reset-YeYuGamerOwnedDirectory -Path $boundaryFixture -Kind BoundaryFixture
$boundarySentinel = Join-Path $boundaryFixture '.yeyu-gamer-owned-directory'
$boundaryMarker = Join-Path $boundaryFixture 'must-survive.txt'
Remove-Item -LiteralPath $boundarySentinel -Force
[System.IO.File]::WriteAllText(
    $boundaryMarker,
    'must-survive',
    [System.Text.UTF8Encoding]::new($false)
)
$missingSentinelRejected = $false
try {
    Reset-YeYuGamerOwnedDirectory -Path $boundaryFixture -Kind BoundaryFixture | Out-Null
} catch { $missingSentinelRejected = $true }
if (-not $missingSentinelRejected -or
    -not (Test-Path -LiteralPath $boundaryMarker -PathType Leaf)) {
    throw 'Owned-directory cleanup did not fail closed without its sentinel.'
}
Remove-Item -LiteralPath $boundaryMarker -Force
Remove-Item -LiteralPath $boundaryFixture -Force
Reset-YeYuGamerOwnedDirectory -Path $boundaryFixture -Kind BoundaryFixture | Out-Null
$boundaryLinkTarget = Join-Path $testResolved 'boundary-link-target'
$boundaryLink = Join-Path $boundaryFixture 'linked'
New-Item -ItemType Directory -Path $boundaryLinkTarget -Force | Out-Null
$boundaryLinkCreated = $false
try {
    New-Item -ItemType Junction -Path $boundaryLink -Target $boundaryLinkTarget -Force | Out-Null
    $boundaryLinkCreated = $true
    $reparseTreeRejected = $false
    try {
        Assert-YeYuGamerFixedProductPath -Path $boundaryFixture -Kind BoundaryFixture | Out-Null
    } catch { $reparseTreeRejected = $true }
    if (-not $reparseTreeRejected) {
        throw 'Owned-directory validation accepted a reparse point in its cleanup tree.'
    }
} finally {
    if ($boundaryLinkCreated -and (Test-Path -LiteralPath $boundaryLink)) {
        Remove-Item -LiteralPath $boundaryLink -Force
    }
}

$python = Get-YeYuGamerPython -ExplicitPath $PythonPath
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $stagedPlatform
    Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @('-m', 'compileall', '-q', $stagedPlatform)
    Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @('-m', 'unittest', 'discover', '-s', (Join-Path $stagedPlatform 'tests'), '-v')
} finally { $env:PYTHONPATH = $oldPythonPath }

$parseFailures = @()
Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'scripts') -Filter '*.ps1' -File | ForEach-Object {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    foreach ($error in $errors) { $parseFailures += "$($_.Name): $($error.Message)" }
}
if ($parseFailures.Count -gt 0) { throw "PowerShell parse failures:`n$($parseFailures -join "`n")" }

$startSource = Get-Content -LiteralPath (Join-Path $SourceRoot 'scripts\Start-YeYuGamer.ps1') -Raw
if (-not $startSource.Contains('yeyu_gamer_platform.tray --config $quotedConfig --ensure-manager') -or
    -not $startSource.Contains('Invoke-YeYuGamerTrayIpc') -or
    -not $startSource.Contains('-Command ensure_manager') -or
    -not $startSource.Contains('-Command open_webgui')) {
    throw 'Start script does not use completion-confirmed tray pairing and WebGUI IPC.'
}
if ($startSource.Contains('--open-webgui') -or
    $startSource.Contains('yeyu_gamer_platform.cli --config $configPath open-webgui') -or
    ([regex]::Matches($startSource, 'manager-start')).Count -ne 1) {
    throw 'Start script still uses a deprecated open flag or an unpaired tray-mode CLI Manager start.'
}
if ($startSource.IndexOf('-Command ensure_manager') -gt $startSource.IndexOf('-Command open_webgui') -or
    $startSource.IndexOf('-Command open_webgui') -gt $startSource.LastIndexOf('Windows accepted the authenticated WebGUI open request')) {
    throw 'Start success is not ordered after completion-confirmed pairing and WebGUI open.'
}

$actorAclFixture = Join-Path $testResolved 'actor-token-acl\actors'
Protect-YeYuGamerActorTokensDirectory -Path $actorAclFixture | Out-Null
$actorAcl = Get-Acl -LiteralPath $actorAclFixture
if (-not $actorAcl.AreAccessRulesProtected) {
    throw 'Actor token directory ACL inheritance was not disabled.'
}
$actorRules = @($actorAcl.GetAccessRules(
    $true,
    $false,
    [Security.Principal.SecurityIdentifier]
))
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$actorRuleSids = @($actorRules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
if (@($actorRuleSids | Where-Object { $_ -notin @($currentSid, 'S-1-5-18') }).Count -gt 0 -or
    $currentSid -notin $actorRuleSids -or 'S-1-5-18' -notin $actorRuleSids) {
    throw "Actor token directory ACL principals are not limited to current user and SYSTEM: $($actorRuleSids -join ', ')"
}
if (@(Get-ChildItem -LiteralPath $actorAclFixture -File -Force).Count -ne 0) {
    throw 'ACL provisioning generated a shared actor token.'
}

$notificationAclFixture = Join-Path $testResolved 'notification-secret-acl\notifications'
New-Item -ItemType Directory -Path $notificationAclFixture -Force | Out-Null
[System.IO.File]::WriteAllBytes(
    (Join-Path $notificationAclFixture 'profile.dpapi'),
    [byte[]](1, 2, 3, 4)
)
Protect-YeYuGamerNotificationSecretsDirectory -Path $notificationAclFixture | Out-Null
foreach ($aclTarget in @(
    $notificationAclFixture,
    (Join-Path $notificationAclFixture 'profile.dpapi')
)) {
    $notificationAcl = Get-Acl -LiteralPath $aclTarget
    if (-not $notificationAcl.AreAccessRulesProtected) {
        throw "Notification secret ACL inheritance was not disabled: $aclTarget"
    }
    $notificationRules = @($notificationAcl.GetAccessRules(
        $true,
        $false,
        [Security.Principal.SecurityIdentifier]
    ))
    $notificationRuleSids = @($notificationRules |
        ForEach-Object { $_.IdentityReference.Value } |
        Sort-Object -Unique)
    if (@($notificationRuleSids | Where-Object {
        $_ -notin @($currentSid, 'S-1-5-18')
    }).Count -gt 0 -or
        $currentSid -notin $notificationRuleSids -or
        'S-1-5-18' -notin $notificationRuleSids) {
        throw "Notification secret ACL principals are not limited to current user and SYSTEM: $($notificationRuleSids -join ', ')"
    }
}

$runtimePackageFixture = Join-Path $testResolved 'runtime-package\manager-adapter-host'
New-Item -ItemType Directory -Path $runtimePackageFixture -Force | Out-Null
$runtimePackageEntryPoint = Join-Path $runtimePackageFixture 'host.exe'
[System.IO.File]::WriteAllText(
    $runtimePackageEntryPoint,
    'fixed-host-fixture',
    [System.Text.UTF8Encoding]::new($false)
)
$runtimePackageManifestPath = Join-Path $runtimePackageFixture 'install-manifest.json'
$runtimePackageManifest = [ordered]@{
    schemaVersion = 1
    packageId = 'manager-adapter-host'
    hostVersion = 'test'
    protocolVersion = '1.1'
    entryPoint = 'host.exe'
    sha256 = (Get-FileHash -LiteralPath $runtimePackageEntryPoint -Algorithm SHA256).Hash.ToLowerInvariant()
    sizeBytes = (Get-Item -LiteralPath $runtimePackageEntryPoint).Length
    hostReady = $true
    executionReady = $false
    supportedGameIds = @('StarRail')
    installedAt = [DateTimeOffset]::UtcNow.ToString('o')
}
Write-YeYuGamerJson -Path $runtimePackageManifestPath -Value $runtimePackageManifest
Protect-YeYuGamerRuntimePackageDirectory -Path $runtimePackageFixture | Out-Null
Test-YeYuGamerRuntimePackage `
    -PackageRoot $runtimePackageFixture `
    -ManifestPath $runtimePackageManifestPath `
    -ExpectedPackageId 'manager-adapter-host' `
    -ExpectedEntryPoint 'host.exe' | Out-Null

[System.IO.File]::AppendAllText($runtimePackageEntryPoint, '-tampered')
$runtimePackageTamperRejected = $false
try {
    Test-YeYuGamerRuntimePackage `
        -PackageRoot $runtimePackageFixture `
        -ManifestPath $runtimePackageManifestPath `
        -ExpectedPackageId 'manager-adapter-host' `
        -ExpectedEntryPoint 'host.exe' | Out-Null
} catch { $runtimePackageTamperRejected = $true }
if (-not $runtimePackageTamperRejected) {
    throw 'Runtime package verifier accepted a size/hash-tampered Host.'
}
[System.IO.File]::WriteAllText(
    $runtimePackageEntryPoint,
    'fixed-host-fixture',
    [System.Text.UTF8Encoding]::new($false)
)

$runtimePackageExtra = Join-Path $runtimePackageFixture 'undeclared.txt'
[System.IO.File]::WriteAllText(
    $runtimePackageExtra,
    'extra',
    [System.Text.UTF8Encoding]::new($false)
)
$runtimePackageExtraRejected = $false
try {
    Test-YeYuGamerRuntimePackage `
        -PackageRoot $runtimePackageFixture `
        -ManifestPath $runtimePackageManifestPath `
        -ExpectedPackageId 'manager-adapter-host' `
        -ExpectedEntryPoint 'host.exe' | Out-Null
} catch { $runtimePackageExtraRejected = $true }
if (-not $runtimePackageExtraRejected) {
    throw 'Runtime package verifier accepted an undeclared Host file.'
}
Remove-Item -LiteralPath $runtimePackageExtra -Force

$runtimePackageLinkTarget = Join-Path $testResolved 'runtime-package-link-target'
$runtimePackageLink = Join-Path $runtimePackageFixture 'linked'
New-Item -ItemType Directory -Path $runtimePackageLinkTarget -Force | Out-Null
$runtimePackageLinkCreated = $false
try {
    New-Item -ItemType Junction -Path $runtimePackageLink -Target $runtimePackageLinkTarget -Force | Out-Null
    $runtimePackageLinkCreated = $true
    $runtimePackageReparseRejected = $false
    try {
        Test-YeYuGamerRuntimePackage `
            -PackageRoot $runtimePackageFixture `
            -ManifestPath $runtimePackageManifestPath `
            -ExpectedPackageId 'manager-adapter-host' `
            -ExpectedEntryPoint 'host.exe' | Out-Null
    } catch { $runtimePackageReparseRejected = $true }
    if (-not $runtimePackageReparseRejected) {
        throw 'Runtime package verifier accepted a reparse point.'
    }
} finally {
    if ($runtimePackageLinkCreated -and (Test-Path -LiteralPath $runtimePackageLink)) {
        $verifiedRuntimePackageLink = Assert-YeYuGamerChildPath `
            -Parent $runtimePackageFixture `
            -Child $runtimePackageLink `
            -Purpose 'runtime package reparse fixture cleanup'
        Remove-Item -LiteralPath $verifiedRuntimePackageLink -Force
    }
}

$installSource = Get-Content -LiteralPath (Join-Path $SourceRoot 'scripts\Install-YeYuGamer.ps1') -Raw
$installTokens = $null
$installParseErrors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
    $installSource,
    [ref]$installTokens,
    [ref]$installParseErrors
) | Out-Null
if ($installParseErrors.Count -gt 0) {
    throw "Installer parser check failed: $($installParseErrors[0].Message)"
}
if (-not $installSource.Contains("runtimeResolved 'import\legacy-config'")) {
    throw 'Installer legacyRoot is not isolated under the local runtime import directory.'
}
if (-not $installSource.Contains('Invoke-YeYuGamerLegacySeed') -or
    -not $installSource.Contains('Split-Path -Parent $sourceResolved')) {
    throw 'Installer does not invoke the one-time structured legacy seed exporter.'
}
if (-not $installSource.Contains("application = 'app'") -or
    $installSource.Contains("application = 'current'")) {
    throw 'Installed layout manifest does not name the actual app directory.'
}
foreach ($requiredOfflineInstallerFragment in @(
    "Join-Path `$buildResolved 'wheelhouse'",
    'Test-YeYuGamerInstallBuildManifest',
    "'appFiles'",
    "'wheelhouseFiles'",
    "'requirements.lock'",
    "'--isolated'",
    "'--no-index'",
    "'--find-links'",
    "'--only-binary=:all:'",
    "'--require-hashes'",
    "'yeyu_gamer_manager-*.whl'",
    "'yeyu_gamer_platform-*.whl'",
    '-AllowRelocatedAppRoot'
)) {
    if (-not $installSource.Contains($requiredOfflineInstallerFragment)) {
        throw "Installer offline wheelhouse contract is missing: $requiredOfflineInstallerFragment"
    }
}
$committedDependencyLock = Join-Path $SourceRoot 'packaging\requirements.cpython312-win_amd64.lock'
if (-not (Test-Path -LiteralPath $committedDependencyLock -PathType Leaf) -or
    [System.IO.File]::ReadAllText($committedDependencyLock) -notmatch '(?m)^PySide6==6\.11\.2 --hash=sha256:[0-9a-f]{64}$') {
    throw 'Committed dependency lock does not pin PySide6 6.11.2 with a trusted hash.'
}
if (([regex]::Matches($installSource, "'pip'")).Count -ne 2 -or
    ([regex]::Matches($installSource, "'--no-index'")).Count -ne 2 -or
    ([regex]::Matches($installSource, "'--find-links'")).Count -ne 2 -or
    # Two pip arguments plus one exact requirements.lock directive validator.
    ([regex]::Matches($installSource, "'--only-binary=:all:'")).Count -ne 3 -or
    ([regex]::Matches($installSource, "'--require-hashes'")).Count -ne 1) {
    throw 'Every installer pip call is not pinned to the two expected offline wheel-only flows.'
}
if ($installSource.Contains("incoming 'platform\requirements.txt'") -or
    $installSource.Contains("backendRoot 'requirements.txt'") -or
    $installSource.Contains("backendRoot 'dist'")) {
    throw 'Installer still contains an online/source-tree dependency installation path.'
}
if (-not $installSource.Contains('if ($RequireTray)') -or
    -not $installSource.Contains('from PySide6 import QtCore, QtGui, QtWidgets') -or
    -not $installSource.Contains('from PySide6.QtWidgets import QSystemTrayIcon') -or
    -not $installSource.Contains('import yeyu_gamer_platform.tray')) {
    throw 'Installer does not smoke-test PySide6/Qt/platform imports for -RequireTray.'
}
if (-not $installSource.Contains('$retiredTrayToken') -or
    -not $installSource.Contains('$legacyStartupShortcuts') -or
    -not $installSource.Contains('Get-YeYuGamerAutoStartRegistrySnapshot') -or
    -not $installSource.Contains('Restore-YeYuGamerAutoStartRegistrySnapshot')) {
    throw 'Installer rollback does not cover reversible startup/token side effects.'
}
if ($installSource.Contains('managerCommand') -or
    $installSource.Contains('managerWorkingDirectory')) {
    throw 'Installer still writes a mutable Manager command or working directory.'
}
if (-not $installSource.Contains('$shortcut.TargetPath = $HostExecutable') -or
    -not $installSource.Contains('Set-YeYuGamerHostShortcut -Path $hostShortcutPath -HostExecutable $desktopHostExecutable') -or
    -not $installSource.Contains(') + $legacyStartupShortcuts + $existingDesktopShortcuts') -or
    $installSource.Contains('--ensure-manager --open-webgui')) {
    throw 'Installer shortcuts must use the single Host and preserve existing desktop icons transactionally.'
}
if (-not $installSource.Contains("(Join-Path `$actorTokensDirectory 'tray.token')") -or
    -not $installSource.Contains('Remove-Item -LiteralPath $retiredTrayToken -Force')) {
    throw 'Installer does not revoke the retired persisted tray bootstrap token.'
}
if (-not $installSource.Contains('set "PYTHONUTF8=1"') -or
    -not $installSource.Contains('set "PYTHONIOENCODING=utf-8"') -or
    -not $installSource.Contains('set "PYTHONDONTWRITEBYTECODE=1"') -or
    -not $installSource.Contains('-I -B -X utf8 -m yeyu_gamer_platform.cli')) {
    throw 'Installed CLI launcher does not force UTF-8 for Unicode JSON output.'
}
$commonSource = Get-Content -LiteralPath (Join-Path $SourceRoot 'scripts\YeYuGamer.Common.ps1') -Raw
if (-not $commonSource.Contains("'-B',") -or
    -not $commonSource.Contains('yeyu_gamer_manager.services.legacy_seed')) {
    throw 'Legacy seed exporter is not protected from writing bytecode.'
}
foreach ($requiredListenerContract in @(
    'IPGlobalProperties',
    'GetActiveTcpListeners',
    'Local TCP listener enumeration failed',
    'Any local listener on the fixed port is a blocker'
)) {
    if (-not $commonSource.Contains($requiredListenerContract)) {
        throw "Installer listener enumeration contract is missing: $requiredListenerContract"
    }
}
if ($commonSource.Contains('[System.Net.Sockets.TcpClient]') -or $commonSource.Contains('BeginConnect(')) {
    throw 'Installer lifecycle preflight still uses a firewall-sensitive outbound TCP connection probe.'
}
foreach ($lifecycleScriptName in @(
    'Start-YeYuGamer.ps1',
    'Stop-YeYuGamer.ps1',
    'Restart-YeYuGamer.ps1'
)) {
    $lifecycleSource = Get-Content -LiteralPath (Join-Path $SourceRoot "scripts\$lifecycleScriptName") -Raw
    if (-not $lifecycleSource.Contains("`$env:PYTHONDONTWRITEBYTECODE = '1'") -or
        -not $lifecycleSource.Contains('$python -I -B -m yeyu_gamer_platform.cli')) {
        throw "$lifecycleScriptName does not prevent runtime bytecode writes."
    }
}
foreach ($legacyWrapperName in @(
    'NightRainGamer.bat.template',
    'Start-DailyGame-GUI.bat.template'
)) {
    $legacyWrapperSource = Get-Content `
        -LiteralPath (Join-Path $SourceRoot "scripts\legacy-wrappers\$legacyWrapperName") `
        -Raw
    if (-not $legacyWrapperSource.Contains('PYTHONDONTWRITEBYTECODE=1') -or
        -not $legacyWrapperSource.Contains('"%YEYU_PYTHON%" -B -m')) {
        throw "$legacyWrapperName does not prevent runtime bytecode writes."
    }
}
$stopSource = Get-Content -LiteralPath (Join-Path $SourceRoot 'scripts\Stop-YeYuGamer.ps1') -Raw
if (-not $stopSource.Contains('Get-YeYuGamerRecordedManagerProcess') -or
    -not $installSource.Contains('Assert-YeYuGamerInstallLifecycleStopped') -or
    -not $commonSource.Contains('Get-YeYuGamerRecordedManagerProcess') -or
    -not $commonSource.Contains('Test-YeYuGamerNamedMutexActive') -or
    -not $commonSource.Contains("'http://127.0.0.1:8877/api/v1'") -or
    -not $commonSource.Contains("'Local\YeYuGamer.InstallLifecycle.v1'")) {
    throw 'Stop and installer lifecycle checks do not cover the recorded Manager host and tray.'
}
$forbiddenInstallerUrlInputs = @('[string]$ManagerBaseUrl', '[string]$WebUrl', '-ManagerEndpoint')
foreach ($forbiddenInstallerUrlInput in $forbiddenInstallerUrlInputs) {
    if ($installSource.Contains($forbiddenInstallerUrlInput)) {
        throw "Installer still accepts a mutable endpoint input: $forbiddenInstallerUrlInput"
    }
}
$installPreflightIndex = $installSource.IndexOf('Assert-YeYuGamerInstallLifecycleStopped')
$installLockIndex = $installSource.IndexOf('$installLifecycleLock = Enter-YeYuGamerInstallLifecycleLock')
$installTransactionIndex = $installSource.IndexOf('Invoke-YeYuGamerInstallTransaction')
foreach ($protectedMutation in @(
    'if (-not $SkipBuild)',
    'New-Item -ItemType Directory -Path $installResolved',
    'Invoke-YeYuGamerInstallTransaction'
)) {
    $mutationIndex = $installSource.IndexOf($protectedMutation)
    if ($installLockIndex -lt 0 -or $mutationIndex -lt 0 -or $installLockIndex -ge $mutationIndex) {
        throw "Installer does not own the OS lifecycle lock before mutation: $protectedMutation"
    }
}
if ($installPreflightIndex -lt 0 -or $installTransactionIndex -lt 0 -or
    $installPreflightIndex -ge $installTransactionIndex -or
    -not $installSource.Contains('Exit-YeYuGamerInstallLifecycleLock')) {
    throw 'Installer does not hold its lifecycle lock from preflight through the install transaction.'
}
foreach ($transactionalArtifact in @(
    '$current; PreserveAs = $previous',
    '$venvRoot; PreserveAs = $venvPrevious',
    '$adapterRuntimeRoot; PreserveAs = $adapterRuntimePrevious',
    '$transactionFileTargets = @(',
    '-FileTargets $transactionFileTargets'
)) {
    if (-not $installSource.Contains($transactionalArtifact)) {
        throw "Installer transaction omits rollback state: $transactionalArtifact"
    }
}
$transactionTargetsStart = $installSource.IndexOf('$transactionFileTargets = @(')
$transactionTargetsEnd = $installSource.IndexOf(') + $legacyStartupShortcuts', $transactionTargetsStart)
if ($transactionTargetsStart -lt 0 -or $transactionTargetsEnd -lt 0) {
    throw 'Installer transaction file target list could not be located.'
}
$transactionTargetsBody = $installSource.Substring(
    $transactionTargetsStart,
    $transactionTargetsEnd - $transactionTargetsStart
)
foreach ($transactionalFile in @('$configPath', '$installManifestPath', '$cliWrapperPath', '$shortcutPath', '$retiredTrayToken')) {
    if (-not $transactionTargetsBody.Contains($transactionalFile)) {
        throw "Installer transaction omits rollback file target: $transactionalFile"
    }
}
$hostCheckIndex = $installSource.IndexOf('$installedAdapterProbeCheck = Invoke-YeYuGamerInstalledAdapterHostCheck')
$promoteIndex = $installSource.IndexOf('Move-Item -LiteralPath $incoming -Destination $current')
$configWriteIndex = $installSource.IndexOf('Write-YeYuGamerJson -Path $configPath -Value $config')
if ($hostCheckIndex -le $installTransactionIndex -or $promoteIndex -le $hostCheckIndex -or
    $configWriteIndex -le $promoteIndex) {
    throw 'Installer does not keep dependency/Host checks and post-promote config writes inside the rollback transaction.'
}

function New-YeYuGamerInstallPreflightFixture {
    param([Parameter(Mandatory = $true)][string]$Name)

    $caseRoot = Join-Path $testResolved "install-lifecycle\$Name"
    $installRoot = Join-Path $caseRoot 'install'
    $runtimeRoot = Join-Path $caseRoot 'runtime'
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    foreach ($relativeDirectory in @('app', 'app.incoming', 'app.previous')) {
        $directory = Join-Path $installRoot $relativeDirectory
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        [System.IO.File]::WriteAllText(
            (Join-Path $directory 'preflight-sentinel.txt'),
            "unchanged:${Name}:$relativeDirectory",
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    return [pscustomobject]@{
        InstallRoot = $installRoot
        RuntimeRoot = $runtimeRoot
    }
}

function Get-YeYuGamerInstallTreeFingerprint {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    return (@(Get-ChildItem -LiteralPath $root -Recurse -Force | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($root.Length).TrimStart('\')
        if ($_.PSIsContainer) { return "D|$relative" }
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        return "F|$relative|$($_.Length)|$hash"
    }) -join "`n")
}

function Assert-YeYuGamerLifecyclePreflightRejects {
    param(
        [Parameter(Mandatory = $true)][object]$Fixture,
        [Parameter(Mandatory = $true)][string]$ExpectedBlocker
    )

    $before = Get-YeYuGamerInstallTreeFingerprint -InstallRoot $Fixture.InstallRoot
    $rejected = $false
    try {
        Assert-YeYuGamerInstallLifecycleStopped -RuntimeRoot $Fixture.RuntimeRoot
    } catch {
        $rejected = $true
        $outputText = $_.Exception.Message
    }
    if (-not $rejected) { throw "Installer lifecycle preflight accepted an active blocker: $ExpectedBlocker" }
    if (-not $outputText.Contains($ExpectedBlocker)) {
        throw "Installer lifecycle preflight did not identify blocker '$ExpectedBlocker': $outputText"
    }
    $after = Get-YeYuGamerInstallTreeFingerprint -InstallRoot $Fixture.InstallRoot
    if ($after -cne $before) {
        throw "Installer lifecycle preflight mutated app/app.incoming/app.previous before rejecting: $ExpectedBlocker"
    }
}

$realListenerEnumerator = (Get-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners).ScriptBlock
try {
    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value { return @() }
    if (Test-YeYuGamerTcpEndpointActive) {
        throw 'An empty authoritative listener table was reported as an active Manager endpoint.'
    }

    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value {
        return [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Loopback, 18877)
    }
    if (Test-YeYuGamerTcpEndpointActive) {
        throw 'An unrelated local TCP listener was reported as the canonical Manager endpoint.'
    }

    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value {
        return [System.Net.IPEndPoint]::new([System.Net.IPAddress]::IPv6Any, 8877)
    }
    if (-not (Test-YeYuGamerTcpEndpointActive)) {
        throw 'A real-shaped local listener on fixed port 8877 was not reported active.'
    }

    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value {
        return [pscustomobject]@{ Port = 8877 }
    }
    $malformedListenerRejected = $false
    try { Test-YeYuGamerTcpEndpointActive | Out-Null } catch {
        $malformedListenerRejected = $_.Exception.Message.Contains('could not prove canonical endpoint')
    }
    if (-not $malformedListenerRejected) {
        throw 'A malformed listener-table entry did not fail closed.'
    }

    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value {
        throw 'simulated listener-table failure'
    }
    $listenerQueryFailureRejected = $false
    try { Test-YeYuGamerTcpEndpointActive | Out-Null } catch {
        $listenerQueryFailureRejected = $_.Exception.Message.Contains('could not prove canonical endpoint')
    }
    if (-not $listenerQueryFailureRejected) {
        throw 'An uncertain listener table did not fail closed.'
    }
} finally {
    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value $realListenerEnumerator
}

$closedFixture = New-YeYuGamerInstallPreflightFixture -Name 'closed-lifecycle'
$closedTrayMutexName = 'Local\YeYuGamer.Test.' + [Guid]::NewGuid().ToString('N')
$realListenerEnumerator = (Get-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners).ScriptBlock
try {
    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value { return @() }
    Assert-YeYuGamerInstallLifecycleStopped `
        -RuntimeRoot $closedFixture.RuntimeRoot `
        -TrayMutexName $closedTrayMutexName
} finally {
    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value $realListenerEnumerator
}

$portFixture = New-YeYuGamerInstallPreflightFixture -Name 'active-port'
$canonicalListener = $null
try {
    if (-not (Test-YeYuGamerTcpEndpointActive)) {
        $canonicalListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8877)
        $canonicalListener.Start()
    }
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $portFixture `
        -ExpectedBlocker 'canonical Manager endpoint port 8877 is active'
} finally {
    if ($null -ne $canonicalListener) { $canonicalListener.Stop() }
}

$moduleProbeRoot = Join-Path $testResolved 'module-process-probe'
$moduleProbeVenv = Join-Path $moduleProbeRoot '.venv'
$moduleProbePackage = Join-Path $moduleProbeRoot 'yeyu_gamer_platform'
New-Item -ItemType Directory -Path $moduleProbePackage -Force | Out-Null
[System.IO.File]::WriteAllText(
    (Join-Path $moduleProbePackage '__init__.py'),
    '',
    [System.Text.UTF8Encoding]::new($false)
)
$moduleProbeSource = @'
import os
from pathlib import Path
import sys
import time

ready = Path(sys.argv[1])
stop = Path(sys.argv[2])
ready.write_text(str(os.getpid()), encoding="ascii")
while not stop.exists():
    time.sleep(0.05)
'@
foreach ($moduleFileName in @('manager_host.py', 'tray.py')) {
    [System.IO.File]::WriteAllText(
        (Join-Path $moduleProbePackage $moduleFileName),
        $moduleProbeSource,
        [System.Text.UTF8Encoding]::new($false)
    )
}
Invoke-YeYuGamerNative `
    -FilePath $python `
    -Arguments @('-I', '-B', '-m', 'venv', '--without-pip', $moduleProbeVenv)
$moduleProbePython = Join-Path $moduleProbeVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $moduleProbePython -PathType Leaf)) {
    throw 'The controlled process fixture venv did not create python.exe.'
}

function Start-YeYuGamerControlledModuleProbe {
    param(
        [Parameter(Mandatory = $true)][string]$ModuleName,
        [Parameter(Mandatory = $true)][string]$ReadyPath,
        [Parameter(Mandatory = $true)][string]$StopPath
    )

    $oldProbePythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $moduleProbeRoot
        return Start-Process `
            -FilePath $moduleProbePython `
            -ArgumentList @('-B', '-m', $ModuleName, $ReadyPath, $StopPath) `
            -WorkingDirectory $moduleProbeRoot `
            -PassThru `
            -WindowStyle Hidden
    } finally {
        $env:PYTHONPATH = $oldProbePythonPath
    }
}

function Wait-YeYuGamerControlledModuleProbe {
    param([Parameter(Mandatory = $true)][string]$ReadyPath)

    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if (Test-Path -LiteralPath $ReadyPath -PathType Leaf) {
            try {
                $readyText = [System.IO.File]::ReadAllText($ReadyPath).Trim()
                $readyPid = 0
                if ([int]::TryParse($readyText, [ref]$readyPid) -and $readyPid -gt 0) {
                    return $readyPid
                }
            } catch [System.IO.IOException] {
                # The controlled child may be between create and write. Retry
                # until the bounded deadline instead of accepting an empty file.
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Controlled Python module process did not publish a valid PID: $ReadyPath"
}

function Stop-YeYuGamerControlledModuleProbe {
    param(
        [object]$Launcher,
        [Parameter(Mandatory = $true)][string]$StopPath,
        [int[]]$KnownProcessId = @()
    )

    [System.IO.File]::WriteAllText($StopPath, 'stop', [System.Text.UTF8Encoding]::new($false))
    if ($null -ne $Launcher) {
        try { Wait-Process -Id $Launcher.Id -Timeout 10 -ErrorAction Stop } catch { }
    }
    foreach ($knownProcessIdValue in @($KnownProcessId | Select-Object -Unique)) {
        if ($knownProcessIdValue -le 0) { continue }
        if ($null -ne (Get-Process -Id $knownProcessIdValue -ErrorAction SilentlyContinue)) {
            # These PIDs belong only to this controlled, local test fixture.
            Stop-Process -Id $knownProcessIdValue -Force -ErrorAction SilentlyContinue
        }
    }
}

$managerFixture = New-YeYuGamerInstallPreflightFixture -Name 'active-manager'
$managerReadyPath = Join-Path $moduleProbeRoot 'manager.ready'
$managerStopPath = Join-Path $moduleProbeRoot 'manager.stop'
$dummyManager = $null
$managerProbePids = @()
try {
    $dummyManager = Start-YeYuGamerControlledModuleProbe `
        -ModuleName 'yeyu_gamer_platform.manager_host' `
        -ReadyPath $managerReadyPath `
        -StopPath $managerStopPath
    $managerBasePid = Wait-YeYuGamerControlledModuleProbe -ReadyPath $managerReadyPath
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $managerModuleProcesses = @(Get-YeYuGamerPythonModuleProcesses `
            -ModuleName 'yeyu_gamer_platform.manager_host')
        $managerProbePids = @($managerModuleProcesses.ProcessId)
        if ($dummyManager.Id -in $managerProbePids -and $managerBasePid -in $managerProbePids) { break }
        Start-Sleep -Milliseconds 100
    }
    if ($dummyManager.Id -notin $managerProbePids -or $managerBasePid -notin $managerProbePids) {
        throw (
            'Exact module enumeration did not retain both the controlled venv launcher and base child: ' +
            "launcher=$($dummyManager.Id), base=$managerBasePid, seen=$($managerProbePids -join ',')"
        )
    }

    $managerBaseCim = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $managerBasePid" `
        -ErrorAction Stop
    $powershellCreationIdentity = Get-YeYuGamerWindowsProcessCreationIdentity `
        -Process $managerBaseCim
    $identityProbePath = Join-Path $moduleProbeRoot 'identity_probe.py'
    [System.IO.File]::WriteAllText(
        $identityProbePath,
        @'
import sys
sys.path.insert(0, sys.argv[1])
from yeyu_gamer_platform.process_control import _process_creation_identity
print(_process_creation_identity(int(sys.argv[2])))
'@,
        [System.Text.UTF8Encoding]::new($false)
    )
    $pythonIdentityOutput = @(Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @('-I', '-B', $identityProbePath, $stagedPlatform, [string]$managerBasePid))
    $pythonCreationIdentity = [string]$pythonIdentityOutput[-1]
    if ($pythonCreationIdentity.Trim() -cne $powershellCreationIdentity) {
        throw (
            'Python GetProcessTimes and PowerShell CIM CreationDate identities differ: ' +
            "python=$($pythonCreationIdentity.Trim()), powershell=$powershellCreationIdentity"
        )
    }

    Write-YeYuGamerJson `
        -Path (Join-Path $managerFixture.RuntimeRoot 'state\manager-process.json') `
        -Value ([ordered]@{
            schemaVersion = 2
            pid = $managerBasePid
            instanceId = 'controlled-process-identity-probe'
            processCreationIdentity = $pythonCreationIdentity.Trim()
        })
    $recordedControlledHost = Get-YeYuGamerRecordedManagerProcess `
        -RuntimeRoot $managerFixture.RuntimeRoot
    if ($null -eq $recordedControlledHost -or
        [int]$recordedControlledHost.ProcessId -ne $managerBasePid) {
        throw 'PowerShell did not accept the real Python schema v2 host identity for the same process.'
    }
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $managerFixture `
        -ExpectedBlocker "recorded Manager host PID $managerBasePid is active"
} finally {
    Stop-YeYuGamerControlledModuleProbe `
        -Launcher $dummyManager `
        -StopPath $managerStopPath `
        -KnownProcessId $managerProbePids
}

$trayProcessFixture = New-YeYuGamerInstallPreflightFixture -Name 'active-tray-process'
$trayReadyPath = Join-Path $moduleProbeRoot 'tray.ready'
$trayStopPath = Join-Path $moduleProbeRoot 'tray.stop'
$dummyTray = $null
$trayProbePids = @()
try {
    $dummyTray = Start-YeYuGamerControlledModuleProbe `
        -ModuleName 'yeyu_gamer_platform.tray' `
        -ReadyPath $trayReadyPath `
        -StopPath $trayStopPath
    $trayBasePid = Wait-YeYuGamerControlledModuleProbe -ReadyPath $trayReadyPath
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $trayModuleProcesses = @(Get-YeYuGamerPythonModuleProcesses `
            -ModuleName 'yeyu_gamer_platform.tray')
        $trayProbePids = @($trayModuleProcesses.ProcessId)
        if ($dummyTray.Id -in $trayProbePids -and $trayBasePid -in $trayProbePids) { break }
        Start-Sleep -Milliseconds 100
    }
    if ($dummyTray.Id -notin $trayProbePids -or $trayBasePid -notin $trayProbePids) {
        throw 'Exact tray module enumeration missed the controlled venv launcher or base child.'
    }
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $trayProcessFixture `
        -ExpectedBlocker "YeYu Gamer module yeyu_gamer_platform.tray PID"
} finally {
    Stop-YeYuGamerControlledModuleProbe `
        -Launcher $dummyTray `
        -StopPath $trayStopPath `
        -KnownProcessId $trayProbePids
}

$corruptPidFixture = New-YeYuGamerInstallPreflightFixture -Name 'corrupt-pid-record'
New-Item -ItemType Directory -Path (Join-Path $corruptPidFixture.RuntimeRoot 'state') -Force | Out-Null
[System.IO.File]::WriteAllText(
    (Join-Path $corruptPidFixture.RuntimeRoot 'state\manager-process.json'),
    '{not-json',
    [System.Text.UTF8Encoding]::new($false)
)
Assert-YeYuGamerLifecyclePreflightRejects `
    -Fixture $corruptPidFixture `
    -ExpectedBlocker 'recorded Manager PID state is uncertain'

$unreadablePidFixture = New-YeYuGamerInstallPreflightFixture -Name 'unreadable-pid-record'
$unreadablePidPath = Join-Path $unreadablePidFixture.RuntimeRoot 'state\manager-process.json'
Write-YeYuGamerJson -Path $unreadablePidPath -Value ([ordered]@{ pid = 4 })
$recordLock = [System.IO.File]::Open(
    $unreadablePidPath,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::None
)
try {
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $unreadablePidFixture `
        -ExpectedBlocker 'recorded Manager PID state is uncertain'
} finally {
    $recordLock.Dispose()
}

$queryFailureFixture = New-YeYuGamerInstallPreflightFixture -Name 'pid-query-failure'
Write-YeYuGamerJson `
    -Path (Join-Path $queryFailureFixture.RuntimeRoot 'state\manager-process.json') `
    -Value ([ordered]@{ pid = 4 })
function Get-CimInstance { throw 'simulated process query failure' }
try {
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $queryFailureFixture `
        -ExpectedBlocker 'recorded Manager PID state is uncertain'
} finally {
    Remove-Item -LiteralPath Function:\Get-CimInstance
}

$tcpFailureFixture = New-YeYuGamerInstallPreflightFixture -Name 'tcp-query-failure'
$realListenerEnumerator = (Get-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners).ScriptBlock
Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value { throw 'simulated listener-table failure' }
try {
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $tcpFailureFixture `
        -ExpectedBlocker 'canonical Manager endpoint state is uncertain'
} finally {
    Set-Item -LiteralPath Function:\Get-YeYuGamerActiveTcpListeners -Value $realListenerEnumerator
}

$trayFixture = New-YeYuGamerInstallPreflightFixture -Name 'active-tray'
$trayMutexCreated = $false
$trayMutex = [System.Threading.Mutex]::new(
    $false,
    'Local\YeYuGamer.Tray.v3',
    [ref]$trayMutexCreated
)
try {
    Assert-YeYuGamerLifecyclePreflightRejects `
        -Fixture $trayFixture `
        -ExpectedBlocker 'the YeYu Gamer tray is active'
} finally {
    $trayMutex.Dispose()
}

function New-YeYuGamerTransactionFixture {
    param([Parameter(Mandatory = $true)][string]$Name)

    $root = Join-Path $testResolved "install-transaction\$Name"
    $installRoot = Join-Path $root 'install'
    $runtimeRoot = Join-Path $root 'runtime'
    $startMenu = Join-Path $root 'start-menu'
    $app = Join-Path $installRoot 'app'
    $incoming = Join-Path $installRoot 'app.incoming'
    $previous = Join-Path $installRoot 'app.previous'
    $venv = Join-Path $installRoot '.venv'
    $venvPrevious = Join-Path $installRoot '.venv.previous'
    $adapter = Join-Path $runtimeRoot 'adapters\manager-adapter-host'
    $adapterPrevious = Join-Path $runtimeRoot 'adapters\manager-adapter-host.previous'
    foreach ($directory in @($app, $incoming, $previous, $venv, $venvPrevious, $adapter, $adapterPrevious)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        [System.IO.File]::WriteAllText(
            (Join-Path $directory 'old-sentinel.txt'),
            "old:${Name}:$directory",
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    $files = @(
        (Join-Path $runtimeRoot 'config\platform.json'),
        (Join-Path $installRoot 'install-manifest.json'),
        (Join-Path $installRoot 'YeYuGamer.cmd'),
        (Join-Path $startMenu 'YeYu Gamer.lnk')
    )
    foreach ($file in $files) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $file) -Force | Out-Null
        [System.IO.File]::WriteAllText($file, "old:${Name}:$file", [System.Text.UTF8Encoding]::new($false))
    }
    return [pscustomobject]@{
        Root = $root
        App = $app
        Incoming = $incoming
        Previous = $previous
        Venv = $venv
        VenvPrevious = $venvPrevious
        Adapter = $adapter
        AdapterPrevious = $adapterPrevious
        FileTargets = $files
        DirectoryTargets = @(
            [pscustomobject]@{ Path = $app; PreserveAs = $previous },
            [pscustomobject]@{ Path = $incoming },
            [pscustomobject]@{ Path = $previous },
            [pscustomobject]@{ Path = $venv; PreserveAs = $venvPrevious },
            [pscustomobject]@{ Path = $venvPrevious },
            [pscustomobject]@{ Path = $adapter; PreserveAs = $adapterPrevious },
            [pscustomobject]@{ Path = $adapterPrevious }
        )
    }
}

foreach ($failurePoint in @('host-self-check', 'dependency-config-write')) {
    $fixture = New-YeYuGamerTransactionFixture -Name $failurePoint
    $before = Get-YeYuGamerInstallTreeFingerprint -InstallRoot $fixture.Root
    $failed = $false
    try {
        Invoke-YeYuGamerInstallTransaction `
            -DirectoryTargets $fixture.DirectoryTargets `
            -FileTargets $fixture.FileTargets `
            -Action {
                foreach ($directory in @($fixture.Incoming, $fixture.Venv, $fixture.Adapter)) {
                    New-Item -ItemType Directory -Path $directory -Force | Out-Null
                    [System.IO.File]::WriteAllText(
                        (Join-Path $directory 'new-sentinel.txt'),
                        "new:$failurePoint",
                        [System.Text.UTF8Encoding]::new($false)
                    )
                }
                if ($failurePoint -eq 'host-self-check') { throw 'injected Host self-check failure' }
                Move-Item -LiteralPath $fixture.Incoming -Destination $fixture.App
                foreach ($file in $fixture.FileTargets) {
                    [System.IO.File]::WriteAllText($file, 'new-config', [System.Text.UTF8Encoding]::new($false))
                }
                throw 'injected dependency/config failure'
            }
    } catch {
        $failed = $true
    }
    if (-not $failed) { throw "Install transaction ignored injected failure: $failurePoint" }
    $after = Get-YeYuGamerInstallTreeFingerprint -InstallRoot $fixture.Root
    if ($after -cne $before) {
        throw "Install transaction did not exactly restore app/venv/runtime/config state: $failurePoint"
    }
}

$rotationFixture = New-YeYuGamerTransactionFixture -Name 'locked-rotation'
$lockProbePath = Join-Path $rotationFixture.Root 'lock-probe.py'
$platformLiteral = (ConvertTo-Json $stagedPlatform -Compress)
[System.IO.File]::WriteAllText(
    $lockProbePath,
    @"
import pathlib
import sys
sys.path.insert(0, $platformLiteral)
from yeyu_gamer_platform.lifecycle_lock import InstallLifecycleLock
lock = InstallLifecycleLock(pathlib.Path(sys.argv[1]))
if lock.acquire():
    lock.release()
    raise SystemExit(9)
raise SystemExit(0)
"@,
    [System.Text.UTF8Encoding]::new($false)
)
$concurrentStartFingerprint = Get-YeYuGamerInstallTreeFingerprint -InstallRoot $rotationFixture.Root
$installMutex = Enter-YeYuGamerInstallLifecycleLock
try {
    $probe = Start-Process `
        -FilePath $python `
        -ArgumentList @('-I', '-B', $lockProbePath, (Join-Path $rotationFixture.Root 'fallback.lock')) `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($probe.ExitCode -ne 0) {
        throw "Python lifecycle start crossed the held installer mutex (exit $($probe.ExitCode))."
    }
    if ((Get-YeYuGamerInstallTreeFingerprint -InstallRoot $rotationFixture.Root) -cne $concurrentStartFingerprint) {
        throw 'Concurrent lifecycle start mutated app/app.incoming/app.previous while the installer lock was held.'
    }
    Invoke-YeYuGamerInstallTransaction `
        -DirectoryTargets $rotationFixture.DirectoryTargets `
        -FileTargets $rotationFixture.FileTargets `
        -Action {
            New-Item -ItemType Directory -Path $rotationFixture.Incoming -Force | Out-Null
            [System.IO.File]::WriteAllText(
                (Join-Path $rotationFixture.Incoming 'candidate.txt'),
                'candidate',
                [System.Text.UTF8Encoding]::new($false)
            )
            New-Item -ItemType Directory -Path $rotationFixture.Venv -Force | Out-Null
            New-Item -ItemType Directory -Path $rotationFixture.Adapter -Force | Out-Null
            Move-Item -LiteralPath $rotationFixture.Incoming -Destination $rotationFixture.App
            foreach ($file in $rotationFixture.FileTargets) {
                [System.IO.File]::WriteAllText($file, 'new', [System.Text.UTF8Encoding]::new($false))
            }
        }
} finally {
    Exit-YeYuGamerInstallLifecycleLock -Mutex $installMutex
}
if (-not (Test-Path -LiteralPath (Join-Path $rotationFixture.App 'candidate.txt') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $rotationFixture.Previous 'old-sentinel.txt') -PathType Leaf)) {
    throw 'Lock-held app rotation did not promote the candidate and preserve the prior app.'
}
if (@(Get-ChildItem -LiteralPath $rotationFixture.Root -Recurse -Force | Where-Object Name -like '*.install-backup-*').Count -ne 0) {
    throw 'Lock-held app rotation left transaction backup artifacts.'
}

$restartSource = Get-Content -LiteralPath (Join-Path $SourceRoot 'scripts\Restart-YeYuGamer.ps1') -Raw
if (-not $restartSource.Contains('beforeManagerId') -or
    -not $restartSource.Contains('afterManagerId')) {
    throw 'Restart script does not verify that Manager identity changed.'
}

$buildSource = Get-Content -LiteralPath (Join-Path $SourceRoot 'scripts\Build-YeYuGamer.ps1') -Raw
foreach ($requiredAdapterBuildFragment in @(
    "adapterHostOutput = Join-Path `$adapterHostRoot 'host.exe'",
    "'/target:winexe'",
    "'/reference:System.Web.Extensions.dll'",
    "'--operation', 'probe'",
    "'--operation', 'canary'",
    "'--protocol-version', '1.1'"
)) {
    if (-not $buildSource.Contains($requiredAdapterBuildFragment)) {
        throw "Build script does not produce the fixed diagnostic Adapter Host: $requiredAdapterBuildFragment"
    }
}
foreach ($adapterProtocolTestPath in @(
    (Join-Path $SourceRoot 'adapter-host\tests\FakeYeYuGamerRunner.cs'),
    (Join-Path $SourceRoot 'adapter-host\tests\Test-YeYuGamerAdapterHostProtocol.ps1')
)) {
    if (-not (Test-Path -LiteralPath $adapterProtocolTestPath -PathType Leaf)) {
        throw "Adapter protocol v1.1 test fixture is missing: $adapterProtocolTestPath"
    }
}
if ($buildSource.Contains("'/target:exe'")) {
    throw 'Build script compiles the diagnostic Host as a console subsystem executable.'
}
if ($buildSource.Contains("adapterHostOutput = Join-Path `$adapterHostRoot 'runner.exe'")) {
    throw 'Build script confuses the diagnostic Host with the real execution-package runner.'
}
foreach ($requiredAdapterInstallFragment in @(
    "adapter-host\host.exe",
    "'manager-adapter-host'",
    'Protect-YeYuGamerRuntimePackageDirectory',
    'Test-YeYuGamerRuntimePackage',
    "'legacy-night-rain-gamer'",
    "'runner.exe'",
    "'missing'",
    'installedByBaseInstaller = $false',
    'Invoke-YeYuGamerInstalledAdapterHostCheck',
    'RedirectStandardOutput',
    'installedAdapterProbeCheck.ExitCode',
    'installedAdapterCanaryCheck.ExitCode'
)) {
    if (-not $installSource.Contains($requiredAdapterInstallFragment)) {
        throw "Installer lacks the split Host/execution-package contract: $requiredAdapterInstallFragment"
    }
}
foreach ($requiredNotificationInstallFragment in @(
    "secrets\notifications",
    'Protect-YeYuGamerNotificationSecretsDirectory'
)) {
    if (-not $installSource.Contains($requiredNotificationInstallFragment)) {
        throw "Installer lacks the notification-secret boundary: $requiredNotificationInstallFragment"
    }
}
$notificationConfigSource = Get-Content `
    -LiteralPath (Join-Path $SourceRoot 'scripts\Set-YeYuGamerNotificationSecret.ps1') `
    -Raw
foreach ($requiredNotificationConfigFragment in @(
    '[Security.Cryptography.ProtectedData]::Protect',
    '[Security.Cryptography.DataProtectionScope]::CurrentUser',
    "'profile.dpapi'",
    'Protect-YeYuGamerNotificationSecretsDirectory'
)) {
    if (-not $notificationConfigSource.Contains($requiredNotificationConfigFragment)) {
        throw "Notification secret configuration lacks: $requiredNotificationConfigFragment"
    }
}
if ($installSource.Contains("Join-Path `$current 'adapter-host\runner.exe'")) {
    throw 'Installer sources the diagnostic Host from a runner.exe slot.'
}
if ($installSource.Contains('(& $adapterHostTarget')) {
    throw 'Installer tries to pipe stdout directly from the Windows-subsystem Adapter Host.'
}

# Host-specific scheduler inventories and parent-workspace retirement audits
# are maintenance records, not prerequisites for a public source checkout.
# Keep the product guard: installation must never run that migration tool.
if ($installSource.Contains('Disable-YeYuGamerLegacyBypassTasks.ps1')) {
    throw 'Installer must not automatically mutate legacy scheduled tasks.'
}

# Validate the optional local StarRail input without executing the build script
# or requiring the private compatibility file in a public source checkout.
$payloadParseTokens = $null
$payloadParseErrors = $null
$starRailBuildAst = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $SourceRoot 'scripts\Build-YeYuGamerStarRailAdapter.ps1'),
    [ref]$payloadParseTokens, [ref]$payloadParseErrors
)
if ($payloadParseErrors.Count -ne 0) { throw 'StarRail build script does not parse.' }
$payloadGuards = @($starRailBuildAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Resolve-March7thCompatibilityPayload'
}, $false))
if ($payloadGuards.Count -ne 1) { throw 'StarRail build must define one compatibility input guard.' }
. ([scriptblock]::Create($payloadGuards[0].Extent.Text))
$publicPayloadFixture = Join-Path $testResolved 'public-starrail-input'
New-Item -ItemType Directory -Path $publicPayloadFixture -Force | Out-Null
$invalidPayloadFixture = Join-Path $publicPayloadFixture 'invalid.txt'
[System.IO.File]::WriteAllText($invalidPayloadFixture, 'invalid input fixture', [System.Text.UTF8Encoding]::new($false))
foreach ($case in @(
    @{ Path = ''; Message = '*public repository does not include this payload*' },
    @{ Path = (Join-Path $publicPayloadFixture 'missing.b64'); Message = '*public repository does not include this payload*' },
    @{ Path = 'relative.b64'; Message = '*absolute local file path*' },
    @{ Path = '\\unreachable-payload-test\share\payload.b64'; Message = '*not UNC*' },
    @{ Path = $invalidPayloadFixture; Message = '*SHA256 does not match*' }
)) {
    $rejection = $null
    try {
        Resolve-March7thCompatibilityPayload -Path $case.Path -SourceCodeRoot $publicPayloadFixture | Out-Null
    } catch { $rejection = $_.Exception.Message }
    if (-not $rejection -or $rejection -notlike $case.Message) {
        throw "StarRail input guard expected '$($case.Message)', received '$rejection'."
    }
}

$seedBackendRoot = Join-Path $testResolved 'seed-backend'
New-Item -ItemType Directory -Path $seedBackendRoot -Force | Out-Null
$seedBackendCopied = $false
for ($attempt = 1; $attempt -le 3 -and -not $seedBackendCopied; $attempt++) {
    try {
        Copy-Item `
            -LiteralPath (Join-Path $SourceRoot 'backend\yeyu_gamer_manager') `
            -Destination $seedBackendRoot `
            -Recurse `
            -Force
        $seedBackendCopied = $true
    } catch {
        if ($attempt -eq 3) { throw }
        Start-Sleep -Milliseconds (200 * $attempt)
    }
}
$seedSource = Join-Path $testResolved 'seed-source'
$seedDestination = Join-Path $testResolved 'seed-runtime\import\legacy-config'
New-Item -ItemType Directory -Path $seedSource -Force | Out-Null
Write-YeYuGamerJson -Path (Join-Path $seedSource 'daily-gui-config.json') -Value ([ordered]@{
    order = @('StarRail')
    enabled = [ordered]@{ StarRail = $true }
    launchAtWindowsLogon = $true
    managerCommand = @('must-not-copy')
})
Write-YeYuGamerJson -Path (Join-Path $seedSource 'game-automation-policy.json') -Value ([ordered]@{
    schemaVersion = 1
    token = 'must-not-copy'
})
$oldSeedPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $seedBackendRoot
    Invoke-YeYuGamerLegacySeed `
        -PythonPath $python `
        -CandidateSourceRoot $seedSource `
        -DestinationRoot $seedDestination | Out-Null
    $seedConfigPath = Join-Path $seedDestination 'daily-gui-config.json'
    $seedPolicyPath = Join-Path $seedDestination 'game-automation-policy.json'
    if (-not (Test-Path -LiteralPath $seedConfigPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $seedPolicyPath -PathType Leaf)) {
        throw 'Legacy seed integration did not produce both local JSON files.'
    }
    $seedText = Get-Content -LiteralPath $seedConfigPath -Raw
    if ($seedText.Contains('must-not-copy') -or $seedText.Contains('managerCommand')) {
        throw 'Legacy seed integration copied a forbidden command or secret field.'
    }
    Write-YeYuGamerJson -Path $seedConfigPath -Value ([ordered]@{ localOverride = 'preserve-me' })
    $configHashBefore = (Get-FileHash -LiteralPath $seedConfigPath -Algorithm SHA256).Hash
    $policyHashBefore = (Get-FileHash -LiteralPath $seedPolicyPath -Algorithm SHA256).Hash
    Invoke-YeYuGamerLegacySeed `
        -PythonPath $python `
        -CandidateSourceRoot $seedSource `
        -DestinationRoot $seedDestination | Out-Null
    if ((Get-FileHash -LiteralPath $seedConfigPath -Algorithm SHA256).Hash -ne $configHashBefore -or
        (Get-FileHash -LiteralPath $seedPolicyPath -Algorithm SHA256).Hash -ne $policyHashBefore) {
        throw 'A second legacy seed migration overwrote existing local configuration.'
    }
    $seedStagingParent = Join-Path ([System.IO.Path]::GetTempPath()) 'YeYuGamer'
    if (Test-Path -LiteralPath $seedStagingParent -PathType Container) {
        $leftoverSeedInputs = @(Get-ChildItem `
            -LiteralPath $seedStagingParent `
            -Directory `
            -Filter 'legacy-seed-*' `
            -Force)
        if ($leftoverSeedInputs.Count -gt 0) {
            throw "Legacy seed migration left raw staging directories: $($leftoverSeedInputs[0].FullName)"
        }
    }
} finally {
    $env:PYTHONPATH = $oldSeedPythonPath
}

$openApiFixtureRoot = Join-Path $testResolved 'openapi-contract'
$openApiFixturePath = Join-Path $openApiFixtureRoot 'openapi.v1.json'
$openApiExporter = Join-Path $SourceRoot 'backend\scripts\export_openapi.py'
$oldOpenApiPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $seedBackendRoot
    Invoke-YeYuGamerNative `
        -FilePath $python `
        -Arguments @($openApiExporter, '--output', $openApiFixturePath)
} finally {
    $env:PYTHONPATH = $oldOpenApiPythonPath
}
$openApiFixtureHashPath = "$openApiFixturePath.sha256"
if (-not (Test-Path -LiteralPath $openApiFixturePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $openApiFixtureHashPath -PathType Leaf)) {
    throw 'OpenAPI exporter integration did not produce both contract files.'
}
$openApiHashLine = [System.IO.File]::ReadAllText($openApiFixtureHashPath).TrimEnd("`r", "`n")
if ($openApiHashLine -notmatch '^([0-9a-f]{64})  openapi\.v1\.json$') {
    throw 'OpenAPI exporter did not emit a canonical 64-character SHA-256 record.'
}
$openApiDeclaredHash = $Matches[1]
$openApiActualHash = (Get-FileHash -LiteralPath $openApiFixturePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($openApiDeclaredHash -ne $openApiActualHash) {
    throw 'OpenAPI exporter digest does not match the exported document.'
}
$openApiDocument = Get-Content -LiteralPath $openApiFixturePath -Raw | ConvertFrom-Json
if ([string]$openApiDocument.openapi -notmatch '^3\.') {
    throw 'OpenAPI exporter output does not contain an OpenAPI 3.x document.'
}

foreach ($requiredBuildFragment in @(
    'scripts\export_openapi.py',
    'contracts',
    'openapi.v1.json',
    '$env:PYTHONPATH = $backendRoot',
    "backendRoot 'dist'",
    "'yeyu_gamer_manager-*.whl'"
)) {
    if (-not $buildSource.Contains($requiredBuildFragment)) {
        throw "Build script does not stage the canonical OpenAPI contract: $requiredBuildFragment"
    }
}

$fixtureRoot = Join-Path $testResolved 'manifest-fixture'
$fixtureStage = Join-Path $fixtureRoot 'staged-app'
$fixtureWheelhouse = Join-Path $fixtureRoot 'wheelhouse'
foreach ($runtimeTopLevel in @('adapter-host', 'backend', 'contracts', 'packages', 'provenance', 'scripts', 'webgui')) {
    New-Item -ItemType Directory -Path (Join-Path $fixtureStage $runtimeTopLevel) -Force | Out-Null
}
New-Item -ItemType Directory -Path $fixtureWheelhouse -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $fixtureStage 'webgui\dist\assets') -Force | Out-Null

$fixtureFile = Join-Path $fixtureStage 'backend\declared.bin'
[System.IO.File]::WriteAllText($fixtureFile, 'verified', [System.Text.UTF8Encoding]::new($false))
$fixtureWebIndex = Join-Path $fixtureStage 'webgui\dist\index.html'
$fixtureWebAsset = Join-Path $fixtureStage 'webgui\dist\assets\app.js'
[System.IO.File]::WriteAllText($fixtureWebIndex, '<!doctype html><div id="app"></div>', [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($fixtureWebAsset, 'globalThis.fixture=true;', [System.Text.UTF8Encoding]::new($false))

$fixtureWheel = Join-Path $fixtureWheelhouse 'demo-1.0-py3-none-any.whl'
[System.IO.File]::WriteAllBytes($fixtureWheel, [byte[]](1, 2, 3, 4))
$fixtureWheelHash = (Get-FileHash -LiteralPath $fixtureWheel -Algorithm SHA256).Hash.ToLowerInvariant()
$fixtureLockText = "--only-binary=:all:`ndemo==1.0 --hash=sha256:$fixtureWheelHash`n"
$fixtureWheelhouseLock = Join-Path $fixtureWheelhouse 'requirements.lock'
$fixturePackagedLock = Join-Path $fixtureStage 'provenance\requirements.lock'
[System.IO.File]::WriteAllText($fixtureWheelhouseLock, $fixtureLockText, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($fixturePackagedLock, $fixtureLockText, [System.Text.UTF8Encoding]::new($false))
$fixtureLockHash = (Get-FileHash -LiteralPath $fixturePackagedLock -Algorithm SHA256).Hash.ToLowerInvariant()
$fixtureProvenancePath = Join-Path $fixtureStage 'provenance\dependency-provenance.json'
Write-YeYuGamerJson -Path $fixtureProvenancePath -Value ([ordered]@{
    schemaVersion = 1
    target = [ordered]@{ implementation = 'CPython'; pythonVersion = '3.12'; abi = 'cp312'; platform = 'win_amd64' }
    lockFile = 'requirements.cpython312-win_amd64.lock'
    lockSha256 = $fixtureLockHash
    artifacts = @([ordered]@{
        name = 'demo'
        version = '1.0'
        filename = 'demo-1.0-py3-none-any.whl'
        sha256 = $fixtureWheelHash
        bytes = (Get-Item -LiteralPath $fixtureWheel).Length
    })
})
$fixtureProvenanceHash = (Get-FileHash -LiteralPath $fixtureProvenancePath -Algorithm SHA256).Hash.ToLowerInvariant()

$fixtureAppFiles = @(Get-ChildItem -LiteralPath $fixtureStage -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
    [ordered]@{
        path = $_.FullName.Substring($fixtureStage.Length + 1).Replace('\', '/')
        bytes = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
})
$fixtureWheelhouseFiles = @(Get-ChildItem -LiteralPath $fixtureWheelhouse -File -Force | Sort-Object Name | ForEach-Object {
    [ordered]@{
        path = $_.Name
        bytes = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
})
$fixtureManifestPath = Join-Path $fixtureRoot 'build-manifest.json'
$fixtureManifest = [ordered]@{
    schemaVersion = 3
    builtAt = [DateTimeOffset]::UtcNow.ToString('o')
    layout = [ordered]@{ application = 'staged-app'; wheelhouse = 'wheelhouse' }
    dependencyLock = [ordered]@{
        sourcePath = 'packaging/requirements.cpython312-win_amd64.lock'
        packagedPath = 'provenance/requirements.lock'
        wheelhousePath = 'requirements.lock'
        provenanceSourcePath = 'packaging/dependency-provenance.cpython312-win_amd64.json'
        provenancePackagedPath = 'provenance/dependency-provenance.json'
        target = 'cpython312-win_amd64'
        packageCount = 1
        sha256 = $fixtureLockHash
        provenanceSha256 = $fixtureProvenanceHash
    }
    wheelhouse = [ordered]@{ relativePath = 'wheelhouse'; lockFile = 'requirements.lock'; installMode = 'no-index'; requireHashes = $true }
    runtimeAllowlist = [ordered]@{ topLevel = @('adapter-host', 'backend', 'contracts', 'packages', 'provenance', 'scripts', 'webgui') }
    packagePrivacy = [ordered]@{ exactMachineTokenScan = 'passed'; absolutePathsInManifest = $false }
    appFiles = $fixtureAppFiles
    wheelhouseFiles = $fixtureWheelhouseFiles
}
Write-YeYuGamerJson -Path $fixtureManifestPath -Value $fixtureManifest
Test-YeYuGamerBuildManifest -StagedRoot $fixtureStage -ManifestPath $fixtureManifestPath | Out-Null
$relocatedStage = Join-Path $testResolved 'relocated-manifest-app'
New-Item -ItemType Directory -Path $relocatedStage -Force | Out-Null
Get-ChildItem -LiteralPath $fixtureStage -Force | Copy-Item -Destination $relocatedStage -Recurse -Force
$relocatedRejected = $false
try {
    Test-YeYuGamerBuildManifest -StagedRoot $relocatedStage -ManifestPath $fixtureManifestPath -AllowRelocatedRoot | Out-Null
} catch { $relocatedRejected = $true }
if (-not $relocatedRejected) { throw 'Build manifest verifier accepted a relocated root outside the fixed package layout.' }

[System.IO.File]::AppendAllText($fixtureFile, 'tampered')
$tamperRejected = $false
try { Test-YeYuGamerBuildManifest -StagedRoot $fixtureStage -ManifestPath $fixtureManifestPath | Out-Null } catch { $tamperRejected = $true }
if (-not $tamperRejected) { throw 'Build manifest verifier accepted a tampered file.' }
[System.IO.File]::WriteAllText($fixtureFile, 'verified', [System.Text.UTF8Encoding]::new($false))
$extraFile = Join-Path $fixtureStage 'undeclared.txt'
[System.IO.File]::WriteAllText($extraFile, 'extra', [System.Text.UTF8Encoding]::new($false))
$extraRejected = $false
try { Test-YeYuGamerBuildManifest -StagedRoot $fixtureStage -ManifestPath $fixtureManifestPath | Out-Null } catch { $extraRejected = $true }
if (-not $extraRejected) { throw 'Build manifest verifier accepted an undeclared file.' }

$traySource = Get-Content -LiteralPath (Join-Path $platformSource 'yeyu_gamer_platform\tray.py') -Raw
foreach ($forbidden in @('QMain' + 'Window', 'QWeb' + 'Engine', 'NightRain' + 'Gamer.bat')) {
    if ($traySource.Contains($forbidden)) { throw "Tray contains forbidden dependency or bypass: $forbidden" }
}
Write-Host 'YeYu Gamer platform checks passed.'
