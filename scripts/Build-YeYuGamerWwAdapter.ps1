[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildRoot = (Join-Path $SourceRoot '.cache\adapter-build'),
    [string]$ToolRoot = 'D:\Game\Tools\Automation\ok-ww',
    [string]$GamePath = 'D:\Game\Games\Wuthering Waves\Wuthering Waves Game\Wuthering Waves.exe',
    [string]$EndfieldToolRoot = 'D:\Game\Tools\Automation\ok-ef',
    [string]$EndfieldGamePath = 'D:\Game\Games\Endfield Game\Endfield.exe',
    [string]$Gf2ToolRoot = 'D:\Game\Tools\Automation\ok-gf2',
    [string]$Gf2GamePath = 'D:\Game\Games\GF2Exilium\GF2 Game\GF2_Exilium.exe',
    [string]$PackageVersion = '0.2.0-local-daily.99',
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
. (Join-Path $source 'adapter-host\Gf2SourceRegistry.ps1')
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$runnerSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\Program.cs') 'WW runner source'
$consoleSource = Assert-LocalFile (Join-Path $source 'adapter-host\UpstreamConsoleCapture.cs') 'upstream console capture source'
$siteCustomizeSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\sitecustomize.py') 'OpenKuro observer bootstrap source'
$wwObserverSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\WwYeYuObserver.py') 'WW observer source'
$gf2ObserverSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\Gf2YeYuObserver.py') 'GF2 observer source'
$officialTaskExitSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\OfficialTaskExit.py') 'official requested task exit source'
$officialStepCaptureSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\OfficialStepCapture.py') 'official boundary screenshot source'
$upstreamLoggingSource = Assert-LocalFile (Join-Path $source 'adapter-host\upstream_logging.py') 'Upstream logging observer source'
$endfieldBridgeSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\EndfieldYeYuBridge.py') 'Endfield YeYu bridge source'
$endfieldObserverSource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\EndfieldRewardStatusObserver.py') 'Endfield inspection observer source'
$endfieldInspectionPolicySource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\EndfieldInspectionContract.json') 'Endfield inspection audit contract'
$compiler = Assert-LocalFile $CSharpCompilerPath 'C# compiler'
$tool = [IO.Path]::GetFullPath($ToolRoot)
if ($tool.StartsWith('\\') -or -not (Test-Path -LiteralPath $tool -PathType Container)) { throw 'WW tool root is not a local directory.' }
$wwCandidateManifestPath = Join-Path $tool 'WW\runtime-preparation.json'
$wwRewardInspectionPatch = $false
$wwBattlePassInspectionPatch = $false
$wwBattlePassHistoryPatch = $false
if (Test-Path -LiteralPath $wwCandidateManifestPath -PathType Leaf) {
    $wwCandidateManifest = Get-Content -LiteralPath (Assert-LocalFile $wwCandidateManifestPath 'WW official source candidate manifest') -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    $wwCandidateSource = $wwCandidateManifest.source; $wwCandidateEntry = $wwCandidateManifest.entry
    $wwWorking = Join-Path $tool 'WW\source'; $wwPython = Assert-LocalFile (Join-Path $tool 'r\Scripts\pythonw.exe') 'WW official candidate Python runtime'; $wwMain = Assert-LocalFile (Join-Path $wwWorking 'main.py') 'WW official candidate source entry'; $wwArchive = Assert-LocalFile (Join-Path $tool 'official-source.zip') 'WW official candidate source archive'; $dailyConfig = Join-Path $wwWorking 'configs\DailyTask.json'
    $wwInspectionPatch = $wwCandidateManifest.candidateKind -ceq 'isolated-local-ww-source-patch' -and
        $wwCandidateSource.commit -ceq '1a779fbf3a2211342c6c83f06011b534cbb80466' -and
        $wwCandidateSource.officialBaseCommit -ceq '6ffaa7f0b0b7f3140367de96c8eacba533c96894' -and
        (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq 'b5f3fb78b095c2a23c17911f10706a565c9c22ead690ed8f6f799cb46452b19b' -and
        (Hash (Join-Path $wwWorking 'src\task\MultiAccountDailyTask.py')) -ceq '2280a301ea9315631a96edabbaf88b775a6d2c3ac4e2dc409c82982321685c45'
    $wwLocalPatch = $wwCandidateManifest.candidateKind -ceq 'isolated-local-ww-source-patch' -and
        $wwCandidateSource.commit -cin @('20a2d40d0eaa0cfa053ceafecd9a4d04395291f4', '737c89ef00e36248baa0ab92df248af935528b56', '4a1b39d002c5754557bdc7b30782fad0048cd106', '567e71c93c32f03a7bd3ad013205438c0adcba11') -and
        $wwCandidateSource.officialBaseCommit -ceq 'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d'
    $wwRewardInspectionPatch = $wwCandidateManifest.candidateKind -ceq 'isolated-local-ww-source-patch' -and
        $wwCandidateSource.commit -ceq 'da98151e6bcd2c4f0c191e80c059f84c5e6c4939' -and
        $wwCandidateSource.officialBaseCommit -ceq '6ffaa7f0b0b7f3140367de96c8eacba533c96894' -and
        (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq 'f51aa3aa9a6e614f9bd2a2341dd19a3a10e571ce4be685d372bd513c94cf275c' -and
        (Hash (Join-Path $wwWorking 'src\task\MultiAccountDailyTask.py')) -ceq '94a90a9dac2ff0f92134af3fed1c083d8a61f5684e57b93309401fdf8f60cea1' -and
        (Hash (Join-Path $wwWorking 'src\task\resources\daily_reward_claimed_check.png')) -ceq '57ef1470aaf2e9d8fb55773d06f14b693a076ea5dc668f685e09b3a83223fd3f'
    $wwBattlePassInspectionPatch = $wwCandidateManifest.candidateKind -ceq 'isolated-local-ww-source-patch' -and
        $wwCandidateSource.commit -ceq '274346512f904b2e2bc2dea78a45982fa0012e83' -and
        $wwCandidateSource.officialBaseCommit -ceq '6ffaa7f0b0b7f3140367de96c8eacba533c96894' -and
        (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq '84f52efb8b61ea1bb24a0e972ab510ffd4f048786af0a94c821e7fec37c92463' -and
        (Hash (Join-Path $wwWorking 'src\task\MultiAccountDailyTask.py')) -ceq '5ccee3872e8190b94490d9b7329aee6920706d3ab79bef4923595217b54ec0ed' -and
        (Hash (Join-Path $wwWorking 'src\task\resources\daily_reward_claimed_check.png')) -ceq '57ef1470aaf2e9d8fb55773d06f14b693a076ea5dc668f685e09b3a83223fd3f'
    $wwBattlePassHistoryPatch = $wwCandidateManifest.candidateKind -ceq 'isolated-local-ww-source-patch' -and
        $wwCandidateSource.officialBaseCommit -ceq '6ffaa7f0b0b7f3140367de96c8eacba533c96894' -and
        (($wwCandidateSource.commit -ceq '93e19b4f455390f50b8e126c23cf4f4be1595adb' -and (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq '0805055d5838b99318b052550666f86b4bade94b400a553d98f21ece53b54198') -or
         ($wwCandidateSource.commit -ceq 'cc2e2e7f32d25b5af269c4b16d063d259ce6cffb' -and (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq 'e673110270a9023e2608c58272897c40da9819d54fe384ee5b110a9dc461fee6') -or
         ($wwCandidateSource.commit -ceq '81edb5af888688be64e879f346b0f2440bacd958' -and (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq '81fd98025c06507c5547e2b8c76fe23ef9d4e12c8fae20c53ca0ac6dc653e828') -or
         ($wwCandidateSource.commit -ceq '85e54379b3a6859f30f2a1df6326091f3a51929d' -and (Hash (Join-Path $wwWorking 'src\task\DailyTask.py')) -ceq 'facab02135deab404e7d59602655c91d915b7ef49a3f74506baf42332f524b5b')) -and
        (Hash (Join-Path $wwWorking 'src\task\MultiAccountDailyTask.py')) -ceq '5ccee3872e8190b94490d9b7329aee6920706d3ab79bef4923595217b54ec0ed' -and
        (Hash (Join-Path $wwWorking 'src\task\resources\daily_reward_claimed_check.png')) -ceq '57ef1470aaf2e9d8fb55773d06f14b693a076ea5dc668f685e09b3a83223fd3f'
    if ($wwBattlePassHistoryPatch) {
        $wwSdk = $wwCandidateManifest.sdkSourcePatch
        $wwSdkFile = Assert-LocalFile (Join-Path $tool 'r\Lib\site-packages\ok\device\capture_methods\windows_graphics.py') 'WW audited SDK capture source'
        $wwSdkWheel = Assert-LocalFile (Join-Path $tool 'source-patches\ok_script-2.0.8+local.wgcclose.20261007-py3-none-any.whl') 'WW audited SDK source wheel'
        if ($wwSdk.repository -cne 'https://github.com/ok-oldking/ok-script.git' -or
            $wwSdk.officialBaseCommit -cne '752e01d02c9e298a3128f11d9ec0ed9c654dd0e6' -or
            $wwSdk.commit -cne '0442e1f200bad209342c468f2ed44106c16cb9f6' -or
            $wwSdk.version -cne '2.0.8+local.wgcclose.20261007' -or
            $wwSdk.candidateKind -cne 'local-upstream-sdk-source-patch' -or
            $wwSdk.windowsGraphicsSha256 -cne 'd6f0d75442b0dc627a5bcfa7b7ae874083826b5a2047de88bc6b7296d4e9a047' -or
            $wwSdk.windowsGraphicsSha256 -cne (Hash $wwSdkFile) -or
            $wwSdk.wheelFile -cne 'source-patches/ok_script-2.0.8+local.wgcclose.20261007-py3-none-any.whl' -or
            $wwSdk.wheelSha256 -cne 'bfb4a4f000d41df8ad8e4ed96144c2c8a2895ae6587f9161e4d55c34605ffe07' -or
            $wwSdk.wheelSha256 -cne (Hash $wwSdkWheel)) { throw 'WW SDK source wheel is not the audited Close repair.' }
    }
    $wwBattlePassInspectionPatch = $wwBattlePassInspectionPatch -or $wwBattlePassHistoryPatch
    $wwRewardInspectionPatch = $wwRewardInspectionPatch -or $wwBattlePassInspectionPatch
    $wwInspectionPatch = $wwInspectionPatch -or $wwRewardInspectionPatch
    $wwOfficialSource = $wwCandidateManifest.candidateKind -ceq 'isolated-official-ww-source' -and
        $wwCandidateSource.commit -cin @('c3fef9a06f61f96abeb53571fb47dd578b6f7063', '07a578842b34dda3428535eb46f8b98f76fddb0d', '36c557437de745cdef506bcfa148569d3e09cfcf', 'f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d', '2243d465d58c74f52765b658174bff76165656ee')
    if ($wwCandidateManifest.schemaVersion -ne 1 -or $wwCandidateManifest.status -cne 'read-only-import-ready' -or -not ($wwOfficialSource -or $wwLocalPatch -or $wwInspectionPatch) -or $wwCandidateSource.repository -cne 'https://github.com/ok-oldking/ok-wuthering-waves.git' -or $wwCandidateSource.sourceDirectory -cne 'WW/source' -or $wwCandidateSource.archive.file -cne 'official-source.zip' -or $wwCandidateSource.archive.sha256 -cne (Hash $wwArchive) -or $wwCandidateEntry.sourceEntry -cne 'main.py' -or $wwCandidateEntry.arguments -cne 'main.py --task 1 --exit' -or $wwCandidateEntry.pythonwRelativePath -cne 'r\Scripts\pythonw.exe' -or $wwCandidateEntry.workingDirectoryRelativePath -cne 'WW/source') { throw 'WW official source candidate manifest does not match its fixed entry layout.' }
    $wwBindingKind = 'official-source-candidate-v1'; $launcher = $null
} else {
    $launcher = Assert-LocalFile (Join-Path $tool 'ok-ww.exe') 'WW tool launcher'
    $dailyConfig = Assert-LocalFile (Join-Path $tool 'data\apps\ok-ww\working\configs\DailyTask.json') 'WW daily configuration'
    $wwPython = $null; $wwMain = $null; $wwBindingKind = 'pyappify-v1'
}
$game = Assert-LocalFile $GamePath 'WW game executable'
$endfieldTool = Assert-LocalDirectory $EndfieldToolRoot 'Endfield tool root'
$endfieldCandidateManifestPath = Join-Path $endfieldTool 'Endfield\runtime-preparation.json'
if (Test-Path -LiteralPath $endfieldCandidateManifestPath -PathType Leaf) {
    $endfieldCandidateManifest = Get-Content -LiteralPath (Assert-LocalFile $endfieldCandidateManifestPath 'Endfield official source candidate manifest') -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    $endfieldCandidateSource = $endfieldCandidateManifest.source
    $endfieldCandidateEntry = $endfieldCandidateManifest.entry
    $endfieldWorking = Join-Path $endfieldTool 'Endfield\source'
    $endfieldPython = Assert-LocalFile (Join-Path $endfieldTool 'r\Scripts\pythonw.exe') 'Endfield official candidate Python runtime'
    $endfieldMain = Assert-LocalFile (Join-Path $endfieldWorking 'main.py') 'Endfield official candidate GUI entry'
    $endfieldDailyConfig = Assert-LocalFile (Join-Path $endfieldWorking 'configs\DailyTask.json') 'Endfield official candidate daily configuration'
    $endfieldLocalPatch = $endfieldCandidateManifest.candidateKind -ceq 'isolated-local-source-patch-with-formal-runtime-binding' -and
        (($endfieldCandidateSource.commit -ceq 'ec5058f90b91f816201227cd66b75770422c7c67' -and
          $endfieldCandidateSource.officialBaseCommit -ceq '34626b32e5cf5f8a7039b0d10c898169a5c421d6') -or
         ($endfieldCandidateSource.commit -ceq '194b7c94f2e29980667c41a1b16df0556cacd6be' -and
          $endfieldCandidateSource.officialBaseCommit -ceq 'bb5af7df07297fdb6edfc483cedd7167840d5e82') -or
         ($endfieldCandidateSource.commit -ceq '19eb6d2015850b25f6d27c74edf27bf932567503' -and
          $endfieldCandidateSource.officialBaseCommit -ceq 'ef58c1c2d87e0f80a0075324e4797c1fbf267d5c') -or
         ($endfieldCandidateSource.commit -ceq '84e5df86197a68c4001f3a810b38c953aad60950' -and
          $endfieldCandidateSource.officialBaseCommit -ceq 'ef58c1c2d87e0f80a0075324e4797c1fbf267d5c') -or
         ($endfieldCandidateSource.commit -ceq '402b2da9bc46be6a2876f3f1ec4ed732754b636f' -and
          $endfieldCandidateSource.officialBaseCommit -ceq '08af80be42ae11fdd482fbdb0b00f87f08cdf2e3') -or
         ($endfieldCandidateSource.commit -ceq '168d7470760d88b01f6441b1d3255b08f4316954' -and
          $endfieldCandidateSource.officialBaseCommit -ceq 'cfa1069a30eab05f9bc26ff571cfa7222b9d064e' -and
          (Hash (Join-Path $endfieldWorking 'src\tasks\onetime\CreditCollectTask.py')) -ceq 'fe4020ff763b6d292be1a4d063f42fc4d47d3e24b9971088102c0ba16d8674e8') -or
         ($endfieldCandidateSource.commit -ceq 'b69a1c12c6907ed0830320ae7433111ddea5e675' -and
          $endfieldCandidateSource.officialBaseCommit -ceq 'cfa1069a30eab05f9bc26ff571cfa7222b9d064e' -and
          (Hash (Join-Path $endfieldWorking 'src\tasks\onetime\CreditCollectTask.py')) -ceq '6dc3024a5fa7511bf5e69de4aebd47619ab2dbbe10452b9baccddc356f5f38b5')) -and
        $endfieldCandidateSource.keyFileSha256.gameFlow -ceq (Hash (Join-Path $endfieldWorking 'src\core\base_mixin\game_flow_mixin.py'))
    $endfieldOfficialSource = $endfieldCandidateManifest.candidateKind -ceq 'isolated-official-source-with-formal-runtime-binding' -and
        $endfieldCandidateSource.commit -cin @('b13f1197a503e6473fbf63c1f927a33838739d1f', '9b45815dcefdb3d4c273fdc26482782eb1e18395', '0815bdf3da08bcf5de0b0e0d55efbd7b7719ace4', '60bb694e63658adeeec83cc474a5a06626f25c6e', '83434dc40e2db364eb9dfa0fe9d0234bc4d7a164', '3d1d521f6fb367c8da2cd480511585fd219e69aa')
    $inspectionPolicy = Get-Content -LiteralPath $endfieldInspectionPolicySource -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    $endfieldRewardInspectionPatch = $endfieldCandidateManifest.candidateKind -ceq 'isolated-local-source-patch-with-formal-runtime-binding' -and
        $inspectionPolicy.schemaVersion -eq 1 -and $inspectionPolicy.files.Count -eq 6 -and
        $endfieldCandidateSource.commit -ceq $inspectionPolicy.commit -and
        $endfieldCandidateSource.officialBaseCommit -ceq $inspectionPolicy.officialBaseCommit -and
        $endfieldCandidateSource.repository -ceq $inspectionPolicy.repository
    if ($endfieldRewardInspectionPatch) {
        $endfieldRewardInspectionPatch = (Hash (Assert-LocalFile (Join-Path $endfieldTool 'official-source.zip') 'Endfield inspection archive')) -ceq $inspectionPolicy.archiveSha256
        foreach ($inspectionFile in $inspectionPolicy.files.GetEnumerator()) {
            $inspectionPath = Assert-LocalFile (Join-Path $endfieldWorking $inspectionFile.Key) 'Endfield inspected source'
            if ((Hash $inspectionPath) -cne $inspectionFile.Value) { throw 'Endfield inspected source hash differs.' }
        }
    }
    if ($endfieldCandidateManifest.schemaVersion -ne 1 -or $endfieldCandidateManifest.status -cne 'read-only-import-ready' -or
        (-not ($endfieldOfficialSource -or $endfieldLocalPatch -or $endfieldRewardInspectionPatch)) -or
        $endfieldCandidateSource.repository -cne 'https://github.com/AliceJump/ok-end-field.git' -or
        $endfieldCandidateSource.sourceDirectory -cne 'Endfield/source' -or
        $endfieldCandidateEntry.sourceEntry -cne 'main.py' -or $endfieldCandidateEntry.arguments -cne 'main.py --task 1 --exit' -or
        ([IO.Path]::GetFullPath([string]$endfieldCandidateEntry.pythonw) -cne $endfieldPython) -or
        ([IO.Path]::GetFullPath([string]$endfieldCandidateEntry.workingDirectory) -cne $endfieldWorking) -or
        $endfieldCandidateSource.keyFileSha256.main -cne (Hash $endfieldMain)) { throw 'Endfield official source candidate manifest does not match its fixed entry layout.' }
    $endfieldBindingKind = 'official-source-candidate-v1'
    $endfieldLauncher = $null; $endfieldUpdateState = $null
} else {
    $endfieldLauncherPath = Join-Path $endfieldTool 'ok-ef.exe'
    $endfieldLauncher = Assert-LocalFile $endfieldLauncherPath 'Endfield formal GUI launcher'
    if ($launcher -and (Hash $endfieldLauncher) -ceq (Hash $launcher)) { throw 'Endfield formal launcher is an invalid copy of the WW launcher.' }
    $endfieldAppRoot = Assert-LocalDirectory (Join-Path $endfieldTool 'data\apps\ok-ef') 'Endfield formal PyAppify app root'
    $endfieldUpdateState = Assert-LocalFile (Join-Path $endfieldAppRoot 'app.json') 'Endfield formal update state'
    $endfieldState = Get-Content -LiteralPath $endfieldUpdateState -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    if (-not $endfieldState.ContainsKey('current_version') -or [string]::IsNullOrWhiteSpace([string]$endfieldState.current_version)) { throw 'Endfield formal update state has no current_version.' }
    $endfieldPython = Assert-LocalFile (Join-Path $endfieldAppRoot 'python\pythonw.exe') 'Endfield GUI Python runtime'
    $endfieldMain = Assert-LocalFile (Join-Path $endfieldAppRoot 'working\main.py') 'Endfield formal GUI entry'
    $endfieldDailyConfig = Assert-LocalFile (Join-Path $endfieldAppRoot 'working\configs\DailyTask.json') 'Endfield daily configuration'
    $endfieldBindingKind = 'pyappify-v1'
}
$endfieldGame = Assert-LocalFile $EndfieldGamePath 'Endfield game executable'
$gf2Candidate = [IO.Path]::GetFullPath($Gf2ToolRoot)
$gf2CandidateManifestPath = Join-Path $gf2Candidate 'GF2\runtime-preparation.json'
$gf2ApprovedCandidate = $false
if (Test-Path -LiteralPath $gf2CandidateManifestPath -PathType Leaf) {
    $gf2CandidateManifest = Get-Content -LiteralPath (Assert-LocalFile $gf2CandidateManifestPath 'GF2 official source candidate manifest') -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    $gf2CandidateSource = $gf2CandidateManifest.source
    $gf2CandidateEntry = $gf2CandidateManifest.entry
    $gf2Tool = Assert-LocalDirectory $gf2Candidate 'GF2 official source candidate root'
    $gf2Working = Join-Path $gf2Tool 'GF2\source'
    $gf2Python = Assert-LocalFile (Join-Path $gf2Tool 'r\Scripts\pythonw.exe') 'GF2 official candidate Python runtime'
    $gf2Main = Assert-LocalFile (Join-Path $gf2Working 'main.py') 'GF2 official candidate source entry'
    $gf2DailyConfig = Assert-LocalFile (Join-Path $gf2Working 'configs\DailyTask.json') 'GF2 official candidate daily configuration'
    $gf2CliPath = Join-Path $gf2Working 'ok\cli.py'
    if (!(Test-Path -LiteralPath $gf2CliPath -PathType Leaf)) { $gf2CliPath = Join-Path $gf2Tool 'r\Lib\site-packages\ok\cli.py' }
    $gf2Cli = Assert-LocalFile $gf2CliPath 'GF2 official candidate ok.cli module'
    $gf2Release1277 = $gf2CandidateSource.repository -ceq 'https://github.com/AliceJump/ok-gf2.git' -and
        $gf2CandidateSource.commit -ceq '5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42' -and $gf2CandidateSource.tag -ceq 'v1.2.77'
    $gf2RegistrySource = Assert-LocalFile (Join-Path $source 'adapter-host\openkuro-runner\gf2-source-candidates.json') 'GF2 audited source registry'
    $gf2ApprovedCandidate = Test-YeYuGf2ApprovedSource -RegistryPath $gf2RegistrySource -Manifest $gf2CandidateManifest -WorkingRoot $gf2Working
    if ($gf2CandidateManifest.schemaVersion -ne 1 -or $gf2CandidateManifest.status -cne 'read-only-import-ready' -or
        ($gf2CandidateManifest.candidateKind -cne 'isolated-official-gf2-source' -and !$gf2ApprovedCandidate) -or
        (!$gf2Release1277 -and !$gf2ApprovedCandidate -and ($gf2CandidateSource.repository -cne 'https://cnb.cool/ok-oldking/ok-gf2.git' -or
        $gf2CandidateSource.commit -cnotin @('4122e01e8a69063ae37807f316f2ae8f80eeedb1', '13a569429b9b0b50db06e95b0cb1f65bf54a38b7'))) -or
        $gf2CandidateSource.sourceDirectory -cne 'GF2/source' -or
        $gf2CandidateEntry.sourceEntry -cne 'main.py' -or $gf2CandidateEntry.arguments -cne 'main.py --task 1 --exit' -or
        ([IO.Path]::GetFullPath([string]$gf2CandidateEntry.pythonw) -cne $gf2Python) -or
        ([IO.Path]::GetFullPath([string]$gf2CandidateEntry.workingDirectory) -cne $gf2Working) -or
        $gf2CandidateSource.keyFileSha256.main -cne (Hash $gf2Main)) { throw 'GF2 official source candidate manifest does not match its fixed entry layout.' }
    if ($gf2CandidateManifest.ContainsKey('sdkSourcePatch')) {
        $gf2SdkPatch = $gf2CandidateManifest.sdkSourcePatch
        $gf2SdkFile = Assert-LocalFile (Join-Path $gf2Tool 'r\Lib\site-packages\ok\device\interaction_methods\genshin.py') 'GF2 patched SDK interaction source'
        if (!$gf2Release1277 -or $gf2SdkPatch.repository -cne 'https://github.com/ok-oldking/ok-script.git' -or
            $gf2SdkPatch.officialBaseCommit -cne '752e01d02c9e298a3128f11d9ec0ed9c654dd0e6' -or
            $gf2SdkPatch.commit -cne 'dabd9f7f99c2dec7ce1ff90a6f84576e0dc99ca4' -or
            $gf2SdkPatch.version -cne '2.0.8+local.genshincleanup.20261003' -or
            $gf2SdkPatch.candidateKind -cne 'local-upstream-sdk-source-patch' -or
            $gf2SdkPatch.genshinSha256 -cne 'a23a384e038dfb012ef7d4dbba76d0bdbaf4e13b993efdaef9a6a2d74cbb5cd8' -or
            $gf2SdkPatch.genshinSha256 -cne (Hash $gf2SdkFile)) { throw 'GF2 SDK patch is not the audited source candidate.' }
    }
    $gf2BindingKind = 'official-source-candidate-v1'
    $gf2Launcher = $null
} else {
    $gf2Tool = [IO.Path]::GetFullPath($Gf2ToolRoot)
    $gf2Launcher = Assert-LocalFile (Join-Path $gf2Tool 'working\ok-gf2.exe') 'GF2 formal GUI entry'
    $gf2Main = Assert-LocalFile (Join-Path $gf2Tool 'working\main.py') 'GF2 GUI source entry'
    $gf2DailyConfig = Assert-LocalFile (Join-Path $gf2Tool 'working\configs\DailyTask.json') 'GF2 daily configuration'
    $gf2Cli = Assert-LocalFile (Join-Path $gf2Tool 'working\ok\cli.py') 'GF2 ok.cli module'
    $gf2Python = $null
    $gf2BindingKind = 'pyappify-v1'
}
$gf2Game = Assert-LocalFile $Gf2GamePath 'GF2 game executable'
$build = [IO.Path]::GetFullPath($BuildRoot)
if (-not $build.StartsWith((Join-Path $SourceRoot '.cache\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Adapter build output must remain below the project cache.' }
New-Item -ItemType Directory -Path $build -Force | Out-Null
$candidate = Join-Path $build 'local-daily-candidate'
if (Test-Path -LiteralPath $candidate) { Move-Item -LiteralPath $candidate -Destination (Join-Path $build ('local-daily-candidate.previous-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))) }
New-Item -ItemType Directory -Path $candidate -Force | Out-Null
$runner = Join-Path $candidate 'runner.exe'
& $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll /r:System.Drawing.dll ("/out:$runner") $runnerSource $consoleSource
if ($LASTEXITCODE -ne 0) { throw 'WW runner compilation failed.' }
$siteCustomize = Join-Path $candidate 'sitecustomize.py'
Copy-Item -LiteralPath $siteCustomizeSource -Destination $siteCustomize
$wwObserver = Join-Path $candidate 'WwYeYuObserver.py'
Copy-Item -LiteralPath $wwObserverSource -Destination $wwObserver
$gf2Observer = Join-Path $candidate 'Gf2YeYuObserver.py'
Copy-Item -LiteralPath $gf2ObserverSource -Destination $gf2Observer
$officialTaskExit = Join-Path $candidate 'OfficialTaskExit.py'
Copy-Item -LiteralPath $officialTaskExitSource -Destination $officialTaskExit
$officialStepCapture = Join-Path $candidate 'OfficialStepCapture.py'
Copy-Item -LiteralPath $officialStepCaptureSource -Destination $officialStepCapture
$upstreamLogging = Join-Path $candidate 'upstream_logging.py'
Copy-Item -LiteralPath $upstreamLoggingSource -Destination $upstreamLogging
$endfieldBridge = Join-Path $candidate 'EndfieldYeYuBridge.py'
Copy-Item -LiteralPath $endfieldObserverSource -Destination (Join-Path $candidate 'EndfieldRewardStatusObserver.py')
Copy-Item -LiteralPath $endfieldInspectionPolicySource -Destination (Join-Path $candidate 'EndfieldInspectionContract.json')
Copy-Item -LiteralPath $endfieldBridgeSource -Destination $endfieldBridge
$binding = [ordered]@{ schemaVersion = 2; tools = [ordered]@{
    WW = [ordered]@{ root=$tool; bindingKind=$wwBindingKind; guiRuntime=$wwPython; guiRuntimeSha256=if ($wwPython) { Hash $wwPython } else { $null }; launcher=$launcher; launcherSha256=if ($launcher) { Hash $launcher } else { $null }; guiEntry=$wwMain; guiEntrySha256=if ($wwMain) { Hash $wwMain } else { $null }; dailyConfig=$dailyConfig; dailyConfigSha256=if (Test-Path -LiteralPath $dailyConfig -PathType Leaf) { Hash $dailyConfig } else { $null }; game=$game }
    Endfield = [ordered]@{ root=$endfieldTool; bindingKind=$endfieldBindingKind; guiRuntime=$endfieldPython; guiRuntimeSha256=(Hash $endfieldPython); guiEntry=$endfieldMain; guiEntrySha256=(Hash $endfieldMain); dailyConfig=$endfieldDailyConfig; dailyConfigSha256=(Hash $endfieldDailyConfig); game=$endfieldGame }
    GF2 = [ordered]@{ root=$gf2Tool; bindingKind=$gf2BindingKind; guiRuntime=$gf2Python; guiRuntimeSha256=if ($gf2Python) { Hash $gf2Python } else { $null }; launcher=$gf2Launcher; launcherSha256=if ($gf2Launcher) { Hash $gf2Launcher } else { $null }; guiEntry=$gf2Main; guiEntrySha256=(Hash $gf2Main); dailyConfig=$gf2DailyConfig; dailyConfigSha256=(Hash $gf2DailyConfig); game=$gf2Game }
} }
$bindingPath = Join-Path $candidate 'tool-binding.json'
[IO.File]::WriteAllText($bindingPath, ($binding | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
$operation = { param($id,$class,$definition,$timeout,$evidenceKinds=@('tool-log-outcome')) $handlerId = if ($id -ceq 'ww.daily_task') { 'ww.account_daily_task' } else { $id }; [ordered]@{ handlerId=$handlerId; mode='granular'; actionClass=$class; risk='routine_action'; todoDefinitionIds=@($definition); adapterCapabilityRefs=@('game.daily.run@1.0'); supportsResume=$false; timeoutSeconds=$timeout; requiredEvidenceKinds=@($evidenceKinds) } }
$manifest = [ordered]@{ schemaVersion=2; packageId='legacy-night-rain-gamer'; packageVersion=$PackageVersion; buildId=('local-daily-'+[DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss')); builtAt=[DateTimeOffset]::UtcNow.ToString('o'); installedAt=[DateTimeOffset]::UtcNow.ToString('o'); protocolVersions=@('1.1'); hostPackageId='manager-adapter-host'; minHostVersion='0.2.0'; entryPoint='runner.exe'; files=@([ordered]@{path='runner.exe';sha256=(Hash $runner);sizeBytes=(Get-Item -LiteralPath $runner).Length},[ordered]@{path='EndfieldYeYuBridge.py';sha256=(Hash $endfieldBridge);sizeBytes=(Get-Item -LiteralPath $endfieldBridge).Length},[ordered]@{path='tool-binding.json';sha256=(Hash $bindingPath);sizeBytes=(Get-Item -LiteralPath $bindingPath).Length}); supportedGameIds=@('WW','Endfield','GF2'); operationBindings=[ordered]@{ WW=[ordered]@{ 'attach-world'=(& $operation 'ww.daily_task' 'session' 'todo.v1.ww.daily.attach-world' 1800); 'inspect-daily-progress'=(& $operation 'ww.daily_task' 'daily' 'todo.v1.ww.daily.inspect-daily-progress' 1800); 'farm-nightmare-daily-echo'=(& $operation 'ww.daily_task' 'daily' 'todo.v1.ww.daily.farm-nightmare-daily-echo' 1800); 'spend-waveplates'=(& $operation 'ww.daily_task' 'stamina' 'todo.v1.ww.daily.spend-waveplates' 1800); 'claim-daily-reward'=(& $operation 'ww.daily_task' 'reward' 'todo.v1.ww.daily.claim-daily-reward' 1800); 'claim-mail'=(& $operation 'ww.daily_task' 'mail' 'todo.v1.ww.daily.claim-mail' 1800); 'claim-battle-pass'=(& $operation 'ww.daily_task' 'reward' 'todo.v1.ww.daily.claim-battle-pass' 1800) }; Endfield=[ordered]@{ 'attach-world'=(& $operation 'endfield.attach_world' 'session' 'todo.v1.endfield.daily.attach-world' 1800); 'mail'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.mail' 1800); 'spend-sanity'=(& $operation 'endfield.daily_task' 'stamina' 'todo.v1.endfield.daily.spend-sanity' 1800); 'delivery-commission'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.delivery-commission' 1800); 'collect-credit'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.collect-credit' 1800); 'dijiang-harvest'=(& $operation 'endfield.daily_task' 'daily' 'todo.v1.endfield.daily.dijiang-harvest' 1800); 'claim-daily-reward'=(& $operation 'endfield.daily_task' 'reward' 'todo.v1.endfield.daily.claim-daily-reward' 1800) }; GF2=[ordered]@{ 'attach-home'=(& $operation 'gf2.attach_home' 'session' 'todo.v1.gf2.daily.attach-home' 1800); 'mail'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.mail' 1800); 'activity-supply'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.activity-supply' 1800); 'star-wish'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.star-wish' 1800); 'event-battle'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.event-battle' 1800); 'exploration-claim'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.exploration-claim' 1800); 'public-area-dispatch'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.public-area-dispatch' 1800); 'spend-stamina'=(& $operation 'gf2.daily_task' 'stamina' 'todo.v1.gf2.daily.spend-stamina' 1800); 'squad-tasks'=(& $operation 'gf2.daily_task' 'daily' 'todo.v1.gf2.daily.squad-tasks' 1800); 'claim-daily-missions'=(& $operation 'gf2.daily_task' 'reward' 'todo.v1.gf2.daily.claim-daily-missions' 1800); 'battle-pass-free-track'=(& $operation 'gf2.daily_task' 'reward' 'todo.v1.gf2.daily.battle-pass-free-track' 1800) } }; forbiddenOperationClasses=@('gacha','purchase','dismantle','enhance','trade','account_settings','pvp','irreversible_choice','arbitrary_command','arbitrary_path','arbitrary_input'); limits=[ordered]@{maxRequestBytes=262144;maxEventBytes=65536;maxArtifactsPerTodo=32}; artifactPolicy=[ordered]@{allowedMimeTypes=@('text/plain','image/png');maxArtifactBytes=20971520}; security=[ordered]@{allowsArbitraryCommand=$false;allowsArbitraryPath=$false;allowsArbitraryInput=$false}; promotion=[ordered]@{status='candidate';replaySuiteDigest='sha256:'+('0'*64);shadowSuiteDigest='sha256:'+('0'*64);canarySuiteDigest='sha256:'+('0'*64);payloadDigest='';receiptFile='';receiptSha256='';receiptResourceId=''}; executionReady=$false }
if ($wwRewardInspectionPatch) {
    $manifest.operationBindings.WW.'claim-daily-reward'.adapterCapabilityRefs += 'ww.daily.reward-status.inspect@1.0'
}
if ($gf2ApprovedCandidate -and $gf2CandidateSource.ContainsKey('inspectionVersion') -and
    $gf2CandidateSource.inspectionVersion -ceq 'gf2.reward-status.inspect@1.0') {
    $manifest.operationBindings.GF2.'claim-daily-missions'.adapterCapabilityRefs += 'gf2.daily.reward-status.inspect@1.0'
    $manifest.operationBindings.GF2.'battle-pass-free-track'.adapterCapabilityRefs += 'gf2.battle-pass.reward-status.inspect@1.0'
}
if ($wwBattlePassInspectionPatch) {
    $manifest.operationBindings.WW.'claim-battle-pass'.adapterCapabilityRefs += 'ww.battle-pass.status.inspect@1.0'
}
if ($wwBattlePassHistoryPatch) {
    $manifest.operationBindings.WW.'claim-battle-pass'.adapterCapabilityRefs += 'ww.battle-pass.history.inspect@1.0'
}
$manifest.files += [ordered]@{path='sitecustomize.py';sha256=(Hash $siteCustomize);sizeBytes=(Get-Item -LiteralPath $siteCustomize).Length}
$manifest.files += [ordered]@{path='WwYeYuObserver.py';sha256=(Hash $wwObserver);sizeBytes=(Get-Item -LiteralPath $wwObserver).Length}
$manifest.files += [ordered]@{path='Gf2YeYuObserver.py';sha256=(Hash $gf2Observer);sizeBytes=(Get-Item -LiteralPath $gf2Observer).Length}
$gf2RegistryPayload = Join-Path $candidate 'gf2-source-candidates.json'
Copy-Item -LiteralPath (Join-Path $source 'adapter-host\openkuro-runner\gf2-source-candidates.json') -Destination $gf2RegistryPayload
$manifest.files += [ordered]@{path='gf2-source-candidates.json';sha256=(Hash $gf2RegistryPayload);sizeBytes=(Get-Item -LiteralPath $gf2RegistryPayload).Length}
$manifest.files += [ordered]@{path='upstream_logging.py';sha256=(Hash $upstreamLogging);sizeBytes=(Get-Item -LiteralPath $upstreamLogging).Length}
$manifest.files += [ordered]@{path='OfficialTaskExit.py';sha256=(Hash $officialTaskExit);sizeBytes=(Get-Item -LiteralPath $officialTaskExit).Length}
$manifest.files += [ordered]@{path='OfficialStepCapture.py';sha256=(Hash $officialStepCapture);sizeBytes=(Get-Item -LiteralPath $officialStepCapture).Length}
foreach ($inspectionPayload in @('EndfieldRewardStatusObserver.py','EndfieldInspectionContract.json')) {
    $inspectionPayloadPath = Join-Path $candidate $inspectionPayload
    $manifest.files += [ordered]@{path=$inspectionPayload;sha256=(Hash $inspectionPayloadPath);sizeBytes=(Get-Item -LiteralPath $inspectionPayloadPath).Length}
}
$manifest.promotion.payloadDigest = Get-YeYuGamerPayloadDigest -Files @($manifest.files)
[IO.File]::WriteAllText((Join-Path $candidate 'install-manifest.json'), ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
[pscustomobject]@{status='candidate-built';candidateRoot=$candidate;gameIds=@('WW','Endfield','GF2');executionReady=$false;gameStarted=$false;validatedToolPaths=@($endfieldLauncher,$endfieldUpdateState,$endfieldPython,$endfieldMain,$endfieldDailyConfig,$endfieldGame,$gf2Launcher,$gf2Python,$gf2Main,$gf2DailyConfig,$gf2Cli,$gf2Game | Where-Object { $_ })} | ConvertTo-Json -Compress
