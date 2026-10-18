function Test-YeYuGf2ApprovedSource {
    param([string]$RegistryPath, [object]$Manifest, [string]$WorkingRoot)
    $registry = Get-Content -LiteralPath $RegistryPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    if ($registry.schemaVersion -ne 1) { throw 'Unsupported GF2 source registry schema.' }
    $record = if ($Manifest -is [System.Collections.IDictionary]) { $Manifest } else {
        $Manifest | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable
    }
    foreach ($row in $registry.candidates) {
        if ($row.candidateKind -cne $record.candidateKind) { continue }
        $matches = $true
        foreach ($field in @('repository','commit','tag','officialBaseCommit')) {
            if ($row.ContainsKey($field) -and $row[$field] -cne $record.source[$field]) { $matches = $false }
        }
        if ($row.ContainsKey('inspectionVersion') -ne $record.source.ContainsKey('inspectionVersion') -or
            ($row.ContainsKey('inspectionVersion') -and $row.inspectionVersion -cne $record.source.inspectionVersion)) { $matches = $false }
        foreach ($relative in $row.files.Keys) {
            $file = [IO.Path]::GetFullPath((Join-Path $WorkingRoot $relative))
            if (!$file.StartsWith($WorkingRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase) -or
                !(Test-Path -LiteralPath $file -PathType Leaf) -or
                (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $row.files[$relative]) { $matches = $false }
        }
        if ($matches) { return $true }
    }
    return $false
}
