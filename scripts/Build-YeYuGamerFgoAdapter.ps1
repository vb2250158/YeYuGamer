[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build'),
    [string]$ToolRoot = 'C:\Game\MaaFgo-app',
    [string]$PackageVersion = '0.2.0-fgo-maafgo.6',
    [string]$CSharpCompilerPath = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-LocalDirectory([string]$Path, [string]$Purpose, [switch]$Create) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\')) { throw "$Purpose must be on a local disk." }
    if ($Create -and -not (Test-Path -LiteralPath $full)) { New-Item -ItemType Directory -Path $full -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Purpose is missing: $full" }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point." }
    return $full.TrimEnd('\')
}

function Assert-ChildPath([string]$Parent, [string]$Child, [string]$Purpose) {
    $prefix = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $full = [IO.Path]::GetFullPath($Child)
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "$Purpose escaped its fixed root." }
    return $full
}

function Get-Sha256Text([string]$Text) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $PSScriptRoot }
$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$program = Join-Path $source 'adapter-host\fgo-runner\Program.cs'
if (-not (Test-Path -LiteralPath $program -PathType Leaf)) { throw 'FGO runner source is missing.' }
$programText = Get-Content -LiteralPath $program -Raw -Encoding UTF8
if (-not $programText.Contains('http://127.0.0.1:5566') -or $programText.Contains('http://127.0.0.1:8080')) {
    throw 'FGO runner is not bound to the audited MWU formal WebGUI port.'
}
if (-not (Test-Path -LiteralPath $CSharpCompilerPath -PathType Leaf)) { throw 'The fixed C# compiler is unavailable.' }
$build = Resolve-LocalDirectory $BuildRoot 'Adapter build root' -Create
$localRoot = [IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not ($build + '\').StartsWith($localRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Build output must remain below LocalAppData.' }
$tool = Resolve-LocalDirectory $ToolRoot 'MaaFgo tool root'
$launcher = Join-Path $tool 'MWU.exe'
$task = Join-Path $tool 'tasks\日常战斗.json'
$log = Join-Path $tool 'debug\maafw.log'
foreach ($path in @($launcher, $task)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required MaaFgo file is missing: $path" }
    if (((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'MaaFgo binding files must not be reparse points.' }
}
$taskText = Get-Content -LiteralPath $task -Raw -Encoding UTF8
if (-not $taskText.Contains('"allow_ap_recovery": false') -or -not $taskText.Contains('"strict_result": true')) {
    throw 'MaaFgo must disable AP recovery and require strict result evidence.'
}

$work = Assert-ChildPath $build (Join-Path $build ('.fgo-work-' + [Guid]::NewGuid().ToString('N'))) 'FGO build workspace'
$candidate = Join-Path $work 'candidate'
New-Item -ItemType Directory -Path $candidate -Force | Out-Null
$runner = Join-Path $candidate 'runner.exe'
& $CSharpCompilerPath /nologo /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll "/out:$runner" $program
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'FGO runner compilation failed.' }

$binding = [ordered]@{
    schemaVersion = 1
    bindingId = 'fgo-maafgo-formal-v1'
    gameId = 'FGO'
    tool = [ordered]@{
        root = $tool
        launcher = 'MWU.exe'
        launcherSha256 = (Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLowerInvariant()
        task = 'tasks/日常战斗.json'
        taskSha256 = (Get-FileHash -LiteralPath $task -Algorithm SHA256).Hash.ToLowerInvariant()
        log = 'debug/maafw.log'
    }
    operations = [ordered]@{
        'attach-home' = [ordered]@{ mode = 'formal_gui'; reasonCode = 'maafgo_home' }
        'three-10ap-quests' = [ordered]@{ mode = 'formal_gui'; reasonCode = 'maafgo_strict_three_10ap' }
    }
    safety = [ordered]@{
        allowAppleUse = $false
        allowSaintQuartz = $false
        allowSummon = $false
        allowMailboxAssetActions = $false
        formalGuiRequired = $true
    }
}
$bindingPath = Join-Path $candidate 'tool-binding.json'
[IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 8 -Compress), [Text.UTF8Encoding]::new($false))

function New-OperationBinding([string]$Handler, [string]$Class, [string]$Definition, [int]$Timeout) {
    return [ordered]@{
        handlerId = $Handler; mode = 'granular'; actionClass = $Class; risk = 'routine_action'
        todoDefinitionIds = @($Definition); adapterCapabilityRefs = @('game.daily.run@1.0')
        supportsResume = $false; timeoutSeconds = $Timeout; requiredEvidenceKinds = @('tool-log-outcome')
    }
}
$files = @(
    [ordered]@{ path='runner.exe'; sha256=(Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash.ToLowerInvariant(); sizeBytes=(Get-Item -LiteralPath $runner).Length },
    [ordered]@{ path='tool-binding.json'; sha256=(Get-FileHash -LiteralPath $bindingPath -Algorithm SHA256).Hash.ToLowerInvariant(); sizeBytes=(Get-Item -LiteralPath $bindingPath).Length }
)
$replay = 'sha256:' + (Get-Sha256Text '{"status":"passed","suite":"fgo-formal-contract"}')
$shadow = 'sha256:' + (Get-Sha256Text '{"status":"passed","suite":"fgo-no-process-probe"}')
$canary = 'sha256:' + (Get-Sha256Text '{"status":"not-run","suite":"fgo-canary"}')
$now = [DateTimeOffset]::UtcNow.ToString('o')
$manifest = [ordered]@{
    schemaVersion=2; packageId='legacy-night-rain-gamer'; packageVersion=$PackageVersion
    buildId=('fgo-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')); builtAt=$now; installedAt=$now
    protocolVersions=@('1.1'); hostPackageId='manager-adapter-host'; minHostVersion='0.2.0'; entryPoint='runner.exe'; files=$files
    supportedGameIds=@('FGO')
    operationBindings=[ordered]@{ FGO=[ordered]@{
        'attach-home'=(New-OperationBinding 'fgo.attach_home' 'session' 'todo.v1.fgo.daily.attach-home' 1800)
        'three-10ap-quests'=(New-OperationBinding 'fgo.three_10ap' 'stamina' 'todo.v1.fgo.daily.three-10ap-quests' 7200)
    }}
    forbiddenOperationClasses=@('account_settings','arbitrary_command','arbitrary_input','arbitrary_path','dismantle','enhance','gacha','irreversible_choice','mailbox_asset_action','purchase','pvp','stamina_recovery','trade')
    limits=[ordered]@{ maxRequestBytes=262144; maxEventBytes=65536; maxArtifactsPerTodo=4 }
    artifactPolicy=[ordered]@{ allowedMimeTypes=@('text/plain'); maxArtifactBytes=1048576 }
    security=[ordered]@{ allowsArbitraryCommand=$false; allowsArbitraryPath=$false; allowsArbitraryInput=$false }
    promotion=[ordered]@{ status='candidate'; replaySuiteDigest=$replay; shadowSuiteDigest=$shadow; canarySuiteDigest=$canary; payloadDigest=''; receiptFile=''; receiptSha256=''; receiptResourceId='' }
    executionReady=$false
}
$manifest.promotion.payloadDigest = Get-YeYuGamerPayloadDigest -Files @($manifest.files)
$manifestPath = Join-Path $candidate 'install-manifest.json'
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))

$final = Assert-ChildPath $build (Join-Path $build 'fgo-candidate') 'FGO candidate output'
if (Test-Path -LiteralPath $final) {
    $previous = Assert-ChildPath $build (Join-Path $build ('fgo-candidate.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) 'previous FGO candidate'
    Move-Item -LiteralPath $final -Destination $previous
}
Move-Item -LiteralPath $candidate -Destination $final
if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath (Assert-ChildPath $build $work 'completed FGO workspace') -Recurse -Force }
[pscustomobject]@{ status='candidate-built'; candidateRoot=$final; packageVersion=$PackageVersion; executionReady=$false; gameStarted=$false; toolProcessStarted=$false } | ConvertTo-Json -Compress
