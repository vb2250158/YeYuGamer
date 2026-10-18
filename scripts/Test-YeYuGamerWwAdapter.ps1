[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path $SourceRoot '.cache\adapter-build\local-daily-candidate'),
    [string]$PythonPath = 'D:\Game\Applications\YeYuGamer\.venv\Scripts\python.exe',
    [string]$CSharpCompilerPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
    [string]$EvidenceDirectory = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PYTHONDONTWRITEBYTECODE = '1'
function Resolve-OpenKuroPythonwPath {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$BoundGuiRuntime, [AllowEmptyString()][string]$LocalAppData)

    if (-not [string]::IsNullOrWhiteSpace($BoundGuiRuntime)) {
        $boundDirectory = Split-Path -Parent $BoundGuiRuntime
        if ([string]::IsNullOrWhiteSpace($boundDirectory)) { throw 'Bound GUI runtime has no parent directory.' }
        $boundPythonw = Join-Path $boundDirectory 'pythonw.exe'
        if (Test-Path -LiteralPath $boundPythonw -PathType Leaf) { return $boundPythonw }
    }
    if (-not [string]::IsNullOrWhiteSpace($LocalAppData)) {
        $localPythonw = Join-Path $LocalAppData 'Programs\Python\Python312\pythonw.exe'
        if (Test-Path -LiteralPath $localPythonw -PathType Leaf) { return $localPythonw }
    }
    throw 'pythonw.exe path is unavailable: no existing bound or local runtime executable.'
}

$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\Gf2SourceRegistry.ps1')
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$candidate = [IO.Path]::GetFullPath($CandidateRoot)
if ($candidate.StartsWith('\\') -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'OpenKuro candidate must be an existing local directory.' }
$runner = Join-Path $candidate 'runner.exe'
$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
$toolBinding = Get-Content -LiteralPath (Join-Path $candidate 'tool-binding.json') -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
$expected = [ordered]@{
    WW=@('attach-world','inspect-daily-progress','farm-nightmare-daily-echo','spend-waveplates','claim-daily-reward','claim-mail','claim-battle-pass')
    Endfield=@('attach-world','mail','spend-sanity','delivery-commission','collect-credit','dijiang-harvest','claim-daily-reward')
    GF2=@('attach-home','mail','activity-supply','star-wish','event-battle','exploration-claim','public-area-dispatch','spend-stamina','squad-tasks','claim-daily-missions','battle-pass-free-track')
}
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady -or
    ((@($manifest.supportedGameIds | Sort-Object) -join ',') -cne (@($expected.Keys | Sort-Object) -join ','))) {
    throw 'OpenKuro tests may update only the exact-scope unpromoted candidate.'
}
foreach ($gameId in $expected.Keys) {
    if ((@($manifest.operationBindings.$gameId.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($expected[$gameId] | Sort-Object) -join ',')) {
        throw "$gameId candidate operation contract is invalid."
    }
}
$expectedOperationCount = 25
$actualOperationCount = ($expected.Values | ForEach-Object { @($_).Count } | Measure-Object -Sum).Sum
if ($actualOperationCount -ne $expectedOperationCount) { throw "OpenKuro candidate stage count is $actualOperationCount, expected $expectedOperationCount." }
if ([string]$manifest.promotion.payloadDigest -cne (Get-YeYuGamerPayloadDigest -Files @($manifest.files))) { throw 'OpenKuro candidate payload digest is invalid.' }

$evidenceRoot = if ([string]::IsNullOrWhiteSpace($EvidenceDirectory)) {
    Join-Path $SourceRoot ('.cache\adapter-build\reports\openkuro-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))
} else { [IO.Path]::GetFullPath($EvidenceDirectory) }
if ($evidenceRoot.StartsWith('\\')) { throw 'OpenKuro evidence directory must be local.' }
New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null

$validator = Join-Path $source 'adapter-host\tests\validate_openkuro_selected_daily.py'
$endfieldProfileReplaySource = Join-Path $source 'adapter-host\tests\EndfieldProfileReplay.cs'
$gf2ProfileReplaySource = Join-Path $source 'adapter-host\tests\Gf2ProfileReplay.cs'
$wwOfficialCandidateReplaySource = Join-Path $source 'adapter-host\tests\WwOfficialSourceCandidateReplay.cs'
$endfieldOfficialCandidateReplaySource = Join-Path $source 'adapter-host\tests\EndfieldOfficialSourceCandidateReplay.cs'
$gf2OfficialCandidateReplaySource = Join-Path $source 'adapter-host\tests\Gf2OfficialSourceCandidateReplay.cs'
$gf2MainObserver = Join-Path $source 'adapter-host\tests\validate_gf2_main_daily_observer.py'
$compiler = [IO.Path]::GetFullPath($CSharpCompilerPath)
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf) -or -not (Test-Path -LiteralPath $endfieldProfileReplaySource -PathType Leaf) -or -not (Test-Path -LiteralPath $gf2ProfileReplaySource -PathType Leaf) -or -not (Test-Path -LiteralPath $wwOfficialCandidateReplaySource -PathType Leaf) -or -not (Test-Path -LiteralPath $endfieldOfficialCandidateReplaySource -PathType Leaf) -or -not (Test-Path -LiteralPath $gf2OfficialCandidateReplaySource -PathType Leaf) -or -not (Test-Path -LiteralPath $gf2MainObserver -PathType Leaf)) {
    throw 'OpenKuro source-candidate replay prerequisites are missing.'
}
$staticOutput = & $PythonPath -X utf8 $validator 2>&1
if ($LASTEXITCODE -ne 0) { throw "OpenKuro static selected-Todo suite failed: $($staticOutput -join ' ')" }
$wwObserverOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_ww_passive_observer.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "WW passive observer replay failed: $($wwObserverOutput -join ' ')" }
$wwLoginInputOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\test_ww_login_input_observer.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "WW login input observation replay failed: $($wwLoginInputOutput -join ' ')" }
$wwAccountObserverOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_ww_account_bridge.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "WW official MultiAccount observer replay failed: $($wwAccountObserverOutput -join ' ')" }
$wwOfficialSourceOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_ww_conditional_stages.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "WW official DailyTask source check failed: $($wwOfficialSourceOutput -join ' ')" }
$methodObserverOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_openkuro_daily_method_observers.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "OpenKuro method observer replay failed: $($methodObserverOutput -join ' ')" }
$gf2PendingOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\test_gf2_pending_outcome.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "GF2 uncertain outcome replay failed: $($gf2PendingOutput -join ' ')" }
$gf2ReadonlyOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\test_gf2_readonly_observer.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "GF2 readonly observation replay failed: $($gf2ReadonlyOutput -join ' ')" }
# The upstream-log archive contract covers both runners and the Manager side of
# the run-scoped diagnostic, so it runs against the maintenance source instead
# of the installed package.
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path $source 'backend') + ';' + (Join-Path $source 'platform')
    $upstreamLogProtocolOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_upstream_log_protocol.py') --runner OpenKuro 2>&1
} finally { $env:PYTHONPATH = $previousPythonPath }
if ($LASTEXITCODE -ne 0) { throw "Upstream log archive protocol replay failed: $($upstreamLogProtocolOutput -join ' ')" }
$idleLogoutOutput = & $PythonPath -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_endfield_idle_logout.py') 2>&1
if ($LASTEXITCODE -ne 0) { throw "Endfield idle logout replay failed: $($idleLogoutOutput -join ' ')" }
$credentialGateOutput = & $PythonPath -B -X utf8 -m unittest discover -s (Join-Path $source 'adapter-host\tests') -p 'test_endfield_credential_gate.py' -v 2>&1
if ($LASTEXITCODE -ne 0) { throw 'Endfield credential gate replay failed.' }
$endfieldReplayRoot = Join-Path $SourceRoot ('.cache\adapter-test\endfield-profile-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $endfieldReplayRoot -Force | Out-Null
try {
    $atomicConfigReplay = Join-Path $endfieldReplayRoot 'atomic-config-replay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /r:System.Web.Extensions.dll ("/out:$atomicConfigReplay") (Join-Path $source 'adapter-host\tests\ConfigAtomicReplaceReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Atomic configuration replacement replay compilation failed.' }
    & $atomicConfigReplay $runner (Join-Path $endfieldReplayRoot 'atomic-config')
    if ($LASTEXITCODE -ne 0) { throw 'Atomic configuration lock recovery or preservation failed.' }
    $endfieldReplay = Join-Path $endfieldReplayRoot 'EndfieldProfileReplay.exe'
    $endfieldDailyConfig = Join-Path $endfieldReplayRoot 'DailyTask.json'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$endfieldReplay") $endfieldProfileReplaySource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Endfield profile replay compilation failed.' }
    & $endfieldReplay $runner $endfieldDailyConfig
    if ($LASTEXITCODE -ne 0) { throw 'Endfield official profile injection replay failed.' }
    $gf2ProfileReplay = Join-Path $endfieldReplayRoot 'Gf2ProfileReplay.exe'
    $gf2DailyConfig = Join-Path $endfieldReplayRoot 'Gf2-DailyTask.json'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$gf2ProfileReplay") $gf2ProfileReplaySource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GF2 profile replay compilation failed.' }
    & $gf2ProfileReplay $runner $gf2DailyConfig
    if ($LASTEXITCODE -ne 0) { throw 'GF2 official profile injection replay failed.' }
    $gf2InspectionReplay = Join-Path $endfieldReplayRoot 'gf2-reward-inspection-replay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu ("/out:$gf2InspectionReplay") (Join-Path $source 'adapter-host\tests\Gf2RewardInspectionReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GF2 readonly inspection replay compilation failed.' }
    & $gf2InspectionReplay $runner
    if ($LASTEXITCODE -ne 0) { throw 'GF2 readonly inspection accepted an invalid frozen scope.' }
    $wwOfficialCandidateReplay = Join-Path $endfieldReplayRoot 'WwOfficialSourceCandidateReplay.exe'
    $wwOfficialCandidateRoot = Join-Path $endfieldReplayRoot 'ww-official-source-candidate'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$wwOfficialCandidateReplay") $wwOfficialCandidateReplaySource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'WW official-source candidate replay compilation failed.' }
    & $wwOfficialCandidateReplay $wwOfficialCandidateRoot $runner
    if ($LASTEXITCODE -ne 0) { throw 'WW official-source candidate fixed-layout replay failed.' }
    $wwSelectionReplay = Join-Path $endfieldReplayRoot 'ww-daily-selection-replay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu ("/out:$wwSelectionReplay") (Join-Path $source 'adapter-host\tests\WwDailySelectionReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'WW daily selection replay compilation failed.' }
    & $wwSelectionReplay $runner | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'WW partial recovery reached the whole official daily.' }
    $endfieldOfficialCandidateReplay = Join-Path $endfieldReplayRoot 'EndfieldOfficialSourceCandidateReplay.exe'
    $endfieldOfficialCandidateRoot = Join-Path $endfieldReplayRoot 'official-source-candidate'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$endfieldOfficialCandidateReplay") $endfieldOfficialCandidateReplaySource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Endfield official-source candidate replay compilation failed.' }
    & $endfieldOfficialCandidateReplay $endfieldOfficialCandidateRoot $runner
    if ($LASTEXITCODE -ne 0) { throw 'Endfield official-source candidate fixed-layout replay failed.' }
    $gf2OfficialCandidateReplay = Join-Path $endfieldReplayRoot 'Gf2OfficialSourceCandidateReplay.exe'
    $gf2OfficialCandidateRoot = Join-Path $endfieldReplayRoot 'gf2-official-source-candidate'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$gf2OfficialCandidateReplay") $gf2OfficialCandidateReplaySource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GF2 official-source candidate replay compilation failed.' }
    & $gf2OfficialCandidateReplay $gf2OfficialCandidateRoot $runner
    if ($LASTEXITCODE -ne 0) { throw 'GF2 official-source candidate fixed-layout replay failed.' }
    $gf2ActualRoot = Join-Path ([string]$toolBinding.tools.GF2.root) 'GF2\source'
    $gf2ActualRecordPath = Join-Path ([string]$toolBinding.tools.GF2.root) 'GF2\runtime-preparation.json'
    if (Test-Path -LiteralPath $gf2ActualRecordPath -PathType Leaf) {
        $gf2ActualRecord = Get-Content -LiteralPath $gf2ActualRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($gf2ActualRecord.candidateKind -ceq 'isolated-local-gf2-source-patch-with-formal-runtime-binding') {
            & $gf2OfficialCandidateReplay (Join-Path $endfieldReplayRoot 'gf2-local-source-candidate') $runner $gf2ActualRoot
            if ($LASTEXITCODE -ne 0) { throw 'GF2 actual local source and tampering replay failed.' }
        }
    }
    $consoleReplay = Join-Path $endfieldReplayRoot 'OpenKuroConsoleReplay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$consoleReplay") (Join-Path $source 'adapter-host\tests\OpenKuroConsoleReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'OpenKuro console replay compilation failed.' }
    & $consoleReplay $runner (Join-Path $endfieldReplayRoot 'console.jsonl')
    if ($LASTEXITCODE -ne 0) { throw 'OpenKuro console output replay failed.' }
    $recognitionReplay = Join-Path $endfieldReplayRoot 'openkuro-recognition-replay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$recognitionReplay") (Join-Path $source 'adapter-host\tests\OpenKuroRecognitionReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'OpenKuro recognition replay compilation failed.' }
    & $recognitionReplay $runner (Join-Path $endfieldReplayRoot 'recognition')
    if ($LASTEXITCODE -ne 0) { throw 'OpenKuro original metadata mapping or inspection scope replay failed.' }
    $startupReplay = Join-Path $endfieldReplayRoot 'openkuro-startup-replay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$startupReplay") (Join-Path $source 'adapter-host\tests\OpenKuroStartupReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'OpenKuro startup replay compilation failed.' }
    & $startupReplay $runner (Join-Path $endfieldReplayRoot 'startup')
    if ($LASTEXITCODE -ne 0) { throw 'OpenKuro startup/official-step separation failed.' }
    $endfieldScopeReplay = Join-Path $endfieldReplayRoot 'endfield-config-replay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu ("/out:$endfieldScopeReplay") (Join-Path $source 'adapter-host\tests\EndfieldConfigReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Endfield selection replay compilation failed.' }
    $endfieldOfficialSource = Split-Path -Parent ([string]$toolBinding.tools.Endfield.guiEntry)
    & $PythonPath -X utf8 -m unittest discover -s (Join-Path $source 'adapter-host\tests') -p 'test_endfield_reward_status_observer.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Endfield reward inspection observation changed an official result or input.' }
    if (Test-Path -LiteralPath (Join-Path $endfieldOfficialSource 'src\tasks\onetime\RewardStatusInspectionTask.py')) {
        $inspectionReplay = Join-Path $endfieldReplayRoot 'endfield-inspection-replay.exe'
        & $compiler /nologo /codepage:65001 /target:exe /r:System.Web.Extensions.dll ("/out:$inspectionReplay") (Join-Path $source 'adapter-host\tests\EndfieldRewardInspectionReplay.cs') | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Endfield inspection replay compilation failed.' }
        & $inspectionReplay $runner ([string]$toolBinding.tools.Endfield.root)
        if ($LASTEXITCODE -ne 0) { throw 'Endfield inspection fixed source or scope replay failed.' }
    }
    if (Test-Path -LiteralPath (Join-Path $endfieldOfficialSource 'src\tasks\daily\daily_feature.py')) {
        $splitReplay = Join-Path $endfieldReplayRoot 'endfield-split-config-replay.exe'
        & $compiler /nologo /codepage:65001 /target:exe /r:System.Web.Extensions.dll ("/out:$splitReplay") (Join-Path $source 'adapter-host\tests\EndfieldSplitConfigReplay.cs') | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Endfield split-config replay compilation failed.' }
        & $PythonPath -X utf8 (Join-Path $source 'adapter-host\tests\validate_endfield_split_config.py') $splitReplay $runner $endfieldOfficialSource
        if ($LASTEXITCODE -ne 0) { throw 'Endfield split configuration scope or recovery replay failed.' }
        $previousEndfieldSource = $env:YEYU_GAMER_ENDFIELD_SOURCE
        try {
            $env:YEYU_GAMER_ENDFIELD_SOURCE = $endfieldOfficialSource
            & $PythonPath -X utf8 -m unittest discover -s (Join-Path $source 'adapter-host\tests') -p 'test_endfield_split_observer.py' -v
            if ($LASTEXITCODE -ne 0) { throw 'Endfield split observer replay failed.' }
        } finally { $env:YEYU_GAMER_ENDFIELD_SOURCE = $previousEndfieldSource }
    } else {
        & $PythonPath -X utf8 (Join-Path $source 'adapter-host\tests\validate_endfield_scope.py') $endfieldScopeReplay $runner $endfieldOfficialSource
        if ($LASTEXITCODE -ne 0) { throw 'Endfield selection reached an unselected official plan entry.' }
    }
    $pythonw = Resolve-OpenKuroPythonwPath -BoundGuiRuntime ([string]$toolBinding.tools.Endfield.guiRuntime) -LocalAppData $env:LOCALAPPDATA
    $pythonwReplay = Join-Path $endfieldReplayRoot 'PythonwStdioReplay.exe'
    & $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$pythonwReplay") (Join-Path $source 'adapter-host\tests\PythonwStdioReplay.cs') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'pythonw stdio replay compilation failed.' }
    & $pythonwReplay $runner $pythonw (Join-Path $endfieldReplayRoot 'pythonw-stdio')
    if ($LASTEXITCODE -ne 0) { throw 'pythonw stdio bootstrap replay failed.' }
} finally {
    if (Test-Path -LiteralPath $endfieldReplayRoot) { Remove-Item -LiteralPath $endfieldReplayRoot -Recurse -Force }
}
$wwRuntime = if ($toolBinding.tools.WW.bindingKind -ceq 'pyappify-v1') {
    Join-Path ([string]$toolBinding.tools.WW.root) 'data\apps\ok-ww\python\python.exe'
} else {
    Join-Path (Split-Path -Parent ([string]$toolBinding.tools.WW.guiRuntime)) 'python.exe'
}
if (-not (Test-Path -LiteralPath $wwRuntime -PathType Leaf)) { throw 'The bound WW replay Python runtime is unavailable.' }
$previousWwPythonPath = $env:PYTHONPATH
try {
    if ($toolBinding.tools.WW.bindingKind -ceq 'pyappify-v1') {
        $env:PYTHONPATH = Split-Path -Parent (Split-Path -Parent ([string]$toolBinding.tools.WW.dailyConfig))
    }
    & $wwRuntime -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_gf2_step_frames.py') WW
    if ($LASTEXITCODE -ne 0) { throw 'WW official method-boundary screenshot replay failed.' }
} finally { $env:PYTHONPATH = $previousWwPythonPath }
$endfieldRuntime = Join-Path (Split-Path -Parent ([string]$toolBinding.tools.Endfield.guiRuntime)) 'python.exe'
& $endfieldRuntime -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_gf2_step_frames.py') Endfield
if ($LASTEXITCODE -ne 0) { throw 'Endfield official step-boundary PNG verification failed.' }
& $endfieldRuntime -B -X utf8 (Join-Path $source 'adapter-host\tests\test_endfield_recognition_timeline.py')
if ($LASTEXITCODE -ne 0) { throw 'Endfield bounded native recognition timeline verification failed.' }
& $endfieldRuntime -B -X utf8 (Join-Path $source 'adapter-host\tests\validate_official_task_exit.py')
if ($LASTEXITCODE -ne 0) { throw 'Endfield bound official failure exit lifecycle failed.' }
$gf2Root = [IO.Path]::GetFullPath([string]$toolBinding.tools.GF2.root)
$gf2MainCandidateManifest = Join-Path $gf2Root 'GF2\runtime-preparation.json'
$gf2MainObserverReplay = $false
$gf2MainObserverOutput = ''
if (Test-Path -LiteralPath $gf2MainCandidateManifest -PathType Leaf) {
    $gf2Record = Get-Content -LiteralPath $gf2MainCandidateManifest -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $gf2Source = $gf2Record.source
    $gf2Entry = $gf2Record.entry
    $gf2Python = Join-Path $gf2Root 'r\Scripts\python.exe'
    & $gf2Python -X utf8 (Join-Path $source 'adapter-host\tests\validate_gf2_step_frames.py')
    if ($LASTEXITCODE -ne 0) { throw 'GF2 official step-boundary PNG verification failed.' }
    $gf2Working = Join-Path $gf2Root 'GF2\source'
    $gf2Release1277 = $gf2Source.repository -ceq 'https://github.com/AliceJump/ok-gf2.git' -and
        $gf2Source.commit -ceq '5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42' -and $gf2Source.tag -ceq 'v1.2.77'
    $gf2ApprovedCandidate = Test-YeYuGf2ApprovedSource -RegistryPath (Join-Path $candidate 'gf2-source-candidates.json') -Manifest $gf2Record -WorkingRoot $gf2Working
    if ($gf2Record.schemaVersion -ne 1 -or $gf2Record.status -cne 'read-only-import-ready' -or
        ($gf2Record.candidateKind -cne 'isolated-official-gf2-source' -and !$gf2ApprovedCandidate) -or
        (!$gf2Release1277 -and !$gf2ApprovedCandidate -and ($gf2Source.repository -cne 'https://cnb.cool/ok-oldking/ok-gf2.git' -or
        $gf2Source.commit -cnotin @('4122e01e8a69063ae37807f316f2ae8f80eeedb1', '13a569429b9b0b50db06e95b0cb1f65bf54a38b7'))) -or
        $gf2Source.sourceDirectory -cne 'GF2/source' -or
        $gf2Entry.sourceEntry -cne 'main.py' -or $gf2Entry.arguments -cne 'main.py --task 1 --exit' -or
        ([IO.Path]::GetFullPath([string]$gf2Entry.workingDirectory) -cne $gf2Working) -or
        -not (Test-Path -LiteralPath $gf2Python -PathType Leaf)) { throw 'GF2 bound source candidate is not the audited main layout.' }
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $gf2Working
        $gf2MainObserverOutput = & $gf2Python -B -X utf8 $gf2MainObserver --source $gf2Working 2>&1
    } finally { $env:PYTHONPATH = $previousPythonPath }
    if ($LASTEXITCODE -ne 0) { throw "GF2 main passive observer replay failed: $($gf2MainObserverOutput -join ' ')" }
    $gf2MainObserverReplay = $true
}
$replayReport = Join-Path $evidenceRoot 'selected-todo-contract.json'
[IO.File]::WriteAllText($replayReport, ([ordered]@{
    schemaVersion=1; suite='openkuro-selected-todo-contract'; passed=$true; games=@($expected.Keys);
    operationCount=$actualOperationCount; expectedOperationCount=$expectedOperationCount;
    validatorOutput=($staticOutput -join "`n"); wwObserverOutput=($wwObserverOutput -join "`n"); wwAccountBridgeOutput=($wwAccountObserverOutput -join "`n");
    methodObserverOutput=($methodObserverOutput -join "`n"); endfieldCredentialGateOutput=($credentialGateOutput -join "`n"); wwOfficialSourceCandidateReplay=$true; endfieldProfileReplay=$true; endfieldOfficialSourceCandidateReplay=$true;
    gf2ProfileReplay=$true; gf2OfficialSourceCandidateReplay=$true; gf2MainObserverReplay=$gf2MainObserverReplay; gf2MainObserverOutput=($gf2MainObserverOutput -join "`n"); gameStarted=$false
} | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))

$before = @(Get-Process -Name 'Wuthering Waves','Endfield','GF2','ok-ww','ok-ef','ok-gf2' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$probeOutput = & $runner --probe-binding
if ($LASTEXITCODE -ne 0) { throw 'OpenKuro fixed binding shadow probe failed.' }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
$after = @(Get-Process -Name 'Wuthering Waves','Endfield','GF2','ok-ww','ok-ef','ok-gf2' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if (-not $probe.ok -or $probe.processStarted -or @($before).Count -ne @($after).Count) { throw 'OpenKuro shadow probe started a process or returned an invalid result.' }
$shadowReport = Join-Path $evidenceRoot 'binding-shadow.json'
[IO.File]::WriteAllText($shadowReport, ([ordered]@{
    schemaVersion=1; suite='openkuro-binding-shadow'; passed=$true; processStarted=$false;
    beforeProcessCount=@($before).Count; afterProcessCount=@($after).Count; probe=$probe
} | ConvertTo-Json -Depth 8 -Compress), [Text.UTF8Encoding]::new($false))

$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayReport -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowReport -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
foreach ($gameId in $expected.Keys) {
    $summary = [ordered]@{
        schemaVersion=1; resourceType='adapter-candidate-test-evidence'; status='passed'; passed=$true;
        packageId=[string]$manifest.packageId; packageVersion=[string]$manifest.packageVersion; buildId=[string]$manifest.buildId;
        supportedGameIds=@($gameId); payloadDigest=[string]$manifest.promotion.payloadDigest;
        replaySuiteDigest=[string]$manifest.promotion.replaySuiteDigest; shadowSuiteDigest=[string]$manifest.promotion.shadowSuiteDigest;
        generatedAt=[DateTimeOffset]::UtcNow.ToString('o'); gameStarted=$false
    }
    [IO.File]::WriteAllText((Join-Path $evidenceRoot ($gameId.ToLowerInvariant() + '.json')), ($summary | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}
[pscustomobject]@{status='passed';evidenceDirectory=$evidenceRoot;gameIds=@($expected.Keys);gameStarted=$false} | ConvertTo-Json -Depth 4 -Compress
