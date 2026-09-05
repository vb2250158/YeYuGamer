from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class StopSourceClientTests(unittest.TestCase):
    def run_probe(self, *, source_client: bool, missing: bool = False, reparse: bool = False) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="yeyu stop source ") as directory:
            root = Path(directory)
            scripts = root / "scripts"
            package = root / "platform" / "yeyu_gamer_platform"
            scripts.mkdir()
            package.mkdir(parents=True)
            for filename in ("__init__.py", "api_client.py", "config.py"):
                (package / filename).write_text("", encoding="utf-8")
            if not missing:
                (package / "cli.py").write_text(
                    "import json,sys\n"
                    "print(json.dumps({'source': __file__, 'args': sys.argv[1:], 'isolated': sys.flags.isolated, 'noBytecode': sys.dont_write_bytecode}))\n",
                    encoding="utf-8",
                )
            if reparse:
                linked = package / "api_client.py"
                linked.unlink()
                try:
                    linked.symlink_to(package / "config.py")
                except OSError as error:
                    self.skipTest(f"Creating a local symlink is unavailable: {error}")
            harness = scripts / "probe.ps1"
            harness.write_text(r"""
param($SourceScripts, $Python, [switch]$UseSourceClient)
$ErrorActionPreference = 'Stop'
# Extract only function definitions. Never execute Stop or Publisher's body.
$definitions = [System.Collections.Generic.List[string]]::new()
foreach ($entry in @(
    @{File='YeYuGamer.Common.ps1'; Names=@('Assert-YeYuGamerLocalTarget','Assert-YeYuGamerNoReparseAncestors')},
    @{File='Stop-YeYuGamer.ps1'; Names=@('Get-YeYuGamerStopCliArguments')}
)) {
    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $SourceScripts $entry.File), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    foreach ($name in $entry.Names) {
        $function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
        if ($null -eq $function) { throw "Missing helper: $name" }
        $definitions.Add($function.Extent.Text)
    }
}
$helperPath = Join-Path $PSScriptRoot 'helpers.ps1'
[IO.File]::WriteAllText($helperPath, ($definitions -join [Environment]::NewLine))
. $helperPath
$arguments = @(Get-YeYuGamerStopCliArguments -UseSourceClient:$UseSourceClient)
if ($UseSourceClient) {
    # The selected fixture CLI only prints its identity. An accidental fallback
    # to the installed CLI sees an invalid command and cannot issue a mutation.
    & $Python @arguments source-client-probe --fixture-only
    exit $LASTEXITCODE
}
ConvertTo-Json -InputObject $arguments -Compress
""", encoding="utf-8")
            args = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(ROOT / "scripts"), sys.executable]
            if source_client:
                args.append("-UseSourceClient")
            return subprocess.run(args, cwd=root, capture_output=True, text=True, errors="replace", timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_normal_stop_keeps_installed_isolated_module_entry(self) -> None:
        result = self.run_probe(source_client=False, missing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["-I", "-B", "-m", "yeyu_gamer_platform.cli"])

    def test_release_bootstrap_selects_snapshot_client_and_preserves_isolation(self) -> None:
        result = self.run_probe(source_client=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("yeyu stop source ", payload["source"])
        self.assertEqual(payload["args"], ["source-client-probe", "--fixture-only"])
        self.assertEqual(payload["isolated"], 1)
        self.assertTrue(payload["noBytecode"])

    def test_incomplete_snapshot_cannot_fall_back_to_installed_client(self) -> None:
        result = self.run_probe(source_client=True, missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source client is incomplete", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_reparse_source_is_rejected_before_python_runs(self) -> None:
        result = self.run_probe(source_client=True, reparse=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reparse-point ancestor", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
