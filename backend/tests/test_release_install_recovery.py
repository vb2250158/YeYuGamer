"""Exercise failure recovery without installing or starting a product."""
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == 'nt', 'PowerShell release lifecycle')
class ReleaseInstallRecoveryTests(unittest.TestCase):
    def replay(self, body):
        root = Path(__file__).resolve().parents[2]
        source = (root/'scripts/YeYuGamer.Common.ps1').read_text(encoding='utf-8-sig')
        start = source.index('function Invoke-YeYuGamerStoppedReleaseInstallation {')
        end = source.index('function Get-YeYuGamerCurrentLocalAppData {', start)
        publisher = (root/'scripts/Publish-YeYuGamerLocalRelease.ps1').read_text(encoding='utf-8-sig')
        gate_start = publisher.index('function Invoke-YeYuGamerCandidateInstallation {')
        gate_end = publisher.index('function Get-CandidateVersion {', gate_start)
        shell = Path(os.environ['WINDIR'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command',
                                 "$ErrorActionPreference='Stop'; "+source[start:end]+publisher[gate_start:gate_end]+body],
                                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'passed', result.stdout)

    def test_candidate_failure_recovers_once_and_preserves_failure(self):
        self.replay("""
$script:recovered=0
try {Invoke-YeYuGamerStoppedReleaseInstallation -Install {throw 'candidate hash changed'} -Recover {$script:recovered++}; throw 'failure hidden'}
catch {if($_.Exception.Message -ne 'candidate hash changed'){throw}}
if($script:recovered -ne 1){throw 'recovery call count incorrect'}
Write-Output 'passed'
""")

    def test_success_does_not_restart_early(self):
        self.replay("""
$script:installed=0;$script:recovered=0
Invoke-YeYuGamerStoppedReleaseInstallation -Install {$script:installed++} -Recover {$script:recovered++}
if($script:installed -ne 1 -or $script:recovered -ne 0){throw 'successful release changed'}
Write-Output 'passed'
""")

    def test_recovery_failure_does_not_replace_installation_failure(self):
        self.replay("""
try {Invoke-YeYuGamerStoppedReleaseInstallation -Install {throw 'install failed'} -Recover {throw 'host unavailable'}; throw 'failure hidden'}
catch {if($_.Exception.Message -ne 'install failed'){throw}}
Write-Output 'passed'
""")

    def test_recovery_runs_after_candidate_environment_gate_is_restored(self):
        self.replay("""
$env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED='true'
try {Invoke-YeYuGamerStoppedReleaseInstallation -Install {Invoke-YeYuGamerCandidateInstallation -Install {if($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -ne 'false'){throw 'candidate gate not disabled'};throw 'candidate failed'}} -Recover {if($env:YEYU_GAMER_LEGACY_EXECUTION_ENABLED -ne 'true'){throw 'execution gate leaked'}};throw 'failure hidden'}
catch {if($_.Exception.Message -ne 'candidate failed'){throw}}
Write-Output 'passed'
""")


if __name__ == '__main__':
    unittest.main()
