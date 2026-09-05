from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "Invoke-YeYuGamerScheduledDaily.ps1"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class ScheduledDailyTests(unittest.TestCase):
    def run_scenario(
        self,
        *,
        health: str = "healthy",
        snapshot: str = '{"activeBatch":null}',
        result: str = '{"raw":{"result":{"batchId":"batch-fixture"}}}',
        cli_exit: int = 0,
        dry_run: bool = False,
        retired_switch: str = "",
        retired_value: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], str, list[str], list[str]]:
        # Run the unchanged action in an isolated installation with a fake CLI,
        # health endpoint, desktop starter and log sink. Never contact a Manager.
        with tempfile.TemporaryDirectory(prefix="yeyu-scheduled-test-") as directory:
            root = Path(directory)
            scripts = root / "scripts"
            install = root / "install"
            runtime = root / "runtime"
            for path in (scripts, install, runtime / "config"):
                path.mkdir(parents=True)
            shutil.copyfile(SOURCE, scripts / SOURCE.name)
            (runtime / "config" / "platform.json").write_text("{}", encoding="utf-8")
            (root / "scenario.json").write_text(json.dumps({
                "health": health,
                "dryRun": dry_run,
                "retiredSwitch": retired_switch,
                "retiredValue": retired_value,
            }), encoding="utf-8")
            (install / "snapshot.json").write_text(snapshot, encoding="ascii")
            (install / "result.json").write_text(result, encoding="ascii")
            (install / "YeYuGamer.cmd").write_text(
                '@echo off\n'
                '>>"%~dp0calls.txt" echo %*\n'
                'if "%~2"=="snapshot" (\n'
                '  type "%~dp0snapshot.json"\n'
                '  exit /b 0\n'
                ')\n'
                'if "%~2"=="start-daily" (\n'
                '  type "%~dp0result.json"\n'
                f'  exit /b {cli_exit}\n'
                ')\n'
                'exit /b 91\n', encoding="ascii",
            )
            (scripts / "YeYuGamer.Common.ps1").write_text(
                "[IO.File]::AppendAllText($global:TracePath, 'common' + [Environment]::NewLine)\n"
                "function Assert-YeYuGamerInstallRoot { param($Path) return $Path }\n"
                "function Assert-YeYuGamerRuntimeRoot { param($Path) return $Path }\n",
                encoding="utf-8",
            )
            (scripts / "Start-YeYuGamer.ps1").write_text(
                "param($InstallRoot, $RuntimeRoot, [switch]$NoOpenWebGui, $TimeoutSeconds)\n"
                "if (-not $NoOpenWebGui -or $TimeoutSeconds -ne 180) { throw 'Invalid host startup contract' }\n"
                "[IO.File]::AppendAllText($global:TracePath, 'start-host' + [Environment]::NewLine)\n",
                encoding="utf-8",
            )
            harness = root / "harness.ps1"
            harness.write_text(r"""
$ErrorActionPreference = 'Stop'
$global:TracePath = Join-Path $PSScriptRoot 'trace.txt'
$global:FixtureLog = Join-Path $PSScriptRoot 'daily.log'
$global:Scenario = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'scenario.json') -Raw | ConvertFrom-Json
$global:HealthCalls = 0
$action = Join-Path $PSScriptRoot 'scripts\Invoke-YeYuGamerScheduledDaily.ps1'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($action, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$forbidden = @('Get-Process','Stop-Process','Get-CimInstance','Invoke-CimMethod','Restart-Computer','Stop-Computer','taskkill','shutdown')
foreach ($command in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true)) {
    if ($command.GetCommandName() -in $forbidden -or $command.Extent.Text -match '(?i)shutdown\.exe|taskkill\.exe') {
        throw 'The scheduled action must not inspect or control game processes, memory or Windows.'
    }
}
function Invoke-RestMethod {
    param($Uri, $TimeoutSec, $ErrorAction)
    if ($Uri -ne 'http://127.0.0.1:8877/api/v1/health' -or $TimeoutSec -ne 3) { throw 'Unexpected endpoint' }
    $global:HealthCalls++
    [IO.File]::AppendAllText($global:TracePath, 'health' + [Environment]::NewLine)
    if ($global:Scenario.health -eq 'unhealthy' -or ($global:Scenario.health -eq 'start-needed' -and $global:HealthCalls -eq 1)) {
        throw 'Fixture Manager is offline'
    }
    return [pscustomobject]@{status='ok';manager='fixture';storage='fixture'}
}
function New-Item {
    param($ItemType, $Path, [switch]$Force)
    if ($ItemType -ne 'Directory' -or $Path -notlike '*YeYuGamer\logs\scheduled-daily') { throw 'Unexpected directory write' }
}
function Add-Content {
    param($LiteralPath, $Value, $Encoding)
    if ($LiteralPath -notlike '*YeYuGamer\logs\scheduled-daily\*.log') { throw 'Unexpected log write' }
    [IO.File]::AppendAllText($global:FixtureLog, $Value + [Environment]::NewLine)
}
function Get-Process { throw 'Scheduler must not enumerate processes' }
function Stop-Process { throw 'Scheduler must not close processes' }
function Get-CimInstance { throw 'Scheduler must not gate on memory before Manager cleanup' }
function Start-Process { throw 'Scheduler must use the installed host starter' }
function Start-Sleep { throw 'Scheduler must not run a residual-process polling loop' }
$parameters = @{
    InstallRoot = (Join-Path $PSScriptRoot 'install')
    RuntimeRoot = (Join-Path $PSScriptRoot 'runtime')
}
if ($global:Scenario.dryRun) { $parameters.DryRun = $true }
if ($global:Scenario.retiredSwitch) { $parameters[$global:Scenario.retiredSwitch] = $global:Scenario.retiredValue }
& $action @parameters
exit $LASTEXITCODE
""", encoding="utf-8")
            completed = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                cwd=root, capture_output=True, text=True, errors="replace", timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            def read_optional(path: Path) -> str:
                return path.read_text(encoding="utf-8") if path.exists() else ""

            return (
                completed,
                read_optional(root / "daily.log"),
                read_optional(install / "calls.txt").splitlines(),
                read_optional(root / "trace.txt").splitlines(),
            )

    def test_healthy_host_requests_one_idempotent_batch_without_local_cleanup(self) -> None:
        completed, log, calls, trace = self.run_scenario()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls[0], "--json snapshot")
        self.assertEqual(len(calls), 2)
        self.assertRegex(calls[1], r"^--json start-daily --idempotency-key scheduled-daily-\d{8}-\d{4}$")
        self.assertEqual(trace, ["common", "health"])
        self.assertIn("scheduled daily run requested: batchId=batch-fixture", log)

    def test_unhealthy_host_is_started_before_requesting_a_batch(self) -> None:
        completed, _log, calls, trace = self.run_scenario(health="start-needed")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(trace, ["common", "health", "start-host", "health"])
        self.assertEqual(len(calls), 2)

    def test_host_start_failure_preserves_exit_two_and_no_batch(self) -> None:
        completed, log, calls, trace = self.run_scenario(health="unhealthy")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(trace.count("start-host"), 1)
        self.assertIn("daily batch was not requested", log)

    def test_active_batch_does_not_create_a_duplicate(self) -> None:
        completed, log, calls, _trace = self.run_scenario(snapshot='{"activeBatch":{"batchId":"active-fixture"}}')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, ["--json snapshot"])
        self.assertIn("an active batch already exists (active-fixture)", log)

    def test_invalid_snapshot_preserves_exit_three(self) -> None:
        completed, log, calls, _trace = self.run_scenario(snapshot="invalid-json")
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(calls, ["--json snapshot"])
        self.assertIn("snapshot could not be parsed", log)

    def test_dry_run_reads_snapshot_without_requesting_a_batch(self) -> None:
        completed, log, calls, _trace = self.run_scenario(dry_run=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, ["--json snapshot"])
        self.assertIn("dry run: Manager healthy and no active batch", log)

    def test_manager_cli_failure_is_logged_and_propagated(self) -> None:
        completed, log, calls, _trace = self.run_scenario(cli_exit=7, result='{"error":"fixture-rejected"}')
        self.assertEqual(completed.returncode, 7, completed.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("start-daily exit=7", log)
        self.assertNotIn("scheduled daily run requested", log)

    def test_nested_batch_receipt_is_logged(self) -> None:
        completed, log, _calls, _trace = self.run_scenario(result='{"result":{"batch":{"batchId":"nested-fixture"}}}')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("scheduled daily run requested: batchId=nested-fixture", log)

    def test_retired_switches_fail_explicitly_before_any_side_effect(self) -> None:
        for switch in ("SkipOnStuckZombies", "RebootOnStuckZombies"):
            for value in (True, False):
                with self.subTest(switch=switch, value=value):
                    completed, log, calls, trace = self.run_scenario(retired_switch=switch, retired_value=value)
                    self.assertEqual(completed.returncode, 1)
                    self.assertIn("are no longer supported", completed.stderr)
                    self.assertEqual((log, calls, trace), ("", [], []))


if __name__ == "__main__":
    unittest.main()
