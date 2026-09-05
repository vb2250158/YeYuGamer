[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build'),
    [ValidateSet('CZN','BD2')][string]$GameId = 'CZN',
    [string]$ToolRoot = '',
    [string]$PackageVersion = '',
    [string]$CSharpCompilerPath = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-LocalDirectory {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Purpose, [switch]$Create)
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full.StartsWith('\\', [StringComparison]::Ordinal)) { throw "$Purpose must be on a local disk." }
    if ($Create -and -not (Test-Path -LiteralPath $full)) { New-Item -ItemType Directory -Path $full -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Purpose is missing: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point." }
    return $full
}

function Assert-LocalFile {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Purpose)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\', [StringComparison]::Ordinal) -or -not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "$Purpose is missing or non-local: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point." }
    return $full
}

function Assert-ChildPath {
    param([Parameter(Mandatory)][string]$Parent, [Parameter(Mandatory)][string]$Child, [Parameter(Mandatory)][string]$Purpose)
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)
    if (-not $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)) { throw "$Purpose escaped its fixed root." }
    return $childFull
}

function Get-Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

function Get-TreeHash([string]$Root) {
    $prefix = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $builder = [System.Text.StringBuilder]::new()
    [string[]]$files = @(Get-ChildItem -LiteralPath $Root -File -Recurse | ForEach-Object { $_.FullName })
    [Array]::Sort($files, [StringComparer]::OrdinalIgnoreCase)
    foreach ($file in $files) {
        $item = Get-Item -LiteralPath $file
        $relative = $file.Substring($prefix.Length).Replace('\','/')
        [void]$builder.Append($relative).Append('|').Append($item.Length).Append('|').Append((Get-Hash $file)).Append("`n")
    }
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($builder.ToString())
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) } finally { $sha.Dispose() }
}

$source = [System.IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$isBd2 = $GameId -ceq 'BD2'
if (-not $ToolRoot) { $ToolRoot = if ($isBd2) { Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\MFABD2' } else { 'C:\Game\MaaKes-runtime-v1.2.3' } }
if (-not $PackageVersion) { $PackageVersion = if ($isBd2) { '0.1.0-bd2.3' } else { '0.1.0-czn.4' } }
$runnerSource = Assert-LocalFile (Join-Path $source 'adapter-host\czn-runner\Program.cs') 'CZN runner source'
$runnerSourceText = Get-Content -LiteralPath $runnerSource -Raw -Encoding UTF8
if (-not $runnerSourceText.Contains('{ "eventType", type }') -or
    -not $runnerSourceText.Contains('{ "at", DateTime.UtcNow.ToString("o") }') -or
    $runnerSourceText.Contains('{ "emittedAt", DateTime.UtcNow.ToString("o") }')) {
    throw 'CZN/BD2 runner event envelope does not match Adapter protocol v1.1.'
}
$compiler = Assert-LocalFile $CSharpCompilerPath 'C# compiler'
$tool = Resolve-LocalDirectory -Path $ToolRoot -Purpose 'Maa_KES runtime root'
$native = Resolve-LocalDirectory -Path (Join-Path $tool 'runtimes\win-x64\native') -Purpose 'MaaFramework native directory'
$resource = Resolve-LocalDirectory -Path (Join-Path $tool 'resource') -Purpose 'Maa_KES resource directory'
$agent = Resolve-LocalDirectory -Path (Join-Path $tool 'MaaAgentBinary') -Purpose 'MaaAgentBinary directory'
$framework = Assert-LocalFile (Join-Path $native 'MaaFramework.dll') 'MaaFramework DLL'
$interface = Assert-LocalFile (Join-Path $tool 'interface.json') 'Maa_KES interface'
$startTask = if ($isBd2) { Assert-LocalFile (Join-Path $tool 'resource\base\pipeline\Global.json') 'MFABD2 home task' } else { Assert-LocalFile (Join-Path $tool 'tasks\进入游戏.json') 'Maa_KES start-game task' }

$interfaceText = Get-Content -LiteralPath $interface -Raw -Encoding UTF8
if ($isBd2) {
    foreach ($required in @('RewardsDaily_Start','Pass_HomePage','QuickHunt_Start')) { if (-not $interfaceText.Contains($required)) { throw "MFABD2 interface is missing audited binding: $required" } }
} else {
    foreach ($required in @('tasks/进入游戏.json', 'tasks/日常/模拟处清体力.json', 'tasks/日常/奖励领取.json')) { if (-not $interfaceText.Contains($required)) { throw "Maa_KES interface is missing audited binding: $required" } }
}
$startTaskText = Get-Content -LiteralPath $startTask -Raw -Encoding UTF8
if (-not $isBd2 -and -not $startTaskText.Contains('com.tencent.czn')) { throw 'Maa_KES start-game task is missing the audited CN package.' }
$resourceTreeHash = Get-TreeHash $resource
$nativeTreeHash = Get-TreeHash $native
$agentTreeHash = Get-TreeHash $agent

$build = Resolve-LocalDirectory -Path $BuildRoot -Purpose 'Adapter build root' -Create
$localAppData = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not ($build + '\').StartsWith($localAppData, [StringComparison]::OrdinalIgnoreCase)) { throw 'Adapter build output must remain below LocalAppData.' }
$work = Assert-ChildPath -Parent $build -Child (Join-Path $build ('.czn-work-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'CZN build workspace'
$candidate = Join-Path $work 'candidate'
New-Item -ItemType Directory -Path $candidate -Force | Out-Null
$runner = Join-Path $candidate 'runner.exe'
$define = if ($isBd2) { '/define:BD2' } else { '/define:CZN' }
& $compiler /nologo /target:exe /optimize+ /platform:x64 $define /r:System.Web.Extensions.dll ("/out:$runner") $runnerSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'CZN runner compilation failed.' }

$binding = [ordered]@{
    schemaVersion = 1
    bindingId = if ($isBd2) { 'mfabd2-selected-daily-v1' } else { 'maa-kes-czn-selected-daily-v1' }
    gameId = $GameId
    androidPackage = if ($isBd2) { 'com.neowizgames.game.browndust2' } else { 'com.tencent.czn' }
    excludedAndroidPackages = if ($isBd2) { @() } else { @('com.smilegate.chaoszero.stove.google') }
    upstream = [ordered]@{
        repository = 'https://github.com/miaojiuqing/Maa_KES'
        auditedRelease = 'v1.2.3'
        auditedCommit = '438368b5392188410728000ccccdde13a0241ee6'
        releaseAssetSha256 = 'ac16a0b86f209147ccc8557292ed452c0972abd7a13fffa66caa6375b3a20e6e'
    }
    tool = [ordered]@{
        root = $tool
        maaFrameworkSha256 = Get-Hash $framework
        interfaceSha256 = Get-Hash $interface
        startTaskSha256 = Get-Hash $startTask
        nativeTreeSha256 = $nativeTreeHash
        agentTreeSha256 = $agentTreeHash
        resourceTreeSha256 = $resourceTreeHash
    }
}
$bindingPath = Join-Path $candidate 'tool-binding.json'
[System.IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 8 -Compress), [System.Text.UTF8Encoding]::new($false))

function New-Operation([string]$Handler, [string]$Class, [string]$Definition, [int]$Timeout) {
    return [ordered]@{
        handlerId = $Handler
        mode = 'granular'
        actionClass = $Class
        risk = 'routine_action'
        todoDefinitionIds = @($Definition)
        adapterCapabilityRefs = @('game.daily.run@1.0')
        supportsResume = $false
        timeoutSeconds = $Timeout
        requiredEvidenceKinds = @('tool-log-outcome')
    }
}

$manifest = [ordered]@{
    schemaVersion = 2
    packageId = 'legacy-night-rain-gamer'
    packageVersion = $PackageVersion
    buildId = 'czn-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')
    builtAt = [DateTimeOffset]::UtcNow.ToString('o')
    installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    protocolVersions = @('1.1')
    hostPackageId = 'manager-adapter-host'
    minHostVersion = '0.3.0'
    entryPoint = 'runner.exe'
    files = @(
        [ordered]@{ path = 'runner.exe'; sha256 = Get-Hash $runner; sizeBytes = (Get-Item -LiteralPath $runner).Length },
        [ordered]@{ path = 'tool-binding.json'; sha256 = Get-Hash $bindingPath; sizeBytes = (Get-Item -LiteralPath $bindingPath).Length }
    )
    supportedGameIds = @($GameId)
    operationBindings = if ($isBd2) { [ordered]@{ BD2 = [ordered]@{
            'attach-home' = (New-Operation 'bd2.attach_home' 'session' 'todo.v1.bd2.daily.attach-home' 1800)
            'daily-claim' = (New-Operation 'bd2.daily_claim' 'reward' 'todo.v1.bd2.daily.daily-claim' 1800)
            'stamina-sweep' = (New-Operation 'bd2.stamina_sweep' 'stamina' 'todo.v1.bd2.daily.stamina-sweep' 3600)
        }} } else { [ordered]@{ CZN = [ordered]@{
            'login-bonus' = (New-Operation 'czn.daily.login_bonus' 'reward' 'todo.v1.czn.daily.login-bonus' 600)
            'achievement-schedule' = (New-Operation 'czn.daily.achievement_schedule' 'daily' 'todo.v1.czn.daily.achievement-schedule' 600)
            'arkhianon-supply' = (New-Operation 'czn.daily.arkhianon_supply' 'daily' 'todo.v1.czn.daily.arkhianon-supply' 600)
            'simulation-stamina' = (New-Operation 'czn.daily.simulation_stamina' 'stamina' 'todo.v1.czn.daily.simulation-stamina' 3600)
        }} }
    forbiddenOperationClasses = @('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','story_progression','roguelike','save_deletion','shop','arbitrary_command','arbitrary_path','arbitrary_input')
    limits = [ordered]@{ maxRequestBytes = 262144; maxEventBytes = 65536; maxArtifactsPerTodo = 20 }
    artifactPolicy = [ordered]@{ allowedMimeTypes = @('text/plain'); maxArtifactBytes = 20971520 }
    security = [ordered]@{ allowsArbitraryCommand = $false; allowsArbitraryPath = $false; allowsArbitraryInput = $false }
    promotion = [ordered]@{
        status = 'candidate'
        replaySuiteDigest = 'sha256:' + ('0' * 64)
        shadowSuiteDigest = 'sha256:' + ('0' * 64)
        canarySuiteDigest = 'sha256:' + ('0' * 64)
        payloadDigest = ''
        receiptFile = ''
        receiptSha256 = ''
        receiptResourceId = ''
    }
    executionReady = $false
}
$manifest.promotion.payloadDigest = Get-YeYuGamerPayloadDigest -Files @($manifest.files)
[System.IO.File]::WriteAllText((Join-Path $candidate 'install-manifest.json'), ($manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))

$candidateLeaf = if ($isBd2) { 'bd2-candidate' } else { 'czn-candidate' }
$final = Assert-ChildPath -Parent $build -Child (Join-Path $build $candidateLeaf) -Purpose "$GameId candidate output"
if (Test-Path -LiteralPath $final) {
    $previous = Assert-ChildPath -Parent $build -Child (Join-Path $build ($candidateLeaf + '.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose "previous $GameId candidate"
    Move-Item -LiteralPath $final -Destination $previous
}
Move-Item -LiteralPath $candidate -Destination $final
if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath (Assert-ChildPath -Parent $build -Child $work -Purpose 'completed CZN build workspace') -Recurse -Force }
[pscustomobject]@{
    status = 'candidate-built'
    candidateRoot = $final
    packageVersion = $PackageVersion
    auditedUpstreamRelease = if ($isBd2) { 'v4.4.1' } else { 'v1.2.3' }
    auditedUpstreamCommit = '438368b5392188410728000ccccdde13a0241ee6'
    resourceTreeSha256 = $resourceTreeHash
    executionReady = $false
    gameStarted = $false
} | ConvertTo-Json -Compress
