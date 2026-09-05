[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot,
    [ValidatePattern('^\d{2}:\d{2}$')][string]$Time = '04:00',
    [string]$TaskPath = '\YeYuGamer\',
    [string]$TaskName = 'DailyRun',
    [switch]$Unregister
)

# Registers the Windows scheduled task that starts the unattended daily round.
#
# The task runs in the interactive session of the current user (games and
# their tools need the desktop), with highest privileges (several tools are
# elevated), wakes the machine if it sleeps, and never starts a second copy.
# The action is the installed Invoke-YeYuGamerScheduledDaily.ps1, which only
# asks the Manager for one daily batch; the Manager stays the single control
# plane.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')
if (-not $InstallRoot) { $InstallRoot = Get-YeYuGamerDefaultInstallRoot }
$installResolved = Assert-YeYuGamerInstallRoot -Path $InstallRoot
$fullName = $TaskPath.TrimEnd('\') + '\' + $TaskName

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "Scheduled task $fullName is not registered."
        exit 0
    }
    if ($PSCmdlet.ShouldProcess($fullName, 'unregister YeYu Gamer daily schedule')) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
        Write-Host "Scheduled task $fullName was removed."
    }
    exit 0
}

$action = Join-Path $installResolved 'app\scripts\Invoke-YeYuGamerScheduledDaily.ps1'
if (-not (Test-Path -LiteralPath $action -PathType Leaf)) {
    throw "The installed scheduled-daily action is missing: $action. Reinstall the current package first."
}
$powershell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
    throw 'Windows PowerShell is unavailable for the scheduled action.'
}
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$taskAction = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $action + '"') `
    -WorkingDirectory (Split-Path -Parent $action)
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

if ($PSCmdlet.ShouldProcess($fullName, "register YeYu Gamer daily schedule at $Time")) {
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -Action $taskAction `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description ('YeYu Gamer: request one daily batch from the installed Manager at ' + $Time + ' (Asia/Shanghai local clock). The Manager runs the queue, evidence contracts and the round e-mail.') `
        -Force | Out-Null
    $registered = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    $info = Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $TaskName
    Write-Host ("Scheduled task {0} registered: state={1} nextRun={2} user={3}" -f $fullName, $registered.State, $info.NextRunTime, $userId)
}
