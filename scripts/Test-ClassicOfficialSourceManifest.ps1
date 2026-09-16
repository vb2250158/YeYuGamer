[CmdletBinding()]
param([string]$SourceRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $SourceRoot 'scripts\Restore-ClassicOfficialBaselines.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Candidate preparation script has parser errors.' }
# Load the production manifest functions without executing a real preparation.
foreach ($name in @('Hash', 'File-Manifest', 'Compare-Manifest', 'Copy-OfficialTree')) {
    $function = $ast.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw "Missing production function: $name" }
    Invoke-Expression $function.Extent.Text
}
$root = Join-Path 'C:\Projects\YeYuGamer\.cache\verification' ('source-manifest-' + [guid]::NewGuid().ToString('N'))
$baseline = Join-Path $root 'baseline'; $candidate = Join-Path $root 'candidate'
New-Item -ItemType Directory -Path (Join-Path $baseline '.git\objects'), (Join-Path $baseline '.github') -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $baseline '.git\objects\fixture'), 'repository metadata')
[IO.File]::WriteAllText((Join-Path $baseline '.gitignore'), 'cache/')
[IO.File]::WriteAllText((Join-Path $baseline '.github\workflow.yml'), 'name: fixture')
[IO.File]::WriteAllText((Join-Path $baseline 'main.py'), '# official source fixture')
Copy-OfficialTree $baseline $candidate
$expected = @(File-Manifest $baseline)
$actual = @(File-Manifest $candidate)
if ((@($expected.path | Sort-Object) -join ',') -cne '.github/workflow.yml,.gitignore,main.py') {
    throw 'Manifest must exclude only Git metadata, preserving source dotfiles.'
}
Compare-Manifest $expected $actual 'fixture'
[IO.File]::AppendAllText((Join-Path $candidate 'main.py'), '# changed')
$rejected = $false
try { Compare-Manifest $expected (File-Manifest $candidate) 'modified fixture' }
catch { if ($_.Exception.Message -match 'different=1') { $rejected = $true } else { throw } }
if (-not $rejected) { throw 'Changed source unexpectedly passed hash verification.' }
@{ status='passed'; cases=3; gameStarted=$false; evidenceDirectory=$root } | ConvertTo-Json -Compress
