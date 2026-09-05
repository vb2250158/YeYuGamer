[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$RuntimeRoot,
    [ValidateRange(5, 300)][int]$TimeoutSeconds = 45,
    [switch]$UseSourceClient
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
if (-not $InstallRoot) { $InstallRoot = Get-YeYuGamerDefaultInstallRoot }
if (-not $RuntimeRoot) { $RuntimeRoot = Get-YeYuGamerDefaultRuntimeRoot }
$installResolved = Assert-YeYuGamerInstallRoot -Path $InstallRoot
$runtimeResolved = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$configPath = Join-Path $runtimeResolved 'config\platform.json'
$python = Join-Path $installResolved '.venv\Scripts\python.exe'
$desktopHost = Join-Path $installResolved 'app\desktop-host\YeYuGamer.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'YeYu Gamer is not installed or its lifecycle configuration is missing.'
}

function Get-YeYuGamerStopCliArguments {
    param([switch]$UseSourceClient)

    if (-not $UseSourceClient) { return @('-I', '-B', '-m', 'yeyu_gamer_platform.cli') }
    # The unified publisher has already tested this immutable local snapshot.
    # Load its repaired client without altering the installed package, endpoint,
    # credentials or Python environment while the old host still owns runtime.
    $platformSource = Assert-YeYuGamerLocalTarget `
        -Path (Join-Path (Split-Path -Parent $PSScriptRoot) 'platform') `
        -Purpose 'safe-stop source client'
    if ([System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($platformSource)).DriveType -ne [System.IO.DriveType]::Fixed) {
        throw 'The safe-stop source client must be on a fixed local disk.'
    }
    foreach ($relative in @('yeyu_gamer_platform\__init__.py', 'yeyu_gamer_platform\cli.py', 'yeyu_gamer_platform\api_client.py', 'yeyu_gamer_platform\config.py')) {
        $sourceFile = Join-Path $platformSource $relative
        Assert-YeYuGamerNoReparseAncestors -Path $sourceFile -Purpose 'safe-stop source client' | Out-Null
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw 'The tested safe-stop source client is incomplete.' }
    }
    $bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('yeyu_gamer_platform.cli',run_name='__main__')"
    return @('-I', '-B', '-c', $bootstrap, $platformSource)
}

$cliArguments = @(Get-YeYuGamerStopCliArguments -UseSourceClient:$UseSourceClient)
& $python @cliArguments --config $configPath health *> $null
if ($LASTEXITCODE -eq 0) {
    & $python @cliArguments --config $configPath manager-stop
    if ($LASTEXITCODE -ne 0) { throw 'YeYu Gamer rejected the safe stop request.' }
}

$trayProcesses = @(Get-YeYuGamerPythonModuleProcesses -ModuleName 'yeyu_gamer_platform.tray')
if ($trayProcesses.Count -gt 0) {
    & $python @cliArguments --config $configPath tray-exit
    if ($LASTEXITCODE -ne 0) { throw 'YeYu Gamer rejected the authenticated tray-exit request.' }
}

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $healthy = $false
    try { $healthy = (Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/v1/health' -TimeoutSec 2).status -eq 'ok' } catch {}
    $desktopHostProcesses = @(Get-CimInstance Win32_Process -Filter "Name='YeYuGamer.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $desktopHost })
    $moduleProcesses = @(Get-YeYuGamerPythonModuleProcesses -ModuleName @(
        'yeyu_gamer_platform.tray',
        'yeyu_gamer_platform.manager_host',
        'yeyu_gamer_manager'
    ))
    if (-not $healthy -and $desktopHostProcesses.Count -eq 0 -and $moduleProcesses.Count -eq 0) {
        Write-Host 'YeYu Gamer desktop host and all legacy lifecycle processes stopped safely.'
        exit 0
    }
    Start-Sleep -Milliseconds 250
} while ([DateTimeOffset]::UtcNow -lt $deadline)
throw 'YeYu Gamer accepted the safe-stop request but one or more lifecycle processes did not exit before timeout. No process was force-killed.'
