Set-StrictMode -Version Latest

$script:YeYuGamerCanonicalManagerBaseUrl = 'http://127.0.0.1:8877/api/v1'
$script:YeYuGamerInstallLifecycleMutexName = 'Local\YeYuGamer.InstallLifecycle.v1'

function Get-YeYuGamerCurrentLocalAppData {
    $localAppData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    if (-not $localAppData) { throw 'The current Windows user LocalApplicationData folder is unavailable.' }
    return [System.IO.Path]::GetFullPath($localAppData)
}

function Get-YeYuGamerCurrentProgramData {
    $programData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    if (-not $programData) { throw 'The Windows CommonApplicationData folder is unavailable.' }
    return [System.IO.Path]::GetFullPath($programData)
}

function Get-YeYuGamerDefaultProductWorkRoot {
    return Join-Path (Get-YeYuGamerCurrentLocalAppData) 'YeYuGamer'
}

function Get-YeYuGamerDefaultBuildRoot {
    return Join-Path (Get-YeYuGamerDefaultProductWorkRoot) 'build'
}

function Get-YeYuGamerDefaultPlatformTestRoot {
    return Join-Path (Get-YeYuGamerDefaultProductWorkRoot) 'platform-test'
}

function Get-YeYuGamerDefaultInstallRoot {
    return Join-Path (Get-YeYuGamerCurrentLocalAppData) 'Programs\YeYuGamer'
}

function Get-YeYuGamerDefaultRuntimeRoot {
    return Join-Path (Get-YeYuGamerCurrentProgramData) 'YeYuGamer\runtime'
}

function Test-YeYuGamerPathEquals {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    $leftResolved = [System.IO.Path]::GetFullPath($Left).TrimEnd('\')
    $rightResolved = [System.IO.Path]::GetFullPath($Right).TrimEnd('\')
    return $leftResolved.Equals($rightResolved, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-YeYuGamerNoReparseAncestors {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $cursor = $resolved
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Purpose has a reparse-point ancestor: $($item.FullName)"
            }
        }
        $parent = [System.IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
        if (-not $parent -or $parent.Equals($cursor, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $cursor = $parent
    }
    return $resolved
}

function Assert-YeYuGamerFixedProductPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Build', 'PlatformTest', 'BoundaryFixture')]
        [string]$Kind
    )

    $productRoot = [System.IO.Path]::GetFullPath((Get-YeYuGamerDefaultProductWorkRoot))
    $leaf = switch ($Kind) {
        'Build' { 'build' }
        'PlatformTest' { 'platform-test' }
        'BoundaryFixture' { 'boundary-fixture' }
    }
    $expected = [System.IO.Path]::GetFullPath((Join-Path $productRoot $leaf))
    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose "$Kind root"
    if (-not (Test-YeYuGamerPathEquals -Left $resolved -Right $expected)) {
        throw "$Kind root must use the fixed current-user YeYu Gamer path: $expected"
    }
    if (Test-YeYuGamerPathEquals -Left $resolved -Right $productRoot) {
        throw "$Kind root may not equal the YeYu Gamer product-work parent."
    }
    Assert-YeYuGamerNoReparseAncestors -Path $resolved -Purpose "$Kind root" | Out-Null
    if (Test-Path -LiteralPath $resolved) {
        Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose "$Kind root" | Out-Null
    }
    return $resolved
}

function Assert-YeYuGamerInstallRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $expected = Get-YeYuGamerDefaultInstallRoot
    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose 'InstallRoot'
    if (-not (Test-YeYuGamerPathEquals -Left $resolved -Right $expected)) {
        throw "InstallRoot must use the fixed current-user YeYu Gamer path: $expected"
    }
    Assert-YeYuGamerNoReparseAncestors -Path $resolved -Purpose 'InstallRoot' | Out-Null
    return $resolved
}

function Assert-YeYuGamerRuntimeRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $expected = Get-YeYuGamerDefaultRuntimeRoot
    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose 'RuntimeRoot'
    if (-not (Test-YeYuGamerPathEquals -Left $resolved -Right $expected)) {
        throw "RuntimeRoot must use the fixed YeYu Gamer ProgramData path: $expected"
    }
    Assert-YeYuGamerNoReparseAncestors -Path $resolved -Purpose 'RuntimeRoot' | Out-Null
    return $resolved
}

function Reset-YeYuGamerOwnedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Build', 'PlatformTest', 'BoundaryFixture')]
        [string]$Kind
    )

    $resolved = Assert-YeYuGamerFixedProductPath -Path $Path -Kind $Kind
    $sentinelName = '.yeyu-gamer-owned-directory'
    $sentinelValue = "YeYuGamer:${Kind}:v1"
    $sentinelPath = Join-Path $resolved $sentinelName
    if (Test-Path -LiteralPath $resolved) {
        if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
            throw "$Kind root is not a directory: $resolved"
        }
        if (-not (Test-Path -LiteralPath $sentinelPath -PathType Leaf)) {
            throw "$Kind root has no YeYu Gamer ownership sentinel; refusing recursive deletion: $resolved"
        }
        $actualSentinel = [System.IO.File]::ReadAllText($sentinelPath)
        if ($actualSentinel -cne $sentinelValue) {
            throw "$Kind root has an invalid YeYu Gamer ownership sentinel; refusing recursive deletion: $resolved"
        }
        Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose "$Kind cleanup root" | Out-Null
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    New-Item -ItemType Directory -Path $resolved -Force | Out-Null
    [System.IO.File]::WriteAllText(
        (Join-Path $resolved $sentinelName),
        $sentinelValue,
        [System.Text.UTF8Encoding]::new($false)
    )
    return $resolved
}

function Assert-YeYuGamerLocalTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if ([Uri]::new($resolved).IsUnc) { throw "$Purpose must be on a local disk, not UNC: $resolved" }
    $root = [System.IO.Path]::GetPathRoot($resolved)
    if (-not $root -or $resolved.TrimEnd('\') -eq $root.TrimEnd('\')) {
        throw "$Purpose may not be a drive root: $resolved"
    }
    if ($resolved -match '^[Zz]:\\') { throw "$Purpose may not use the NAS workspace drive: $resolved" }
    return $resolved
}

function Assert-YeYuGamerChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $parentResolved = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childResolved = [System.IO.Path]::GetFullPath($Child)
    if (-not $childResolved.StartsWith($parentResolved, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose escaped its intended root: $childResolved"
    }
    return $childResolved
}

function Remove-YeYuGamerAdapterBackups {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory = $true)][string]$AdapterRoot,
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$PackageName,
        [ValidateRange(0, 1000)][int]$Keep = 3
    )

    $root = Assert-YeYuGamerLocalTarget -Path $AdapterRoot -Purpose 'Adapter backup root'
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Adapter backup root is missing: $root"
    }
    Assert-YeYuGamerNoReparseTree -Path $root -Purpose 'Adapter backup root' | Out-Null

    $pattern = '^{0}\.previous-(\d{{14}})$' -f [regex]::Escape($PackageName)
    $backups = @(
        Get-ChildItem -LiteralPath $root -Directory -Force |
            Where-Object { $_.Name -match $pattern } |
            ForEach-Object {
                $match = [regex]::Match($_.Name, $pattern)
                [pscustomobject]@{
                    Name = $_.Name
                    FullName = $_.FullName
                    Timestamp = $match.Groups[1].Value
                }
            } |
            Sort-Object -Property Timestamp, Name -Descending
    )

    $remove = @($backups | Select-Object -Skip $Keep)
    foreach ($backup in $remove) {
        $safeBackup = Assert-YeYuGamerChildPath -Parent $root -Child $backup.FullName -Purpose 'Adapter backup cleanup target'
        if ($safeBackup -eq (Join-Path $root $PackageName)) {
            throw "Refusing to remove the live adapter package: $safeBackup"
        }
        if ($PSCmdlet.ShouldProcess($safeBackup, 'Remove adapter backup')) {
            Remove-Item -LiteralPath $safeBackup -Recurse -Force
        }
    }

    return [pscustomobject]@{
        adapterRoot = $root
        packageName = $PackageName
        keep = $Keep
        matchedCount = $backups.Count
        removalCandidateCount = $remove.Count
    }
}

function Get-YeYuGamerPython {
    param(
        [string]$InstallRoot,
        [string]$ExplicitPath
    )
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "The requested Python executable does not exist: $ExplicitPath"
        }
        return [System.IO.Path]::GetFullPath($ExplicitPath)
    }
    if ($InstallRoot) {
        $installed = Join-Path $InstallRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = (& $launcher.Source -3 -c "import sys; print(sys.executable)").Trim()
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            return $resolved
        }
    }
    if ($env:USERPROFILE) {
        $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundled -PathType Leaf) { return $bundled }
    }
    throw 'Python 3.11 or later was not found.'
}

function Invoke-YeYuGamerNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-YeYuGamerTrayIpc {
    [OutputType([int])]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)]
        [ValidateSet('ping', 'exit', 'ensure_manager', 'open_webgui')]
        [string]$Command,
        [ValidateRange(1, 300)][int]$TimeoutSeconds = 3
    )

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "The installed Python runtime is missing: $PythonPath"
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "The YeYu Gamer platform configuration is missing: $ConfigPath"
    }
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $previousNativePreference = $null
    try {
        if ($null -ne $nativePreference) {
            $previousNativePreference = [bool]$nativePreference.Value
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local
        }
        & $PythonPath -I -B -m yeyu_gamer_platform.tray_ipc `
            --config $ConfigPath `
            --timeout-seconds $TimeoutSeconds `
            $Command *> $null
        $exitCode = [int]$LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $previousNativePreference -Scope Local
        }
    }
    if ($exitCode -notin @(0, 2, 3, 4)) {
        throw "The tray IPC client returned an undocumented exit code $exitCode for $Command."
    }
    return $exitCode
}

function Write-YeYuGamerJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $json = $Value | ConvertTo-Json -Depth 20
    $temporary = "$Path.tmp"
    [System.IO.File]::WriteAllText($temporary, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-YeYuGamerWindowsProcessCreationIdentity {
    param([Parameter(Mandatory = $true)][object]$Process)

    if ($null -eq $Process.CreationDate) {
        throw 'Win32_Process did not expose CreationDate.'
    }
    try {
        $creationDate = if ($Process.CreationDate -is [DateTime]) {
            [DateTime]$Process.CreationDate
        } else {
            [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Process.CreationDate)
        }
        $fileTime = $creationDate.ToFileTimeUtc()
    } catch {
        throw "Win32_Process CreationDate could not be converted to a Windows file time: $($_.Exception.Message)"
    }
    if ($fileTime -le 0) { throw 'Win32_Process CreationDate produced an invalid Windows file time.' }
    return "windows-filetime:$fileTime"
}

function ConvertFrom-YeYuGamerWindowsCommandLine {
    [OutputType([string[]])]
    param([Parameter(Mandatory = $true)][string]$CommandLine)

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Exact YeYu Gamer process command-line parsing requires Windows.'
    }
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        throw 'Win32_Process did not expose a readable command line.'
    }

    $parserType = 'YeYuGamer.WindowsCommandLineParser' -as [type]
    if ($null -eq $parserType) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace YeYuGamer
{
    public static class WindowsCommandLineParser
    {
        [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CommandLineToArgvW(string commandLine, out int argumentCount);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static string[] Split(string commandLine)
        {
            int argumentCount;
            IntPtr arguments = CommandLineToArgvW(commandLine, out argumentCount);
            if (arguments == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            try
            {
                var result = new string[argumentCount];
                for (int index = 0; index < argumentCount; index++)
                {
                    IntPtr item = Marshal.ReadIntPtr(arguments, index * IntPtr.Size);
                    result[index] = Marshal.PtrToStringUni(item) ?? string.Empty;
                }
                return result;
            }
            finally
            {
                LocalFree(arguments);
            }
        }
    }
}
'@
        $parserType = 'YeYuGamer.WindowsCommandLineParser' -as [type]
        if ($null -eq $parserType) {
            throw 'The exact Windows command-line parser could not be loaded.'
        }
    }
    try {
        return @($parserType::Split($CommandLine))
    } catch {
        throw "Win32_Process command line could not be parsed exactly: $($_.Exception.Message)"
    }
}

function Get-YeYuGamerPythonModuleName {
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $true)][object]$Process,
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            'yeyu_gamer_platform.manager_host',
            'yeyu_gamer_manager',
            'yeyu_gamer_platform.tray'
        )]
        [string[]]$AllowedModuleName
    )

    if ($null -eq $Process.PSObject.Properties['Name'] -or
        [string]::IsNullOrWhiteSpace([string]$Process.Name)) {
        throw 'Win32_Process enumeration returned an entry without a process name.'
    }
    $processName = [System.IO.Path]::GetFileName([string]$Process.Name)
    if ($processName -notlike 'python*.exe') { return $null }

    if ($null -eq $Process.PSObject.Properties['ProcessId']) {
        throw "Win32_Process entry $processName is missing ProcessId."
    }
    $processId = [int]$Process.ProcessId
    if ($processId -le 0) {
        throw "Win32_Process entry $processName has an invalid ProcessId."
    }
    if ($null -eq $Process.PSObject.Properties['CommandLine'] -or
        [string]::IsNullOrWhiteSpace([string]$Process.CommandLine)) {
        throw "Python process PID $processId has no readable command line; lifecycle state is uncertain."
    }

    $arguments = @(ConvertFrom-YeYuGamerWindowsCommandLine -CommandLine ([string]$Process.CommandLine))
    if ($arguments.Count -lt 2) { return $null }
    $allowed = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($moduleName in $AllowedModuleName) { [void]$allowed.Add($moduleName) }

    # Parse only Python interpreter options. A later string passed to -c, a
    # script, or `--` is application data and must never look like ownership.
    for ($index = 1; $index -lt $arguments.Count; $index++) {
        $argument = [string]$arguments[$index]
        if ($argument -ceq '-m') {
            if ($index + 1 -ge $arguments.Count) { return $null }
            $moduleName = [string]$arguments[$index + 1]
            if ($allowed.Contains($moduleName)) { return $moduleName }
            return $null
        }
        if ($argument -ceq '--' -or $argument -ceq '-c' -or
            $argument.StartsWith('-c', [StringComparison]::Ordinal)) {
            return $null
        }
        if ($argument -in @('-W', '-X', '--check-hash-based-pycs')) {
            # These interpreter options consume the next token. Do not mistake
            # that value for a Python execution selector.
            $index++
            continue
        }
        if (-not $argument.StartsWith('-', [StringComparison]::Ordinal)) {
            return $null
        }
    }
    return $null
}

function Get-YeYuGamerPythonModuleProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            'yeyu_gamer_platform.manager_host',
            'yeyu_gamer_manager',
            'yeyu_gamer_platform.tray'
        )]
        [string[]]$ModuleName
    )

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'YeYu Gamer lifecycle process enumeration requires Windows.'
    }
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        throw (
            'Win32_Process module enumeration failed; lifecycle state is uncertain and the operation must fail closed: ' +
            $_.Exception.Message
        )
    }

    $matches = [System.Collections.Generic.List[object]]::new()
    foreach ($process in $processes) {
        if ($null -eq $process) {
            throw 'Win32_Process module enumeration returned a null entry; lifecycle state is uncertain.'
        }
        $matchedModule = Get-YeYuGamerPythonModuleName `
            -Process $process `
            -AllowedModuleName $ModuleName
        if ($null -eq $matchedModule) { continue }
        $matches.Add([pscustomobject]@{
            ProcessId = [int]$process.ProcessId
            ParentProcessId = if ($null -ne $process.PSObject.Properties['ParentProcessId']) {
                [int]$process.ParentProcessId
            } else { $null }
            Name = [string]$process.Name
            CommandLine = [string]$process.CommandLine
            ModuleName = [string]$matchedModule
            CreationDate = if ($null -ne $process.PSObject.Properties['CreationDate']) {
                $process.CreationDate
            } else { $null }
        })
    }
    return @($matches)
}

function Get-YeYuGamerRecordedManagerProcess {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot
    )

    $runtimeResolved = Assert-YeYuGamerLocalTarget -Path $RuntimeRoot -Purpose 'RuntimeRoot'
    $recordPath = Join-Path $runtimeResolved 'state\manager-process.json'
    try {
        $recordItem = Get-Item -LiteralPath $recordPath -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    } catch {
        throw "Manager PID record state cannot be read safely: $recordPath ($($_.Exception.Message))"
    }
    if ($recordItem.PSIsContainer) {
        throw "Manager PID record is not a regular file: $recordPath"
    }

    $recordValidationFailure = $null
    $recordedIdentity = $null
    try {
        $record = [System.IO.File]::ReadAllText($recordPath) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $record.PSObject.Properties['pid']) { throw 'the pid field is missing' }
        $recordedPid = [int]$record.pid
        if ($recordedPid -le 0) { throw 'the pid field is not a positive integer' }
        $schemaVersion = if ($null -eq $record.PSObject.Properties['schemaVersion']) {
            1
        } else {
            [int]$record.schemaVersion
        }
        if ($schemaVersion -eq 2) {
            if ($null -eq $record.PSObject.Properties['processCreationIdentity']) {
                $recordValidationFailure = 'schemaVersion 2 is missing processCreationIdentity'
            } else {
                $recordedIdentity = [string]$record.processCreationIdentity
                if ($recordedIdentity -cnotmatch '^windows-filetime:[1-9][0-9]*$') {
                    $recordValidationFailure = 'schemaVersion 2 has an invalid processCreationIdentity'
                }
            }
        } elseif ($schemaVersion -ne 1) {
            $recordValidationFailure = "the schemaVersion is unsupported: $schemaVersion"
        }
    } catch {
        throw "Manager PID record is invalid and installation must fail closed: $recordPath ($($_.Exception.Message))"
    }
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction Stop
    } catch {
        throw "Manager PID $recordedPid could not be queried; installation must fail closed: $($_.Exception.Message)"
    }
    if ($null -eq $process) { return $null }
    if ($recordValidationFailure) {
        throw "Manager PID record is malformed while PID $recordedPid is live; installation must fail closed: $recordValidationFailure"
    }
    if ($schemaVersion -eq 2) {
        $liveIdentity = Get-YeYuGamerWindowsProcessCreationIdentity -Process $process
        if (-not $liveIdentity.Equals($recordedIdentity, [StringComparison]::Ordinal)) {
            # The PID was reused after the recorded Manager exited.  The stale
            # record is not evidence that a Manager process is active.
            return $null
        }
    }
    $recordedModule = Get-YeYuGamerPythonModuleName `
        -Process $process `
        -AllowedModuleName 'yeyu_gamer_platform.manager_host'
    if ($recordedModule -cne 'yeyu_gamer_platform.manager_host') {
        throw "Manager PID record points at an unexpected live process $recordedPid; installation must fail closed"
    }
    return $process
}

function Get-YeYuGamerActiveTcpListeners {
    try {
        $properties = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
        if ($null -eq $properties) {
            throw 'IPGlobalProperties returned no local network-properties object'
        }
        $listeners = @($properties.GetActiveTcpListeners())
        foreach ($listener in $listeners) {
            if ($listener -isnot [System.Net.IPEndPoint] -or
                $null -eq $listener.Address -or
                $listener.Port -lt 1 -or
                $listener.Port -gt 65535) {
                throw 'the local TCP listener table contained an invalid endpoint'
            }
        }
        return $listeners
    } catch {
        throw (
            'Local TCP listener enumeration failed; installation must fail closed: ' +
            $_.Exception.Message
        )
    }
}

function Test-YeYuGamerTcpEndpointActive {
    try {
        $endpoint = [Uri]$script:YeYuGamerCanonicalManagerBaseUrl
        if (-not $endpoint.IsLoopback -or $endpoint.Port -lt 1 -or $endpoint.Port -gt 65535) {
            throw "the canonical Manager endpoint is not a valid loopback TCP endpoint: $endpoint"
        }
        foreach ($listener in @(Get-YeYuGamerActiveTcpListeners)) {
            if ($listener -isnot [System.Net.IPEndPoint] -or
                $null -eq $listener.Address -or
                $listener.Port -lt 1 -or
                $listener.Port -gt 65535) {
                throw 'the local TCP listener table contained an invalid endpoint'
            }
            # Any local listener on the fixed port is a blocker. Even when it is
            # bound to a non-loopback address, treating the port as available
            # would make installation depend on address-sharing socket flags.
            if ($listener.Port -eq $endpoint.Port) { return $true }
        }
        return $false
    } catch {
        throw (
            "Manager TCP listener enumeration could not prove canonical endpoint " +
            "$script:YeYuGamerCanonicalManagerBaseUrl closed; installation must fail closed: " +
            $_.Exception.Message
        )
    }
}

function Test-YeYuGamerNamedMutexActive {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { return $false }
    $mutex = $null
    $createdNew = $false
    try {
        $mutex = [System.Threading.Mutex]::new($false, $Name, [ref]$createdNew)
        return -not $createdNew
    } catch [System.UnauthorizedAccessException] {
        # An existing mutex that cannot be opened is still an active blocker.
        return $true
    } finally {
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

function Assert-YeYuGamerInstallLifecycleStopped {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [string]$TrayMutexName = 'Local\YeYuGamer.Tray.v3'
    )

    $blockers = [System.Collections.Generic.List[string]]::new()
    try {
        if (Test-YeYuGamerTcpEndpointActive) {
            $blockers.Add('canonical Manager endpoint port 8877 is active')
        }
    } catch {
        $blockers.Add("canonical Manager endpoint state is uncertain: $($_.Exception.Message)")
    }
    try {
        $recordedManagerProcess = Get-YeYuGamerRecordedManagerProcess -RuntimeRoot $RuntimeRoot
        if ($null -ne $recordedManagerProcess) {
            $blockers.Add("recorded Manager host PID $($recordedManagerProcess.ProcessId) is active")
        }
    } catch {
        $blockers.Add("recorded Manager PID state is uncertain: $($_.Exception.Message)")
    }
    try {
        $moduleProcesses = @(Get-YeYuGamerPythonModuleProcesses -ModuleName @(
            'yeyu_gamer_platform.manager_host',
            'yeyu_gamer_manager',
            'yeyu_gamer_platform.tray'
        ))
        foreach ($moduleProcess in $moduleProcesses) {
            $blockers.Add(
                "YeYu Gamer module $($moduleProcess.ModuleName) PID $($moduleProcess.ProcessId) is active"
            )
        }
    } catch {
        $blockers.Add("YeYu Gamer module process state is uncertain: $($_.Exception.Message)")
    }
    if (Test-YeYuGamerNamedMutexActive -Name $TrayMutexName) {
        $blockers.Add('the YeYu Gamer tray is active')
    }
    if ($blockers.Count -gt 0) {
        throw (
            'YeYu Gamer installation preflight failed before app directory rotation. ' +
            "Active lifecycle blockers: $($blockers -join '; '). " +
            'Use the tray menu to safely stop Manager and then exit the tray; ' +
            'if the port remains active, resolve its owner before retrying. No process was force-killed.'
        )
    }
}

function Enter-YeYuGamerInstallLifecycleLock {
    param([string]$Name = $script:YeYuGamerInstallLifecycleMutexName)

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'The YeYu Gamer installer lifecycle lock requires Windows.'
    }
    $createdNew = $false
    $mutex = $null
    try {
        $mutex = [System.Threading.Mutex]::new($false, $Name, [ref]$createdNew)
        try {
            $acquired = $mutex.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            try { $mutex.ReleaseMutex() } catch { }
            throw 'A previous YeYu Gamer installation abandoned the lifecycle lock; repair the interrupted installation before retrying.'
        }
        if (-not $acquired) {
            throw 'Another YeYu Gamer installation or lifecycle transition is already in progress.'
        }
        return $mutex
    } catch {
        if ($null -ne $mutex) { $mutex.Dispose() }
        throw
    }
}

function Exit-YeYuGamerInstallLifecycleLock {
    param([Parameter(Mandatory = $true)][System.Threading.Mutex]$Mutex)

    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}

function New-YeYuGamerInstallTransaction {
    param(
        [Parameter(Mandatory = $true)][object[]]$DirectoryTargets,
        [Parameter(Mandatory = $true)][string[]]$FileTargets
    )

    $transactionId = [Guid]::NewGuid().ToString('N')
    $entries = [System.Collections.Generic.List[object]]::new()
    $seenTargets = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    try {
        foreach ($targetSpec in $DirectoryTargets) {
            $target = [System.IO.Path]::GetFullPath([string]$targetSpec.Path)
            if (-not $seenTargets.Add($target)) { throw "Duplicate install transaction target: $target" }
            $preserveAs = if ($null -ne $targetSpec.PSObject.Properties['PreserveAs'] -and $targetSpec.PreserveAs) {
                [System.IO.Path]::GetFullPath([string]$targetSpec.PreserveAs)
            } else { $null }
            $backup = "$target.install-backup-$transactionId"
            if (Test-Path -LiteralPath $backup) { throw "Install transaction backup already exists: $backup" }
            $existed = Test-Path -LiteralPath $target -PathType Container
            if ($existed) {
                Assert-YeYuGamerNoReparseTree -Path $target -Purpose 'install transaction directory' | Out-Null
                Move-Item -LiteralPath $target -Destination $backup
            } elseif (Test-Path -LiteralPath $target) {
                throw "Install transaction directory target is not a directory: $target"
            }
            $entries.Add([pscustomobject]@{
                Target = $target
                Backup = $backup
                RestoreSource = $backup
                Existed = $existed
                IsDirectory = $true
                PreserveAs = $preserveAs
            })
        }
        foreach ($fileTarget in $FileTargets) {
            $target = [System.IO.Path]::GetFullPath($fileTarget)
            if (-not $seenTargets.Add($target)) { throw "Duplicate install transaction target: $target" }
            $backup = "$target.install-backup-$transactionId"
            if (Test-Path -LiteralPath $backup) { throw "Install transaction backup already exists: $backup" }
            $existed = Test-Path -LiteralPath $target -PathType Leaf
            if ($existed) {
                $item = Get-Item -LiteralPath $target -Force
                if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "Install transaction file is a reparse point: $target"
                }
                Move-Item -LiteralPath $target -Destination $backup
            } elseif (Test-Path -LiteralPath $target) {
                throw "Install transaction file target is not a regular file: $target"
            }
            $entries.Add([pscustomobject]@{
                Target = $target
                Backup = $backup
                RestoreSource = $backup
                Existed = $existed
                IsDirectory = $false
                PreserveAs = $null
            })
        }
    } catch {
        foreach ($entry in $entries) {
            if ($entry.Existed -and (Test-Path -LiteralPath $entry.Backup)) {
                Move-Item -LiteralPath $entry.Backup -Destination $entry.Target
            }
        }
        throw
    }
    return [pscustomobject]@{ Id = $transactionId; Entries = @($entries); Committed = $false }
}

function Undo-YeYuGamerInstallTransaction {
    param([Parameter(Mandatory = $true)][object]$Transaction)

    foreach ($entry in $Transaction.Entries) {
        if (Test-Path -LiteralPath $entry.Target) {
            if ($entry.IsDirectory) {
                Assert-YeYuGamerNoReparseTree -Path $entry.Target -Purpose 'failed install transaction output' | Out-Null
                Remove-Item -LiteralPath $entry.Target -Recurse -Force
            } else {
                Remove-Item -LiteralPath $entry.Target -Force
            }
        }
        if ($entry.Existed -and (Test-Path -LiteralPath $entry.RestoreSource)) {
            Move-Item -LiteralPath $entry.RestoreSource -Destination $entry.Target
        }
    }
}

function Complete-YeYuGamerInstallTransaction {
    param([Parameter(Mandatory = $true)][object]$Transaction)

    foreach ($entry in $Transaction.Entries | Where-Object { $_.Existed -and $_.PreserveAs }) {
        if (Test-Path -LiteralPath $entry.PreserveAs) {
            throw "Install transaction preserve target unexpectedly exists: $($entry.PreserveAs)"
        }
        Move-Item -LiteralPath $entry.Backup -Destination $entry.PreserveAs
        $entry.RestoreSource = $entry.PreserveAs
    }
    $Transaction.Committed = $true

    foreach ($entry in $Transaction.Entries | Where-Object {
        $_.Existed -and -not $_.PreserveAs -and (Test-Path -LiteralPath $_.Backup)
    }) {
        try {
            if ($entry.IsDirectory) {
                Assert-YeYuGamerNoReparseTree -Path $entry.Backup -Purpose 'obsolete install transaction backup' | Out-Null
                Remove-Item -LiteralPath $entry.Backup -Recurse -Force
            } else {
                Remove-Item -LiteralPath $entry.Backup -Force
            }
        } catch {
            Write-Warning "Installed successfully but could not remove obsolete transaction backup $($entry.Backup): $($_.Exception.Message)"
        }
    }
}

function Invoke-YeYuGamerInstallTransaction {
    param(
        [Parameter(Mandatory = $true)][object[]]$DirectoryTargets,
        [Parameter(Mandatory = $true)][string[]]$FileTargets,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    $transaction = New-YeYuGamerInstallTransaction `
        -DirectoryTargets $DirectoryTargets `
        -FileTargets $FileTargets
    try {
        & $Action
        Complete-YeYuGamerInstallTransaction -Transaction $transaction
    } catch {
        if (-not $transaction.Committed) {
            Undo-YeYuGamerInstallTransaction -Transaction $transaction
        }
        throw
    }
}

function Invoke-YeYuGamerLegacySeed {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$CandidateSourceRoot,
        [Parameter(Mandatory = $true)][string]$DestinationRoot
    )

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Legacy seed Python executable does not exist: $PythonPath"
    }
    $sourceResolved = [System.IO.Path]::GetFullPath($CandidateSourceRoot)
    $destinationResolved = Assert-YeYuGamerLocalTarget -Path $DestinationRoot -Purpose 'legacy seed destination'
    $legacyConfig = Join-Path $sourceResolved 'daily-gui-config.json'
    $legacyPolicy = Join-Path $sourceResolved 'game-automation-policy.json'
    if (-not (Test-Path -LiteralPath $legacyConfig -PathType Leaf) -or
        -not (Test-Path -LiteralPath $legacyPolicy -PathType Leaf)) {
        Write-Warning 'No complete legacy config/policy pair was found; local seed remains empty.'
        return
    }
    New-Item -ItemType Directory -Path $destinationResolved -Force | Out-Null

    # Python can intermittently fail while opening non-ASCII files on some SMB
    # redirectors.  Stage only the two allowlisted migration inputs on the local
    # disk, retry their exact copies, run the structured exporter locally, and
    # always remove the raw staging files.  No scripts or sibling files cross the
    # boundary.
    $temporaryParent = Assert-YeYuGamerLocalTarget `
        -Path (Join-Path ([System.IO.Path]::GetTempPath()) 'YeYuGamer') `
        -Purpose 'legacy seed staging parent'
    New-Item -ItemType Directory -Path $temporaryParent -Force | Out-Null
    $stagingResolved = Assert-YeYuGamerChildPath `
        -Parent $temporaryParent `
        -Child (Join-Path $temporaryParent ("legacy-seed-{0}" -f [Guid]::NewGuid().ToString('N'))) `
        -Purpose 'legacy seed staging directory'
    New-Item -ItemType Directory -Path $stagingResolved -Force | Out-Null
    try {
        foreach ($sourceFile in @($legacyConfig, $legacyPolicy)) {
            $destinationFile = Join-Path $stagingResolved (Split-Path -Leaf $sourceFile)
            $copied = $false
            for ($attempt = 1; $attempt -le 3 -and -not $copied; $attempt++) {
                try {
                    Copy-Item `
                        -LiteralPath $sourceFile `
                        -Destination $destinationFile `
                        -Force `
                        -ErrorAction Stop
                    if (-not (Test-Path -LiteralPath $destinationFile -PathType Leaf)) {
                        throw "Legacy seed staging copy did not create: $destinationFile"
                    }
                    $copied = $true
                } catch {
                    if ($attempt -eq 3) { throw }
                    Start-Sleep -Milliseconds (250 * $attempt)
                }
            }
        }

        Invoke-YeYuGamerNative -FilePath $PythonPath -Arguments @(
            '-B',
            '-m',
            'yeyu_gamer_manager.services.legacy_seed',
            '--source',
            $stagingResolved,
            '--destination',
            $destinationResolved
        )
    } finally {
        $verifiedStaging = Assert-YeYuGamerChildPath `
            -Parent $temporaryParent `
            -Child $stagingResolved `
            -Purpose 'legacy seed staging cleanup'
        if (Test-Path -LiteralPath $verifiedStaging -PathType Container) {
            Remove-Item -LiteralPath $verifiedStaging -Recurse -Force
        }
    }
}

function Protect-YeYuGamerActorTokensDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose 'actor token directory'
    New-Item -ItemType Directory -Path $resolved -Force | Out-Null
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Actor token ACL provisioning is supported only on Windows.'
    }

    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $currentSid = $currentIdentity.User
    if ($null -eq $currentSid) { throw 'Could not resolve the current Windows user SID.' }
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $fullControl = [Security.AccessControl.FileSystemRights]::FullControl

    $directoryAcl = [Security.AccessControl.DirectorySecurity]::new()
    $directoryAcl.SetAccessRuleProtection($true, $false)
    $directoryAcl.SetOwner($currentSid)
    foreach ($sid in @($currentSid, $systemSid)) {
        $directoryAcl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                $fullControl,
                $inheritance,
                $propagation,
                $allow
            )
        ) | Out-Null
    }
    Set-Acl -LiteralPath $resolved -AclObject $directoryAcl

    # Reapply the same explicit principals to tokens preserved by an upgrade.
    foreach ($tokenFile in @(Get-ChildItem -LiteralPath $resolved -File -Filter '*.token' -Force)) {
        $tokenAcl = [Security.AccessControl.FileSecurity]::new()
        $tokenAcl.SetAccessRuleProtection($true, $false)
        $tokenAcl.SetOwner($currentSid)
        foreach ($sid in @($currentSid, $systemSid)) {
            $tokenAcl.AddAccessRule(
                [Security.AccessControl.FileSystemAccessRule]::new(
                    $sid,
                    $fullControl,
                    $allow
                )
            ) | Out-Null
        }
        Set-Acl -LiteralPath $tokenFile.FullName -AclObject $tokenAcl
    }

    $verified = Get-Acl -LiteralPath $resolved
    if (-not $verified.AreAccessRulesProtected) {
        throw 'Actor token directory still inherits ACL entries.'
    }
    $explicitRules = @($verified.GetAccessRules(
        $true,
        $false,
        [Security.Principal.SecurityIdentifier]
    ))
    $expectedSids = @($currentSid.Value, $systemSid.Value)
    foreach ($rule in $explicitRules) {
        if ($rule.AccessControlType -ne $allow -or
            $rule.IdentityReference.Value -notin $expectedSids) {
            throw "Actor token directory contains an unexpected explicit ACL: $($rule.IdentityReference.Value)"
        }
    }
    foreach ($expectedSid in $expectedSids) {
        $matchingRules = @($explicitRules | Where-Object {
            $_.IdentityReference.Value -eq $expectedSid -and
            (($_.FileSystemRights -band $fullControl) -eq $fullControl)
        })
        if ($matchingRules.Count -eq 0) {
            throw "Actor token directory lacks FullControl for SID $expectedSid"
        }
    }
    return $resolved
}

function Assert-YeYuGamerNoReparseTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose $Purpose
    Assert-YeYuGamerNoReparseAncestors -Path $resolved -Purpose $Purpose | Out-Null
    if (-not (Test-Path -LiteralPath $resolved)) { return $resolved }

    $rootItem = Get-Item -LiteralPath $resolved -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Purpose contains a reparse point: $($rootItem.FullName)"
    }
    if ($rootItem.PSIsContainer) {
        $pending = [System.Collections.Generic.Stack[string]]::new()
        $pending.Push($rootItem.FullName)
        while ($pending.Count -gt 0) {
            $directory = $pending.Pop()
            foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force)) {
                if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "$Purpose contains a reparse point: $($item.FullName)"
                }
                if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            }
        }
    }
    return $resolved
}

function Protect-YeYuGamerRuntimePackageDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $resolved = Assert-YeYuGamerLocalTarget -Path $Path -Purpose 'runtime package directory'
    New-Item -ItemType Directory -Path $resolved -Force | Out-Null
    Assert-YeYuGamerNoReparseTree -Path $resolved -Purpose 'runtime package directory' | Out-Null
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Runtime package ACL provisioning is supported only on Windows.'
    }

    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentSid) { throw 'Could not resolve the current Windows user SID.' }
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None

    $directoryAcl = [Security.AccessControl.DirectorySecurity]::new()
    $directoryAcl.SetAccessRuleProtection($true, $false)
    $directoryAcl.SetOwner($currentSid)
    foreach ($sid in @($currentSid, $systemSid)) {
        $directoryAcl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                $fullControl,
                $inheritance,
                $propagation,
                $allow
            )
        ) | Out-Null
    }
    Set-Acl -LiteralPath $resolved -AclObject $directoryAcl

    foreach ($file in @(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force)) {
        $fileAcl = [Security.AccessControl.FileSecurity]::new()
        $fileAcl.SetAccessRuleProtection($true, $false)
        $fileAcl.SetOwner($currentSid)
        foreach ($sid in @($currentSid, $systemSid)) {
            $fileAcl.AddAccessRule(
                [Security.AccessControl.FileSystemAccessRule]::new(
                    $sid,
                    $fullControl,
                    $allow
                )
            ) | Out-Null
        }
        Set-Acl -LiteralPath $file.FullName -AclObject $fileAcl
    }
    return $resolved
}

function Protect-YeYuGamerNotificationSecretsDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    # The runtime-package protector implements the same required ACL contract:
    # inheritance disabled, no reparse points, and FullControl for exactly the
    # current Windows user plus SYSTEM on the directory and every blob.
    $resolved = Protect-YeYuGamerRuntimePackageDirectory -Path $Path
    $verified = Get-Acl -LiteralPath $resolved
    if (-not $verified.AreAccessRulesProtected) {
        throw 'Notification secrets directory still inherits ACL entries.'
    }
    return $resolved
}

function Test-YeYuGamerRuntimePackage {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedPackageId,
        [Parameter(Mandatory = $true)][string]$ExpectedEntryPoint
    )

    $packageResolved = Assert-YeYuGamerLocalTarget -Path $PackageRoot -Purpose 'runtime package root'
    if (-not (Test-Path -LiteralPath $packageResolved -PathType Container)) {
        throw "Runtime package root is missing: $packageResolved"
    }
    Assert-YeYuGamerNoReparseTree -Path $packageResolved -Purpose 'runtime package' | Out-Null
    $manifestResolved = Assert-YeYuGamerChildPath `
        -Parent $packageResolved `
        -Child $ManifestPath `
        -Purpose 'runtime package manifest'
    $expectedManifestPath = Join-Path $packageResolved 'install-manifest.json'
    if (-not $manifestResolved.Equals(
        [System.IO.Path]::GetFullPath($expectedManifestPath),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Runtime package manifest must use the fixed install-manifest.json path.'
    }
    if (-not (Test-Path -LiteralPath $manifestResolved -PathType Leaf)) {
        throw "Runtime package manifest is missing: $manifestResolved"
    }

    $manifest = Get-Content -LiteralPath $manifestResolved -Raw | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1) {
        throw "Unsupported runtime package manifest schema: $($manifest.schemaVersion)"
    }
    if ([string]$manifest.packageId -cne $ExpectedPackageId) {
        throw "Runtime package ID mismatch: $($manifest.packageId)"
    }
    if ([string]$manifest.entryPoint -cne $ExpectedEntryPoint -or
        [System.IO.Path]::IsPathRooted([string]$manifest.entryPoint) -or
        @(([string]$manifest.entryPoint -split '[\\/]') | Where-Object {
            -not $_ -or $_ -in @('.', '..')
        }).Count -gt 0) {
        throw "Runtime package entry point mismatch: $($manifest.entryPoint)"
    }

    $entryPointPath = Assert-YeYuGamerChildPath `
        -Parent $packageResolved `
        -Child (Join-Path $packageResolved ([string]$manifest.entryPoint)) `
        -Purpose 'runtime package entry point'
    if (-not (Test-Path -LiteralPath $entryPointPath -PathType Leaf)) {
        throw "Runtime package entry point is missing: $entryPointPath"
    }
    $entryPoint = Get-Item -LiteralPath $entryPointPath -Force
    if ([int64]$manifest.sizeBytes -ne $entryPoint.Length) {
        throw "Runtime package size mismatch: $($manifest.entryPoint)"
    }
    $actualHash = (Get-FileHash -LiteralPath $entryPointPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne ([string]$manifest.sha256).ToLowerInvariant()) {
        throw "Runtime package SHA-256 mismatch: $($manifest.entryPoint)"
    }

    $actualDirectories = @(Get-ChildItem -LiteralPath $packageResolved -Recurse -Directory -Force)
    if ($actualDirectories.Count -ne 0) {
        throw "Runtime package contains an undeclared directory: $($actualDirectories[0].FullName)"
    }
    $actualFiles = @(Get-ChildItem -LiteralPath $packageResolved -Recurse -File -Force)
    $expectedFileNames = @($ExpectedEntryPoint, 'install-manifest.json')
    if ($actualFiles.Count -ne $expectedFileNames.Count) {
        throw "Runtime package file count mismatch: actual=$($actualFiles.Count), expected=$($expectedFileNames.Count)"
    }
    foreach ($file in $actualFiles) {
        $relative = $file.FullName.Substring($packageResolved.Length + 1)
        if ($relative -notin $expectedFileNames) {
            throw "Runtime package contains an undeclared file: $relative"
        }
    }

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Runtime package ACL verification is supported only on Windows.'
    }
    $expectedSids = @(
        [Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
        'S-1-5-18'
    )
    $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($securedItem in @((Get-Item -LiteralPath $packageResolved -Force)) + $actualFiles) {
        $acl = Get-Acl -LiteralPath $securedItem.FullName
        if (-not $acl.AreAccessRulesProtected) {
            throw "Runtime package ACL inheritance is enabled: $($securedItem.FullName)"
        }
        $rules = @($acl.GetAccessRules(
            $true,
            $false,
            [Security.Principal.SecurityIdentifier]
        ))
        foreach ($rule in $rules) {
            if ($rule.AccessControlType -ne $allow -or
                $rule.IdentityReference.Value -notin $expectedSids) {
                throw "Runtime package contains an unexpected ACL: $($rule.IdentityReference.Value)"
            }
        }
        foreach ($expectedSid in $expectedSids) {
            $matchingRules = @($rules | Where-Object {
                $_.IdentityReference.Value -eq $expectedSid -and
                (($_.FileSystemRights -band $fullControl) -eq $fullControl)
            })
            if ($matchingRules.Count -eq 0) {
                throw "Runtime package lacks FullControl for SID $expectedSid"
            }
        }
    }
    return $manifest
}

function Assert-YeYuGamerManifestFileTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object[]]$Entries,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $rootResolved = Assert-YeYuGamerLocalTarget -Path $Root -Purpose "$Label manifest root"
    if (-not (Test-Path -LiteralPath $rootResolved -PathType Container)) {
        throw "$Label manifest root is missing: $rootResolved"
    }
    Assert-YeYuGamerNoReparseTree -Path $rootResolved -Purpose "$Label manifest tree" | Out-Null
    if ($Entries.Count -eq 0) { throw "$Label manifest contains no files." }
    $declared = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $Entries) {
        $relativeText = [string]$entry.path
        $segments = $relativeText -split '[\\/]'
        if (-not $relativeText -or [System.IO.Path]::IsPathRooted($relativeText) -or
            @($segments | Where-Object { -not $_ -or $_ -in @('.', '..') }).Count -gt 0 -or
            $declared.ContainsKey($relativeText)) {
            throw "$Label manifest contains an invalid or duplicate path: $relativeText"
        }
        $declaredHash = [string]$entry.sha256
        if ($declaredHash -cnotmatch '^[0-9a-f]{64}$' -or [int64]$entry.bytes -lt 0) {
            throw "$Label manifest contains invalid file metadata: $relativeText"
        }
        $relativeNative = $relativeText.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        $filePath = Assert-YeYuGamerChildPath -Parent $rootResolved -Child (Join-Path $rootResolved $relativeNative) -Purpose "$Label manifest file"
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "$Label manifest-declared file is missing: $relativeText"
        }
        $file = Get-Item -LiteralPath $filePath -Force
        if ([int64]$entry.bytes -ne [int64]$file.Length) { throw "$Label manifest size mismatch: $relativeText" }
        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -cne $declaredHash) { throw "$Label manifest SHA-256 mismatch: $relativeText" }
        $declared.Add($relativeText, $entry)
    }
    $actualFiles = @(Get-ChildItem -LiteralPath $rootResolved -Recurse -File -Force)
    if ($actualFiles.Count -ne $declared.Count) {
        throw "$Label manifest file count differs from its tree: actual=$($actualFiles.Count), declared=$($declared.Count)"
    }
    foreach ($file in $actualFiles) {
        $relative = $file.FullName.Substring($rootResolved.Length + 1).Replace('\', '/')
        if (-not $declared.ContainsKey($relative)) { throw "$Label tree contains an undeclared file: $relative" }
    }
}

function Test-YeYuGamerBuildManifest {
    param(
        [Parameter(Mandatory = $true)][string]$StagedRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [switch]$AllowRelocatedRoot
    )

    $stageResolved = Assert-YeYuGamerLocalTarget -Path $StagedRoot -Purpose 'staged build root'
    $manifestResolved = Assert-YeYuGamerLocalTarget -Path $ManifestPath -Purpose 'build manifest'
    if (-not (Test-Path -LiteralPath $manifestResolved -PathType Leaf)) { throw "Build manifest is missing: $manifestResolved" }
    $buildResolved = Split-Path -Parent $manifestResolved
    $expectedManifest = Join-Path $buildResolved 'build-manifest.json'
    $expectedStage = Join-Path $buildResolved 'staged-app'
    if (-not $manifestResolved.Equals([System.IO.Path]::GetFullPath($expectedManifest), [StringComparison]::OrdinalIgnoreCase) -or
        -not $stageResolved.Equals([System.IO.Path]::GetFullPath($expectedStage), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build manifest and staged app must use the fixed sibling layout under one BuildRoot.'
    }
    Assert-YeYuGamerNoReparseTree -Path $buildResolved -Purpose 'selected build tree' | Out-Null
    $manifestText = Get-Content -LiteralPath $manifestResolved -Raw
    $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
    if ([int]$manifest.schemaVersion -ne 3) { throw "Unsupported build manifest schema: $($manifest.schemaVersion)" }
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
    if ([string]$manifest.wheelhouse.relativePath -cne 'wheelhouse' -or
        [string]$manifest.wheelhouse.lockFile -cne 'requirements.lock' -or
        [string]$manifest.wheelhouse.installMode -cne 'no-index' -or
        -not [bool]$manifest.wheelhouse.requireHashes) {
        throw 'Build manifest wheelhouse contract is invalid.'
    }
    $wheelhouseResolved = Join-Path $buildResolved 'wheelhouse'
    $allowedTopLevel = @('.yeyu-gamer-owned-directory', 'build-manifest.json', 'staged-app', 'wheelhouse')
    $unexpected = @(Get-ChildItem -LiteralPath $buildResolved -Force | Where-Object Name -notin $allowedTopLevel)
    if ($unexpected.Count -gt 0) { throw "BuildRoot contains an undeclared top-level item: $($unexpected[0].Name)" }

    Assert-YeYuGamerManifestFileTree -Root $stageResolved -Entries @($manifest.appFiles) -Label 'application'
    Assert-YeYuGamerManifestFileTree -Root $wheelhouseResolved -Entries @($manifest.wheelhouseFiles) -Label 'wheelhouse'

    $packagedLock = Join-Path $stageResolved 'provenance\requirements.lock'
    $wheelhouseLock = Join-Path $wheelhouseResolved 'requirements.lock'
    $packagedProvenance = Join-Path $stageResolved 'provenance\dependency-provenance.json'
    foreach ($dependencyFile in @($packagedLock, $wheelhouseLock, $packagedProvenance)) {
        if (-not (Test-Path -LiteralPath $dependencyFile -PathType Leaf) -or
            (Get-Item -LiteralPath $dependencyFile).Length -le 0) {
            throw "Build manifest dependency provenance is missing or empty: $(Split-Path -Leaf $dependencyFile)"
        }
    }
    $lockHash = (Get-FileHash -LiteralPath $packagedLock -Algorithm SHA256).Hash.ToLowerInvariant()
    $wheelhouseLockHash = (Get-FileHash -LiteralPath $wheelhouseLock -Algorithm SHA256).Hash.ToLowerInvariant()
    $provenanceHash = (Get-FileHash -LiteralPath $packagedProvenance -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([string]$manifest.dependencyLock.sourcePath -cne 'packaging/requirements.cpython312-win_amd64.lock' -or
        [string]$manifest.dependencyLock.packagedPath -cne 'provenance/requirements.lock' -or
        [string]$manifest.dependencyLock.wheelhousePath -cne 'requirements.lock' -or
        [string]$manifest.dependencyLock.target -cne 'cpython312-win_amd64' -or
        [int]$manifest.dependencyLock.packageCount -le 0 -or
        [string]$manifest.dependencyLock.sha256 -cne $lockHash -or
        $wheelhouseLockHash -cne $lockHash -or
        [string]$manifest.dependencyLock.provenanceSha256 -cne $provenanceHash) {
        throw 'Build manifest dependency lock does not match its source-derived trust anchor.'
    }
    $dependencyProvenance = Get-Content -LiteralPath $packagedProvenance -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([int]$dependencyProvenance.schemaVersion -ne 1 -or
        [string]$dependencyProvenance.lockSha256 -cne $lockHash -or
        [string]$dependencyProvenance.target.implementation -cne 'CPython' -or
        [string]$dependencyProvenance.target.pythonVersion -cne '3.12' -or
        [string]$dependencyProvenance.target.abi -cne 'cp312' -or
        [string]$dependencyProvenance.target.platform -cne 'win_amd64' -or
        @($dependencyProvenance.artifacts).Count -ne [int]$manifest.dependencyLock.packageCount) {
        throw 'Build manifest dependency provenance is invalid.'
    }

    $webIndex = Join-Path $stageResolved 'webgui\dist\index.html'
    $webAssets = Join-Path $stageResolved 'webgui\dist\assets'
    if (-not (Test-Path -LiteralPath $webIndex -PathType Leaf) -or (Get-Item -LiteralPath $webIndex).Length -le 0 -or
        -not (Test-Path -LiteralPath $webAssets -PathType Container) -or
        @(Get-ChildItem -LiteralPath $webAssets -Recurse -File | Where-Object Length -gt 0).Count -eq 0) {
        throw 'Build manifest does not cover a complete WebGUI dist/index.html and asset tree.'
    }
    $expectedRuntimeTopLevel = @('adapter-host', 'backend', 'contracts', 'packages', 'provenance', 'scripts', 'webgui')
    if ((@($manifest.runtimeAllowlist.topLevel) -join '|') -cne ($expectedRuntimeTopLevel -join '|') -or
        [string]$manifest.packagePrivacy.exactMachineTokenScan -cne 'passed' -or
        [bool]$manifest.packagePrivacy.absolutePathsInManifest) {
        throw 'Build manifest runtime allowlist or privacy contract is invalid.'
    }
    $forbiddenRuntimeSegments = @('tests', 'docs', '__pycache__', '.pytest_cache', 'node_modules')
    foreach ($appEntry in @($manifest.appFiles)) {
        $appRelative = [string]$appEntry.path
        $segments = $appRelative -split '/'
        if (@($segments | Where-Object { $_ -in $forbiddenRuntimeSegments }).Count -gt 0 -or
            $appRelative -match '(?i)\.(?:py|pyc|pyo|cs|ts|tsx|vue|map|md)$' -or
            (Split-Path -Leaf $appRelative) -match '^(?:Build|Install|Test)-') {
            throw "Build manifest includes a development-only runtime file: $appRelative"
        }
    }
    return $manifest
}
