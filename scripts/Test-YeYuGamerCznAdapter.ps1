[CmdletBinding()]
param(
    [string]$CandidateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YeYuGamer\adapter-build\czn-candidate'),
    [string]$EvidencePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$candidate = [System.IO.Path]::GetFullPath($CandidateRoot)
if ($candidate.StartsWith('\\') -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'CZN candidate is unavailable or non-local.' }
$runner = Join-Path $candidate 'runner.exe'
$manifestPath = Join-Path $candidate 'install-manifest.json'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf) -or -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'CZN candidate is incomplete.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ([string]$manifest.promotion.status -cne 'candidate' -or [bool]$manifest.executionReady) { throw 'Only an unpromoted CZN candidate may be tested.' }

$expected = @('login-bonus','achievement-schedule','arkhianon-supply','simulation-stamina')
$actual = @($manifest.operationBindings.CZN.PSObject.Properties.Name)
if (@(Compare-Object $expected $actual).Count -ne 0) { throw 'CZN candidate operation scope differs from the audited minimal set.' }
foreach ($forbidden in @('policy-office','garden-cafe','verify-daily-reward','gacha','story','roguelike','shop','save-delete')) {
    if ($null -ne $manifest.operationBindings.CZN.PSObject.Properties[$forbidden]) { throw "Forbidden CZN operation is bound: $forbidden" }
}

$testRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-tests\czn-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$profiles = @(
    @{ staminaCategory='成长'; targets=@('单元币','主战员升级材料','辅战员升级材料') },
    @{ staminaCategory='主战员'; targets=@('前锋','守卫','游侠','猎人','奥义师','操控师') },
    @{ staminaCategory='辅战员'; targets=@('前锋','守卫','游侠','猎人','奥义师','操控师') },
    @{ staminaCategory='潜能'; targets=@('热情','秩序','本能','虚无','正义') }
)
$compiled = @()
foreach ($profile in $profiles) {
    foreach ($target in $profile.targets) {
        foreach ($efficiency in 1..5) {
            $inputDocument = [ordered]@{
                staminaCategory = [string]$profile.staminaCategory
                staminaTarget = [string]$target
                battleEfficiency = $efficiency
                untilExhausted = $true
            }
            $inputText = $inputDocument | ConvertTo-Json -Compress
            $output = $inputText | & $runner --validate-profile
            if ($LASTEXITCODE -ne 0) { throw "Safe CZN profile was rejected: $inputText" }
            $result = ($output -join "`n") | ConvertFrom-Json -ErrorAction Stop
            if (-not $result.ok -or [string]$result.pipelineOverrideSha256 -notmatch '^[0-9a-f]{64}$') { throw 'CZN profile compiler returned an invalid result.' }
            $compiled += [string]$result.pipelineOverrideSha256
        }
    }
}

$denied = @(
    @{ staminaCategory='挑战'; staminaTarget='哀嚎的浪子'; battleEfficiency=5; untilExhausted=$true },
    @{ staminaCategory='记忆碎片'; staminaTarget='进化与荆棘'; battleEfficiency=4; untilExhausted=$true },
    @{ staminaCategory='成长'; staminaTarget='单元币'; battleEfficiency=4; untilExhausted=$false },
    @{ staminaCategory='成长'; staminaTarget='单元币'; battleEfficiency=0; untilExhausted=$true }
)
foreach ($inputDocument in $denied) {
    $output = ($inputDocument | ConvertTo-Json -Compress) | & $runner --validate-profile
    if ($LASTEXITCODE -eq 0) { throw 'Unsafe CZN profile was accepted.' }
}

$before = @(Get-Process -Name 'dnplayer','MFAAvalonia','com.tencent.czn' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
$probeOutput = & $runner --probe-binding
if ($LASTEXITCODE -ne 0) { throw 'CZN binding shadow probe failed.' }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
$after = @(Get-Process -Name 'dnplayer','MFAAvalonia','com.tencent.czn' -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime)
if (-not $probe.ok -or $probe.processStarted -or @($before).Count -ne @($after).Count) { throw 'CZN shadow probe started a process or returned an invalid result.' }

$replayPath = Join-Path $testRoot 'profile-replay.json'
$shadowPath = Join-Path $testRoot 'binding-shadow.json'
[System.IO.File]::WriteAllText($replayPath, ([ordered]@{
    schemaVersion=1; suite='czn-profile-compiler'; passed=$true; acceptedProfiles=$compiled.Count; rejectedProfiles=$denied.Count;
    overrideDigests=@($compiled | Sort-Object -Unique); gameStarted=$false
} | ConvertTo-Json -Depth 5 -Compress), [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($shadowPath, ([ordered]@{
    schemaVersion=1; suite='czn-binding-shadow'; passed=$true; processStarted=$false;
    beforeProcessCount=@($before).Count; afterProcessCount=@($after).Count; resourceTreeSha256=[string]$probe.resourceTreeSha256
} | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))

$manifest.promotion.replaySuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $replayPath -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest.promotion.shadowSuiteDigest = 'sha256:' + (Get-FileHash -LiteralPath $shadowPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress), [System.Text.UTF8Encoding]::new($false))

$evidence = if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('YeYuGamer\adapter-build\reports\czn-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddHHmmss') + '.json')
} else { [System.IO.Path]::GetFullPath($EvidencePath) }
if ($evidence.StartsWith('\\')) { throw 'CZN evidence path must be local.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $evidence) -Force | Out-Null
$summary = [ordered]@{
    schemaVersion=1; resourceType='adapter-candidate-test-evidence'; status='passed'; passed=$true;
    packageId=[string]$manifest.packageId; packageVersion=[string]$manifest.packageVersion; buildId=[string]$manifest.buildId;
    supportedGameIds=@('CZN'); payloadDigest=[string]$manifest.promotion.payloadDigest;
    replaySuiteDigest=[string]$manifest.promotion.replaySuiteDigest; shadowSuiteDigest=[string]$manifest.promotion.shadowSuiteDigest;
    generatedAt=[DateTimeOffset]::UtcNow.ToString('o'); gameStarted=$false
}
[System.IO.File]::WriteAllText($evidence, ($summary | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
[pscustomobject]@{ status='passed'; evidencePath=$evidence; evidence=$summary } | ConvertTo-Json -Depth 5 -Compress
