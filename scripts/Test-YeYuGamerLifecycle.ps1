[CmdletBinding()]
param(
    [string]$ScriptsRoot = $PSScriptRoot,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function Assert-Contract([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }

$sources = @{}
foreach ($name in @(
    'Start-YeYuGamer.ps1',
    'Stop-YeYuGamer.ps1',
    'Restart-YeYuGamer.ps1',
    'Install-YeYuGamer.ps1',
    'Build-YeYuGamer.ps1',
    'YeYuGamer.Common.ps1'
)) {
    $path = Join-Path $ScriptsRoot $name
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    Assert-Contract ($errors.Count -eq 0) "$name contains parser errors."
    $sources[$name] = [System.IO.File]::ReadAllText($path)
}

$nativeInvocationCount = 0
foreach ($scriptPath in @(Get-ChildItem -LiteralPath $ScriptsRoot -Filter '*.ps1' -File | Select-Object -ExpandProperty FullName)) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
    Assert-Contract ($errors.Count -eq 0) "$(Split-Path -Leaf $scriptPath) contains parser errors."
    foreach ($invocation in @($ast.FindAll(
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
        Assert-Contract (
            'FilePath' -in $parameterNames -and 'Arguments' -in $parameterNames
        ) (
            "Native invocation must use exact named parameters to protect dash-prefixed child arguments: " +
            "$(Split-Path -Leaf $scriptPath):$($invocation.Extent.StartLineNumber)"
        )
    }
}
Assert-Contract ($nativeInvocationCount -gt 0) 'No native-wrapper invocation was found for parameter-binding validation.'

$common = $sources['YeYuGamer.Common.ps1']
foreach ($text in @(
    'yeyu_gamer_platform.tray_ipc',
    '--timeout-seconds $TimeoutSeconds',
    "@(0, 2, 3, 4)",
    "ValidateSet('ping', 'exit', 'ensure_manager', 'open_webgui')",
    'processCreationIdentity',
    'CreationDate',
    'ToFileTimeUtc()',
    '^windows-filetime:[1-9][0-9]*$',
    '$schemaVersion -eq 2',
    'ConvertFrom-YeYuGamerWindowsCommandLine',
    'CommandLineToArgvW',
    'Get-YeYuGamerPythonModuleName',
    'Get-YeYuGamerPythonModuleProcesses',
    'Get-CimInstance Win32_Process -ErrorAction Stop',
    "'yeyu_gamer_platform.manager_host'",
    "'yeyu_gamer_manager'",
    "'yeyu_gamer_platform.tray'",
    'manifest.appFiles',
    'manifest.wheelhouseFiles',
    'manifest.dependencyLock',
    'manifest.runtimeAllowlist',
    "lockFile -cne 'requirements.lock'"
)) { Assert-Contract ($common.Contains($text)) "Common contract is missing: $text" }
foreach ($listenerContract in @(
    'IPGlobalProperties',
    'GetActiveTcpListeners',
    'Local TCP listener enumeration failed',
    'Any local listener on the fixed port is a blocker'
)) {
    Assert-Contract ($common.Contains($listenerContract)) "Listener-enumeration contract is missing: $listenerContract"
}
Assert-Contract (-not $common.Contains('[System.Net.Sockets.TcpClient]')) 'Installer preflight still creates an outbound TcpClient probe.'
Assert-Contract (-not $common.Contains('BeginConnect(')) 'Installer preflight still depends on a firewall-sensitive TCP connect timeout.'
$buildManifestBody = $common.Substring($common.IndexOf('function Test-YeYuGamerBuildManifest'))
Assert-Contract (-not $buildManifestBody.Contains('$manifest.schemaVersion -ne 1')) 'Schema v1 build-manifest validation remains reachable.'
Assert-Contract (-not $buildManifestBody.Contains('$manifest.schemaVersion -ne 2')) 'Schema v2 build-manifest validation remains reachable.'
Assert-Contract ($buildManifestBody.Contains('$manifest.schemaVersion -ne 3')) 'Schema v3 build-manifest validation is missing.'
$dependencyLockPath = Join-Path (Split-Path -Parent $ScriptsRoot) 'packaging\requirements.cpython312-win_amd64.lock'
Assert-Contract (Test-Path -LiteralPath $dependencyLockPath -PathType Leaf) 'Committed dependency lock is missing.'
$dependencyLockText = [System.IO.File]::ReadAllText($dependencyLockPath)
Assert-Contract ($dependencyLockText -match '(?m)^PySide6==6\.11\.2 --hash=sha256:[0-9a-f]{64}$') 'Committed dependency lock does not pin PySide6 6.11.2 with a trusted hash.'
$liveIdentityIndex = $common.IndexOf('$liveIdentity')
$reuseReturnIndex = $common.IndexOf('return $null', $liveIdentityIndex)
$recordedModuleIndex = $common.IndexOf('Get-YeYuGamerPythonModuleName', $reuseReturnIndex)
Assert-Contract ($liveIdentityIndex -ge 0 -and $reuseReturnIndex -gt $liveIdentityIndex -and
    $recordedModuleIndex -gt $reuseReturnIndex) `
    'PID reuse must return inactive before validating the reused process command line.'

$start = $sources['Start-YeYuGamer.ps1']
foreach ($text in @('import PySide6', 'Use -NoTray -NoOpenWebGui', '-Command ping', '-Command ensure_manager', '-Command open_webgui', 'paired with the primary tray')) {
    Assert-Contract ($start.Contains($text)) "Start contract is missing: $text"
}
Assert-Contract (([regex]::Matches($start, 'manager-start')).Count -eq 1) 'Start may call CLI manager-start only in explicit Manager-only mode.'
Assert-Contract ($start.IndexOf('-Command ensure_manager') -lt $start.IndexOf('-Command open_webgui')) 'Start must complete tray pairing before opening WebGUI.'
Assert-Contract ($start.IndexOf('-Command open_webgui') -lt $start.LastIndexOf('Windows accepted the authenticated WebGUI open request')) 'Start success text must follow the completion-confirmed open command.'
$stop = $sources['Stop-YeYuGamer.ps1']
foreach ($text in @('[switch]$ExitTray', 'manager-stop', '-Command exit', '$trayPing -eq 3', 'The tray remains open')) {
    Assert-Contract ($stop.Contains($text)) "Stop contract is missing: $text"
}
Assert-Contract ($stop.IndexOf('manager-stop') -lt $stop.IndexOf('-Command exit')) 'Stop must stop Manager before requesting tray exit.'
foreach ($text in @(
    'Get-YeYuGamerPythonModuleProcesses',
    "'yeyu_gamer_platform.manager_host'",
    "'yeyu_gamer_manager'",
    '$managerModuleProcesses.Count -eq 0'
)) {
    Assert-Contract ($stop.Contains($text)) "Stop process-fence contract is missing: $text"
}
$exitTrayBranch = $stop.IndexOf('if (-not $ExitTray)')
$trayModuleFence = $stop.IndexOf("'yeyu_gamer_platform.tray'")
Assert-Contract ($exitTrayBranch -ge 0 -and $trayModuleFence -gt $exitTrayBranch) `
    'Stop script must only wait for the tray module inside the explicit -ExitTray branch.'
Assert-Contract ($stop.Contains('$trayPing -eq 3 -and $trayModuleProcesses.Count -eq 0')) `
    'Stop -ExitTray success must require both IPC disappearance and tray module process exit.'
$restart = $sources['Restart-YeYuGamer.ps1']
foreach ($text in @('-Command ping', 'manager-restart', '-Command ensure_manager', 're-paired with the existing tray')) {
    Assert-Contract ($restart.Contains($text)) "Restart contract is missing: $text"
}
Assert-Contract (-not $restart.Contains('-Command exit')) 'Restart may not exit the tray.'
Assert-Contract (-not $restart.Contains('manager-start')) 'Restart must not create an unpaired Manager through the CLI.'

$install = $sources['Install-YeYuGamer.ps1']
Assert-Contract (
    $install.Contains("-Arguments @('-I', '-B', '-m', 'venv', `$venvRoot)")
) 'Fresh venv creation does not protect dash-prefixed Python arguments with the native-wrapper named contract.'

. (Join-Path $ScriptsRoot 'YeYuGamer.Common.ps1')
$python = Get-YeYuGamerPython -ExplicitPath $PythonPath

$exactHostProcess = [pscustomobject]@{
    ProcessId = 41001
    ParentProcessId = 40001
    Name = 'pythonw.exe'
    CommandLine = '"C:\fixture\.venv\Scripts\pythonw.exe" -I -B -m yeyu_gamer_platform.manager_host --config "C:\fixture\platform.json"'
    CreationDate = [DateTime]::UtcNow
}
Assert-Contract (
    (Get-YeYuGamerPythonModuleName `
        -Process $exactHostProcess `
        -AllowedModuleName 'yeyu_gamer_platform.manager_host') -ceq 'yeyu_gamer_platform.manager_host'
) 'An exact pythonw -m Manager host process was not recognized.'

$falsePositiveProcesses = @(
    [pscustomobject]@{
        ProcessId = 41002
        ParentProcessId = 40001
        Name = 'pwsh.exe'
        CommandLine = 'pwsh.exe -m yeyu_gamer_platform.manager_host'
        CreationDate = [DateTime]::UtcNow
    },
    [pscustomobject]@{
        ProcessId = 41003
        ParentProcessId = 40001
        Name = 'python.exe'
        CommandLine = 'python.exe -c "print(''yeyu_gamer_platform.manager_host'')"'
        CreationDate = [DateTime]::UtcNow
    },
    [pscustomobject]@{
        ProcessId = 41004
        ParentProcessId = 40001
        Name = 'python.exe'
        CommandLine = 'python.exe harmless.py -m yeyu_gamer_manager'
        CreationDate = [DateTime]::UtcNow
    },
    [pscustomobject]@{
        ProcessId = 41005
        ParentProcessId = 40001
        Name = 'python.exe'
        CommandLine = 'python.exe -m harmless_module yeyu_gamer_platform.tray'
        CreationDate = [DateTime]::UtcNow
    }
)
foreach ($falsePositiveProcess in $falsePositiveProcesses) {
    Assert-Contract ($null -eq (Get-YeYuGamerPythonModuleName `
        -Process $falsePositiveProcess `
        -AllowedModuleName @(
            'yeyu_gamer_platform.manager_host',
            'yeyu_gamer_manager',
            'yeyu_gamer_platform.tray'
        ))) "A non-module command was misclassified as YeYu Gamer PID $($falsePositiveProcess.ProcessId)."
}

$script:moduleProcessFixtures = @(
    $exactHostProcess,
    [pscustomobject]@{
        ProcessId = 41006
        ParentProcessId = 41001
        Name = 'python.exe'
        CommandLine = '"C:\Python312\python.exe" -I -B -m yeyu_gamer_platform.manager_host --config "C:\fixture\platform.json"'
        CreationDate = [DateTime]::UtcNow
    },
    [pscustomobject]@{
        ProcessId = 41007
        ParentProcessId = 40001
        Name = 'python.exe'
        CommandLine = 'python.exe -I -B -m yeyu_gamer_manager --config "C:\fixture\manager.json"'
        CreationDate = [DateTime]::UtcNow
    },
    [pscustomobject]@{
        ProcessId = 41008
        ParentProcessId = 40001
        Name = 'pythonw.exe'
        CommandLine = 'pythonw.exe -I -B -m yeyu_gamer_platform.tray --config "C:\fixture\platform.json"'
        CreationDate = [DateTime]::UtcNow
    }
) + $falsePositiveProcesses
function Get-CimInstance {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
    return @($script:moduleProcessFixtures)
}
try {
    $hostMatches = @(Get-YeYuGamerPythonModuleProcesses `
        -ModuleName 'yeyu_gamer_platform.manager_host')
    Assert-Contract ($hostMatches.Count -eq 2) `
        'Exact process enumeration did not retain both the venv launcher and base Python child.'
    Assert-Contract (@($hostMatches.ProcessId | Sort-Object) -join ',' -ceq '41001,41006') `
        'Exact process enumeration returned the wrong Manager host PIDs.'
    $allMatches = @(Get-YeYuGamerPythonModuleProcesses -ModuleName @(
        'yeyu_gamer_platform.manager_host',
        'yeyu_gamer_manager',
        'yeyu_gamer_platform.tray'
    ))
    Assert-Contract ($allMatches.Count -eq 4) `
        'Installer process enumeration did not include host, Manager, and tray modules.'
} finally {
    Remove-Item -LiteralPath Function:\Get-CimInstance
    Remove-Variable -Name moduleProcessFixtures -Scope Script -ErrorAction SilentlyContinue
}

function Get-CimInstance {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
    throw 'simulated Win32_Process enumeration failure'
}
try {
    $enumerationFailureRejected = $false
    try {
        Get-YeYuGamerPythonModuleProcesses `
            -ModuleName 'yeyu_gamer_platform.manager_host' | Out-Null
    } catch {
        $enumerationFailureRejected = $_.Exception.Message.Contains(
            'lifecycle state is uncertain and the operation must fail closed'
        )
    }
    Assert-Contract $enumerationFailureRejected `
        'A failed Win32_Process query did not fail the lifecycle check closed.'
} finally {
    Remove-Item -LiteralPath Function:\Get-CimInstance
}

$missingCommandLineRejected = $false
try {
    Get-YeYuGamerPythonModuleName `
        -Process ([pscustomobject]@{ ProcessId = 41009; Name = 'python.exe'; CommandLine = $null }) `
        -AllowedModuleName 'yeyu_gamer_platform.manager_host' | Out-Null
} catch {
    $missingCommandLineRejected = $_.Exception.Message.Contains('lifecycle state is uncertain')
}
Assert-Contract $missingCommandLineRejected `
    'A Python process with an unreadable command line did not fail closed.'

$nativeProbeMarker = 'yeyu-native-named-arguments-ok'
$nativeProbeOutput = @(Invoke-YeYuGamerNative `
    -FilePath $python `
    -Arguments @('-I', '-B', '-c', "print('$nativeProbeMarker')"))
Assert-Contract ($nativeProbeMarker -in $nativeProbeOutput) `
    'Native-wrapper behavior probe did not forward -I/-B as child-process arguments.'

[pscustomobject]@{
    parser='passed'
    trayIpc='passed'
    pidIdentity='passed'
    exactModuleProcessEnumeration='passed'
    manifestV3='passed'
    lifecycleOrdering='passed'
    tcpListenerEnumeration='passed'
    nativeNamedArguments='passed'
}
