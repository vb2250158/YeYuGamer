"""Exercise the production PowerShell binding and verifier without installation."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ClassicConfigurationBindingTests(unittest.TestCase):
    def run_case(self, change):
        cache = ROOT / '.cache' / 'verification'
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='classic-config-', dir=cache) as temporary:
            fixture = Path(temporary)
            paths = {'pgrTool': fixture/'pgr', 'zzzTool': fixture/'zzz', 'nikkeTool': fixture/'nikke'}
            for path in paths.values():
                path.mkdir()
            names = {
                'pgr': ('pgrEntry', 'pgrConfig', 'pgrPythonPath', 'pgrSerumResource', 'pgrGame'),
                'zzz': ('zzzLauncher', 'zzzGroup', 'zzzCoffee', 'zzzPythonPath', 'zzzGame'),
                'nikke': ('nikkeConfig', 'nikkeGui', 'nikkePythonwPath', 'nikkeReturn',
                          'nikkeOutpost', 'nikkeDispatch', 'nikkePythonPath', 'nikkeGame'),
            }
            for folder, variables in names.items():
                for name in variables:
                    path = fixture/folder/name
                    path.write_text('original', encoding='utf-8')
                    paths[name] = path
            candidate = fixture/'candidate'
            candidate.mkdir()
            settings = {'fixture': str(fixture), 'candidate': str(candidate),
                        'paths': {name: str(path) for name, path in paths.items()}, 'change': change,
                        'builder': str(ROOT/'scripts/Build-YeYuGamerClassicAdapter.ps1'),
                        'installer': str(ROOT/'scripts/Install-YeYuGamerClassicAdapter.ps1')}
            fixture_json = fixture/'fixture.json'
            fixture_json.write_text(json.dumps(settings), encoding='utf-8')
            script = r'''
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$data=Get-Content -LiteralPath $args[0] -Raw -Encoding UTF8 | ConvertFrom-Json
$tokens=$null;$errors=$null
$installAst=[Management.Automation.Language.Parser]::ParseFile($data.installer,[ref]$tokens,[ref]$errors)
foreach($name in @('Resolve-Local','Hash','Verify-Candidate')) {
 $definition=$installAst.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
 Invoke-Expression $definition.Extent.Text
}
foreach($property in $data.paths.PSObject.Properties) {Set-Variable -Name $property.Name -Value ([string]$property.Value)}
$buildAst=[Management.Automation.Language.Parser]::ParseFile($data.builder,[ref]$tokens,[ref]$errors)
$assignment=$buildAst.Find({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -ceq '$binding'},$true)
Invoke-Expression $assignment.Extent.Text
if($data.change -eq 'escape') {
 $outside=Join-Path $data.fixture 'outside-config';Set-Content -LiteralPath $outside -Value 'outside'
 $binding.bindings.ZZZ.configurationFiles=@($outside)
}
$bindingPath=Join-Path $data.candidate 'tool-binding.json'
$binding | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $bindingPath -Encoding UTF8
$operations=[ordered]@{PGR=[ordered]@{};ZZZ=[ordered]@{};NIKKE=[ordered]@{}}
foreach($op in @('attach-home','claim-serum','dorm','simulation-field','maintainer-action','claim-daily-tasks','battle-pass-free-track')) {$operations.PGR[$op]=@{}}
foreach($op in @('attach-home','coffee','scratch-card','trigrams-collection','suibian-temple','random-play','charge-plan','city-fund-free-claim','engagement-reward')) {$operations.ZZZ[$op]=@{}}
foreach($op in @('attach-lobby','outpost','dispatch-friend')) {$operations.NIKKE[$op]=@{}}
# The public and current installer have different declared game scopes. Reuse
# their literal fixture scope without exercising unrelated NIKKE promotion.
$scopeAssignment=$installAst.Find({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -ceq '$expectedGames'},$true)
$fixtureGames=@(Invoke-Expression $scopeAssignment.Right.Extent.Text)
foreach($game in @($operations.Keys)) {if($game -notin $fixtureGames) {$operations.Remove($game)}}
$manifest=@{schemaVersion=2;packageId='legacy-night-rain-gamer';entryPoint='runner.exe';supportedGameIds=$fixtureGames;operationBindings=$operations;files=@(@{path='tool-binding.json';sha256=(Hash $bindingPath)})}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $data.candidate 'install-manifest.json') -Encoding UTF8
switch($data.change) {
 'mutable' {Set-Content -LiteralPath $zzzGroup -Value 'official-runtime-write';Set-Content -LiteralPath $zzzCoffee -Value 'official-runtime-write';Set-Content -LiteralPath $pgrConfig -Value 'official-runtime-write'}
 'immutable' {Set-Content -LiteralPath $zzzLauncher -Value 'changed-executable'}
 'missing' {Remove-Item -LiteralPath $zzzGroup}
}
try {$null=Verify-Candidate $data.candidate;Write-Output 'accepted'}
catch {Write-Output ('rejected:'+ $_.Exception.Message)}
'''
            powershell = shutil.which('pwsh')
            self.assertIsNotNone(powershell, 'release publisher requires PowerShell 7')
            # Inline production functions avoid changing the machine execution policy.
            command = "& {" + script + "} '" + str(fixture_json).replace("'", "''") + "'"
            result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(0, result.returncode, result.stderr)
            return result.stdout.strip()

    def test_official_runtime_configuration_write_does_not_invalidate_code(self):
        self.assertEqual('accepted', self.run_case('mutable'))

    def test_executable_change_remains_rejected(self):
        self.assertIn('upstream tool changed', self.run_case('immutable'))

    def test_missing_runtime_configuration_remains_rejected(self):
        self.assertIn('configuration is missing', self.run_case('missing'))

    def test_runtime_configuration_cannot_escape_tool_root(self):
        self.assertIn('outside its bound tool root', self.run_case('escape'))


if __name__ == '__main__':
    unittest.main()
