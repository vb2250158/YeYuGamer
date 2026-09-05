[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build'),
    [string]$ToolRoot = 'C:\Game\March7thAssistant_full',
    [string]$PackageVersion = '0.2.0-starrail.27',
    [string]$CSharpCompilerPath = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    [string]$CompatibilityPayloadPath = '',
    [switch]$TestBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

function Resolve-March7thCompatibilityPayload {
    param([string]$Path, [Parameter(Mandatory)][string]$SourceCodeRoot)
    # This input is a separately maintained, precompiled compatibility payload.
    # The public source repository does not supply or regenerate it. Preserve
    # the existing local location unless the operator supplies an explicit file.
    if ([string]::IsNullOrWhiteSpace($Path)) {
        $Path = Join-Path $SourceCodeRoot 'March7thAssistantBasePatch.b64'
    }
    if (-not [System.IO.Path]::IsPathFullyQualified($Path)) {
        throw 'March7th compatibility payload must use an absolute local file path.'
    }
    $full = Assert-YeYuGamerLocalTarget -Path $Path -Purpose 'March7th compatibility payload'
    $driveRoot = [System.IO.Path]::GetPathRoot($full)
    $drive = Get-PSDrive -Name $driveRoot.TrimEnd('\', ':') -ErrorAction SilentlyContinue
    if ($drive -and ($drive.DisplayRoot -or $drive.Root.StartsWith('\\'))) {
        throw 'March7th compatibility payload must not use a mapped network drive.'
    }
    if ([System.IO.DriveInfo]::new($driveRoot).DriveType -ne [System.IO.DriveType]::Fixed) {
        throw 'March7th compatibility payload must be on a fixed local disk.'
    }
    Assert-YeYuGamerNoReparseAncestors -Path $full -Purpose 'March7th compatibility payload' | Out-Null
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw 'March7th compatibility payload is missing. The public repository does not include this payload and cannot independently build the StarRail Adapter. Supply the audited local file with -CompatibilityPayloadPath.'
    }
    $expectedHash = '03877ba5e748fd5c246d1a225caf1a54f44954a0dd0f1971d190024f35784d43'
    if ((Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedHash) {
        throw 'March7th compatibility payload SHA256 does not match the audited input.'
    }
    return $full
}

function Resolve-LocalDirectory {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Purpose, [switch]$Create)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\', [StringComparison]::Ordinal)) { throw "$Purpose must be on a local disk." }
    if ($Create -and -not (Test-Path -LiteralPath $full)) { New-Item -ItemType Directory -Path $full -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Purpose is missing: $full" }
    $item = Get-Item -LiteralPath $full -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Purpose must not be a reparse point." }
    return $full.TrimEnd('\')
}

function Assert-ChildPath {
    param([Parameter(Mandatory)][string]$Parent, [Parameter(Mandatory)][string]$Child, [Parameter(Mandatory)][string]$Purpose)
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)
    if (-not $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)) { throw "$Purpose escaped its fixed root." }
    return $childFull
}

function Get-Sha256Text {
    param([Parameter(Mandatory)][string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $hash = [System.Security.Cryptography.SHA256]::HashData($bytes)
    return ([Convert]::ToHexString($hash)).ToLowerInvariant()
}

function Read-TopLevelYamlValue {
    param([Parameter(Mandatory)][string]$Text, [Parameter(Mandatory)][string]$Name)
    $match = [regex]::Match($Text, '(?m)^' + [regex]::Escape($Name) + ':\s*([^#\r\n]*?)\s*(?:#.*)?$')
    if (-not $match.Success) { throw "March7th config is missing $Name." }
    return $match.Groups[1].Value.Trim()
}

function Assert-March7thBinding {
    param([Parameter(Mandatory)][string]$Root)
    $formalLauncher = Join-Path $Root 'March7th Launcher.exe'
    $command = Join-Path $Root 'March7th Assistant.exe'
    $config = Join-Path $Root 'config.yaml'
    foreach ($file in @($formalLauncher, $command, $config)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required March7th file is missing: $(Split-Path -Leaf $file)" }
        if (((Get-Item -LiteralPath $file -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "March7th binding file must not be a reparse point: $(Split-Path -Leaf $file)"
        }
    }
    $text = Get-Content -LiteralPath $config -Raw -Encoding UTF8
    $required = [ordered]@{
        instance_type = '拟造花萼（金）'
        use_reserved_trailblaze_power = 'false'
        after_finish = 'None'
        auto_set_game_path_enable = 'false'
        exit_after_failure = 'false'
        scheduled_run_enable = 'false'
        scheduled_tasks = '[]'
        daily_memory_one_enable = 'false'
        update_via_launcher = 'true'
        use_background_screenshot = 'true'
    }
    foreach ($entry in $required.GetEnumerator()) {
        if ((Read-TopLevelYamlValue -Text $text -Name $entry.Key) -cne $entry.Value) {
            throw "Unsafe March7th setting: $($entry.Key) must be $($entry.Value)."
        }
    }
    $gamePath = (Read-TopLevelYamlValue -Text $text -Name 'game_path').Trim("'", '"')
    if (-not [System.IO.Path]::IsPathRooted($gamePath) -or $gamePath.StartsWith('\\') -or -not (Test-Path -LiteralPath $gamePath -PathType Leaf)) {
        throw 'March7th game_path must name an existing local game executable.'
    }
    return [pscustomobject]@{
        FormalLauncherPath = $formalLauncher
        CommandPath = $command
        ConfigPath = $config
        FormalLauncherSha256 = (Get-FileHash -LiteralPath $formalLauncher -Algorithm SHA256).Hash.ToLowerInvariant()
        CommandSha256 = (Get-FileHash -LiteralPath $command -Algorithm SHA256).Hash.ToLowerInvariant()
        ConfigSha256 = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

$source = [System.IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$sourceCodeRoot = Join-Path $source 'adapter-host\starrail-runner'
$compatibilityPayload = Resolve-March7thCompatibilityPayload -Path $CompatibilityPayloadPath -SourceCodeRoot $sourceCodeRoot
foreach ($name in @('Protocol.cs', 'March7thTool.cs', 'DailyTaskListVerifier.cs', 'Program.cs')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceCodeRoot $name) -PathType Leaf)) { throw "Runner source is missing: $name" }
}
if (-not (Test-Path -LiteralPath $CSharpCompilerPath -PathType Leaf)) { throw 'The fixed .NET Framework C# compiler is unavailable.' }
$frameworkWpf = Join-Path (Split-Path -Parent $CSharpCompilerPath) 'WPF'
foreach ($assembly in @('UIAutomationClient.dll', 'UIAutomationTypes.dll', 'WindowsBase.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $frameworkWpf $assembly) -PathType Leaf)) {
        throw "The fixed .NET Framework UI Automation reference is unavailable: $assembly"
    }
}
$build = Resolve-LocalDirectory -Path $BuildRoot -Purpose 'Adapter build root' -Create
$localAppData = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not ($build + '\').StartsWith($localAppData, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Adapter build output must remain below LocalAppData.'
}
$tool = Resolve-LocalDirectory -Path $ToolRoot -Purpose 'March7th tool root'
$bindingEvidence = Assert-March7thBinding -Root $tool
$buildId = 'starrail-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')
$work = Assert-ChildPath -Parent $build -Child (Join-Path $build ('.work-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'local build workspace'
$snapshot = Join-Path $work 'source-snapshot'
$candidate = Join-Path $work 'candidate'
New-Item -ItemType Directory -Path $snapshot, $candidate -Force | Out-Null
foreach ($name in @('Protocol.cs', 'March7thTool.cs', 'DailyTaskListVerifier.cs', 'Program.cs')) {
    Copy-Item -LiteralPath (Join-Path $sourceCodeRoot $name) -Destination (Join-Path $snapshot $name)
}
$snapshotPayload = Join-Path $snapshot 'March7thAssistantBasePatch.b64'
Copy-Item -LiteralPath $compatibilityPayload -Destination $snapshotPayload
Resolve-March7thCompatibilityPayload -Path $snapshotPayload -SourceCodeRoot $sourceCodeRoot | Out-Null

$runner = Join-Path $candidate 'runner.exe'
$compilerArgs = @(
    '/nologo', '/target:exe', '/optimize+', '/platform:anycpu',
    ('/out:' + $runner),
    '/r:System.Web.Extensions.dll', '/r:System.Drawing.dll',
    ('/r:' + (Join-Path $frameworkWpf 'UIAutomationClient.dll')),
    ('/r:' + (Join-Path $frameworkWpf 'UIAutomationTypes.dll')),
    ('/r:' + (Join-Path $frameworkWpf 'WindowsBase.dll')),
    (Join-Path $snapshot 'Protocol.cs'), (Join-Path $snapshot 'March7thTool.cs'),
    (Join-Path $snapshot 'DailyTaskListVerifier.cs'), (Join-Path $snapshot 'Program.cs')
)
if ($TestBuild) { $compilerArgs = @('/define:YEYU_STARRAIL_TEST_BUILD') + $compilerArgs }
& $CSharpCompilerPath @compilerArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'StarRail runner compilation failed.' }
$patchPayload = Join-Path $candidate 'March7thAssistantBasePatch.b64'
Copy-Item -LiteralPath $snapshotPayload -Destination $patchPayload
Resolve-March7thCompatibilityPayload -Path $patchPayload -SourceCodeRoot $sourceCodeRoot | Out-Null

$binding = [ordered]@{
    schemaVersion = 1
    bindingId = 'starrail-march7th-v1'
    gameId = 'StarRail'
    tool = [ordered]@{
        root = $tool
        formalLauncher = 'March7th Launcher.exe'
        formalLauncherSha256 = $bindingEvidence.FormalLauncherSha256
        command = 'March7th Assistant.exe'
        commandSha256 = $bindingEvidence.CommandSha256
        config = 'config.yaml'
        configSha256 = $bindingEvidence.ConfigSha256
    }
    commands = [ordered]@{
        'attach-home' = [ordered]@{ task = 'game'; timeoutSeconds = 1800 }
        'spend-trailblaze-power' = [ordered]@{ task = 'power'; timeoutSeconds = 1200 }
        # A Currency Wars run can legitimately exceed half an hour.  Keep the
        # formal GUI task alive long enough to reach 500 points instead of
        # cutting it off mid-stage and turning visible progress into a retry.
        'daily-training-objectives' = [ordered]@{ task = 'daily'; timeoutSeconds = 3600 }
        'claim-daily-training-rewards' = [ordered]@{ task = 'daily'; timeoutSeconds = 3600 }
    }
    safety = [ordered]@{
        requiredInstanceType = '拟造花萼（金）'
        useReservedTrailblazePower = $false
        afterFinish = 'None'
        autoSetGamePath = $false
        preserveGameClient = $true
    }
}
$bindingPath = Join-Path $candidate 'tool-binding.json'
[System.IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 8 -Compress), [System.Text.UTF8Encoding]::new($false))

$files = @(
    [ordered]@{ path = 'runner.exe'; sha256 = (Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash.ToLowerInvariant(); sizeBytes = (Get-Item -LiteralPath $runner).Length },
    [ordered]@{ path = 'March7thAssistantBasePatch.b64'; sha256 = (Get-FileHash -LiteralPath $patchPayload -Algorithm SHA256).Hash.ToLowerInvariant(); sizeBytes = (Get-Item -LiteralPath $patchPayload).Length },
    [ordered]@{ path = 'tool-binding.json'; sha256 = (Get-FileHash -LiteralPath $bindingPath -Algorithm SHA256).Hash.ToLowerInvariant(); sizeBytes = (Get-Item -LiteralPath $bindingPath).Length }
)
function New-Binding([string]$HandlerId, [string]$ActionClass, [string]$DefinitionId, [int]$Timeout,
    [string[]]$EvidenceKinds, [string]$Risk = 'routine_action') {
    return [ordered]@{
        handlerId = $HandlerId
        mode = 'granular'
        actionClass = $ActionClass
        risk = $Risk
        todoDefinitionIds = @($DefinitionId)
        adapterCapabilityRefs = @('game.daily.run@1.0')
        supportsResume = $true
        timeoutSeconds = $Timeout
        requiredEvidenceKinds = @($EvidenceKinds)
    }
}
$pendingReplay = 'sha256:' + (Get-Sha256Text '{"status":"pending","suite":"replay"}')
$pendingShadow = 'sha256:' + (Get-Sha256Text '{"status":"pending","suite":"shadow"}')
$notRunCanary = 'sha256:' + (Get-Sha256Text '{"status":"not-run","suite":"canary"}')
$now = [DateTimeOffset]::UtcNow.ToString('o')
$manifest = [ordered]@{
    schemaVersion = 2
    packageId = 'legacy-night-rain-gamer'
    packageVersion = $PackageVersion
    buildId = $buildId
    builtAt = $now
    installedAt = $now
    protocolVersions = @('1.1')
    hostPackageId = 'manager-adapter-host'
    minHostVersion = '0.2.0'
    entryPoint = 'runner.exe'
    files = $files
    supportedGameIds = @('StarRail')
    operationBindings = [ordered]@{
        StarRail = [ordered]@{
            'attach-home' = New-Binding 'starrail.attach_home' 'session' 'todo.v1.starrail.daily.attach-home' 1800 @('tool-log-outcome', 'game-ui-main-window')
            'spend-trailblaze-power' = New-Binding 'starrail.spend_power' 'stamina' 'todo.v1.starrail.daily.spend-trailblaze-power' 1200 @('tool-log-outcome')
            'daily-training-objectives' = New-Binding 'starrail.daily_objectives' 'daily' 'todo.v1.starrail.daily.daily-training-objectives' 3600 @('tool-log-outcome', 'game-ui-daily-task-list')
            'claim-daily-training-rewards' = New-Binding 'starrail.claim_daily_rewards' 'reward' 'todo.v1.starrail.daily.claim-daily-training-rewards' 3600 @('tool-log-outcome', 'game-ui-daily-reward-raw', 'game-ui-daily-reward-watermarked')
            'verify-daily-task-list' = New-Binding 'starrail.verify_daily_task_list' 'evidence' 'todo.v1.starrail.daily.verify-daily-task-list' 60 @('tool-log-outcome', 'game-ui-daily-task-list') 'observe_only'
        }
    }
    forbiddenOperationClasses = @('account_settings', 'arbitrary_command', 'arbitrary_input', 'arbitrary_path', 'dismantle', 'enhance', 'gacha', 'irreversible_choice', 'purchase', 'pvp', 'trade')
    limits = [ordered]@{ maxRequestBytes = 262144; maxEventBytes = 65536; maxArtifactsPerTodo = 20 }
    artifactPolicy = [ordered]@{ allowedMimeTypes = @('image/png', 'text/plain'); maxArtifactBytes = 20971520 }
    security = [ordered]@{ allowsArbitraryCommand = $false; allowsArbitraryPath = $false; allowsArbitraryInput = $false }
    promotion = [ordered]@{ status = 'candidate'; replaySuiteDigest = $pendingReplay; shadowSuiteDigest = $pendingShadow; canarySuiteDigest = $notRunCanary; payloadDigest = ''; receiptFile = ''; receiptSha256 = ''; receiptResourceId = '' }
    executionReady = $false
}
$manifest.promotion.payloadDigest = Get-YeYuGamerPayloadDigest -Files @($manifest.files)
$manifest.operationBindings.StarRail.'spend-trailblaze-power'.supportsResume = $false
$manifestPath = Join-Path $candidate 'install-manifest.json'
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))

$final = Assert-ChildPath -Parent $build -Child (Join-Path $build 'starrail-candidate') -Purpose 'candidate output'
if (Test-Path -LiteralPath $final) {
    $previous = Assert-ChildPath -Parent $build -Child (Join-Path $build ('starrail-candidate.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous candidate'
    Move-Item -LiteralPath $final -Destination $previous
}
Move-Item -LiteralPath $candidate -Destination $final
if (Test-Path -LiteralPath $work) {
    $verifiedWork = Assert-ChildPath -Parent $build -Child $work -Purpose 'completed build workspace'
    Remove-Item -LiteralPath $verifiedWork -Recurse -Force
}
[pscustomobject]@{
    status = 'candidate-built'
    candidateRoot = $final
    packageVersion = $PackageVersion
    formalLauncherSha256 = $bindingEvidence.FormalLauncherSha256
    commandSha256 = $bindingEvidence.CommandSha256
    configSha256 = $bindingEvidence.ConfigSha256
    executionReady = $false
    gameStarted = $false
} | ConvertTo-Json -Compress
