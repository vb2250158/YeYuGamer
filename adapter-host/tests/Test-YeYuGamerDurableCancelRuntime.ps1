[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][string]$WorkRoot,
    [string]$CSharpCompilerPath = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
)

$ErrorActionPreference = 'Stop'
$sourceResolved = (Resolve-Path -LiteralPath $SourceRoot).Path
$workResolved = [IO.Path]::GetFullPath($WorkRoot)
if (Test-Path -LiteralPath $workResolved) { throw 'Use a fresh isolated WorkRoot.' }
$fixturePath = Join-Path $sourceResolved 'tests\Test-YeYuGamerAdapterHostProtocol.ps1'
$fixtureText = [IO.File]::ReadAllText($fixturePath)
$fixtureTokens = $null
$fixtureErrors = $null
$fixtureAst = [Management.Automation.Language.Parser]::ParseInput($fixtureText, [ref]$fixtureTokens, [ref]$fixtureErrors)
if ($fixtureErrors.Count) { throw 'Existing fixture has parse errors.' }
$setupStatements = @()
foreach ($statement in $fixtureAst.EndBlock.Statements) {
    if ($statement -is [Management.Automation.Language.AssignmentStatementAst] -and $statement.Left.Extent.Text -eq '$startInfo') { break }
    $setupStatements += $statement.Extent.Text
}
if ($setupStatements.Count -lt 20) { throw 'Existing fixture setup boundary changed.' }
. ([scriptblock]::Create($setupStatements -join "`n"))
$derivedRuntime = Join-Path $workResolved 'manager-owned-runtime'
New-Item -ItemType Directory -Path $derivedRuntime | Out-Null
$controlRoot = Join-Path $derivedRuntime 'adapter-controls'
$fallbackRoot = Join-Path $testProgramData 'YeYuGamer\runtime\adapter-controls'
New-Item -ItemType Directory -Path $fallbackRoot -Force | Out-Null
$results = @()
foreach ($scenario in @('valid', 'bad-hmac', 'runner-not-reading')) {
    $caseRequest = $request | ConvertTo-Json -Depth 12 -Compress | ConvertFrom-Json
    $caseRequest.runAttemptId = [guid]::NewGuid().ToString()
    $caseRequest.issuedAt = [DateTimeOffset]::UtcNow.ToString('o')
    $caseRequest.expiresAt = [DateTimeOffset]::UtcNow.AddSeconds(30).ToString('o')
    $caseRequest.timeoutSeconds = 20
    $at = [DateTimeOffset]::UtcNow.ToString('o')
    $nonce = 'n' * 32
    $canonical = "1.1`ncancel`n$($caseRequest.runId)`n$($caseRequest.runAttemptId)`n$at`nuser_cancelled`n$nonce"
    $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($caseRequest.cancelAuthority))
    try { $mac = 'hmac-sha256:' + (-join ($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical)) | ForEach-Object { $_.ToString('x2') })) }
    finally { $hmac.Dispose() }
    if ($scenario -eq 'bad-hmac') { $mac = 'hmac-sha256:' + ('0' * 64) }
    $control = [ordered]@{
        schemaVersion=1; protocolVersion='1.1'; controlType='cancel'; runId=$caseRequest.runId
        runAttemptId=$caseRequest.runAttemptId; at=$at; reasonCode='user_cancelled'; nonce=$nonce; authorityMac=$mac
    }
    $pending = Join-Path $controlRoot ($caseRequest.runAttemptId + '.cancel.json')
    $delivered = Join-Path $controlRoot ($caseRequest.runAttemptId + '.delivered.json')
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $hostPath
    $start.Arguments = '--operation execute --protocol-version 1.1 --run-id ' + $caseRequest.runId + ' --game-id StarRail'
    $start.WorkingDirectory = $hostRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables.Clear()
    foreach ($name in @('SYSTEMROOT','WINDIR','TEMP','TMP')) { $start.EnvironmentVariables[$name] = [Environment]::GetEnvironmentVariable($name) }
    $start.EnvironmentVariables['ProgramData'] = $testProgramData
    $start.EnvironmentVariables['YEYU_GAMER_RUNTIME_DIR'] = $derivedRuntime
    $start.EnvironmentVariables['YEYU_GAMER_ADAPTER_STAGING_DIR'] = $stagingRoot
    $start.EnvironmentVariables['FAKE_ADAPTER_OUTCOME'] = if ($scenario -eq 'runner-not-reading') { 'hang_after_hello' } else { 'wait_cancel' }
    $owned = [Diagnostics.Process]::new()
    $owned.StartInfo = $start
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $owned.Start()) { throw 'Fixture Host did not start.' }
        $parentPid = $PID
        Add-Content -LiteralPath (Join-Path $workResolved 'owned-processes.jsonl') -Value (@{
            purpose='PIPE1 compiled durable cancellation fixture'; scenario=$scenario; pid=$owned.Id
            parentPid=$parentPid; source=$hostPath; logRoot=$workResolved
        } | ConvertTo-Json -Compress)
        $stdoutTask = $owned.StandardOutput.ReadToEndAsync()
        $stderrTask = $owned.StandardError.ReadToEndAsync()
        $owned.StandardInput.WriteLine(($caseRequest | ConvertTo-Json -Depth 12 -Compress))
        $owned.StandardInput.Flush()
        $readyDeadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
        while (-not (Test-Path -LiteralPath $controlRoot)) {
            if ($owned.HasExited -or [DateTimeOffset]::UtcNow -gt $readyDeadline) { throw 'Derived runtime polling directory unavailable.' }
            Start-Sleep -Milliseconds 25
        }
        $temporary = $pending + '.tmp'
        [IO.File]::WriteAllText($temporary, ($control | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporary, $pending)
        if (-not $owned.WaitForExit(12000)) { throw 'Fixture exceeded bounded exit deadline.' }
        if (-not $stdoutTask.Wait(2000) -or -not $stderrTask.Wait(2000)) { throw 'Fixture reader drain exceeded deadline.' }
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        [IO.File]::WriteAllText((Join-Path $workResolved ($scenario + '.stdout.jsonl')), $stdout)
        [IO.File]::WriteAllText((Join-Path $workResolved ($scenario + '.stderr.log')), $stderr)
        $events = @($stdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
        foreach ($hello in @($events | Where-Object { $_.eventType -eq 'hello' })) {
            Add-Content -LiteralPath (Join-Path $workResolved 'owned-processes.jsonl') -Value (@{
                purpose='PIPE1 compiled inert Runner fixture'; scenario=$scenario; pid=$hello.runnerPid
                parentPid=$owned.Id; source=$runner; logRoot=$workResolved
            } | ConvertTo-Json -Compress)
        }
        if ($events.Count -ne 2 -or $events[-1].eventType -ne 'run_terminal') { throw "Unexpected terminal stream: $scenario $stderr" }
        if ($scenario -eq 'bad-hmac') {
            if ($owned.ExitCode -ne 70 -or (Test-Path -LiteralPath $delivered) -or $stderr -notmatch 'durable_cancel_authentication_failed' -or $events[-1].status -ne 'failed') { throw 'Bad HMAC was not rejected.' }
        } else {
            if (-not (Test-Path -LiteralPath $delivered) -or (Test-Path -LiteralPath $pending) -or $events[-1].status -ne 'cancelled') { throw "Derived runtime cancel not delivered: $scenario $stderr" }
            if ($scenario -eq 'valid' -and ($owned.ExitCode -ne 0 -or $stderr)) { throw 'Valid durable cancellation failed.' }
            if ($scenario -eq 'runner-not-reading' -and $stderr -notmatch 'runner_cancel_grace_exceeded') { throw 'Non-reading Runner did not reach cancel grace.' }
        }
        if (@(Get-ChildItem -LiteralPath $fallbackRoot -File).Count) { throw 'Unexpected ProgramData control file.' }
        $results += [pscustomobject]@{scenario=$scenario;passed=$true;hostPid=$owned.Id;exitCode=$owned.ExitCode;elapsedMs=$clock.ElapsedMilliseconds;eventCount=$events.Count}
    } finally {
        if ($owned.Id -and -not $owned.HasExited) { $owned.Kill(); $owned.WaitForExit(3000) | Out-Null }
        $owned.Dispose()
    }
}
$results | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workResolved 'results.json')
$results
