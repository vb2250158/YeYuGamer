[CmdletBinding()]
param([string]$InstallScriptPath = (Join-Path $PSScriptRoot 'Install-YeYuGamer.ps1'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path (Split-Path -Parent $InstallScriptPath) 'YeYuGamer.Common.ps1')

function Assert-Shortcut {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $InstallScriptPath, [ref]$tokens, [ref]$parseErrors
)
Assert-Shortcut ($parseErrors.Count -eq 0) 'Installer must parse successfully.'
foreach ($name in @('Get-YeYuGamerExistingDesktopShortcuts', 'Set-YeYuGamerHostShortcut')) {
    $functions = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name
    }, $false))
    Assert-Shortcut ($functions.Count -eq 1) "Expected exactly one $name function."
    . ([scriptblock]::Create($functions[0].Extent.Text))
}
$source = $ast.Extent.Text
Assert-Shortcut ($source.Contains(') + $legacyStartupShortcuts + $existingDesktopShortcuts')) `
    'Desktop shortcuts must participate in the installer rollback transaction.'
Assert-Shortcut ($source.Contains('foreach ($hostShortcutPath in @($shortcutPath) + $existingDesktopShortcuts)')) `
    'Start menu and existing desktop shortcuts must use one Host shortcut writer.'
Assert-Shortcut ($source.IndexOf('$existingDesktopShortcuts = @(') -lt $source.IndexOf('Invoke-YeYuGamerInstallTransaction')) `
    'Select existing icons before the transaction moves them aside.'

$testRoot = Join-Path (Get-YeYuGamerDefaultProductWorkRoot) ('shortcut-tests\' + [Guid]::NewGuid().ToString('N'))
$testRoot = Assert-YeYuGamerLocalTarget -Path $testRoot -Purpose 'shortcut test root'
$desktop = Join-Path $testRoot 'desktop'
$emptyDesktop = Join-Path $testRoot 'empty-desktop'
$app = Join-Path $testRoot 'app'
$startMenu = Join-Path $testRoot 'start-menu'
New-Item -ItemType Directory -Path $desktop,$emptyDesktop,$app,$startMenu -Force | Out-Null
$hostExecutable = Join-Path $app 'YeYuGamer.exe'
$configPath = Join-Path $testRoot 'platform.json'
[IO.File]::WriteAllText($hostExecutable, 'fixture only; never executed')
[IO.File]::WriteAllText($configPath, '{}')
$localizedName = [string][char]0x591c + [char]0x96e8 + 'Gamer.lnk'
$legacyGuiName = [string][char]0x6bcf + [char]0x65e5 + '-' + [char]0x6e38 + [char]0x620f + [char]0x4e00 + [char]0x6761 + [char]0x9f99 + 'GUI.lnk'
$desktopNames = @('YeYu Gamer.lnk', $localizedName, $legacyGuiName, 'Unrelated.lnk')
$shell = New-Object -ComObject WScript.Shell
foreach ($name in $desktopNames) {
    $link = $shell.CreateShortcut((Join-Path $desktop $name))
    $link.TargetPath = Join-Path $env:WINDIR 'System32\notepad.exe'
    $link.Arguments = 'old shortcut fixture'
    if ($name -ceq $legacyGuiName) {
        $link.TargetPath = Join-Path $testRoot 'legacy\pythonw.exe'
        $link.Arguments = '-I -B -m yeyu_gamer_platform.tray --ensure-manager --open-webgui'
    }
    $link.WorkingDirectory = $testRoot
    $link.Save()
}
$existing = @(Get-YeYuGamerExistingDesktopShortcuts -DesktopPath $desktop)
Assert-Shortcut ($existing.Count -eq 3) 'Select exactly the three known existing product icons, including the legacy tray entry.'
Assert-Shortcut ($existing -ccontains (Join-Path $desktop $legacyGuiName)) 'The exact historical GUI entry must join the install transaction.'
Assert-Shortcut (@(Get-YeYuGamerExistingDesktopShortcuts -DesktopPath $emptyDesktop).Count -eq 0) `
    'An empty desktop must not request new product icons.'
$originalHashes = @{}
foreach ($path in $existing) { $originalHashes[$path] = (Get-FileHash -LiteralPath $path).Hash }
$unrelated = Join-Path $desktop 'Unrelated.lnk'
$unrelatedHash = (Get-FileHash -LiteralPath $unrelated).Hash
$directoryTargets = @([pscustomobject]@{Path=(Join-Path $testRoot 'transaction-unused')})
$failure = $null
try {
    Invoke-YeYuGamerInstallTransaction -DirectoryTargets $directoryTargets -FileTargets $existing -Action {
        foreach ($path in $existing) {
            Set-YeYuGamerHostShortcut -Path $path -HostExecutable $hostExecutable -ConfigPath $configPath -WorkingDirectory $app
        }
        throw 'simulated install failure after shortcut update'
    }
} catch { $failure = $_.Exception.Message }
Assert-Shortcut ($failure -ceq 'simulated install failure after shortcut update') 'Expected simulated install failure.'
foreach ($path in $existing) {
    Assert-Shortcut ((Get-FileHash -LiteralPath $path).Hash -ceq $originalHashes[$path]) 'Rollback must restore original shortcut bytes.'
}

$startMenuPath = Join-Path $startMenu 'YeYu Gamer.lnk'
$allPaths = @($startMenuPath) + $existing
Invoke-YeYuGamerInstallTransaction -DirectoryTargets $directoryTargets -FileTargets $allPaths -Action {
    foreach ($path in $allPaths) {
        Set-YeYuGamerHostShortcut -Path $path -HostExecutable $hostExecutable -ConfigPath $configPath -WorkingDirectory $app
    }
}
foreach ($path in $allPaths) {
    $link = $shell.CreateShortcut($path)
    Assert-Shortcut ($link.TargetPath -ceq $hostExecutable) 'Shortcut must target the same single Host EXE.'
    Assert-Shortcut ($link.Arguments -ceq "--config `"$configPath`"") 'Shortcut must use the installed config arguments.'
    Assert-Shortcut ($link.WorkingDirectory -ceq $app) 'Shortcut must use the installed app directory.'
}
Assert-Shortcut ((Get-FileHash -LiteralPath $unrelated).Hash -ceq $unrelatedHash) 'Unrelated desktop icons must remain untouched.'
Assert-Shortcut (@(Get-ChildItem -LiteralPath $desktop -File -Filter '*.lnk').Count -eq 4) 'Do not add or delete desktop icons.'
Assert-Shortcut (@(Get-ChildItem -LiteralPath $emptyDesktop -Force).Count -eq 0) 'Do not populate an empty desktop.'

[pscustomobject]@{
    status = 'passed'
    desktopShortcutsUpdated = 3
    startMenuShortcutsCreated = 1
    rollbackVerified = $true
    unrelatedShortcutPreserved = $true
    emptyDesktopPreserved = $true
    userDesktopModified = $false
    evidenceRoot = $testRoot
} | ConvertTo-Json -Compress
