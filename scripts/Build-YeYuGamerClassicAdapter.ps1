[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build'),
    [string]$PgrToolRoot = 'C:\Game\MPA-win-x86_64-v3.7.15',
    [string]$PgrGamePath = 'C:\Game\Punishing Gray Raven\Punishing Gray Raven Game\PGR.exe',
    [string]$PgrPython = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Python\Python312\python.exe'),
    [string]$ZzzToolRoot = 'C:\Game\ZZZTool',
    [string]$ZzzGamePath = 'C:\Game\ZenlessZoneZero Game\ZenlessZoneZero.exe',
    [string]$ZzzPython = 'C:\Game\ZZZTool\.venv\Scripts\python.exe',
    [string]$NikkeToolRoot = 'C:\Game\ok-NIKKE',
    [string]$NikkeGamePath = 'C:\Game\胜利女神：新的希望(2002017)\WeGameLauncher\launcher.exe',
    [string]$NikkePython = 'C:\Game\ok-nte-src\.venv\Scripts\python.exe',
    [string]$PackageVersion = '0.3.0-classic-selected.17',
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
function Assert-LocalDirectory([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\') -or -not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Purpose is missing or not local: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point." }
    return $full
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$runnerSource = Assert-LocalFile (Join-Path $source 'adapter-host\classic-runner\Program.cs') 'classic runner source'
$driverSource = Assert-LocalFile (Join-Path $source 'adapter-host\classic-runner\classic_tool_driver.py') 'classic tool driver'
$compiler = Assert-LocalFile $CSharpCompilerPath 'C# compiler'

$pgrTool = Assert-LocalDirectory $PgrToolRoot 'PGR tool root'
$pgrEntry = if (Test-Path -LiteralPath (Join-Path $pgrTool 'FOS.exe') -PathType Leaf) { Assert-LocalFile (Join-Path $pgrTool 'FOS.exe') 'PGR tool entry' } else { Assert-LocalFile (Join-Path $pgrTool 'MFW.exe') 'PGR tool entry' }
$pgrConfig = Assert-LocalFile (Join-Path $pgrTool 'config\configs\c_d300db28e6bd482b947ce83c5521c567.json') 'PGR base task profile'
$pgrGame = Assert-LocalFile $PgrGamePath 'PGR game executable'
$pgrPythonPath = Assert-LocalFile $PgrPython 'PGR Python runtime'

$zzzTool = Assert-LocalDirectory $ZzzToolRoot 'ZZZ tool root'
$zzzLauncher = Assert-LocalFile (Join-Path $zzzTool 'OneDragon-Launcher.exe') 'ZZZ formal GUI/update entry'
$zzzGroup = Assert-LocalFile (Join-Path $zzzTool 'config\01\one_dragon\_group.yml') 'ZZZ selected application group'
$zzzCoffee = Assert-LocalFile (Join-Path $zzzTool 'config\01\one_dragon\coffee.yml') 'ZZZ coffee configuration'
$zzzGame = Assert-LocalFile $ZzzGamePath 'ZZZ game executable'
$zzzPythonPath = Assert-LocalFile $ZzzPython 'ZZZ Python runtime'

$nikkeTool = Assert-LocalDirectory $NikkeToolRoot 'NIKKE tool root'
$nikkeConfig = Assert-LocalFile (Join-Path $nikkeTool 'run_nikke_behavior_tree.py') 'NIKKE behavior-tree config'
$nikkeGui = Assert-LocalFile (Join-Path $nikkeTool 'run_nikke_gui.py') 'NIKKE formal GUI entry'
$nikkeReturn = Assert-LocalFile (Join-Path $nikkeTool 'ok_tasks\trees\nikke_return_lobby.json') 'NIKKE return-lobby tree'
$nikkeOutpost = Assert-LocalFile (Join-Path $nikkeTool 'ok_tasks\trees\nikke_outpost.json') 'NIKKE outpost tree'
$nikkeDispatch = Assert-LocalFile (Join-Path $nikkeTool 'ok_tasks\trees\nikke_dispatch_friend.json') 'NIKKE dispatch/friend tree'
$nikkeGame = Assert-LocalFile $NikkeGamePath 'NIKKE game executable'
$nikkePythonPath = Assert-LocalFile $NikkePython 'NIKKE Python runtime'
$nikkePythonwPath = Assert-LocalFile (Join-Path (Split-Path -Parent $nikkePythonPath) 'pythonw.exe') 'NIKKE GUI Python runtime'

$build = [IO.Path]::GetFullPath($BuildRoot)
$localAppData = ([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not $build.StartsWith($localAppData, [StringComparison]::OrdinalIgnoreCase)) { throw 'Adapter build output must remain below LocalAppData.' }
New-Item -ItemType Directory -Path $build -Force | Out-Null
$candidate = Join-Path $build 'classic-selected-daily-candidate'
if (Test-Path -LiteralPath $candidate) {
    Move-Item -LiteralPath $candidate -Destination (Join-Path $build ('classic-selected-daily-candidate.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')))
}
New-Item -ItemType Directory -Path $candidate -Force | Out-Null
$runner = Join-Path $candidate 'runner.exe'
& $compiler /nologo /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$runner") $runnerSource
if ($LASTEXITCODE -ne 0) { throw 'Classic selected-daily runner compilation failed.' }
$driver = Join-Path $candidate 'classic_tool_driver.py'
Copy-Item -LiteralPath $driverSource -Destination $driver

$binding = [ordered]@{
    schemaVersion = 1
    bindings = [ordered]@{
        PGR = [ordered]@{ toolRoot=$pgrTool; gamePath=$pgrGame; python=$pgrPythonPath; verifiedFiles=@(
            [ordered]@{path=$pgrEntry;sha256=(Hash $pgrEntry)}, [ordered]@{path=$pgrConfig;sha256=(Hash $pgrConfig)}, [ordered]@{path=$pgrPythonPath;sha256=(Hash $pgrPythonPath)}
        ) }
        ZZZ = [ordered]@{ toolRoot=$zzzTool; gamePath=$zzzGame; python=$zzzPythonPath; verifiedFiles=@(
            [ordered]@{path=$zzzLauncher;sha256=(Hash $zzzLauncher)}, [ordered]@{path=$zzzGroup;sha256=(Hash $zzzGroup)}, [ordered]@{path=$zzzCoffee;sha256=(Hash $zzzCoffee)}, [ordered]@{path=$zzzPythonPath;sha256=(Hash $zzzPythonPath)}
        ) }
        NIKKE = [ordered]@{ toolRoot=$nikkeTool; gamePath=$nikkeGame; python=$nikkePythonPath; verifiedFiles=@(
            [ordered]@{path=$nikkeConfig;sha256=(Hash $nikkeConfig)}, [ordered]@{path=$nikkeGui;sha256=(Hash $nikkeGui)},
            [ordered]@{path=$nikkePythonwPath;sha256=(Hash $nikkePythonwPath)}, [ordered]@{path=$nikkeReturn;sha256=(Hash $nikkeReturn)},
            [ordered]@{path=$nikkeOutpost;sha256=(Hash $nikkeOutpost)}, [ordered]@{path=$nikkeDispatch;sha256=(Hash $nikkeDispatch)},
            [ordered]@{path=$nikkePythonPath;sha256=(Hash $nikkePythonPath)}
        ) }
    }
}
$bindingPath = Join-Path $candidate 'tool-binding.json'
[IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 10 -Compress), [Text.UTF8Encoding]::new($false))

$operation = {
    param($handlerId,$actionClass,$definition,$timeout)
    [ordered]@{ handlerId=$handlerId; mode='granular'; actionClass=$actionClass; risk='routine_action'; todoDefinitionIds=@($definition); adapterCapabilityRefs=@('game.daily.run@1.0'); supportsResume=$false; timeoutSeconds=$timeout; requiredEvidenceKinds=@('tool-log-outcome') }
}
$manifest = [ordered]@{
    schemaVersion=2; packageId='legacy-night-rain-gamer'; packageVersion=$PackageVersion
    buildId=('classic-selected-'+[DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')); builtAt=[DateTimeOffset]::UtcNow.ToString('o'); installedAt=[DateTimeOffset]::UtcNow.ToString('o')
    protocolVersions=@('1.1'); hostPackageId='manager-adapter-host'; minHostVersion='0.2.0'; entryPoint='runner.exe'
    files=@(
        [ordered]@{path='runner.exe';sha256=(Hash $runner);sizeBytes=(Get-Item -LiteralPath $runner).Length},
        [ordered]@{path='classic_tool_driver.py';sha256=(Hash $driver);sizeBytes=(Get-Item -LiteralPath $driver).Length},
        [ordered]@{path='tool-binding.json';sha256=(Hash $bindingPath);sizeBytes=(Get-Item -LiteralPath $bindingPath).Length}
    )
    supportedGameIds=@('PGR','ZZZ','NIKKE')
    operationBindings=[ordered]@{
        PGR=[ordered]@{
            'attach-home'=(& $operation 'pgr.selected_task' 'session' 'todo.v1.pgr.daily.attach-home' 1800)
            'claim-serum'=(& $operation 'pgr.selected_task' 'stamina' 'todo.v1.pgr.daily.claim-serum' 1800)
            'dorm'=(& $operation 'pgr.selected_task' 'base' 'todo.v1.pgr.daily.dorm' 1800)
            'simulation-field'=(& $operation 'pgr.selected_task' 'stamina' 'todo.v1.pgr.daily.simulation-field' 1800)
            'maintainer-action'=(& $operation 'pgr.selected_task' 'daily' 'todo.v1.pgr.daily.maintainer-action' 1800)
            'claim-daily-tasks'=(& $operation 'pgr.selected_task' 'reward' 'todo.v1.pgr.daily.claim-daily-tasks' 1800)
            'battle-pass-free-track'=(& $operation 'pgr.selected_task' 'reward' 'todo.v1.pgr.daily.battle-pass-free-track' 1800)
        }
        ZZZ=[ordered]@{
            'attach-home'=(& $operation 'zzz.selected_application' 'session' 'todo.v1.zzz.daily.attach-home' 1800)
            'coffee'=(& $operation 'zzz.selected_application' 'daily' 'todo.v1.zzz.daily.coffee' 1800)
            'scratch-card'=(& $operation 'zzz.selected_application' 'daily' 'todo.v1.zzz.daily.scratch-card' 1800)
            'trigrams-collection'=(& $operation 'zzz.selected_application' 'daily' 'todo.v1.zzz.daily.trigrams-collection' 1800)
            'suibian-temple'=(& $operation 'zzz.selected_application' 'daily' 'todo.v1.zzz.daily.suibian-temple' 1800)
            'random-play'=(& $operation 'zzz.selected_application' 'daily' 'todo.v1.zzz.daily.random-play' 1800)
            'charge-plan'=(& $operation 'zzz.selected_application' 'stamina' 'todo.v1.zzz.daily.charge-plan' 1800)
            'city-fund-free-claim'=(& $operation 'zzz.selected_application' 'reward' 'todo.v1.zzz.daily.city-fund-free-claim' 1800)
            'engagement-reward'=(& $operation 'zzz.selected_application' 'reward' 'todo.v1.zzz.daily.engagement-reward' 1800)
        }
        NIKKE=[ordered]@{
            'attach-lobby'=(& $operation 'nikke.selected_subtree' 'session' 'todo.v1.nikke.daily.attach-lobby' 1200)
            'outpost'=(& $operation 'nikke.selected_subtree' 'base' 'todo.v1.nikke.daily.outpost' 1200)
            'dispatch-friend'=(& $operation 'nikke.selected_subtree' 'social' 'todo.v1.nikke.daily.dispatch-friend' 1200)
        }
    }
    forbiddenOperationClasses=@('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','arbitrary_command','arbitrary_path','arbitrary_input')
    limits=[ordered]@{maxRequestBytes=262144;maxEventBytes=65536;maxArtifactsPerTodo=20}
    artifactPolicy=[ordered]@{allowedMimeTypes=@('text/plain');maxArtifactBytes=20971520}
    security=[ordered]@{allowsArbitraryCommand=$false;allowsArbitraryPath=$false;allowsArbitraryInput=$false}
    promotion=[ordered]@{status='candidate';replaySuiteDigest='sha256:'+('0'*64);shadowSuiteDigest='sha256:'+('0'*64);canarySuiteDigest='sha256:'+('0'*64);payloadDigest='';receiptFile='';receiptSha256='';receiptResourceId=''}
    executionReady=$false
}
$manifest.promotion.payloadDigest = Get-YeYuGamerPayloadDigest -Files @($manifest.files)
[IO.File]::WriteAllText((Join-Path $candidate 'install-manifest.json'), ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{status='candidate-built';candidateRoot=$candidate;gameIds=@('PGR','ZZZ','NIKKE');executionReady=$false;gameStarted=$false;validatedGamePaths=@($pgrGame,$zzzGame,$nikkeGame)} | ConvertTo-Json -Depth 4 -Compress
