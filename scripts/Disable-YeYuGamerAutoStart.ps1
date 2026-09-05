[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
foreach ($name in @('YeYuGamer', 'YeYuGamerTray')) {
    if (Get-ItemProperty -LiteralPath $runKey -Name $name -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess("$runKey\$name", 'disable YeYu Gamer login startup')) {
            Remove-ItemProperty -LiteralPath $runKey -Name $name
        }
    }
}
$startup = [Environment]::GetFolderPath('Startup')
foreach ($name in @('YeYu Gamer.lnk', 'YeYu Gamer Tray.lnk')) {
    $shortcut = Join-Path $startup $name
    if (Test-Path -LiteralPath $shortcut -PathType Leaf) {
        if ($PSCmdlet.ShouldProcess($shortcut, 'disable YeYu Gamer login startup')) {
            Remove-Item -LiteralPath $shortcut -Force
        }
    }
}
Write-Host 'YeYu Gamer login startup is disabled.'

