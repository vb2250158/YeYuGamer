"""No-input Windows inventory regression for omitted CIM executable paths."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from yeyu_gamer_manager.services.hoyoplay_native_observation import _NATIVE_WORKER, _known_game_names


@unittest.skipUnless(os.name == 'nt', 'Windows process-query contract')
class InventoryIdentityTests(unittest.TestCase):
    def test_query_limited_fallback_preserves_inventory_rejections(self):
        prefix, marker, _ = _NATIVE_WORKER.partition('\ntry {\n    Install-Association\n')
        self.assertTrue(marker)
        names = json.dumps(_known_game_names())
        environment = os.environ.copy()
        environment.update(YEYU_HYP_SAMPLE_ID='bc81da8a-48d8-4544-870b-5f63174e43c7',
                           YEYU_HYP_GAME_NAMES=names,
                           YEYU_HYP_GAME_NAMES_COUNT=str(len(_known_game_names())),
                           YEYU_HYP_GAME_NAMES_SHA256=hashlib.sha256(names.encode()).hexdigest())
        body = r'''
$session=[Diagnostics.Process]::GetCurrentProcess().SessionId
$native=Resolve-InventoryImage $PID
if($native.Exited -or -not $native.ImagePath){throw 'current process query failed'}
$gone=Resolve-InventoryImage 2147483647
if(-not $gone.Exited){throw 'absent process incorrectly alive'}
function Get-CimInstance {return $script:inventory}
$script:inventory=@([pscustomobject]@{ProcessId=$PID;Name=[IO.Path]::GetFileName($native.ImagePath);ExecutablePath=$null;SessionId=$session})
$timer.Restart();$actual=@(No-Games 'admission')
if($actual.Count -ne 1 -or $actual[0].ExecutablePath -cne $native.ImagePath -or $inventoryChecks[0].resolvedImagePathCount -ne 1){throw 'CIM omitted path was not resolved'}
function Resolve-InventoryImage {if($script:identityError){throw 'query denied'};return $script:identity}
function Case([string]$name,$rows,$identity,[string]$expected='') {
    $timer.Restart();$script:inventory=$rows;$script:identity=$identity;$script:identityError=$false
    $script:inventoryChecks=[Collections.Generic.List[object]]::new();$caught=''
    try{$result=@(No-Games 'admission')}catch{$caught=$_.Exception.Message}
    if($caught -cne $expected){throw "case $name expected $expected actual $caught"}
    if(-not $expected -and $inventoryChecks.Count -ne 1){throw 'missing inventory metadata'}
}
$normal=[pscustomobject]@{ProcessId=123;Name='normal.exe';ExecutablePath=$null;SessionId=$session}
Case 'exited' @($normal) @{Exited=$true}
Case 'missing' @($normal) @{Exited=$false;ImagePath=$null} 'zzz_observation_process_identity_unreadable'
Case 'identity-drift' @($normal) @{Exited=$false;ImagePath='C:\Windows\other.exe'} 'zzz_observation_process_identity_unreadable'
Case 'resolved-game-root' @($normal) @{Exited=$false;ImagePath='D:\Game\Games\Unknown\normal.exe'} 'zzz_observation_unknown_game_process'
$known=ConvertFrom-Json $env:YEYU_HYP_GAME_NAMES
$game=[pscustomobject]@{ProcessId=124;Name=$known[0];ExecutablePath=$null;SessionId=$session}
Case 'known-game' @($game) @{} 'zzz_observation_game_already_running'
$rootGame=[pscustomobject]@{ProcessId=125;Name='other.exe';ExecutablePath='D:\Game\Games\Unknown\other.exe';SessionId=$session}
Case 'readable-game-root' @($rootGame) @{} 'zzz_observation_unknown_game_process'
$foreign=[pscustomobject]@{ProcessId=126;Name='normal.exe';ExecutablePath=$null;SessionId=($session+1)}
Case 'foreign-unknown' @($foreign) @{}
if($inventoryChecks[0].unreadableImagePathCount -ne 1 -or $inventoryChecks[0].unknownExecutableAbsence -cne 'unknown'){throw 'foreign unknown was reclassified'}
$script:inventory=@($normal);$script:identityError=$true;$timer.Restart();$rejected=$false
try{$null=No-Games 'admission'}catch{$rejected=$_.Exception.Message -ceq 'zzz_observation_process_identity_unreadable'}
if(-not $rejected){throw 'native read failure must remain blocked'}
Write-Output 'inventory-native-and-negative-vectors-passed'
'''
        with tempfile.TemporaryDirectory(prefix='hyp-inventory-') as temporary:
            script = Path(temporary) / 'inventory.ps1'
            script.write_text(prefix + '\n' + body, encoding='utf-8-sig')
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                env=environment, capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self.assertEqual(0, result.returncode, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
        self.assertIn(b'inventory-native-and-negative-vectors-passed', result.stdout)
