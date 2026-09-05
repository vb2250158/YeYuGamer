[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build'),
    [string]$ToolRoot = 'C:\Game\ok-ww',
    [string]$GamePath = 'C:\Game\Wuthering Waves\Wuthering Waves Game\Wuthering Waves.exe',
    [string]$EndfieldToolRoot = 'C:\Game\ok-ef',
    [string]$EndfieldGamePath = 'C:\Game\Endfield Game\Endfield.exe',
    [string]$Gf2ToolRoot = 'C:\Game\ok-gf2',
    [string]$Gf2GamePath = 'C:\Game\GF2 Game\GF2_Exilium.exe',
    [string]$PackageVersion = '0.2.0-local-daily.50',
    [string]$CSharpCompilerPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-LocalFile([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\') -or -not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "$Purpose is missing or not local: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point." }
    return $full
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$runnerSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\Program.cs') 'WW runner source'
$endfieldBridgeSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\EndfieldYeYuBridge.py') 'Endfield YeYu bridge source'
$compiler = Assert-LocalFile $CSharpCompilerPath 'C# compiler'
$tool = [IO.Path]::GetFullPath($ToolRoot)
if ($tool.StartsWith('\\') -or -not (Test-Path -LiteralPath $tool -PathType Container)) { throw 'WW tool root is not a local directory.' }
$launcher = Assert-LocalFile (Join-Path $tool 'ok-ww.exe') 'WW tool launcher'
$dailyConfig = Assert-LocalFile (Join-Path $tool 'data\apps\ok-ww\working\configs\DailyTask.json') 'WW daily configuration'
$game = Assert-LocalFile $GamePath 'WW game executable'
$endfieldTool = [IO.Path]::GetFullPath($EndfieldToolRoot)
$endfieldLauncherPath = Join-Path $endfieldTool 'ok-ef.exe'
$endfieldLauncher = Assert-LocalFile $endfieldLauncherPath 'Endfield formal GUI launcher'
if ((Hash $endfieldLauncher) -ceq (Hash $launcher)) { throw 'Endfield formal launcher is an invalid copy of the WW launcher.' }
$endfieldAppRoot = Join-Path $endfieldTool 'data\apps\ok-ef'
if (-not (Test-Path -LiteralPath $endfieldAppRoot -PathType Container) -or
    ((Get-Item -LiteralPath $endfieldAppRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Endfield formal PyAppify app root is missing or unsafe.'
}
$endfieldUpdateState = Assert-LocalFile (Join-Path $endfieldAppRoot 'app.json') 'Endfield formal update state'
$endfieldState = Get-Content -LiteralPath $endfieldUpdateState -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
if (-not $endfieldState.ContainsKey('current_version') -or [string]::IsNullOrWhiteSpace([string]$endfieldState.current_version)) { throw 'Endfield formal update state has no current_version.' }
$endfieldState.app_starting_version = [string]$endfieldState.current_version
$endfieldState.update_method = 'AUTO_UPDATE'
$endfieldState.auto_start = $true
$endfieldState.update_state = 'idle'
$endfieldState.update_target_version = $null
$endfieldState.update_error = $null
$endfieldStateText = $endfieldState | ConvertTo-Json -Depth 12
$endfieldStateTemp = $endfieldUpdateState + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
[IO.File]::WriteAllText($endfieldStateTemp, $endfieldStateText, [Text.UTF8Encoding]::new($false))
$endfieldStateBackup = $endfieldUpdateState + '.' + [Guid]::NewGuid().ToString('N') + '.bak'
[IO.File]::Replace($endfieldStateTemp, $endfieldUpdateState, $endfieldStateBackup)
Remove-Item -LiteralPath $endfieldStateBackup
$endfieldPython = Assert-LocalFile (Join-Path $endfieldAppRoot 'python\pythonw.exe') 'Endfield GUI Python runtime'
$endfieldMainPath = Join-Path $endfieldAppRoot 'working\main.py'
$endfieldMain = Assert-LocalFile $endfieldMainPath 'Endfield formal GUI entry'
$endfieldPatchRoot = Join-Path $endfieldAppRoot 'working\src\patches'
if (-not (Test-Path -LiteralPath $endfieldPatchRoot -PathType Container)) { throw 'Endfield patch root is missing.' }
$endfieldBridgeTarget = Join-Path $endfieldPatchRoot 'yeyu_gamer_bridge.py'
$bridgeTemporary = $endfieldBridgeTarget + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
[IO.File]::WriteAllBytes($bridgeTemporary, [IO.File]::ReadAllBytes($endfieldBridgeSource))
if (Test-Path -LiteralPath $endfieldBridgeTarget -PathType Leaf) {
    $bridgeBackup = $endfieldBridgeTarget + '.' + [Guid]::NewGuid().ToString('N') + '.bak'
    [IO.File]::Replace($bridgeTemporary, $endfieldBridgeTarget, $bridgeBackup)
    Remove-Item -LiteralPath $bridgeBackup
} else {
    [IO.File]::Move($bridgeTemporary, $endfieldBridgeTarget)
}
$endfieldMainText = [IO.File]::ReadAllText($endfieldMain, [Text.Encoding]::UTF8).Replace("`r`n", "`n")
$endfieldMarker = '# YEYU_GAMER_ENDFIELD_BRIDGE_V2'
if (-not $endfieldMainText.Contains($endfieldMarker)) {
    $endfieldAnchor = "    install_startup_patches()`n"
    if (-not $endfieldMainText.Contains($endfieldAnchor)) { throw 'Endfield GUI entry shape changed.' }
    $endfieldInjection = $endfieldAnchor + "    $endfieldMarker`n    from src.patches.yeyu_gamer_bridge import install_yeyu_gamer_bridge`n    install_yeyu_gamer_bridge()`n"
    $endfieldMainText = $endfieldMainText.Replace($endfieldAnchor, $endfieldInjection)
    $mainTemporary = $endfieldMain + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($mainTemporary, $endfieldMainText, [Text.UTF8Encoding]::new($false))
    $mainBackup = $endfieldMain + '.' + [Guid]::NewGuid().ToString('N') + '.bak'
    [IO.File]::Replace($mainTemporary, $endfieldMain, $mainBackup)
    Remove-Item -LiteralPath $mainBackup
}
if (-not [IO.File]::ReadAllText($endfieldMain, [Text.Encoding]::UTF8).Contains($endfieldMarker)) { throw 'Endfield persistent Manager bridge installation failed.' }
$formalMarker = '# YEYU_GAMER_GUI_RUN_BRIDGE_V1'
$endfieldMainText = [IO.File]::ReadAllText($endfieldMain, [Text.Encoding]::UTF8).Replace("`r`n", "`n")
if (-not $endfieldMainText.Contains($formalMarker)) {
    $formalAnchor = if ($endfieldMainText.Contains("if __name__ == '__main__':`n")) {
        "if __name__ == '__main__':`n"
    } elseif ($endfieldMainText.Contains('if __name__ == "__main__":' + "`n")) {
        'if __name__ == "__main__":' + "`n"
    } else { throw 'Endfield formal GUI entry point shape changed.' }
    $formalBridge = @'
# YEYU_GAMER_GUI_RUN_BRIDGE_V1
import json as _yeyu_json
import os as _yeyu_os
import sys as _yeyu_sys
import time as _yeyu_time

def _yeyu_manager_run_requested():
    bridge_path = _yeyu_os.path.join(_yeyu_os.path.dirname(__file__), 'configs', 'YeYuGamerRun.json')
    try:
        with open(bridge_path, 'r', encoding='utf-8') as bridge_file:
            bridge = _yeyu_json.load(bridge_file)
        age = _yeyu_time.time() - float(bridge.get('createdAtUnix', 0))
        return (bridge.get('schemaVersion') == 1 and bridge.get('active') is True
                and -300 <= age <= 3600)
    except Exception:
        return False

'@
    $formalBridge = $formalBridge.Replace("`r`n", "`n")
    $formalGuard = $formalAnchor + "    if _yeyu_manager_run_requested():`n        if '--task' not in _yeyu_sys.argv and '-t' not in _yeyu_sys.argv:`n            _yeyu_sys.argv.extend(['--task', '1'])`n        if '--exit' not in _yeyu_sys.argv:`n            _yeyu_sys.argv.append('--exit')`n"
    $endfieldMainText = $endfieldMainText.Replace($formalAnchor, $formalBridge + $formalGuard)
    $mainTemporary = $endfieldMain + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($mainTemporary, $endfieldMainText, [Text.UTF8Encoding]::new($false))
    $mainBackup = $endfieldMain + '.' + [Guid]::NewGuid().ToString('N') + '.bak'
    [IO.File]::Replace($mainTemporary, $endfieldMain, $mainBackup)
    Remove-Item -LiteralPath $mainBackup
}
if (-not [IO.File]::ReadAllText($endfieldMain, [Text.Encoding]::UTF8).Contains($formalMarker)) { throw 'Endfield persistent formal GUI run bridge installation failed.' }
$endfieldMain = Assert-LocalFile $endfieldMain 'Endfield bridged formal GUI entry'
$endfieldDailyConfig = Assert-LocalFile (Join-Path $endfieldAppRoot 'working\configs\DailyTask.json') 'Endfield daily configuration'
$endfieldGame = Assert-LocalFile $EndfieldGamePath 'Endfield game executable'
$gf2Tool = [IO.Path]::GetFullPath($Gf2ToolRoot)
$gf2Launcher = Assert-LocalFile (Join-Path $gf2Tool 'working\ok-gf2.exe') 'GF2 formal GUI entry'
$gf2Main = Assert-LocalFile (Join-Path $gf2Tool 'working\main.py') 'GF2 GUI source entry'
$gf2DailyConfig = Assert-LocalFile (Join-Path $gf2Tool 'working\configs\DailyTask.json') 'GF2 daily configuration'
$gf2Cli = Assert-LocalFile (Join-Path $gf2Tool 'working\ok\cli.py') 'GF2 ok.cli module'
$gf2Game = Assert-LocalFile $Gf2GamePath 'GF2 game executable'
$build = [IO.Path]::GetFullPath($BuildRoot)
if (-not $build.StartsWith(([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Adapter build output must remain below LocalAppData.' }
New-Item -ItemType Directory -Path $build -Force | Out-Null
$candidate = Join-Path $build 'local-daily-candidate'
if (Test-Path -LiteralPath $candidate) { Move-Item -LiteralPath $candidate -Destination (Join-Path $build ('local-daily-candidate.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) }
New-Item -ItemType Directory -Path $candidate -Force | Out-Null
$runner = Join-Path $candidate 'runner.exe'
$uiaClient = Assert-LocalFile (Join-Path $env:WINDIR 'Microsoft.NET\assembly\GAC_MSIL\UIAutomationClient\v4.0_4.0.0.0__31bf3856ad364e35\UIAutomationClient.dll') 'UI Automation client reference'
$uiaTypes = Assert-LocalFile (Join-Path $env:WINDIR 'Microsoft.NET\assembly\GAC_MSIL\UIAutomationTypes\v4.0_4.0.0.0__31bf3856ad364e35\UIAutomationTypes.dll') 'UI Automation types reference'
& $compiler /nologo /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll /r:System.Drawing.dll ("/r:$uiaClient") ("/r:$uiaTypes") ("/out:$runner") $runnerSource
if ($LASTEXITCODE -ne 0) { throw 'WW runner compilation failed.' }
$endfieldBridge = Join-Path $candidate 'EndfieldYeYuBridge.py'
Copy-Item -LiteralPath $endfieldBridgeSource -Destination $endfieldBridge
$binding = [ordered]@{ schemaVersion = 2; tools = [ordered]@{
    WW = [ordered]@{ root=$tool; launcher=$launcher; launcherSha256=(Hash $launcher); dailyConfig=$dailyConfig; dailyConfigSha256=(Hash $dailyConfig); game=$game }
    Endfield = [ordered]@{ root=$endfieldTool; launcher=$endfieldLauncher; launcherSha256=(Hash $endfieldLauncher); updateState=$endfieldUpdateState; updateStateSha256=(Hash $endfieldUpdateState); guiRuntime=$endfieldPython; guiRuntimeSha256=(Hash $endfieldPython); guiEntry=$endfieldMain; guiEntrySha256=(Hash $endfieldMain); dailyConfig=$endfieldDailyConfig; dailyConfigSha256=(Hash $endfieldDailyConfig); game=$endfieldGame }
    GF2 = [ordered]@{ root=$gf2Tool; launcher=$gf2Launcher; launcherSha256=(Hash $gf2Launcher); guiEntry=$gf2Main; guiEntrySha256=(Hash $gf2Main); dailyConfig=$gf2DailyConfig; dailyConfigSha256=(Hash $gf2DailyConfig); game=$gf2Game }
} }
$bindingPath = Join-Path $candidate 'tool-binding.json'
[IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
$operation = { param($id,$class,$definition,$timeout,$evidenceKinds=@('tool-log-outcome')) [ordered]@{ handlerId=$id; mode='granular'; actionClass=$class; risk='routine_action'; todoDefinitionIds=@($definition); adapterCapabilityRefs=@('game.daily.run@1.0'); supportsResume=$false; timeoutSeconds=$timeout; requiredEvidenceKinds=@($evidenceKinds) } }
$manifest = [ordered]@{ schemaVersion=2; packageId='legacy-night-rain-gamer'; packageVersion=$PackageVersion; buildId=('local-daily-'+[DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')); builtAt=[DateTimeOffset]::UtcNow.ToString('o'); installedAt=[DateTimeOffset]::UtcNow.ToString('o'); protocolVersions=@('1.1'); hostPackageId='manager-adapter-host'; minHostVersion='0.2.0'; entryPoint='runner.exe'; files=@([ordered]@{path='runner.exe';sha256=(Hash $runner);sizeBytes=(Get-Item -LiteralPath $runner).Length},[ordered]@{path='EndfieldYeYuBridge.py';sha256=(Hash $endfieldBridge);sizeBytes=(Get-Item -LiteralPath $endfieldBridge).Length},[ordered]@{path='tool-binding.json';sha256=(Hash $bindingPath);sizeBytes=(Get-Item -LiteralPath $bindingPath).Length}); supportedGameIds=@('WW','Endfield','GF2'); operationBindings=[ordered]@{ WW=[ordered]@{ 'attach-world'=(& $operation 'ww.attach_world' 'session' 'todo.v1.ww.daily.attach-world' 1800); 'inspect-daily-progress'=(& $operation 'ww.daily_task' 'daily' 'todo.v1.ww.daily.inspect-daily-progress' 1800); 'farm-nightmare-daily-echo'=(& $operation 'ww.daily_task' 'daily' 'todo.v1.ww.daily.farm-nightmare-daily-echo' 1800); 'spend-waveplates'=(& $operation 'ww.daily_task' 'stamina' 'todo.v1.ww.daily.spend-waveplates' 1800); 'claim-daily-reward'=(& $operation 'ww.daily_task' 'reward' 'todo.v1.ww.daily.claim-daily-reward' 1800 @('tool-log-outcome','game-ui-daily-activity-100','game-ui-daily-reward-before','game-ui-daily-reward-raw','game-ui-daily-reward-watermarked')); 'claim-mail'=(& $operation 'ww.daily_task' 'mail' 'todo.v1.ww.daily.claim-mail' 1800); 'claim-battle-pass'=(& $operation 'ww.daily_task' 'reward' 'todo.v1.ww.daily.claim-battle-pass' 1800) }; Endfield=[ordered]@{ 'attach-world'=(& $operation 'endfield.attach_world' 'session' 'todo.v1.endfield.daily.attach-world' 1800); 'mail'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.mail' 1800); 'spend-sanity'=(& $operation 'endfield.daily_task' 'stamina' 'todo.v1.endfield.daily.spend-sanity' 1800); 'delivery-commission'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.delivery-commission' 1800); 'collect-credit'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.collect-credit' 1800); 'dijiang-harvest'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.dijiang-harvest' 1800); 'claim-daily-reward'=(& $operation 'endfield.daily_task' 'reward' 'todo.v1.endfield.daily.claim-daily-reward' 1800) }; GF2=[ordered]@{ 'attach-home'=(& $operation 'gf2.attach_home' 'session' 'todo.v1.gf2.daily.attach-home' 1800); 'mail'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.mail' 1800); 'public-area-dispatch'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.public-area-dispatch' 1800); 'spend-stamina'=(& $operation 'gf2.daily_task' 'stamina' 'todo.v1.gf2.daily.spend-stamina' 1800); 'squad-tasks'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.squad-tasks' 1800); 'claim-daily-missions'=(& $operation 'gf2.daily_task' 'reward' 'todo.v1.gf2.daily.claim-daily-missions' 1800); 'battle-pass-free-track'=(& $operation 'gf2.daily_task' 'reward' 'todo.v1.gf2.daily.battle-pass-free-track' 1800) } }; forbiddenOperationClasses=@('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','arbitrary_command','arbitrary_path','arbitrary_input'); limits=[ordered]@{maxRequestBytes=262144;maxEventBytes=65536;maxArtifactsPerTodo=20}; artifactPolicy=[ordered]@{allowedMimeTypes=@('text/plain','image/png');maxArtifactBytes=20971520}; security=[ordered]@{allowsArbitraryCommand=$false;allowsArbitraryPath=$false;allowsArbitraryInput=$false}; promotion=[ordered]@{status='candidate';replaySuiteDigest='sha256:'+('0'*64);shadowSuiteDigest='sha256:'+('0'*64);canarySuiteDigest='sha256:'+('0'*64);payloadDigest='';receiptFile='';receiptSha256='';receiptResourceId=''}; executionReady=$false }
$manifest.promotion.payloadDigest = Get-YeYuGamerPayloadDigest -Files @($manifest.files)
[IO.File]::WriteAllText((Join-Path $candidate 'install-manifest.json'), ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{status='candidate-built';candidateRoot=$candidate;gameIds=@('WW','Endfield','GF2');executionReady=$false;gameStarted=$false;validatedToolPaths=@($endfieldLauncher,$endfieldUpdateState,$endfieldPython,$endfieldMain,$endfieldDailyConfig,$endfieldGame,$gf2Launcher,$gf2Main,$gf2DailyConfig,$gf2Cli,$gf2Game)} | ConvertTo-Json -Compress
