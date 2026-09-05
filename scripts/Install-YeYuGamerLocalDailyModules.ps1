[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'YeYuGamer\runtime'),
    [switch]$DailyBatchValidationPending
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This legacy entry used to clone one installed package into per-game modules
# and then rewrite candidate manifests as promoted/execution-ready. Promotion
# is now a Manager-owned, evidence-bound transition and cannot be performed by
# a filesystem installer. Keep the command as a fail-closed compatibility
# tombstone so old automation cannot silently recreate forged manifests.
throw @'
Install-YeYuGamerLocalDailyModules.ps1 is retired and cannot install or promote adapters.
Use Publish-YeYuGamerLocalRelease.ps1: it builds and tests local candidates, installs them as
candidate/executionReady=false, then asks the Manager typed promotion API to independently
validate candidate evidence, run its no-process canary, and issue the promotion receipt.
'@
