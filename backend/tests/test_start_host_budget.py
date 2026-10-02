"""Replay the real startup script with inert launch and health functions."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows desktop lifecycle')
class StartHostBudgetTests(unittest.TestCase):
    def replay(self, initially_healthy):
        root=Path(__file__).resolve().parents[2]
        start=root/'scripts/Start-YeYuGamer.ps1'
        command=r'''
$ErrorActionPreference='Stop'
$global:taskLaunches=0;$global:taskHealthCalls=0;$global:taskArguments=@()
function Start-Process {param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle) $global:taskLaunches++;$global:taskArguments=$ArgumentList}
function Invoke-RestMethod {param($Uri,$TimeoutSec,$ErrorAction) $global:taskHealthCalls++;if(-not INITIAL -and $global:taskHealthCalls -eq 1){throw 'fixture host down'};[pscustomobject]@{status='ok'}}
& 'START' -NoOpenWebGui -TimeoutSeconds 240
if(INITIAL) {if($global:taskLaunches -ne 0){throw 'healthy service was launched again'}}
else {if($global:taskLaunches -ne 1){throw 'unexpected launch count'};$index=[Array]::IndexOf($global:taskArguments,'--startup-timeout-seconds');if($index -lt 0 -or $global:taskArguments[$index+1] -ne '240'){throw 'host budget not forwarded'};if('--no-browser' -notin $global:taskArguments){throw 'silent startup lost'}}
Write-Output 'passed'
'''.replace('INITIAL','$true' if initially_healthy else '$false').replace('START',str(start).replace("'","''"))
        shell=shutil.which('pwsh')
        self.assertIsNotNone(shell)
        result=subprocess.run([shell,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',command],
                              capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(0,result.returncode,result.stderr.decode('utf-8',errors='replace'))
        self.assertIn(b'passed',result.stdout)

    def test_new_host_receives_timeout_and_no_browser(self):
        self.replay(False)

    def test_healthy_host_is_reused_without_second_launch(self):
        self.replay(True)


if __name__=='__main__':
    unittest.main()
