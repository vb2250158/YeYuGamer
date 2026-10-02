"""No-process replay of a batch starting between release idle check and stop."""
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name=='nt','PowerShell release lifecycle')
class ReleaseIdleRaceTests(unittest.TestCase):
    def replay(self,body):
        root=Path(__file__).resolve().parents[2]
        source=(root/'scripts/YeYuGamer.Common.ps1').read_text(encoding='utf-8-sig')
        start=source.index('function Invoke-YeYuGamerReleaseSafeStop {')
        end=source.index('function Get-YeYuGamerCurrentLocalAppData {',start)
        function=source[start:end]
        shell=Path(os.environ['WINDIR'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        command="$ErrorActionPreference='Stop'; "+function+body
        result=subprocess.run([str(shell),'-NoProfile','-NonInteractive','-Command',command],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
        self.assertIn(b'passed',result.stdout)

    def test_new_batch_after_idle_check_is_waited_without_force_stop(self):
        self.replay("""
$script:reads=0; $script:stops=0
$read={ $script:reads++; [pscustomobject]@{activeBatch=if($script:reads -eq 2){'new-batch'}else{$null};executionControl=[pscustomobject]@{activeControllerLeaseCount=0}} }
$stop={ $script:stops++; if($script:stops -eq 1){throw 'safe stop rejected'} }
Invoke-YeYuGamerReleaseSafeStop -ReadSnapshot $read -Stop $stop -WaitForIdleSeconds 5 -PollMilliseconds 1
if($script:reads -ne 3 -or $script:stops -ne 2){throw 'unexpected calls'}
Write-Output 'passed'
""")

    def test_idle_stop_failure_is_not_hidden(self):
        self.replay("""
$read={ [pscustomobject]@{activeBatch=$null;executionControl=[pscustomobject]@{activeControllerLeaseCount=0}} }
try {Invoke-YeYuGamerReleaseSafeStop -ReadSnapshot $read -Stop {throw 'broken stop'} -WaitForIdleSeconds 5 -PollMilliseconds 1; throw 'failure hidden'}
catch {if($_.Exception.Message -ne 'broken stop'){throw}}
Write-Output 'passed'
""")

    def test_active_controller_without_wait_never_calls_stop(self):
        self.replay("""
$script:stops=0
$read={ [pscustomobject]@{activeBatch=$null;executionControl=[pscustomobject]@{activeControllerLeaseCount=1}} }
try {Invoke-YeYuGamerReleaseSafeStop -ReadSnapshot $read -Stop {$script:stops++} -WaitForIdleSeconds 0; throw 'busy ignored'}
catch {if($_.Exception.Message -notlike '*idle wait expired*'){throw}}
if($script:stops -ne 0){throw 'stop called'}
Write-Output 'passed'
""")

    def test_unknown_snapshot_never_calls_stop(self):
        self.replay("""
$script:stops=0
try {Invoke-YeYuGamerReleaseSafeStop -ReadSnapshot {throw 'unknown state'} -Stop {$script:stops++} -WaitForIdleSeconds 0; throw 'unknown ignored'}
catch {if($_.Exception.Message -ne 'unknown state'){throw}}
if($script:stops -ne 0){throw 'stop called'}
Write-Output 'passed'
""")


if __name__=='__main__': unittest.main()
