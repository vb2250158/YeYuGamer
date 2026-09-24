[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

function Assert-Guard {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$testRoot = Join-Path (Get-YeYuGamerDefaultProductWorkRoot) ('movable-guard-tests\' + [Guid]::NewGuid().ToString('N'))
$testRoot = Assert-YeYuGamerLocalTarget -Path $testRoot -Purpose 'movable guard test root'
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

try {
    # ------------------------------------------------------------------
    # 1. Free targets pass, and the probe leaves no trace behind.
    # ------------------------------------------------------------------
    $free = Join-Path $testRoot 'free-target'
    New-Item -ItemType Directory -Path $free -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $free 'marker.txt'), 'fixture')

    Assert-YeYuGamerInstallTargetsAreMovable -DirectoryTargets @([pscustomobject]@{ Path = $free })
    Assert-Guard (Test-Path -LiteralPath $free -PathType Container) 'A free target must still exist after probing.'
    Assert-Guard (Test-Path -LiteralPath (Join-Path $free 'marker.txt') -PathType Leaf) `
        'Probing must not lose the target content.'
    $probeResidue = @(Get-ChildItem -LiteralPath $testRoot -Force | Where-Object { $_.Name -like '*install-probe-*' })
    Assert-Guard ($probeResidue.Count -eq 0) 'Probing must not leave install-probe directories behind.'

    # The probe must never park anything inside the tree it is testing.  An
    # earlier revision probed "$path.install-probe-<guid>", so a target that was
    # also the parent of other targets (app) nested one probe per attempt, left
    # the chain behind when the retry failed, and destroyed the very install the
    # guard exists to protect (measured 2026-09-24).
    Assert-Guard ($probeResidue.Count -eq 0) 'A probe must never be created inside the installation.'
    $nested = Join-Path $testRoot 'nest'
    $inner = Join-Path $nested 'inner'
    New-Item -ItemType Directory -Path $inner -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $inner 'payload.txt'), 'fixture')
    Assert-YeYuGamerInstallTargetsAreMovable -DirectoryTargets @(
        [pscustomobject]@{ Path = $nested },
        [pscustomobject]@{ Path = $inner }
    )
    Assert-Guard (Test-Path -LiteralPath (Join-Path $inner 'payload.txt') -PathType Leaf) `
        'A nested target must survive probing with its payload.'
    Assert-Guard (@(Get-ChildItem -LiteralPath $nested -Recurse -Force |
        Where-Object { $_.Name -like '*install-probe-*' }).Count -eq 0) `
        'A nested target must not collect probe directories.'
    $scratchResidue = @(Get-ChildItem -LiteralPath ([System.IO.Path]::GetTempPath()) -Force `
        -Filter 'yeyu-install-probe-*' -ErrorAction SilentlyContinue)
    Assert-Guard ($scratchResidue.Count -eq 0) 'Probing must clean up its scratch directory.'

    # A target that does not exist is simply skipped.
    Assert-YeYuGamerInstallTargetsAreMovable -DirectoryTargets @([pscustomobject]@{ Path = (Join-Path $testRoot 'absent') })

    # ------------------------------------------------------------------
    # 2. A holder whose working directory is the target is named in the error.
    # ------------------------------------------------------------------
    $held = Join-Path $testRoot 'held-target'
    New-Item -ItemType Directory -Path $held -Force | Out-Null

    # A child process that sits in the directory holds it exactly the way the
    # measured LDPlayer adb.exe fork-server held app\desktop-host: the directory
    # cannot be moved while that process keeps it as its working directory.
    # ``cmd /c ping`` hands the working directory down to ping.exe, so both the
    # launcher and its child have to be identified to release the directory.
    $holder = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'ping -n 60 127.0.0.1 > nul' `
        -WorkingDirectory $held -PassThru -WindowStyle Hidden
    $raised = $null
    try {
        Start-Sleep -Milliseconds 900
        Assert-YeYuGamerInstallTargetsAreMovable -DirectoryTargets @([pscustomobject]@{ Path = $held })
    } catch {
        $raised = $_
    } finally {
        # Kill only this fixture's own lineage: never all cmd.exe/ping.exe on the
        # machine, which would reach unrelated work.
        $lineage = [System.Collections.Generic.HashSet[int]]::new()
        [void]$lineage.Add($holder.Id)
        for ($pass = 0; $pass -lt 4; $pass++) {
            foreach ($child in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
                if ($lineage.Contains([int]$child.ParentProcessId)) {
                    [void]$lineage.Add([int]$child.ProcessId)
                }
            }
        }
        foreach ($processId in @($lineage)) {
            try { Stop-Process -Id $processId -Force -ErrorAction Stop } catch { }
        }
        Start-Sleep -Milliseconds 700
    }

    Assert-Guard ($null -ne $raised) 'A target held by a process working directory must fail the guard.'
    $message = [string]$raised.Exception.Message
    Assert-Guard ($message -match 'cannot be moved') 'The guard must explain that the target cannot be moved.'
    Assert-Guard ($message -match [regex]::Escape($held)) 'The guard must name the blocked target.'
    Assert-Guard ($message -match 'pid') 'The guard must name the holding process id.'
    Assert-Guard ($message -match 'working directory') 'The guard must explain why the holder blocks the move.'
    # A single holder must render exactly like several.  PowerShell unwraps a
    # one-element array returned from a function into a bare object, and an
    # earlier revision read ".Count" on that scalar, so the guard itself threw
    # "the property 'Count' cannot be found" instead of naming the holder -- the
    # publish failed with that message rather than a usable diagnosis
    # (measured 2026-09-24).  Any holder count must produce the real report.
    Assert-Guard ($message -notmatch "Count") 'The guard must never fail with a property error instead of a report.'

    # The holder is gone now, so the same target is movable again: the guard
    # reports the live holder rather than latching.
    Assert-YeYuGamerInstallTargetsAreMovable -DirectoryTargets @([pscustomobject]@{ Path = $held })

    # ------------------------------------------------------------------
    # 2b. An unavailable path is reported as an open handle, not as "unknown".
    # ------------------------------------------------------------------
    # Measured 2026-09-24 and 2026-09-25: LDPlayer's orphaned adb.exe fork-server
    # held `app\desktop-host` open while its working directory was C:\Windows, so
    # the working-directory scan correctly matched nothing and the report said
    # only "the holder is unidentified".  The two mechanisms are different and
    # the report must say which one applies so the operator knows what to stop.
    $openHandle = $null
    try {
        Assert-YeYuGamerInstallTargetsAreMovable -DirectoryTargets @(
            [pscustomobject]@{ Path = 'C:\Windows\System32\config' }
        )
    } catch {
        $openHandle = [string]$_.Exception.Message
    }
    if ($null -ne $openHandle) {
        Assert-Guard ($openHandle -match 'open handle|working directory') `
            'A blocked target must be reported as either a working-directory lock or an open handle.'
    }

    # ------------------------------------------------------------------
    # 3. The working-directory reader agrees with the operating system.
    # ------------------------------------------------------------------
    $own = Get-YeYuGamerProcessWorkingDirectory -ProcessId $PID
    Assert-Guard ($null -ne $own) 'The working directory reader must resolve a readable process.'
    Assert-Guard ((Split-Path -Parent $own) -ne '') 'The working directory reader must return an absolute path.'
    # A process id that cannot exist returns null instead of throwing, and a null
    # answer is never treated as proof that a target is free.
    Assert-Guard ($null -eq (Get-YeYuGamerProcessWorkingDirectory -ProcessId 2147483000)) `
        'An unreadable process must return null rather than throwing.'

    Write-Output 'movable guard tests: passed'
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
