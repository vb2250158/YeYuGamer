"""Run the production PS5 helper transport/Job with an inert process inventory."""

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from yeyu_gamer_manager.services import hoyoplay_native_observation as native


_REGISTRY_FIXTURE = r'''
function Get-CimInstance { param($ClassName, $Property) return $script:fixtureProcesses }
function Resolve-InventoryImage { param($number) throw 'fixture-unreadable' }
$script:fixtureProcesses = @()
$checks = [Collections.Generic.List[string]]::new()
$originalJson = $env:YEYU_HYP_GAME_NAMES
$originalCount = $env:YEYU_HYP_GAME_NAMES_COUNT
$originalHash = $env:YEYU_HYP_GAME_NAMES_SHA256
$names = ConvertFrom-Json $originalJson
$session = [Diagnostics.Process]::GetCurrentProcess().SessionId
function Fixture-Process($name, $path, $sessionId) {
    return [pscustomobject]@{ProcessId=12345;Name=$name;ExecutablePath=$path;SessionId=$sessionId}
}
function Expect-Rejected($label, $reason) {
    $caught = $null
    try { $null = No-Games 'fixture-rejected' } catch { $caught = $_.Exception.Message }
    if ($caught -cne $reason) { throw "fixture expected $label => $reason; got $caught" }
    $checks.Add($label)
}
try {
    $script:fixtureProcesses = @(Fixture-Process 'ordinary.exe' 'C:\fixture\ordinary.exe' $session)
    $null = No-Games 'fixture-clear'
    if ($inventoryChecks.Count -ne 1 -or $inventoryChecks[0].rulesSha256 -cne $originalHash) {
        throw 'fixture registry digest not retained'
    }
    $checks.Add('complete-registry-accepted')
    foreach ($name in $names) {
        $script:fixtureProcesses = @(Fixture-Process $name.ToUpperInvariant() 'C:\fixture\game.exe' $session)
        Expect-Rejected ('known:' + $name) 'zzz_observation_game_already_running'
    }
    $script:fixtureProcesses = @(Fixture-Process 'unknown.exe' 'D:\Game\Games\fixture\unknown.exe' $session)
    Expect-Rejected 'unknown-game-root' 'zzz_observation_unknown_game_process'
    $script:fixtureProcesses = @(Fixture-Process 'ordinary.exe' $null $session)
    Expect-Rejected 'same-session-unreadable' 'zzz_observation_process_identity_unreadable'
    $script:fixtureProcesses = @()
    Expect-Rejected 'empty-inventory' 'zzz_observation_process_limit'
    $script:fixtureProcesses = @(Fixture-Process 'ordinary.exe' 'C:\fixture\ordinary.exe' $session)
    foreach ($count in @('', '0', '1', 'not-an-integer')) {
        $env:YEYU_HYP_GAME_NAMES_COUNT = $count
        Expect-Rejected ('bad-count:' + $count) 'zzz_observation_game_registry_missing'
    }
    $env:YEYU_HYP_GAME_NAMES_COUNT = $originalCount
    $env:YEYU_HYP_GAME_NAMES_SHA256 = '0' * 64
    Expect-Rejected 'bad-digest' 'zzz_observation_game_registry_missing'
    $env:YEYU_HYP_GAME_NAMES_SHA256 = $originalHash
    $env:YEYU_HYP_GAME_NAMES = ConvertTo-Json -InputObject @($names[0..($names.Count - 2)]) -Compress
    Expect-Rejected 'truncated-transport' 'zzz_observation_game_registry_missing'
    foreach ($invalid in @('nested','duplicate','null-item','object-item','uppercase-item','path-item','empty','scalar')) {
        $altered = @($names)
        switch ($invalid) {
            'nested' { $encoded = '[' + $originalJson + ']' }
            'duplicate' { $altered[0] = $altered[1] }
            'null-item' { $altered[0] = $null }
            'object-item' { $altered[0] = @{name='game.exe'} }
            'uppercase-item' { $altered[0] = $altered[0].ToUpperInvariant() }
            'path-item' { $altered[0] = 'C:\game.exe' }
            'empty' { $altered = @() }
            'scalar' { $encoded = '"game.exe"' }
        }
        if ($invalid -notin @('nested','scalar')) { $encoded = ConvertTo-Json -InputObject $altered -Compress }
        $env:YEYU_HYP_GAME_NAMES = $encoded
        $env:YEYU_HYP_GAME_NAMES_SHA256 = Hash-Bytes ([Text.Encoding]::UTF8.GetBytes($encoded))
        $env:YEYU_HYP_GAME_NAMES_COUNT = if ($invalid -in @('nested','scalar')) { '1' } else { [string]$altered.Count }
        Expect-Rejected $invalid 'zzz_observation_game_registry_missing'
    }
    @{ok=$true;psVersion=$PSVersionTable.PSVersion.ToString();knownCount=$names.Count;
        rulesSha256=$originalHash;names=@($names);cases=@($checks)} | ConvertTo-Json -Depth 6 -Compress
    exit 0
} catch {
    @{ok=$false;reasonCode='zzz_observation_registry_fixture_failed';detail=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 2
}
'''


class HoYoPlayNativeRegistryTests(unittest.TestCase):
    def test_registry_covers_every_manager_rule_and_emulator(self):
        expected = set(native.window_capture._LDPLAYER_CAPTURE_PROCESS_NAMES)
        for rule in native.window_capture._RULES.values():
            expected.update(rule.process_names)
        self.assertEqual(native._known_game_names(), sorted(name.casefold() for name in expected))

    @unittest.skipUnless(os.name == 'nt', 'production native helper requires Windows PowerShell 5')
    def test_ps5_transport_checks_all_games_and_rejects_incomplete_registry(self):
        # Preserve production parsing, No-Games, env transport, process birth check,
        # suspended launch and Job cleanup. Only the external inventory is inert.
        boundary = '\ntry {\n    Install-Association'
        self.assertEqual(native._NATIVE_WORKER.count(boundary), 1)
        worker = native._NATIVE_WORKER.split(boundary)[0] + _REGISTRY_FIXTURE
        records = []
        sampler = native.NativeHoYoPlaySampler(
            native.FIXED_GAME_EXECUTABLE, cancelled=lambda: False,
            register_helper=records.append, task_id=str(uuid.uuid4()),
            manager_id='offline-ps5-registry-test', launch_guard=nullcontext,
        )
        sampler._deadline = time.monotonic() + native.MAX_SECONDS
        with tempfile.TemporaryDirectory(prefix='hno-registry-') as directory:
            with patch.object(native, '_NATIVE_WORKER', worker):
                result = sampler._run_worker('admit', Path(directory))
            self.assertTrue(result['psVersion'].startswith('5.1.'))
            expected = native._known_game_names()
            self.assertEqual(result['knownCount'], len(expected))
            self.assertEqual(result['names'], expected)
            self.assertEqual(result['rulesSha256'], hashlib.sha256(json.dumps(expected).encode()).hexdigest())
            self.assertEqual(len(result['cases']), len(expected) + 18)
            self.assertTrue(sampler._helper_closed)
            self.assertFalse(sampler._remaining_helpers)
            self.assertEqual(sampler._helpers[0]['exitCode'], 0)
            self.assertTrue(sampler._helpers[0]['createdAt'])
            print(json.dumps({'nativeRegistry': result, 'helpers': sampler._helpers,
                              'frozen': bool(getattr(sys, 'frozen', False))}, ensure_ascii=True))


if __name__ == '__main__':
    unittest.main()
