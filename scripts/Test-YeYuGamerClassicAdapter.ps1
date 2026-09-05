[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\classic-selected-daily-candidate'),
    [string]$Python = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Python\Python312\python.exe'),
    [string]$CSharpCompilerPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
    [string]$EvidenceDirectory = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-LocalFile([string]$Path, [string]$Purpose) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\') -or -not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "$Purpose is missing or not local: $full" }
    return $full
}
function Invoke-Replay([string]$Runner, [string]$StageRoot, [string]$GameId, [string]$Operation, [string]$Mode) {
    $runId = [guid]::NewGuid().ToString()
    $attemptId = [guid]::NewGuid().ToString()
    $staging = Join-Path $StageRoot ($GameId.ToLowerInvariant() + '-' + $Mode + '-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    $toolRoot = Join-Path $StageRoot ('tool-' + $GameId.ToLowerInvariant())
    New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
    $gamePath = Join-Path $StageRoot ('game-' + $GameId.ToLowerInvariant() + '.exe')
    if (-not (Test-Path -LiteralPath $gamePath)) { [IO.File]::WriteAllBytes($gamePath, [byte[]](0)) }
    $bindingPath = Join-Path $staging 'installation-binding.json'
    $managerBinding = [ordered]@{schemaVersion=1;gameId=$GameId;toolPath=$toolRoot;gamePath=$gamePath}
    [IO.File]::WriteAllText($bindingPath, ($managerBinding | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    $request = [ordered]@{
        protocolVersion='1.1'; runId=$runId; runAttemptId=$attemptId; fencingToken='fake-fence'; gameId=$GameId
        preserveClientOnStop=$true; executableTodoInstanceIds=@('todo-1'); todos=@([ordered]@{operation=$Operation;priorAttempts=2})
    }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Runner
    $start.Arguments = "--protocol-version 1.1 --run-id $runId --run-attempt-id $attemptId --game-id $GameId"
    $start.WorkingDirectory = Split-Path -Parent $Runner
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables['YEYU_GAMER_ADAPTER_STAGING_DIR'] = $staging
    $start.EnvironmentVariables['YEYU_GAMER_INSTALLATION_BINDING_PATH'] = $bindingPath
    $start.EnvironmentVariables['CLASSIC_FAKE_MODE'] = $Mode
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'Replay runner did not start.' }
    $process.StandardInput.WriteLine(($request | ConvertTo-Json -Depth 8 -Compress))
    $process.StandardInput.Close()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $events = @($stdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json -ErrorAction Stop })
    if (@($events).Count -lt 2) { throw "Replay emitted too few protocol events: $stdout" }
    $hello = @($events | Where-Object eventType -eq 'hello')[-1]
    $terminal = @($events | Where-Object eventType -eq 'run_terminal')[-1]
    if ([string]$hello.packageId -cne 'legacy-night-rain-gamer') { throw 'Runner hello packageId is incompatible with AdapterHost.' }
    if ($process.ExitCode -ne [int]$terminal.exitCode) {
        throw "Replay runner process exit $($process.ExitCode) differs from declared exit $($terminal.exitCode): $stderr"
    }
    return [pscustomobject]@{Events=$events;Terminal=$terminal}
}

$source = [IO.Path]::GetFullPath($SourceRoot)
. (Join-Path $source 'adapter-host\YeYuGamerPromotionContract.ps1')
$candidate = [IO.Path]::GetFullPath($CandidateRoot)
$runnerSource = Assert-LocalFile (Join-Path $candidate 'runner.exe') 'classic candidate runner'
$driverSource = Assert-LocalFile (Join-Path $candidate 'classic_tool_driver.py') 'classic candidate driver'
$validator = Assert-LocalFile (Join-Path $source 'adapter-host\tests\validate_classic_selected_daily.py') 'classic validator'
$fakeSource = Assert-LocalFile (Join-Path $source 'adapter-host\tests\FakeClassicPython.cs') 'fake classic Python source'
$pythonPath = Assert-LocalFile $Python 'test Python runtime'
$compiler = Assert-LocalFile $CSharpCompilerPath 'C# compiler'
$evidenceRoot = if ([string]::IsNullOrWhiteSpace($EvidenceDirectory)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\classic-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss'))
} else { [IO.Path]::GetFullPath($EvidenceDirectory) }
if ($evidenceRoot.StartsWith('\\')) { throw 'Classic test evidence directory must be local.' }
New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null

& $pythonPath $driverSource --self-test | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Classic driver self-test failed.' }
& $pythonPath $validator | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Classic static validation failed.' }

$testRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-test\classic-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
try {
    $package = Join-Path $testRoot 'package'
    Copy-Item -LiteralPath $candidate -Destination $package -Recurse
    $fake = Join-Path $testRoot 'fake-python.exe'
    & $compiler /nologo /target:exe /optimize+ /platform:anycpu /r:System.Web.Extensions.dll ("/out:$fake") $fakeSource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Fake classic tool compilation failed.' }
    $bindings = [ordered]@{schemaVersion=1;bindings=[ordered]@{}}
    foreach ($gameId in @('PGR','ZZZ','NIKKE')) {
        $toolRoot = Join-Path $testRoot ('tool-' + $gameId.ToLowerInvariant())
        New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
        $gamePath = Join-Path $testRoot ('game-' + $gameId.ToLowerInvariant() + '.exe')
        [IO.File]::WriteAllBytes($gamePath, [byte[]](0))
        $bindings.bindings[$gameId] = [ordered]@{toolRoot=$toolRoot;gamePath=$gamePath;python=$fake;verifiedFiles=@()}
    }
    [IO.File]::WriteAllText((Join-Path $package 'tool-binding.json'), ($bindings | ConvertTo-Json -Depth 8 -Compress), [Text.UTF8Encoding]::new($false))
    $runner = Join-Path $package 'runner.exe'
    $completedCases = @(
        @{game='PGR';operation='claim-serum'},
        @{game='ZZZ';operation='coffee'},
        @{game='ZZZ';operation='city-fund-free-claim'},
        @{game='NIKKE';operation='outpost'}
    )
    foreach ($case in $completedCases) {
        $result = Invoke-Replay $runner $testRoot $case.game $case.operation 'completed'
        if ([string]$result.Terminal.status -cne 'completed' -or @($result.Terminal.completedTodoInstanceIds).Count -ne 1) { throw "$($case.game) completed replay was not accepted." }
    }
    $missing = Invoke-Replay $runner $testRoot 'PGR' 'claim-serum' 'missing'
    if ([string]$missing.Terminal.status -cne 'review_required' -or @($missing.Terminal.completedTodoInstanceIds).Count -ne 0) { throw 'Clean tool exit without a task stage was incorrectly treated as completed.' }
    $failed = Invoke-Replay $runner $testRoot 'NIKKE' 'outpost' 'failed'
    if ([string]$failed.Terminal.status -cne 'failed' -or @($failed.Terminal.completedTodoInstanceIds).Count -ne 0) { throw 'Failed task replay was incorrectly treated as completed.' }

    $replayReport = Join-Path $evidenceRoot 'replay.json'
    [IO.File]::WriteAllText($replayReport, ([ordered]@{
        schemaVersion=1; suite='classic-selected-daily-replay'; passed=$true; completedCases=$completedCases.Count;
        missingStageRejected=$true; failedStageRejected=$true; protocolReplays=6; gameStarted=$false
    } | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
} finally {
    $fullTestRoot = [IO.Path]::GetFullPath($testRoot)
    $safeRoot = ([Environment]::GetFolderPath('LocalApplicationData')).TrimEnd('\') + '\YeYuGamer\adapter-test\'
    if ($fullTestRoot.StartsWith($safeRoot, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $fullTestRoot)) {
        Remove-Item -LiteralPath $fullTestRoot -Recurse -Force
    }
}

$before = @(Get-Process -Name 'PGR','ZenlessZoneZero','nikke' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$probeOutput = & $runnerSource --probe-binding
if ($LASTEXITCODE -ne 0) { throw 'Classic formal binding shadow probe failed.' }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
$after = @(Get-Process -Name 'PGR','ZenlessZoneZero','nikke' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if (-not $probe.ok -or $probe.processStarted -or @($before).Count -ne @($after).Count) { throw 'Classic shadow probe started a process or returned an invalid result.' }
$shadowReport = Join-Path $evidenceRoot 'shadow.json'
[IO.File]::WriteAllText($shadowReport, ([ordered]@{
    schemaVersion=1; suite='classic-binding-shadow'; passed=$true; processStarted=$false;
    beforeProcessCount=@($before).Count; afterProcessCount=@($after).Count; probe=$probe
} | ConvertTo-Json -Depth 8 -Compress), [Text.UTF8Encoding]::new($false))

$manifestPath = Join-Path $candidate 'install-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) { throw 'Classic tests may update only an unpromoted candidate manifest.' }
$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayReport -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowReport -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]$manifest.promotion.payloadDigest -cne (Get-YeYuGamerPayloadDigest -Files @($manifest.files))) { throw 'Classic candidate payload digest is invalid.' }
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))

foreach ($gameId in @('PGR','ZZZ','NIKKE')) {
    $summary = [ordered]@{
        schemaVersion=1; resourceType='adapter-candidate-test-evidence'; status='passed'; passed=$true;
        packageId=[string]$manifest.packageId; packageVersion=[string]$manifest.packageVersion; buildId=[string]$manifest.buildId;
        supportedGameIds=@($gameId); payloadDigest=[string]$manifest.promotion.payloadDigest;
        replaySuiteDigest=[string]$manifest.promotion.replaySuiteDigest; shadowSuiteDigest=[string]$manifest.promotion.shadowSuiteDigest;
        generatedAt=[DateTimeOffset]::UtcNow.ToString('o'); gameStarted=$false
    }
    [IO.File]::WriteAllText((Join-Path $evidenceRoot ($gameId.ToLowerInvariant() + '.json')), ($summary | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}
[pscustomobject]@{status='passed';staticTests=17;protocolReplays=6;gameStarted=$false;packageId='legacy-night-rain-gamer';evidenceDirectory=$evidenceRoot} | ConvertTo-Json -Compress
