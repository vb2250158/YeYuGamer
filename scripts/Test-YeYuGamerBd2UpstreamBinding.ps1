[CmdletBinding()]
param(
    [string]$ToolRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\MFABD2'),
    [string]$EvidencePath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\bd2-upstream-binding.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-LocalRoot([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not [IO.Path]::IsPathRooted($full) -or $full.StartsWith('\\') -or -not (Test-Path -LiteralPath $full -PathType Container)) {
        throw "$Purpose must be an existing local directory."
    }
    if (((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Purpose must not be a reparse point."
    }
    return $full
}

function Read-Json([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required upstream file is missing: $Path" }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
}

function Test-JsonMember([object]$Object, [string]$Name) {
    return $null -ne $Object.PSObject.Properties[$Name]
}

function Get-JsonMember([object]$Object, [string]$Name) {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

$root = Assert-LocalRoot $ToolRoot 'MFABD2 tool root'
$local = [IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\'
if (-not ($root + '\').StartsWith($local, [StringComparison]::OrdinalIgnoreCase)) { throw 'MFABD2 must be installed below LocalAppData.' }

$paths = [ordered]@{
    launcher = Join-Path $root 'MFAAvalonia.exe'
    interface = Join-Path $root 'interface.json'
    global = Join-Path $root 'resource\base\pipeline\Global.json'
    quest = Join-Path $root 'resource\base\pipeline\QuestList.json'
    pass = Join-Path $root 'resource\base\pipeline\Pass.json'
    battle = Join-Path $root 'resource\base\pipeline\Battle.json'
}
foreach ($path in $paths.Values) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required upstream file is missing: $path" }
}

$interface = Read-Json $paths.interface
$global = Read-Json $paths.global
$quest = Read-Json $paths.quest
$pass = Read-Json $paths.pass
$battle = Read-Json $paths.battle
$taskByEntry = @{}
foreach ($task in @($interface.task)) { $taskByEntry[[string]$task.entry] = $task }
foreach ($entry in @('RewardsDaily_Start','Pass_HomePage','QuickHunt_Start')) {
    if (-not $taskByEntry.ContainsKey($entry)) { throw "MFABD2 no longer exposes $entry." }
}
foreach ($entry in @('GachaADV_Start','PVP_TaskStart','Arbitrage_Start','Event_HomePage','Activities_Start','Mail_HomePage','Daily_HomePage')) {
    if (-not $taskByEntry.ContainsKey($entry)) { throw "Expected excluded upstream entry disappeared: $entry" }
}

foreach ($node in @('Global_ToHomePage','Global_ToHomePage_Enter','Rec_HomePage_GA_Ocr','Rec_HomePage_GA_Clr')) {
    if (-not (Test-JsonMember $global $node)) { throw "Home verifier node is missing: $node" }
}
$homeEntry = Get-JsonMember $global 'Global_ToHomePage_Enter'
if (@($homeEntry.all_of) -notcontains 'Rec_HomePage_GA_Ocr' -or
    @($homeEntry.all_of) -notcontains 'Rec_HomePage_GA_Clr') {
    throw 'The upstream home verifier no longer requires both OCR and color evidence.'
}
foreach ($node in @('RewardsDaily_Start','RewardsDaily_DailyReward','RewardsDaily_GetDailyReward','RewardsDaily_Get')) {
    if (-not (Test-JsonMember $quest $node)) { throw "Daily reward node is missing: $node" }
}
foreach ($node in @('Pass_HomePage','Pass_EnterPass','Pass_GetAll','Pass_Back')) {
    if (-not (Test-JsonMember $pass $node)) { throw "Pass reward node is missing: $node" }
}
$passText = Get-Content -LiteralPath $paths.pass -Raw -Encoding UTF8
if ($passText -match '购买|充值|付费|价格|￥|¥') { throw 'Pass pipeline contains a purchase/currency marker and must be re-reviewed.' }
foreach ($node in @('QuickHunt_Start','QuickHunt_HuntingGrounds','QuickHunt_AdventureRoute','QuickHunt_CrystalCave','QuickHunt_RiceRecheck_HuntingGrounds_Entry','QuickHunt_FastBattleReward','QuickHunt_NoRiceAP_Skip')) {
    if (-not (Test-JsonMember $battle $node)) { throw "Stamina pipeline node is missing: $node" }
}

$safeOverride = [ordered]@{
    QuickHunt_AdventureRoute = [ordered]@{ enabled = $false }
    QuickHunt_CrystalCave = [ordered]@{ enabled = $false }
    QuickHunt_HuntingGrounds = [ordered]@{ enabled = $true }
    QuickHunt_RiceRecheck_HuntingGrounds_Entry = [ordered]@{ enabled = $true }
}
$hashes = [ordered]@{}
foreach ($item in $paths.GetEnumerator()) { $hashes[$item.Key] = (Get-FileHash -LiteralPath $item.Value -Algorithm SHA256).Hash.ToLowerInvariant() }

$evidence = [ordered]@{
    schemaVersion = 1
    status = 'binding-audited-runtime-blocked'
    gameId = 'BD2'
    upstream = [ordered]@{
        repository = 'https://github.com/sunyink/MFABD2'
        release = 'v4.4.1'
        releaseArchiveSha256 = '798cb954ebad045dc7caf7304679ad33fcf9da24cecf29f3c1f79f321218ca8f'
        toolRoot = $root
        fileSha256 = $hashes
    }
    selectedTodoBindings = [ordered]@{
        'attach-home' = [ordered]@{ entries=@('Global_ToHomePage','Global_ToHomePage_Enter'); status='blocked'; reasonCode='live_home_replay_missing' }
        'daily-claim' = [ordered]@{ entries=@('RewardsDaily_Start','Pass_HomePage'); status='blocked'; reasonCode='selected_claim_runtime_receipt_missing' }
        'stamina-sweep' = [ordered]@{ entries=@('QuickHunt_Start'); pipelineOverride=$safeOverride; status='blocked'; reasonCode='free_rice_business_evidence_missing' }
    }
    excludedEntries = @('Daily_HomePage','GachaADV_Start','PVP_TaskStart','Arbitrage_Start','Event_HomePage','Activities_Start','Mail_HomePage')
    executionReady = $false
    promoted = $false
    toolProcessStarted = $false
    gameStarted = $false
}

$evidenceFull = [IO.Path]::GetFullPath($EvidencePath)
if (-not $evidenceFull.StartsWith($local, [StringComparison]::OrdinalIgnoreCase)) { throw 'Evidence must remain below LocalAppData.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $evidenceFull) -Force | Out-Null
[IO.File]::WriteAllText($evidenceFull, ($evidence | ConvertTo-Json -Depth 12 -Compress) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
$evidence | ConvertTo-Json -Depth 12 -Compress
