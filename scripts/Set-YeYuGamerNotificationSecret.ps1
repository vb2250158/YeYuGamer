[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceJsonPath,
    [string]$RuntimeRoot,
    [string]$BindingId = 'self-email'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'YeYuGamer.Common.ps1')

if (-not $RuntimeRoot) { $RuntimeRoot = Get-YeYuGamerDefaultRuntimeRoot }
$runtimeResolved = Assert-YeYuGamerRuntimeRoot -Path $RuntimeRoot
$sourceResolved = [System.IO.Path]::GetFullPath($SourceJsonPath)
if (-not (Test-Path -LiteralPath $sourceResolved -PathType Leaf)) {
    throw 'The selected private notification profile does not exist.'
}
$sourceItem = Get-Item -LiteralPath $sourceResolved -Force
if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'The selected private notification profile may not be a reparse point.'
}
if ($sourceItem.Length -le 0 -or $sourceItem.Length -gt 64KB) {
    throw 'The selected private notification profile has an invalid size.'
}
if ($BindingId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$') {
    throw 'BindingId must be an opaque identifier.'
}

$profile = Get-Content -LiteralPath $sourceResolved -Raw -Encoding UTF8 | ConvertFrom-Json
$requiredFields = @(
    'smtpHost',
    'smtpPort',
    'smtpUsername',
    'smtpPassword',
    'senderAddress',
    'recipientAddress'
)
foreach ($field in $requiredFields) {
    if ($null -eq $profile.PSObject.Properties[$field]) {
        throw "The private notification profile is missing required field: $field"
    }
}
$port = [int]$profile.smtpPort
if ($port -lt 1 -or $port -gt 65535) { throw 'The private notification profile has an invalid SMTP port.' }
$security = if ($profile.PSObject.Properties['security']) { [string]$profile.security } else { 'starttls' }
if ($security -notin @('starttls', 'tls')) { throw 'The private notification profile has an invalid transport security mode.' }
$timeoutSeconds = if ($profile.PSObject.Properties['timeoutSeconds']) { [int]$profile.timeoutSeconds } else { 20 }
if ($timeoutSeconds -lt 5 -or $timeoutSeconds -gt 60) { throw 'The private notification profile has an invalid timeout.' }

$plaintextDocument = [ordered]@{
    schemaVersion = 1
    bindingId = $BindingId
    smtpHost = [string]$profile.smtpHost
    smtpPort = $port
    smtpUsername = [string]$profile.smtpUsername
    smtpPassword = [string]$profile.smtpPassword
    senderAddress = [string]$profile.senderAddress
    recipientAddress = [string]$profile.recipientAddress
    security = $security
    timeoutSeconds = $timeoutSeconds
}
foreach ($field in @('smtpHost', 'smtpUsername', 'smtpPassword', 'senderAddress', 'recipientAddress')) {
    if ([string]::IsNullOrWhiteSpace([string]$plaintextDocument[$field])) {
        throw "The private notification profile has an empty required field: $field"
    }
}

$secretDirectory = Assert-YeYuGamerChildPath `
    -Parent $runtimeResolved `
    -Child (Join-Path $runtimeResolved 'secrets\notifications') `
    -Purpose 'notification secrets directory'
Protect-YeYuGamerNotificationSecretsDirectory -Path $secretDirectory | Out-Null
$target = Assert-YeYuGamerChildPath `
    -Parent $secretDirectory `
    -Child (Join-Path $secretDirectory 'profile.dpapi') `
    -Purpose 'notification DPAPI profile'
$temporary = Assert-YeYuGamerChildPath `
    -Parent $secretDirectory `
    -Child (Join-Path $secretDirectory ('.profile.{0}.tmp' -f [Guid]::NewGuid().ToString('N'))) `
    -Purpose 'notification DPAPI temporary profile'

$utf8 = [System.Text.UTF8Encoding]::new($false)
$plaintextBytes = $null
$ciphertextBytes = $null
try {
    $json = $plaintextDocument | ConvertTo-Json -Depth 4 -Compress
    $plaintextBytes = $utf8.GetBytes($json)
    $entropy = $utf8.GetBytes('YeYuGamer.NotificationSecrets.v1')
    $ciphertextBytes = [Security.Cryptography.ProtectedData]::Protect(
        $plaintextBytes,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    [System.IO.File]::WriteAllBytes($temporary, $ciphertextBytes)
    Move-Item -LiteralPath $temporary -Destination $target -Force
    Protect-YeYuGamerNotificationSecretsDirectory -Path $secretDirectory | Out-Null
} finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) {
        Remove-Item -LiteralPath $temporary -Force
    }
    if ($null -ne $plaintextBytes) { [Array]::Clear($plaintextBytes, 0, $plaintextBytes.Length) }
    if ($null -ne $ciphertextBytes) { [Array]::Clear($ciphertextBytes, 0, $ciphertextBytes.Length) }
    $plaintextDocument = $null
    $profile = $null
}

Write-Host 'YeYu Gamer notification binding was stored as a DPAPI CurrentUser blob. No address or SMTP credential was printed.'
