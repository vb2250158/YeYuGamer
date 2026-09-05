[CmdletBinding()]
param(
    [string]$ReleaseScriptPath = (Join-Path $PSScriptRoot 'Publish-YeYuGamerLocalRelease.ps1')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-ReleaseSource {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-ReleaseSourceFailure {
    param([scriptblock]$Action, [string]$ExpectedMessage)
    $message = $null
    try { & $Action | Out-Null } catch { $message = $_.Exception.Message }
    Assert-ReleaseSource ($null -ne $message -and $message -like $ExpectedMessage) `
        "Expected '$ExpectedMessage', received '$message'."
}

# Load only the isolated helpers. Never execute release/install/promotion code.
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $ReleaseScriptPath, [ref]$tokens, [ref]$parseErrors
)
Assert-ReleaseSource ($parseErrors.Count -eq 0) 'Release script must parse without errors.'
. (Join-Path (Split-Path -Parent $ReleaseScriptPath) 'YeYuGamer.Common.ps1')
$sourceGuards = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Assert-YeYuGamerReleaseSourceRoot'
}, $false))
Assert-ReleaseSource ($sourceGuards.Count -eq 1) 'Expected one local release source guard.'
. ([scriptblock]::Create($sourceGuards[0].Extent.Text))
$localTestSource = Split-Path -Parent $ReleaseScriptPath
Assert-ReleaseSource ((Assert-YeYuGamerReleaseSourceRoot -Path $localTestSource) -ceq $localTestSource) `
    'A local release source must pass the guard.'
Assert-ReleaseSourceFailure {
    Assert-YeYuGamerReleaseSourceRoot -Path '\\unreachable-release-test\share\source'
} '*not UNC*'
Assert-ReleaseSourceFailure {
    Assert-YeYuGamerReleaseSourceRoot -Path 'Z:\release-test\source'
} '*NAS workspace drive*'
& {
    function Get-PSDrive {
        [CmdletBinding()]
        param([string]$Name)
        if ($Name -ceq 'Q') {
            return [pscustomobject]@{ Root = 'Q:\'; DisplayRoot = '\\unreachable-release-test\share' }
        }
        Microsoft.PowerShell.Management\Get-PSDrive @PSBoundParameters
    }
    Assert-ReleaseSourceFailure {
        Assert-YeYuGamerReleaseSourceRoot -Path 'Q:\source'
    } '*mapped network drive*'
}
$sourceGuardCases = 4
$releaseSource = $ast.Extent.Text
$guardOffset = $releaseSource.IndexOf('$SourceRoot = Assert-YeYuGamerReleaseSourceRoot')
Assert-ReleaseSource ($guardOffset -ge 0 -and
    $guardOffset -lt $releaseSource.IndexOf('$env:PYTHONDONTWRITEBYTECODE') -and
    $guardOffset -lt $releaseSource.IndexOf('$releaseId =')) `
    'The source guard must run before release environment changes or work directory creation.'

$copyFunctions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Copy-YeYuGamerReleaseSource'
}, $false))
Assert-ReleaseSource ($copyFunctions.Count -eq 1) 'Expected one release source copy function.'
. ([scriptblock]::Create($copyFunctions[0].Extent.Text))

$installFunctions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Invoke-YeYuGamerCandidateInstallation'
}, $false))
Assert-ReleaseSource ($installFunctions.Count -eq 1) 'Expected one candidate installation scope helper.'
. ([scriptblock]::Create($installFunctions[0].Extent.Text))
$gateName = 'YEYU_GAMER_LEGACY_EXECUTION_ENABLED'
$gatePath = 'Env:\' + $gateName
$originalGateWasSet = Test-Path -LiteralPath $gatePath
$originalGate = [Environment]::GetEnvironmentVariable($gateName, 'Process')
$gateCases = 0
try {
    foreach ($initialGate in @($null, 'true', 'false')) {
        if ($null -eq $initialGate) {
            Remove-Item -LiteralPath $gatePath -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($gateName, $initialGate, 'Process')
        }
        $installResult = Invoke-YeYuGamerCandidateInstallation -Install {
            Assert-ReleaseSource ($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -ceq 'false') `
                'Candidate installation must see a disabled execution gate.'
            'candidate-installed'
        }
        Assert-ReleaseSource ($installResult -ceq 'candidate-installed') 'Installation output must be preserved.'
        Assert-ReleaseSource ([Environment]::GetEnvironmentVariable($gateName, 'Process') -ceq $initialGate) `
            'Successful installation must restore the previous gate, including an unset value.'
        Assert-ReleaseSource ((Test-Path -LiteralPath $gatePath) -eq ($null -ne $initialGate)) `
            'Successful installation must preserve whether the gate exists.'
        $gateCases++

        Assert-ReleaseSourceFailure {
            Invoke-YeYuGamerCandidateInstallation -Install {
                Assert-ReleaseSource ($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -ceq 'false') `
                    'Failing installation must also see a disabled execution gate.'
                throw 'simulated candidate installation failure'
            }
        } 'simulated candidate installation failure'
        Assert-ReleaseSource ([Environment]::GetEnvironmentVariable($gateName, 'Process') -ceq $initialGate) `
            'Failed installation must restore the previous gate, including an unset value.'
        Assert-ReleaseSource ((Test-Path -LiteralPath $gatePath) -eq ($null -ne $initialGate)) `
            'Failed installation must preserve whether the gate exists.'
        $gateCases++
    }
} finally {
    if ($originalGateWasSet) {
        [Environment]::SetEnvironmentVariable($gateName, $originalGate, 'Process')
    } else {
        Remove-Item -LiteralPath $gatePath -ErrorAction SilentlyContinue
    }
}

$installCalls = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -ceq 'Invoke-YeYuGamerCandidateInstallation'
}, $true))
Assert-ReleaseSource ($installCalls.Count -eq 1) 'Release must have one candidate installation scope.'
$installCallText = $installCalls[0].Extent.Text
foreach ($adapter in @('Classic', 'Ww', 'Nte', 'Fgo', 'StarRail', 'Czn', 'Bd2')) {
    Assert-ReleaseSource ($installCallText.Contains("Install-YeYuGamer${adapter}Adapter.ps1")) `
        "The $adapter installer must run inside the disabled execution-gate scope."
}
Assert-ReleaseSource (-not $installCallText.Contains('Start-YeYuGamer.ps1')) `
    'The installed Host must start after the caller execution gate is restored.'

$testRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) `
    ('YeYuGamer\release-source-tests\' + [Guid]::NewGuid().ToString('N'))
$sourceRoot = Join-Path $testRoot 'source'
New-Item -ItemType Directory -Path $sourceRoot -Force | Out-Null
$script:excludedNames = @('.git', '.venv', '__pycache__', 'node_modules', 'dist', '.pytest_cache', 'logs', 'runs')
$script:enumeratedDirectories = [System.Collections.Generic.List[string]]::new()
$script:fakeReparseFile = $null
$script:corruptCopy = $false

# Throw on an attempted descent into excluded directories, even when their
# contents would later be filtered out. This reproduces the NAS traversal bug.
function Get-ChildItem {
    [CmdletBinding()]
    param([string]$LiteralPath, [switch]$Force, [switch]$Recurse, [switch]$File)
    Assert-ReleaseSource (-not $Recurse) 'Release source enumerator must prune before recursion.'
    $segments = $LiteralPath -split '[\\/]'
    Assert-ReleaseSource (@($segments | Where-Object { $_ -in $script:excludedNames }).Count -eq 0) `
        'Release source enumerator descended into an excluded directory.'
    $script:enumeratedDirectories.Add($LiteralPath)
    foreach ($entry in @(Microsoft.PowerShell.Management\Get-ChildItem @PSBoundParameters)) {
        if ($entry.FullName -ceq $script:fakeReparseFile) {
            [pscustomobject]@{
                Name = $entry.Name
                FullName = $entry.FullName
                Attributes = $entry.Attributes -bor [System.IO.FileAttributes]::ReparsePoint
                PSIsContainer = $false
            }
        } else { $entry }
    }
}

function Copy-Item {
    [CmdletBinding()]
    param([string]$LiteralPath, [string]$Destination)
    Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
    if ($script:corruptCopy) { [System.IO.File]::WriteAllText($Destination, 'corrupt copy') }
}

$expectedFiles = @('README.md', 'webgui\src\page.vue', 'backend\config [sample].json')
foreach ($relative in $expectedFiles) {
    $path = Join-Path $sourceRoot $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    [System.IO.File]::WriteAllText($path, 'payload: ' + $relative)
}
foreach ($excludedName in $script:excludedNames) {
    foreach ($parent in @($sourceRoot, (Join-Path $sourceRoot 'webgui'))) {
        $directory = Join-Path (Join-Path $parent $excludedName) 'never-visit'
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        [System.IO.File]::WriteAllText((Join-Path $directory 'private.txt'), 'excluded')
    }
}
$destination = Join-Path $testRoot 'copied'
$result = Copy-YeYuGamerReleaseSource -Root $sourceRoot -Destination $destination
Assert-ReleaseSource ($result -ceq $destination) 'Copy must return its local destination.'
$actualFiles = @(Microsoft.PowerShell.Management\Get-ChildItem -LiteralPath $destination -Recurse -File -Force)
Assert-ReleaseSource ($actualFiles.Count -eq $expectedFiles.Count) 'Only source payload files may be copied.'
Assert-ReleaseSource ($script:enumeratedDirectories.Count -eq 4) 'Each included directory must be enumerated once.'
foreach ($relative in $expectedFiles) {
    $sourceHash = (Get-FileHash -LiteralPath (Join-Path $sourceRoot $relative)).Hash
    $targetHash = (Get-FileHash -LiteralPath (Join-Path $destination $relative)).Hash
    Assert-ReleaseSource ($sourceHash -ceq $targetHash) 'Copied source payload must match its SHA256.'
}

$linkTarget = Join-Path $testRoot 'link-target'
New-Item -ItemType Directory -Path $linkTarget | Out-Null
[System.IO.File]::WriteAllText((Join-Path $linkTarget 'outside.txt'), 'outside source')
$linkSource = Join-Path $testRoot 'link-source'
New-Item -ItemType Directory -Path $linkSource | Out-Null
New-Item -ItemType Junction -Path (Join-Path $linkSource 'linked') -Target $linkTarget | Out-Null
$rejectedDestination = Join-Path $testRoot 'rejected-directory-link'
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root $linkSource -Destination $rejectedDestination
} '*contains a reparse point*'
Assert-ReleaseSource (-not (Test-Path -LiteralPath $rejectedDestination)) 'Reject links before creating staging.'
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root (Join-Path $linkSource 'linked') `
        -Destination (Join-Path $testRoot 'rejected-root-link')
} '*contains a reparse point*'

$script:fakeReparseFile = Join-Path $sourceRoot 'README.md'
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root $sourceRoot -Destination (Join-Path $testRoot 'rejected-file-link')
} '*contains a reparse point*'
$script:fakeReparseFile = $null

$script:corruptCopy = $true
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root $sourceRoot -Destination (Join-Path $testRoot 'corrupted')
} '*changed or copied incorrectly*'
$script:corruptCopy = $false

$emptySource = Join-Path $testRoot 'empty'
New-Item -ItemType Directory -Path $emptySource | Out-Null
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root $emptySource -Destination (Join-Path $testRoot 'empty-result')
} '*snapshot is empty*'
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root $sourceRoot -Destination $destination
} '*already exists*'
$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$outsideLocalAppData = Join-Path (Split-Path -Parent $localAppData) 'Local-other\release-test'
Assert-ReleaseSourceFailure {
    Copy-YeYuGamerReleaseSource -Root $sourceRoot -Destination $outsideLocalAppData
} '*must remain below LocalApplicationData*'

[pscustomobject]@{
    status = 'passed'
    cases = 8 + $gateCases + $sourceGuardCases
    candidateGateCases = $gateCases
    sourceGuardCases = $sourceGuardCases
    sourceFiles = $expectedFiles.Count
    releaseExecuted = $false
    evidenceRoot = $testRoot
} | ConvertTo-Json -Compress
