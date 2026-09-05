[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [ValidateRange(0, 1000)][int]$Keep = 3
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

$runtime = Assert-YeYuGamerLocalTarget -Path $RuntimeRoot -Purpose 'RuntimeRoot'
if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
    throw "RuntimeRoot is missing: $runtime"
}
Assert-YeYuGamerNoReparseAncestors -Path $runtime -Purpose 'RuntimeRoot' | Out-Null
if (((Get-Item -LiteralPath $runtime -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "RuntimeRoot must not be a reparse point: $runtime"
}

$adapters = Assert-YeYuGamerChildPath -Parent $runtime -Child (Join-Path $runtime 'adapters') -Purpose 'Adapter runtime root'
if (-not (Test-Path -LiteralPath $adapters -PathType Container)) {
    throw "Adapter runtime root is missing: $adapters"
}

$results = @()
foreach ($packageName in @('legacy-night-rain-gamer', 'manager-adapter-host')) {
    $results += Remove-YeYuGamerAdapterBackups -AdapterRoot $adapters -PackageName $packageName -Keep $Keep -WhatIf:$WhatIfPreference
}

$modules = Assert-YeYuGamerChildPath -Parent $adapters -Child (Join-Path $adapters 'game-modules') -Purpose 'game module root'
if (Test-Path -LiteralPath $modules -PathType Container) {
    $modulePackages = @(
        Get-ChildItem -LiteralPath $modules -Directory -Force |
            Where-Object { $_.Name -match '^(.+)\.previous-\d{14}$' } |
            ForEach-Object { [regex]::Match($_.Name, '^(.+)\.previous-\d{14}$').Groups[1].Value } |
            Sort-Object -Unique
    )
    foreach ($packageName in $modulePackages) {
        $results += Remove-YeYuGamerAdapterBackups -AdapterRoot $modules -PackageName $packageName -Keep $Keep -WhatIf:$WhatIfPreference
    }
}

[pscustomobject]@{
    status = 'adapter-backup-cleanup-complete'
    runtimeRoot = $runtime
    keep = $Keep
    packages = $results
} | ConvertTo-Json -Depth 5
