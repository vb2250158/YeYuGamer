Set-StrictMode -Version Latest

function Test-YeYuGamerNonZeroDigest {
    param([AllowEmptyString()][string]$Digest)
    if ($Digest -cnotmatch '^sha256:[0-9a-f]{64}$') { return $false }
    return $Digest.Substring(7) -cnotmatch '^0{64}$'
}

function Test-YeYuGamerTimestampWithOffset {
    param([AllowEmptyString()][string]$Timestamp)
    [DateTimeOffset]$parsed = [DateTimeOffset]::MinValue
    return $Timestamp -cmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})$' -and
        [DateTimeOffset]::TryParse(
            $Timestamp,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$parsed
        )
}

function Get-YeYuGamerPayloadDigest {
    param([Parameter(Mandatory)][object[]]$Files)
    $lines = [System.Collections.Generic.List[string]]::new()
    $byPath = [System.Collections.Generic.SortedDictionary[string,object]]::new([StringComparer]::Ordinal)
    foreach ($file in @($Files)) {
        $path = [string]$file.path
        if ($path -ceq 'promotion-receipt.json') { continue }
        $hash = ([string]$file.sha256).ToLowerInvariant()
        if (-not $path -or $hash -cnotmatch '^[0-9a-f]{64}$' -or [int64]$file.sizeBytes -lt 1) {
            throw 'Adapter payload manifest entry is invalid.'
        }
        if ($byPath.ContainsKey($path)) { throw 'Adapter payload manifest contains a duplicate path.' }
        $byPath.Add($path, $file)
    }
    foreach ($entry in $byPath.GetEnumerator()) {
        $path = [string]$entry.Key
        $file = $entry.Value
        $hash = ([string]$file.sha256).ToLowerInvariant()
        $lines.Add($path + [char]0 + [int64]$file.sizeBytes + [char]0 + $hash + "`n")
    }
    if ($lines.Count -eq 0) { throw 'Adapter payload is empty.' }
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes(($lines -join ''))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return 'sha256:' + (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }))
    }
    finally { $sha.Dispose() }
}

function Assert-YeYuGamerCandidateTestEvidence {
    param(
        [Parameter(Mandatory)][string]$EvidencePath,
        [Parameter(Mandatory)][pscustomobject]$Manifest,
        [Parameter(Mandatory)][string[]]$ExpectedGameIds
    )
    $resolved = [System.IO.Path]::GetFullPath($EvidencePath)
    if ($resolved.StartsWith('\\') -or -not (Test-Path -LiteralPath $resolved -PathType Leaf) -or
        ((Get-Item -LiteralPath $resolved -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Adapter candidate test evidence must be a local regular file.'
    }
    $evidence = Get-Content -LiteralPath $resolved -Raw -Encoding UTF8 | ConvertFrom-Json -DateKind String -ErrorAction Stop
    $required = @('schemaVersion','resourceType','status','passed','packageId','packageVersion','buildId','supportedGameIds','payloadDigest','replaySuiteDigest','shadowSuiteDigest','generatedAt','gameStarted')
    if ((@($evidence.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($required | Sort-Object) -join ',')) {
        throw 'Adapter candidate test evidence fields are invalid.'
    }
    $actualGames = @($evidence.supportedGameIds | ForEach-Object { [string]$_ })
    $manifestGames = @($Manifest.supportedGameIds | ForEach-Object { [string]$_ })
    $payloadDigest = Get-YeYuGamerPayloadDigest -Files @($Manifest.files)
    if ([int]$evidence.schemaVersion -ne 1 -or [string]$evidence.resourceType -cne 'adapter-candidate-test-evidence' -or
        [string]$evidence.status -cne 'passed' -or -not [bool]$evidence.passed -or [bool]$evidence.gameStarted -or
        [string]$evidence.packageId -cne [string]$Manifest.packageId -or
        [string]$evidence.packageVersion -cne [string]$Manifest.packageVersion -or
        [string]$evidence.buildId -cne [string]$Manifest.buildId -or
        ($actualGames -join ',') -cne (@($ExpectedGameIds) -join ',') -or
        ($manifestGames -join ',') -cne (@($ExpectedGameIds) -join ',') -or
        [string]$Manifest.promotion.payloadDigest -cne $payloadDigest -or
        [string]$evidence.payloadDigest -cne $payloadDigest -or
        [string]$evidence.replaySuiteDigest -cne [string]$Manifest.promotion.replaySuiteDigest -or
        [string]$evidence.shadowSuiteDigest -cne [string]$Manifest.promotion.shadowSuiteDigest -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$evidence.replaySuiteDigest)) -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$evidence.shadowSuiteDigest)) -or
        -not (Test-YeYuGamerTimestampWithOffset ([string]$evidence.generatedAt))) {
        throw 'Adapter candidate test evidence does not bind this package payload.'
    }
    return [pscustomobject]@{ Path=$resolved; Document=$evidence; Sha256=('sha256:' + (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()) }
}

function Install-YeYuGamerCandidateTestEvidence {
    param(
        [Parameter(Mandatory)][string]$RuntimeRoot,
        [Parameter(Mandatory)][string]$GameId,
        [Parameter(Mandatory)][string]$EvidencePath
    )
    if ($GameId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { throw 'Adapter evidence gameId is invalid.' }
    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $root = [System.IO.Path]::GetFullPath((Join-Path $runtime ('adapters\promotion-evidence\' + $GameId.ToLowerInvariant())))
    if (-not ($root + '\').StartsWith($runtime + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Adapter evidence path escaped the runtime root.'
    }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $target = Join-Path $root 'candidate-test-evidence.json'
    $temporary = Join-Path $root ('.candidate-test-evidence.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Copy-Item -LiteralPath $EvidencePath -Destination $temporary
        if (Test-Path -LiteralPath $target) { [System.IO.File]::Move($temporary, $target, $true) }
        else { Move-Item -LiteralPath $temporary -Destination $target }
    }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
    return $target
}

function Assert-YeYuGamerPromotionReceipt {
    param(
        [Parameter(Mandatory)][string]$PackageRoot,
        [Parameter(Mandatory)][pscustomobject]$Manifest,
        [Parameter(Mandatory)][string]$HostExecutable
    )
    $promotion = $Manifest.promotion
    $requiredPromotion = @('status','replaySuiteDigest','shadowSuiteDigest','canarySuiteDigest','payloadDigest','receiptFile','receiptSha256','receiptResourceId')
    if ((@($promotion.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($requiredPromotion | Sort-Object) -join ',')) {
        throw 'Adapter promotion fields do not match the receipt contract.'
    }
    [guid]$receiptResourceId = [guid]::Empty
    if ([string]$promotion.status -cne 'promoted' -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$promotion.replaySuiteDigest)) -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$promotion.shadowSuiteDigest)) -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$promotion.canarySuiteDigest)) -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$promotion.payloadDigest)) -or
        [string]$promotion.receiptFile -cne 'promotion-receipt.json' -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$promotion.receiptSha256)) -or
        -not [guid]::TryParse([string]$promotion.receiptResourceId, [ref]$receiptResourceId)) {
        throw 'Adapter promotion receipt summary is invalid.'
    }
    $computedPayload = Get-YeYuGamerPayloadDigest -Files @($Manifest.files)
    if ($computedPayload -cne [string]$promotion.payloadDigest) { throw 'Adapter payload digest differs from promotion.' }
    $receiptPath = Join-Path $PackageRoot 'promotion-receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw 'Adapter promotion receipt file is missing.' }
    $receiptHash = 'sha256:' + (Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($receiptHash -cne [string]$promotion.receiptSha256) { throw 'Adapter promotion receipt hash differs from manifest.' }
    $declaredReceipt = @($Manifest.files | Where-Object { [string]$_.path -ceq 'promotion-receipt.json' })
    if ($declaredReceipt.Count -ne 1 -or ('sha256:' + [string]$declaredReceipt[0].sha256) -cne $receiptHash -or
        [int64]$declaredReceipt[0].sizeBytes -ne (Get-Item -LiteralPath $receiptPath).Length) {
        throw 'Adapter promotion receipt is not declared as an installed file.'
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $requiredReceipt = @('schemaVersion','resourceType','resourceId','state','packageId','packageVersion','buildId','supportedGameIds','payloadDigest','replaySuiteDigest','shadowSuiteDigest','canarySuiteDigest','candidateTestEvidenceSha256','managerCanaryEvidenceSha256','hostEntryPointSha256','issuedAt')
    if ((@($receipt.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($requiredReceipt | Sort-Object) -join ',')) {
        throw 'Adapter promotion receipt fields are invalid.'
    }
    $hostHash = 'sha256:' + (Get-FileHash -LiteralPath $HostExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestGames = @($Manifest.supportedGameIds | ForEach-Object { [string]$_ })
    $receiptGames = @($receipt.supportedGameIds | ForEach-Object { [string]$_ })
    if ([int]$receipt.schemaVersion -ne 1 -or [string]$receipt.resourceType -cne 'adapter-promotion-receipt' -or
        [string]$receipt.state -cne 'passed' -or [string]$receipt.resourceId -cne [string]$promotion.receiptResourceId -or
        [string]$receipt.packageId -cne [string]$Manifest.packageId -or
        [string]$receipt.packageVersion -cne [string]$Manifest.packageVersion -or [string]$receipt.buildId -cne [string]$Manifest.buildId -or
        ($receiptGames -join ',') -cne ($manifestGames -join ',') -or
        [string]$receipt.payloadDigest -cne $computedPayload -or
        [string]$receipt.replaySuiteDigest -cne [string]$promotion.replaySuiteDigest -or
        [string]$receipt.shadowSuiteDigest -cne [string]$promotion.shadowSuiteDigest -or
        [string]$receipt.canarySuiteDigest -cne [string]$promotion.canarySuiteDigest -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$receipt.candidateTestEvidenceSha256)) -or
        -not (Test-YeYuGamerNonZeroDigest ([string]$receipt.managerCanaryEvidenceSha256)) -or
        [string]$receipt.hostEntryPointSha256 -cne $hostHash) {
        throw 'Adapter promotion receipt does not bind this package, evidence, and Host.'
    }
    if (-not (Test-YeYuGamerTimestampWithOffset ([string]$receipt.issuedAt))) { throw 'Adapter promotion receipt issuedAt is invalid.' }
    return $receipt
}
