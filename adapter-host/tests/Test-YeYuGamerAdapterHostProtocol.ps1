[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][string]$WorkRoot,
    [string]$CSharpCompilerPath = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
)

$ErrorActionPreference = 'Stop'
$sourceResolved = (Resolve-Path -LiteralPath $SourceRoot).Path
. (Join-Path $sourceResolved 'YeYuGamerPromotionContract.ps1')
$workResolved = [System.IO.Path]::GetFullPath($WorkRoot)
if (-not (Test-Path -LiteralPath $CSharpCompilerPath -PathType Leaf)) {
    throw 'The .NET Framework C# compiler is unavailable.'
}
New-Item -ItemType Directory -Path $workResolved -Force | Out-Null
$testProgramData = Join-Path $workResolved 'program-data'
New-Item -ItemType Directory -Path $testProgramData -Force | Out-Null
$adaptersRoot = Join-Path $workResolved 'adapters'
$hostRoot = Join-Path $adaptersRoot 'manager-adapter-host'
$runnerRoot = Join-Path $adaptersRoot 'legacy-night-rain-gamer'
$stagingRoot = Join-Path $workResolved 'artifact-inbox\22222222-2222-4222-8222-222222222222'
foreach ($path in @($hostRoot, $runnerRoot, $stagingRoot)) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}

$hostSource = Join-Path $sourceResolved 'YeYuGamerAdapterHost.cs'
$fakeSource = Join-Path $sourceResolved 'tests\FakeYeYuGamerRunner.cs'
$hostPath = Join-Path $hostRoot 'host.exe'
$runner = Join-Path $runnerRoot 'runner.exe'
& $CSharpCompilerPath /nologo /optimize+ /target:winexe /platform:anycpu `
    /reference:System.Web.Extensions.dll "/out:$hostPath" $hostSource
if ($LASTEXITCODE -ne 0) { throw 'Adapter Host compilation failed.' }
& $CSharpCompilerPath /nologo /optimize+ /target:exe /platform:anycpu `
    /reference:System.Web.Extensions.dll "/out:$runner" $fakeSource
if ($LASTEXITCODE -ne 0) { throw 'Fake runner compilation failed.' }

$forbidden = @(
    'gacha', 'purchase', 'dismantle', 'enhance', 'trade', 'account_settings',
    'pvp', 'irreversible_choice', 'arbitrary_command', 'arbitrary_path', 'arbitrary_input'
)
$payloadFiles = @([ordered]@{
    path = 'runner.exe'
    sha256 = (Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash.ToLowerInvariant()
    sizeBytes = (Get-Item -LiteralPath $runner).Length
})
$payloadDigest = Get-YeYuGamerPayloadDigest -Files $payloadFiles
$receiptResourceId = '77777777-7777-4777-8777-777777777777'
$receipt = [ordered]@{
    schemaVersion = 1
    resourceType = 'adapter-promotion-receipt'
    resourceId = $receiptResourceId
    state = 'passed'
    packageId = 'legacy-night-rain-gamer'
    packageVersion = '0.1.0'
    buildId = 'fake-e2e'
    supportedGameIds = @('StarRail', 'WW')
    payloadDigest = $payloadDigest
    replaySuiteDigest = 'sha256:' + ('1' * 64)
    shadowSuiteDigest = 'sha256:' + ('2' * 64)
    canarySuiteDigest = 'sha256:' + ('3' * 64)
    candidateTestEvidenceSha256 = 'sha256:' + ('4' * 64)
    managerCanaryEvidenceSha256 = 'sha256:' + ('5' * 64)
    # An unchanged execution package remains compatible after a Host upgrade.
    # This digest is the immutable canary provenance from the earlier Host.
    hostEntryPointSha256 = 'sha256:' + ('6' * 64)
    issuedAt = '2026-08-28T00:00:30Z'
}
$receiptPath = Join-Path $runnerRoot 'promotion-receipt.json'
[System.IO.File]::WriteAllText($receiptPath, ($receipt | ConvertTo-Json -Depth 6 -Compress), [System.Text.UTF8Encoding]::new($false))
$receiptHash = (Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    schemaVersion = 2
    packageId = 'legacy-night-rain-gamer'
    packageVersion = '0.1.0'
    buildId = 'fake-e2e'
    builtAt = '2026-08-28T00:00:00Z'
    installedAt = '2026-08-28T00:01:00Z'
    protocolVersions = @('1.1')
    hostPackageId = 'manager-adapter-host'
    minHostVersion = '0.2.0'
    entryPoint = 'runner.exe'
    files = @($payloadFiles) + @([ordered]@{ path='promotion-receipt.json'; sha256=$receiptHash; sizeBytes=(Get-Item -LiteralPath $receiptPath).Length })
    supportedGameIds = @('StarRail', 'WW')
    operationBindings = [ordered]@{
        StarRail = [ordered]@{
            'observe-panel' = [ordered]@{
                handlerId = 'starrail.observe-panel'
                mode = 'granular'
                actionClass = 'observation'
                risk = 'observe_only'
                todoDefinitionIds = @('starrail.observe-panel')
                adapterCapabilityRefs = @('game.daily.run@1.0')
                supportsResume = $true
                timeoutSeconds = 300
                requiredEvidenceKinds = @('game-ui-task-result')
            }
        }
    }
    forbiddenOperationClasses = $forbidden
    limits = [ordered]@{ maxRequestBytes = 262144; maxEventBytes = 65536; maxArtifactsPerTodo = 20 }
    artifactPolicy = [ordered]@{ allowedMimeTypes = @('image/png', 'image/jpeg', 'text/plain'); maxArtifactBytes = 20971520 }
    security = [ordered]@{ allowsArbitraryCommand = $false; allowsArbitraryPath = $false; allowsArbitraryInput = $false }
    promotion = [ordered]@{
        status = 'promoted'
        replaySuiteDigest = 'sha256:' + ('1' * 64)
        shadowSuiteDigest = 'sha256:' + ('2' * 64)
        canarySuiteDigest = 'sha256:' + ('3' * 64)
        payloadDigest = $payloadDigest
        receiptFile = 'promotion-receipt.json'
        receiptSha256 = 'sha256:' + $receiptHash
        receiptResourceId = $receiptResourceId
    }
    executionReady = $true
}
$manifest.operationBindings['WW'] = $manifest.operationBindings.StarRail
$manifest | ConvertTo-Json -Depth 12 -Compress | Set-Content -LiteralPath (Join-Path $runnerRoot 'install-manifest.json') -Encoding utf8NoBOM

$todoId = 'todo-instance-33333333-3333-4333-8333-333333333333'
$request = [ordered]@{
    schemaVersion = 1
    protocolVersion = '1.1'
    requestType = 'execute'
    runId = '11111111-1111-4111-8111-111111111111'
    runAttemptId = '22222222-2222-4222-8222-222222222222'
    fencingToken = ('a' * 32)
    cancelAuthority = ('c' * 32)
    gameId = 'StarRail'
    cadence = 'daily'
    managerStateVersion = 7
    catalogVersion = 'catalog-1'
    policyDigest = 'sha256:' + ('b' * 64)
    issuedAt = '2099-08-28T10:00:00+08:00'
    expiresAt = '2099-08-28T10:05:00+08:00'
    timeoutSeconds = 60
    preserveClientOnStop = $true
    executableTodoInstanceIds = @($todoId)
    todos = @([ordered]@{
        todoInstanceId = $todoId
        todoDefinitionId = 'starrail.observe-panel'
        definitionVersion = 1
        operation = 'observe-panel'
        risk = 'observe_only'
        adapterCapabilityRef = 'game.daily.run@1.0'
        priorAttempts = 0
        executionDisposition = 'executable'
    })
}

$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $hostPath
$startInfo.Arguments = '--operation execute --protocol-version 1.1 --run-id 11111111-1111-4111-8111-111111111111 --game-id StarRail'
$startInfo.WorkingDirectory = $hostRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.EnvironmentVariables.Clear()
$startInfo.EnvironmentVariables['SYSTEMROOT'] = $env:SYSTEMROOT
$startInfo.EnvironmentVariables['WINDIR'] = $env:WINDIR
$startInfo.EnvironmentVariables['TEMP'] = $env:TEMP
$startInfo.EnvironmentVariables['TMP'] = $env:TMP
$startInfo.EnvironmentVariables['ProgramData'] = $testProgramData
$startInfo.EnvironmentVariables['YEYU_GAMER_ADAPTER_STAGING_DIR'] = $stagingRoot
$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
if (-not $process.Start()) { throw 'Adapter Host did not start.' }
$process.StandardInput.WriteLine(($request | ConvertTo-Json -Depth 10 -Compress))
$process.StandardInput.Close()
$stdout = $process.StandardOutput.ReadToEnd()
$stderr = $process.StandardError.ReadToEnd()
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw "Adapter Host e2e failed with $($process.ExitCode): $stderr" }
$events = @($stdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
if ($events.Count -ne 5 -or $events[0].eventType -ne 'hello' -or $events[-1].eventType -ne 'run_terminal') {
    throw 'Adapter Host did not relay the complete fake runner JSONL stream.'
}
if ($events[-1].completedTodoInstanceIds[0] -ne $todoId) {
    throw 'Adapter Host changed the Todo completion scope.'
}
if (-not (Test-Path -LiteralPath (Join-Path $stagingRoot 'evidence.txt') -PathType Leaf)) {
    throw 'Fake runner did not use the Manager-owned staging directory.'
}

# Exercise the actual compiled Host stdin/package/child-transport chain. These
# are synthetic labels in an isolated fixture package, never installed games.
$wwRequest = $request | ConvertTo-Json -Depth 10 | ConvertFrom-Json -AsHashtable
$wwRequest.gameId = 'WW'
$wwRequest.accountId = '88888888-8888-4888-8888-888888888888'
$wwRequest.accountSnapshot = @{label='Fixture A';saved_account_label='fixture****example.com'}
$originalArguments = $startInfo.Arguments
try {
    $startInfo.Arguments = $originalArguments.Replace('--game-id StarRail', '--game-id WW')
    $wwProcess = [Diagnostics.Process]::new()
    $wwProcess.StartInfo = $startInfo
    if (-not $wwProcess.Start()) { throw 'WW account Host fixture did not start.' }
    $wwProcess.StandardInput.WriteLine(($wwRequest | ConvertTo-Json -Depth 10 -Compress))
    $wwProcess.StandardInput.Close()
    $wwOutput = $wwProcess.StandardOutput.ReadToEnd()
    $wwError = $wwProcess.StandardError.ReadToEnd()
    $wwProcess.WaitForExit()
    $wwEvents = @($wwOutput -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
    $received = Get-Content -LiteralPath (Join-Path $stagingRoot 'account-scope-received.json') -Raw | ConvertFrom-Json
    if ($wwProcess.ExitCode -ne 0 -or $wwError -or $wwEvents.Count -ne 5 -or
        $wwEvents[-1].status -ne 'completed' -or $received.accountId -cne $wwRequest.accountId -or
        $received.accountSnapshot.saved_account_label -cne $wwRequest.accountSnapshot.saved_account_label -or
        $received.cancelAuthorityPresent) { throw 'Compiled Host did not preserve the WW account scope to its fixture Runner.' }
    $wwProcess.Dispose()
    foreach ($accountCase in @('other-game', 'noncanonical-id', 'extra-field', 'extra-top-field', 'scalar-snapshot', 'empty-selector', 'path-selector', 'unmasked-selector')) {
        $invalid = $wwRequest | ConvertTo-Json -Depth 10 | ConvertFrom-Json -AsHashtable
        switch ($accountCase) {
            'other-game' { $invalid.gameId = 'StarRail' }
            'noncanonical-id' { $invalid.accountId = 'NOT-A-UUID' }
            'extra-field' { $invalid.accountSnapshot.password = 'placeholder' }
            'extra-top-field' { $invalid.accountCommand = 'placeholder' }
            'scalar-snapshot' { $invalid.accountSnapshot = 'invalid' }
            'empty-selector' { $invalid.accountSnapshot.saved_account_label = '' }
            'path-selector' { $invalid.accountSnapshot.saved_account_label = 'C:\fixture****' }
            'unmasked-selector' { $invalid.accountSnapshot.saved_account_label = 'unmasked' }
        }
        $startInfo.Arguments = $originalArguments.Replace('--game-id StarRail', ('--game-id ' + $invalid.gameId))
        $badAccountProcess = [Diagnostics.Process]::new()
        $badAccountProcess.StartInfo = $startInfo
        if (-not $badAccountProcess.Start()) { throw 'Invalid account fixture Host did not start.' }
        $badAccountProcess.StandardInput.WriteLine(($invalid | ConvertTo-Json -Depth 10 -Compress))
        $badAccountProcess.StandardInput.Close()
        $badAccountOutput = $badAccountProcess.StandardOutput.ReadToEnd()
        $badAccountError = $badAccountProcess.StandardError.ReadToEnd()
        $badAccountProcess.WaitForExit()
        $badAccountResult = $badAccountOutput | ConvertFrom-Json
        if ($badAccountProcess.ExitCode -ne 64 -or $badAccountError -or
            $badAccountResult.code -ne 'invalid_execute_request' -or $badAccountResult.adapterProcessStarted) {
            throw "Compiled Host accepted invalid account scope: $accountCase"
        }
        $badAccountProcess.Dispose()
    }
} finally {
    $startInfo.Arguments = $originalArguments
}

# Compatibility must not weaken receipt/payload integrity. Rebind the fixture
# manifest to each deliberately invalid receipt so rejection tests semantics,
# not just a stale file checksum. Only isolated fake packages are modified.
$validReceiptText = [IO.File]::ReadAllText($receiptPath)
$manifestPath = Join-Path $runnerRoot 'install-manifest.json'
$validManifestText = [IO.File]::ReadAllText($manifestPath)
foreach ($receiptCase in @('zero-host-digest', 'malformed-host-digest', 'other-package', 'other-payload')) {
    $invalidReceipt = $validReceiptText | ConvertFrom-Json
    switch ($receiptCase) {
        'zero-host-digest' { $invalidReceipt.hostEntryPointSha256 = 'sha256:' + ('0' * 64) }
        'malformed-host-digest' { $invalidReceipt.hostEntryPointSha256 = 'invalid' }
        'other-package' { $invalidReceipt.packageVersion = '0.1.1' }
        'other-payload' { $invalidReceipt.payloadDigest = 'sha256:' + ('9' * 64) }
    }
    try {
        [IO.File]::WriteAllText($receiptPath, ($invalidReceipt | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
        $invalidManifest = $validManifestText | ConvertFrom-Json
        $changedHash = (Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $invalidManifest.promotion.receiptSha256 = 'sha256:' + $changedHash
        $receiptFile = @($invalidManifest.files | Where-Object path -eq 'promotion-receipt.json')[0]
        $receiptFile.sha256 = $changedHash
        $receiptFile.sizeBytes = (Get-Item -LiteralPath $receiptPath).Length
        [IO.File]::WriteAllText($manifestPath, ($invalidManifest | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
        $rejectedProcess = [Diagnostics.Process]::new()
        $rejectedProcess.StartInfo = $startInfo
        if (-not $rejectedProcess.Start()) { throw "Host did not start for $receiptCase." }
        $rejectedProcess.StandardInput.WriteLine(($request | ConvertTo-Json -Depth 10 -Compress))
        $rejectedProcess.StandardInput.Close()
        $rejectedStdout = $rejectedProcess.StandardOutput.ReadToEnd()
        $rejectedStderr = $rejectedProcess.StandardError.ReadToEnd()
        $rejectedProcess.WaitForExit()
        $rejection = $rejectedStdout | ConvertFrom-Json
        if ($rejectedProcess.ExitCode -ne 78 -or $rejection.success -ne $false -or
            $rejection.code -ne 'execution_package_unavailable' -or
            $rejection.adapterProcessStarted -ne $false -or $rejectedStderr) {
            throw "Host did not reject $receiptCase before starting the fake runner."
        }
        $rejectedProcess.Dispose()
    } finally {
        [IO.File]::WriteAllText($receiptPath, $validReceiptText, [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText($manifestPath, $validManifestText, [Text.UTF8Encoding]::new($false))
    }
}

$humanStartInfo = [Diagnostics.ProcessStartInfo]::new()
$humanStartInfo.FileName = $hostPath
$humanStartInfo.Arguments = '--operation execute --protocol-version 1.1 --run-id 11111111-1111-4111-8111-111111111111 --game-id StarRail'
$humanStartInfo.WorkingDirectory = $hostRoot
$humanStartInfo.UseShellExecute = $false
$humanStartInfo.CreateNoWindow = $true
$humanStartInfo.RedirectStandardInput = $true
$humanStartInfo.RedirectStandardOutput = $true
$humanStartInfo.RedirectStandardError = $true
$humanStartInfo.EnvironmentVariables.Clear()
$humanStartInfo.EnvironmentVariables['SYSTEMROOT'] = $env:SYSTEMROOT
$humanStartInfo.EnvironmentVariables['WINDIR'] = $env:WINDIR
$humanStartInfo.EnvironmentVariables['TEMP'] = $env:TEMP
$humanStartInfo.EnvironmentVariables['TMP'] = $env:TMP
$humanStartInfo.EnvironmentVariables['ProgramData'] = $testProgramData
$humanStartInfo.EnvironmentVariables['YEYU_GAMER_ADAPTER_STAGING_DIR'] = $stagingRoot
$humanStartInfo.EnvironmentVariables['FAKE_ADAPTER_OUTCOME'] = 'human_required'
$humanProcess = [Diagnostics.Process]::new()
$humanProcess.StartInfo = $humanStartInfo
if (-not $humanProcess.Start()) { throw 'Adapter Host human_required relay did not start.' }
$humanProcess.StandardInput.WriteLine(($request | ConvertTo-Json -Depth 10 -Compress))
$humanProcess.StandardInput.Close()
$humanStdout = $humanProcess.StandardOutput.ReadToEnd()
$humanStderr = $humanProcess.StandardError.ReadToEnd()
$humanProcess.WaitForExit()
if ($humanProcess.ExitCode -ne 0) { throw "Adapter Host human_required relay failed with $($humanProcess.ExitCode): $humanStderr" }
$humanEvents = @($humanStdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
$humanTodoTerminal = @($humanEvents | Where-Object eventType -eq 'todo_terminal')
if ($humanEvents.Count -ne 5 -or $humanTodoTerminal.Count -ne 1 -or
    $humanTodoTerminal[0].status -ne 'human_required' -or $humanTodoTerminal[0].retryable -ne $false -or
    $humanEvents[-1].status -ne 'human_required' -or $humanEvents[-1].unresolvedTodoInstanceIds[0] -ne $todoId) {
    throw 'Adapter Host did not preserve the typed non-retryable human_required terminal stream.'
}

$failedStartInfo = $humanStartInfo
$failedStartInfo.EnvironmentVariables['FAKE_ADAPTER_OUTCOME'] = 'failed'
$failedProcess = [Diagnostics.Process]::new()
$failedProcess.StartInfo = $failedStartInfo
if (-not $failedProcess.Start()) { throw 'Adapter Host failed-terminal relay did not start.' }
$failedProcess.StandardInput.WriteLine(($request | ConvertTo-Json -Depth 10 -Compress))
$failedProcess.StandardInput.Close()
$failedStdout = $failedProcess.StandardOutput.ReadToEnd()
$failedStderr = $failedProcess.StandardError.ReadToEnd()
$failedProcess.WaitForExit()
if ($failedProcess.ExitCode -ne 0) { throw "Adapter Host failed-terminal relay failed with $($failedProcess.ExitCode): $failedStderr" }
$failedEvents = @($failedStdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
$failedTodoTerminal = @($failedEvents | Where-Object eventType -eq 'todo_terminal')
if ($failedEvents.Count -ne 5 -or $failedTodoTerminal.Count -ne 1 -or
    $failedTodoTerminal[0].status -ne 'failed' -or $failedTodoTerminal[0].retryable -ne $true -or
    $failedEvents[-1].status -ne 'failed' -or $failedEvents[-1].unresolvedTodoInstanceIds[0] -ne $todoId) {
    throw 'Adapter Host did not relay the typed retryable failed terminal stream.'
}

$helloOnlyStartInfo = $humanStartInfo
$helloOnlyStartInfo.EnvironmentVariables['FAKE_ADAPTER_OUTCOME'] = 'hello_only'
$helloOnlyProcess = [Diagnostics.Process]::new()
$helloOnlyProcess.StartInfo = $helloOnlyStartInfo
if (-not $helloOnlyProcess.Start()) { throw 'Adapter Host hello-only recovery test did not start.' }
$helloOnlyProcess.StandardInput.WriteLine(($request | ConvertTo-Json -Depth 10 -Compress))
$helloOnlyProcess.StandardInput.Close()
$helloOnlyStdout = $helloOnlyProcess.StandardOutput.ReadToEnd()
$helloOnlyStderr = $helloOnlyProcess.StandardError.ReadToEnd()
$helloOnlyProcess.WaitForExit()
$helloOnlyEvents = @($helloOnlyStdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
if ($helloOnlyProcess.ExitCode -ne 70 -or $helloOnlyEvents.Count -ne 2 -or
    $helloOnlyEvents[0].eventType -ne 'hello' -or $helloOnlyEvents[-1].eventType -ne 'run_terminal' -or
    $helloOnlyEvents[-1].status -ne 'failed' -or $helloOnlyEvents[-1].transportOutcome -ne 'crashed' -or
    $helloOnlyEvents[-1].unresolvedTodoInstanceIds[0] -ne $todoId -or
    [string]$helloOnlyEvents[-1].terminalEventDigest -notmatch '^sha256:(?!0{64})[0-9a-f]{64}$' -or
    $helloOnlyStderr -notmatch 'adapter_host_terminalized:runner_exited_without_terminal') {
    throw 'Adapter Host did not synthesize a typed non-success terminal after a hello-only runner exit.'
}

$deadlineRequest = (($request | ConvertTo-Json -Depth 10 -Compress) | ConvertFrom-Json)
$deadlineRequest.issuedAt = [DateTimeOffset]::UtcNow.ToString('o')
$deadlineRequest.expiresAt = [DateTimeOffset]::UtcNow.AddSeconds(10).ToString('o')
$deadlineRequest.timeoutSeconds = 1
$deadlineStartInfo = $humanStartInfo
$deadlineStartInfo.EnvironmentVariables['FAKE_ADAPTER_OUTCOME'] = 'hang_after_hello'
$deadlineProcess = [Diagnostics.Process]::new()
$deadlineProcess.StartInfo = $deadlineStartInfo
if (-not $deadlineProcess.Start()) { throw 'Adapter Host deadline recovery test did not start.' }
$deadlineProcess.StandardInput.WriteLine(($deadlineRequest | ConvertTo-Json -Depth 10 -Compress))
$deadlineProcess.StandardInput.Close()
$deadlineStdout = $deadlineProcess.StandardOutput.ReadToEnd()
$deadlineStderr = $deadlineProcess.StandardError.ReadToEnd()
$deadlineProcess.WaitForExit()
$deadlineEvents = @($deadlineStdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
if ($deadlineProcess.ExitCode -ne 70 -or $deadlineEvents.Count -ne 2 -or
    $deadlineEvents[-1].status -ne 'failed' -or $deadlineEvents[-1].transportOutcome -ne 'crashed' -or
    $deadlineStderr -notmatch 'adapter_host_terminalized:runner_deadline_exceeded') {
    throw 'Adapter Host did not terminalize a runner at the execution deadline.'
}

$cancelRequest = (($request | ConvertTo-Json -Depth 10 -Compress) | ConvertFrom-Json)
$cancelRequest.runAttemptId = [guid]::NewGuid().ToString()
$cancelRequest.issuedAt = [DateTimeOffset]::UtcNow.ToString('o')
$cancelRequest.expiresAt = [DateTimeOffset]::UtcNow.AddSeconds(30).ToString('o')
$cancelRequest.timeoutSeconds = 20
$cancelAt = [DateTimeOffset]::UtcNow.ToString('o')
$cancelNonce = -join ((1..32) | ForEach-Object { 'n' })
$canonical = "1.1`ncancel`n$($cancelRequest.runId)`n$($cancelRequest.runAttemptId)`n$cancelAt`nuser_cancelled`n$cancelNonce"
$hmac = [System.Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes([string]$cancelRequest.cancelAuthority))
try { $authorityMac = 'hmac-sha256:' + (-join ($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical)) | ForEach-Object { $_.ToString('x2') })) }
finally { $hmac.Dispose() }
$durableControl = [ordered]@{
    schemaVersion=1; protocolVersion='1.1'; controlType='cancel'; runId=[string]$cancelRequest.runId;
    runAttemptId=[string]$cancelRequest.runAttemptId; at=$cancelAt; reasonCode='user_cancelled'; nonce=$cancelNonce; authorityMac=$authorityMac
}
$controlRoot = Join-Path $testProgramData 'YeYuGamer\runtime\adapter-controls'
$pendingControl = Join-Path $controlRoot ($cancelRequest.runAttemptId + '.cancel.json')
$deliveredControl = Join-Path $controlRoot ($cancelRequest.runAttemptId + '.delivered.json')
New-Item -ItemType Directory -Path $controlRoot -Force | Out-Null
$cancelStartInfo = $humanStartInfo
$cancelStartInfo.EnvironmentVariables['FAKE_ADAPTER_OUTCOME'] = 'wait_cancel'
$cancelProcess = [Diagnostics.Process]::new()
$cancelProcess.StartInfo = $cancelStartInfo
if (-not $cancelProcess.Start()) { throw 'Adapter Host durable-cancel test did not start.' }
$cancelProcess.StandardInput.WriteLine(($cancelRequest | ConvertTo-Json -Depth 10 -Compress))
$cancelProcess.StandardInput.Flush()
$aclDeadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
do {
    $controlAcl = Get-Acl -LiteralPath $controlRoot
    if ($controlAcl.AreAccessRulesProtected) { break }
    Start-Sleep -Milliseconds 50
} while ([DateTimeOffset]::UtcNow -lt $aclDeadline -and -not $cancelProcess.HasExited)
if (-not $controlAcl.AreAccessRulesProtected) {
    throw 'Adapter Host did not protect the durable-control directory DACL.'
}
$expectedControlSids = @(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
    'S-1-5-18',
    'S-1-5-32-544'
)
$actualControlSids = @($controlAcl.Access | ForEach-Object {
    $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
} | Sort-Object -Unique)
if (@(Compare-Object ($expectedControlSids | Sort-Object) $actualControlSids).Count -ne 0 -or
    @($controlAcl.Access | Where-Object { $_.AccessControlType -ne 'Allow' -or $_.FileSystemRights -notmatch 'FullControl' }).Count -ne 0) {
    throw 'Adapter Host durable-control directory DACL differs from the fixed local principals.'
}
$pendingTemporary = $pendingControl + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
[IO.File]::WriteAllText($pendingTemporary, ($durableControl | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $pendingTemporary -Destination $pendingControl
$cancelProcess.StandardInput.Close()
$cancelStdout = $cancelProcess.StandardOutput.ReadToEnd()
$cancelStderr = $cancelProcess.StandardError.ReadToEnd()
$cancelProcess.WaitForExit()
$cancelEvents = @($cancelStdout -split "`r?`n" | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
if ($cancelProcess.ExitCode -ne 0 -or $cancelEvents.Count -ne 2 -or $cancelEvents[-1].status -ne 'cancelled' -or
    $cancelEvents[-1].transportOutcome -ne 'cancelled' -or -not (Test-Path -LiteralPath $deliveredControl -PathType Leaf) -or
    (Test-Path -LiteralPath $pendingControl) -or $cancelStderr) {
    throw "Adapter Host did not authenticate, relay, and acknowledge the durable cancel: $cancelStderr"
}
Remove-Item -LiteralPath $deliveredControl -Force
[pscustomobject]@{
    protocolVersion = '1.1'
    eventCount = $events.Count
    completedTodoInstanceId = $todoId
    humanRequiredRelayed = $true
    humanRequiredRetryable = [bool]$humanTodoTerminal[0].retryable
    failedRelayed = $true
    failedRetryable = [bool]$failedTodoTerminal[0].retryable
    helloOnlyTerminalized = $true
    deadlineTerminalized = $true
    durableCancelRelayed = $true
    controlAclProtected = $true
    priorHostPromotionAccepted = $true
    invalidPromotionRejected = $true
    wwAccountScopeRelayed = $true
    invalidAccountScopeRejected = $true
    gameProcessStarted = $false
}
