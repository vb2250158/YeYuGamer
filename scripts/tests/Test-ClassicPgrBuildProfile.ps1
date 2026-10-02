$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = Join-Path (Split-Path -Parent $PSScriptRoot) 'Build-YeYuGamerClassicAdapter.ps1'
$tokens=$null; $parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$parseErrors)
if ($parseErrors.Count) { throw 'Build script parsing failed' }
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-PgrBuildProfileRegistry'},$true)
if ($null -eq $definition) { throw 'Build profile selector missing' }
Invoke-Expression $definition.Extent.Text
$evidence=Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ('.cache\verification\pgr-build-profile-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$registry=Join-Path $evidence 'multi_config.json'
$journal=Join-Path $evidence '.yeyu-profile-restore.json'
$original='{"curr_config_id":"c_original","config_list":["c_original"]}'
[IO.File]::WriteAllText($registry,$original)
if ((Get-PgrBuildProfileRegistry $registry).curr_config_id -cne 'c_original') { throw 'Idle selection changed' }
[IO.File]::WriteAllText($registry,'{"curr_config_id":"c_yeyu_owned","config_list":["c_original","c_yeyu_owned"]}')
$restore=@{schemaVersion=1;configId='c_yeyu_owned';originalRegistry=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($original))}
[IO.File]::WriteAllText($journal,($restore | ConvertTo-Json))
$before=[IO.File]::ReadAllBytes($registry)
if ((Get-PgrBuildProfileRegistry $registry).curr_config_id -cne 'c_original') { throw 'Ephemeral selection frozen into package' }
if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($registry)) -cne [Convert]::ToBase64String($before)) { throw 'Read-only selector changed registry' }
[IO.File]::WriteAllText($registry,$original)
if ((Get-PgrBuildProfileRegistry $registry).curr_config_id -cne 'c_original') { throw 'Restored selection rejected' }
[IO.File]::WriteAllText($registry,'{"curr_config_id":"c_foreign","config_list":["c_foreign"]}')
$rejected=$false
try { Get-PgrBuildProfileRegistry $registry | Out-Null } catch { $rejected=$_.Exception.Message -like '*outside the owned recovery journal*' }
if (-not $rejected) { throw 'Foreign changed selection accepted' }
$restore.originalRegistry=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"curr_config_id":"../foreign","config_list":["../foreign"]}'))
$restore.configId='c_foreign'
[IO.File]::WriteAllText($journal,($restore | ConvertTo-Json))
$rejected=$false
try { Get-PgrBuildProfileRegistry $registry | Out-Null } catch { $rejected=$true }
if (-not $rejected) { throw 'Invalid recovery identity accepted' }
@{status='passed';cases=5;gameStarted=$false;registryNotModified=$true;evidence=$evidence} | ConvertTo-Json -Compress
