[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build'),
    [string]$ToolRoot = 'C:\Game\ok-nte',
    [string]$PackageVersion = '0.2.0-nte.5',
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
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Purpose must not be a reparse point."
    }
    return $full
}

function Assert-LocalFile {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Purpose)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\', [StringComparison]::Ordinal) -or -not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "$Purpose is missing or non-local: $full"
    }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Purpose must not be a reparse point."
    }
    return $full
}

function Assert-ChildPath {
    param([Parameter(Mandatory)][string]$Parent, [Parameter(Mandatory)][string]$Child, [Parameter(Mandatory)][string]$Purpose)
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)
    if (-not $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)) { throw "$Purpose escaped its fixed root." }
    return $childFull
}

function Hash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$source = [System.IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$runnerSource = Assert-LocalFile (Join-Path $source 'adapter-host\nte-runner\Program.cs') 'NTE runner source'
$bridgeSource = Assert-LocalFile (Join-Path $source 'adapter-host\nte-runner\NteYeYuBridge.py') 'NTE run-scoped bridge source'
$compiler = Assert-LocalFile $CSharpCompilerPath 'C# compiler'
$tool = Resolve-LocalDirectory -Path $ToolRoot -Purpose 'formal ok-nte root'
$formalLauncher = Assert-LocalFile (Join-Path $tool 'ok-nte.exe') 'ok-nte formal GUI entry'
$appState = Assert-LocalFile (Join-Path $tool 'data\apps\ok-nte\app.json') 'ok-nte formal update state'
$python = Assert-LocalFile (Join-Path $tool 'data\apps\ok-nte\python\python.exe') 'ok-nte managed Python runtime'
$pythonw = Assert-LocalFile (Join-Path $tool 'data\apps\ok-nte\python\pythonw.exe') 'ok-nte managed GUI Python runtime'
$guiEntry = Assert-LocalFile (Join-Path $tool 'data\apps\ok-nte\working\main.py') 'ok-nte managed GUI entry'
$dailyTask = Assert-LocalFile (Join-Path $tool 'data\apps\ok-nte\working\src\tasks\DailyTask.py') 'ok-nte upstream DailyTask'

$state = Get-Content -LiteralPath $appState -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([string]$state.current_version -cne 'v1.3.11' -or [string]$state.current_profile -cne 'China' -or
    -not [bool]$state.installed -or [string]$state.update_method -cne 'AUTO_UPDATE') {
    throw 'ok-nte formal installation is not the audited v1.3.11 China AUTO_UPDATE profile.'
}
$pythonVersion = (& $python --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^Python 3\.12\.') { throw 'ok-nte managed runtime must be Python 3.12.' }
$taskText = Get-Content -LiteralPath $dailyTask -Raw -Encoding UTF8
foreach ($required in @('class DailyTask', 'def do_run(', 'def claim_mail(', 'def check_activity(',
    'def complete_daily_activities(', 'def claim_activity_rewards(', 'def claim_battle_pass_rewards(')) {
    if (-not $taskText.Contains($required)) { throw "ok-nte upstream DailyTask identity is missing: $required" }
}
$bridgeText = Get-Content -LiteralPath $bridgeSource -Raw -Encoding UTF8
foreach ($required in @('YEYU_GAMER_STAGE_FILE', 'YEYU_GAMER_SELECTED_OPERATIONS', 'YEYU_GAMER_NTE_PROFILE',
    'attach-world', 'claim-mail', 'inspect-daily-progress', 'spend-urban-vitality',
    'claim-daily-reward', 'claim-period-reward', 'def install()')) {
    if (-not $bridgeText.Contains($required)) { throw "NTE run-scoped bridge identity is missing: $required" }
}
& $python -c "import ast,pathlib,sys; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8'), filename=p) for p in sys.argv[1:]]" $dailyTask $bridgeSource
if ($LASTEXITCODE -ne 0) { throw 'NTE upstream task or run-scoped bridge is not valid Python.' }

$build = Resolve-LocalDirectory -Path $BuildRoot -Purpose 'Adapter build root' -Create
$localAppData = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not ($build + '\').StartsWith($localAppData, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Adapter build output must remain below LocalAppData.'
}
$work = Assert-ChildPath -Parent $build -Child (Join-Path $build ('.nte-work-' + [Guid]::NewGuid().ToString('N'))) -Purpose 'NTE build workspace'
$candidate = Join-Path $work 'candidate'
New-Item -ItemType Directory -Path $candidate -Force | Out-Null
$runner = Join-Path $candidate 'runner.exe'
$uiaClient = Assert-LocalFile (Join-Path $env:WINDIR 'Microsoft.NET\assembly\GAC_MSIL\UIAutomationClient\v4.0_4.0.0.0__31bf3856ad364e35\UIAutomationClient.dll') 'UI Automation client reference'
$uiaTypes = Assert-LocalFile (Join-Path $env:WINDIR 'Microsoft.NET\assembly\GAC_MSIL\UIAutomationTypes\v4.0_4.0.0.0__31bf3856ad364e35\UIAutomationTypes.dll') 'UI Automation types reference'
& $compiler /nologo /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/r:$uiaClient") ("/r:$uiaTypes") ("/out:$runner") $runnerSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'NTE runner compilation failed.' }
$bridge = Join-Path $candidate 'NteYeYuBridge.py'
Copy-Item -LiteralPath $bridgeSource -Destination $bridge

$binding = [ordered]@{
    schemaVersion = 1
    bindingId = 'nte-local-selected-daily-v1'
    gameId = 'NTE'
    upstream = [ordered]@{
        repository = 'https://github.com/BnanZ0/ok-nte'
        auditedRelease = 'v1.3.11'
        auditedCommit = '44de38e583e72f1ba921f2df2eb0e9cec6d4ab81'
        activeToolVersion = [string]$state.current_version
    }
    tool = [ordered]@{
        root = $tool
        formalLauncherSha256 = Hash $formalLauncher
    }
}
$bindingPath = Join-Path $candidate 'tool-binding.json'
[System.IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 8 -Compress), [System.Text.UTF8Encoding]::new($false))

$operation = {
    param($handler, $class, $definition, $timeout)
    [ordered]@{
        handlerId = $handler
        mode = 'granular'
        actionClass = $class
        risk = 'routine_action'
        todoDefinitionIds = @($definition)
        adapterCapabilityRefs = @('game.daily.run@1.0')
        supportsResume = $false
        timeoutSeconds = $timeout
        requiredEvidenceKinds = @('tool-log-outcome')
    }
}
$manifest = [ordered]@{
    schemaVersion = 2
    packageId = 'legacy-night-rain-gamer'
    packageVersion = $PackageVersion
    buildId = 'nte-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')
    builtAt = [DateTimeOffset]::UtcNow.ToString('o')
    installedAt = [DateTimeOffset]::UtcNow.ToString('o')
    protocolVersions = @('1.1')
    hostPackageId = 'manager-adapter-host'
    minHostVersion = '0.3.0'
    entryPoint = 'runner.exe'
    files = @(
        [ordered]@{ path = 'runner.exe'; sha256 = Hash $runner; sizeBytes = (Get-Item -LiteralPath $runner).Length },
        [ordered]@{ path = 'NteYeYuBridge.py'; sha256 = Hash $bridge; sizeBytes = (Get-Item -LiteralPath $bridge).Length },
        [ordered]@{ path = 'tool-binding.json'; sha256 = Hash $bindingPath; sizeBytes = (Get-Item -LiteralPath $bindingPath).Length }
    )
    supportedGameIds = @('NTE')
    operationBindings = [ordered]@{
        NTE = [ordered]@{
            'attach-home' = (& $operation 'nte.attach_home' 'session' 'todo.v1.nte.daily.attach-home' 300)
            'mail' = (& $operation 'nte.daily.mail' 'mail' 'todo.v1.nte.daily.mail' 300)
            'daily-activity' = (& $operation 'nte.daily.inspect_activity' 'daily' 'todo.v1.nte.daily.daily-activity' 300)
            'spend-city-vitality' = (& $operation 'nte.daily.spend_vitality' 'stamina' 'todo.v1.nte.daily.spend-city-vitality' 1800)
            'claim-activity-reward' = (& $operation 'nte.daily.claim_activity' 'reward' 'todo.v1.nte.daily.claim-activity-reward' 300)
            'claim-cycle-reward' = (& $operation 'nte.daily.claim_cycle' 'reward' 'todo.v1.nte.daily.claim-cycle-reward' 300)
        }
    }
    forbiddenOperationClasses = @('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','arbitrary_command','arbitrary_path','arbitrary_input')
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

$final = Assert-ChildPath -Parent $build -Child (Join-Path $build 'nte-candidate') -Purpose 'NTE candidate output'
if (Test-Path -LiteralPath $final) {
    $previous = Assert-ChildPath -Parent $build -Child (Join-Path $build ('nte-candidate.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) -Purpose 'previous NTE candidate'
    Move-Item -LiteralPath $final -Destination $previous
}
Move-Item -LiteralPath $candidate -Destination $final
if (Test-Path -LiteralPath $work) {
    $verifiedWork = Assert-ChildPath -Parent $build -Child $work -Purpose 'completed NTE build workspace'
    Remove-Item -LiteralPath $verifiedWork -Recurse -Force
}
[pscustomobject]@{
    status = 'candidate-built'
    candidateRoot = $final
    packageVersion = $PackageVersion
    activeToolVersion = [string]$state.current_version
    auditedUpstreamRelease = 'v1.3.11'
    pythonVersion = $pythonVersion
    executionReady = $false
    gameStarted = $false
} | ConvertTo-Json -Compress
