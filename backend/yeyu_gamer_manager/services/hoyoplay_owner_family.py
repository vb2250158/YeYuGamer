"""Narrow reviewed HYP/HYPHelper ownership, shared by capture and execution.

Only the owned native producer creates this proof. The Python check validates
its binding; it is never a caller-supplied permission or an action selector.
"""
from pathlib import PureWindowsPath
import json

PROFILE = "hyp-1.18.0.380-helper-v1"
ROOT_IMAGE = r"D:\Game\Launchers\HoYoPlay\1.18.0.380\HYP.exe"
HELPER_IMAGE = r"D:\Game\Launchers\HoYoPlay\1.18.0.380\HYPHelper.exe"
ROOT_SHA256 = "554c578e5ff0510e459cd2dd8ba9be3a25eb712dc01c1e10dce20decaaef831a"
HELPER_SHA256 = "870501007ce67af0931ab576c9b06335cdc6126a5f898dadbb3fda816113989d"
_FIELDS = frozenset(("schemaVersion", "profile", "sampleId", "rootPid", "rootCreatedAt",
    "rootSessionId", "ownerPid", "ownerCreatedAt", "ownerParentPid", "ownerSessionId",
    "workerSessionId", "rootHwnd", "anchorHwnd", "rootSha256", "ownerSha256",
    "ownerImagePath", "signatureStatus", "signer"))


def node_owner_key(raw):
    return (raw["ownerPid"], raw["ownerHwnd"],
            json.dumps(raw.get("ownerIdentity"), sort_keys=True, separators=(",", ":")))


def valid_node_owner(raw, process, hwnd, *, root_node=False):
    """Preserve same-owner policy; foreign owners need the exact family proof."""
    if not isinstance(raw, dict) or not isinstance(process, dict):
        return False
    pid, owner = process.get("pid"), raw.get("ownerPid")
    if (type(pid) is not int or pid <= 0 or type(owner) is not int or owner <= 0
            or type(hwnd) is not int or hwnd <= 0 or type(raw.get("ownerHwnd")) is not int
            or raw["ownerHwnd"] != hwnd):
        return False
    proof = raw.get("ownerIdentity")
    if owner == pid:
        return proof is None
    if root_node or not isinstance(proof, dict) or proof.keys() != _FIELDS:
        return False
    positive = ("rootPid", "rootCreatedAt", "ownerPid", "ownerCreatedAt", "ownerParentPid", "rootHwnd", "anchorHwnd")
    sessions = ("rootSessionId", "ownerSessionId", "workerSessionId")
    if (any(type(proof[k]) is not int or not 0 < proof[k] < 2**63 for k in positive)
            or any(type(proof[k]) is not int or not 0 <= proof[k] < 2**31 for k in sessions)
            or any(proof[k] >= 2**31 for k in ("rootPid", "ownerPid", "ownerParentPid"))
            or type(process.get("sessionId")) is not int):
        return False
    return (type(proof["schemaVersion"]) is int and proof["schemaVersion"] == 1
        and proof["profile"] == PROFILE and proof["rootPid"] == pid
        and proof["rootCreatedAt"] == process.get("createdAt")
        and proof["ownerPid"] == owner and proof["ownerParentPid"] == pid
        and proof["ownerCreatedAt"] > proof["rootCreatedAt"]
        and proof["rootSessionId"] == proof["ownerSessionId"] == proof["workerSessionId"] == process["sessionId"]
        and proof["rootHwnd"] == hwnd and proof["anchorHwnd"] != hwnd
        and isinstance(process.get("sampleId"), str) and bool(process["sampleId"])
        and proof["sampleId"] == process["sampleId"]
        and proof["rootSha256"] == ROOT_SHA256 and proof["ownerSha256"] == HELPER_SHA256
        and isinstance(process.get("imagePath"), str) and PureWindowsPath(process["imagePath"]) == PureWindowsPath(ROOT_IMAGE)
        and isinstance(proof["ownerImagePath"], str) and PureWindowsPath(proof["ownerImagePath"]) == PureWindowsPath(HELPER_IMAGE)
        and proof["signatureStatus"] == "Valid" and proof["signer"] == "miHoYo Co.,Ltd.")


# Included after UIA/C# definitions in BOTH workers; no separate native policy.
OWNER_FAMILY_NATIVE = r'''
    function Close-OwnerFamily {
        foreach($stream in @($script:familyPins)) { if($null -ne $stream) {$stream.Dispose()} }
        $script:familyPins=@(); $script:familyOwners=@{}; $script:familyAnchors=@{}
        $script:familyRoot=$null; $script:familyHwnd=0
    }
    function Begin-OwnerFamily($identity, [long]$hwnd) {
        Close-OwnerFamily
        $script:familyRoot=$identity; $script:familyHwnd=$hwnd
    }
    function Family-Digest($stream) {
        $hash=[Security.Cryptography.SHA256]::Create()
        try {return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
        finally {$hash.Dispose()}
    }
    function Pin-FamilyImages {
        if($script:familyPins.Count -eq 2) {return}
        $paths=@((Join-Path $root '1.18.0.380\HYP.exe'),(Join-Path $root '1.18.0.380\HYPHelper.exe'))
        $hashes=@('554c578e5ff0510e459cd2dd8ba9be3a25eb712dc01c1e10dce20decaaef831a','870501007ce67af0931ab576c9b06335cdc6126a5f898dadbb3fda816113989d')
        if($script:familyRoot.imagePath -ine $paths[0]) {throw 'owner-family-rejected'}
        for($i=0;$i -lt 2;$i++) {
            Limit-Time; $full=Local-Path $paths[$i]
            # Keep both reviewed bytes immutable until capture/request finally.
            $stream=[IO.File]::Open($full,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
            $script:familyPins+=,$stream
            if($stream.Length -lt 1 -or $stream.Length -gt 134217728) {throw 'owner-family-rejected'}
            $digest=Family-Digest $stream
            $sig=Get-AuthenticodeSignature -LiteralPath $full
            if($digest -cne $hashes[$i] -or $sig.Status -ne 'Valid' -or $null -eq $sig.SignerCertificate -or
                $sig.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false) -cne 'miHoYo Co.,Ltd.') {throw 'owner-family-rejected'}
            $null=Local-Path $full
        }
    }
    function Read-FamilyProcess([int]$number) {
        Limit-Time
        if($number -le 0) {throw 'owner-family-rejected'}
        $p=[Diagnostics.Process]::GetProcessById($number)
        try {
            # Retain this process handle across the CIM parent read. A reused
            # PID cannot replace this live handle; HasExited rejects old owners.
            $null=$p.Handle
            $created=$p.StartTime.ToUniversalTime().ToFileTimeUtc(); $image=$p.MainModule.FileName; $session=$p.SessionId
            $rows=@(Get-CimInstance Win32_Process -Filter ("ProcessId="+$number) -Property ProcessId,ParentProcessId,ExecutablePath,SessionId -OperationTimeoutSec 1)
            Limit-Time
            if($rows.Count -ne 1 -or $p.HasExited) {throw 'owner-family-rejected'}
            $row=$rows[0]
            if($row.ExecutablePath -ine $image -or $row.SessionId -ne $session -or $row.ProcessId -ne $number -or
                $null -eq $row.ParentProcessId) {throw 'owner-family-rejected'}
            return @{pid=$number;createdAt=$created;imagePath=$image;sessionId=$session;parentPid=[int]$row.ParentProcessId}
        } finally {$p.Dispose()}
    }
    function Family-Session { return [Diagnostics.Process]::GetCurrentProcess().SessionId }
    function Owner-Parent($element) { return [Windows.Automation.TreeWalker]::RawViewWalker.GetParent($element) }
    function Check-FamilyPair($child) {
        $identity=$script:familyRoot
        $current=Read-FamilyProcess $identity.pid
        $workerSession=Family-Session
        if($current.createdAt -ne $identity.createdAt -or $current.imagePath -ine $identity.imagePath -or
            $current.imagePath -ine (Join-Path $root '1.18.0.380\HYP.exe') -or
            $current.sessionId -ne $identity.sessionId -or $current.sessionId -ne $workerSession -or
            $child.pid -eq $current.pid -or $child.parentPid -ne $current.pid -or
            $child.createdAt -le $current.createdAt -or $child.sessionId -ne $current.sessionId -or
            $child.imagePath -ine (Join-Path $root '1.18.0.380\HYPHelper.exe') -or
            [HYPReadOnly]::Owner($script:familyHwnd) -ne $current.pid) {throw 'owner-family-rejected'}
    }
    function Family-Anchor($element, [int]$owner) {
        $cursor=$element
        for($depth=0;$depth -le 12;$depth++) {
            Limit-Time
            if($null -eq $cursor) {throw 'owner-family-rejected'}
            $current=$cursor.Current
            if($current.ProcessId -ne $owner) {throw 'owner-family-rejected'}
            $anchor=[long]$current.NativeWindowHandle
            if($anchor -gt 0) {
                if([HYPReadOnly]::Owner($anchor) -ne $owner -or
                    -not [HYPReadOnly]::Descendant($script:familyHwnd,$anchor)) {throw 'owner-family-rejected'}
                return $anchor
            }
            if($anchor -lt 0) {throw 'owner-family-rejected'}
            $cursor=Owner-Parent $cursor
        }
        throw 'owner-family-rejected'
    }
    function Node-OwnerProof($element, $current, [bool]$rootNode) {
        if($current.ProcessId -eq $script:familyRoot.pid) {return $null}
        if($rootNode -or $current.ProcessId -le 0) {throw 'owner-family-rejected'}
        Pin-FamilyImages
        $number=[int]$current.ProcessId
        if(-not $script:familyOwners.ContainsKey($number)) {
            if($script:familyOwners.Count -ge 4) {throw 'owner-family-rejected'}
            $child=Read-FamilyProcess $number; Check-FamilyPair $child
            $script:familyOwners[$number]=$child
        }
        $child=$script:familyOwners[$number]; $identity=$script:familyRoot
        $anchor=Family-Anchor $element $number
        $script:familyAnchors[$anchor]=$number
        return [ordered]@{schemaVersion=1;profile='hyp-1.18.0.380-helper-v1';sampleId=$sampleId;
            rootPid=$identity.pid;rootCreatedAt=$identity.createdAt;rootSessionId=$identity.sessionId;
            ownerPid=$number;ownerCreatedAt=$child.createdAt;ownerParentPid=$child.parentPid;ownerSessionId=$child.sessionId;
            workerSessionId=$identity.sessionId;rootHwnd=$script:familyHwnd;anchorHwnd=$anchor;
            rootSha256='554c578e5ff0510e459cd2dd8ba9be3a25eb712dc01c1e10dce20decaaef831a';
            ownerSha256='870501007ce67af0931ab576c9b06335cdc6126a5f898dadbb3fda816113989d';
            ownerImagePath=$child.imagePath;signatureStatus='Valid';signer='miHoYo Co.,Ltd.'}
    }
    function Recheck-OwnerFamily {
        foreach($old in @($script:familyOwners.Values)) {
            $now=Read-FamilyProcess $old.pid; Check-FamilyPair $now
            if($now.createdAt -ne $old.createdAt -or $now.imagePath -ine $old.imagePath -or
                $now.parentPid -ne $old.parentPid -or $now.sessionId -ne $old.sessionId) {throw 'owner-family-rejected'}
        }
        foreach($anchor in @($script:familyAnchors.Keys)) {
            if([HYPReadOnly]::Owner($anchor) -ne $script:familyAnchors[$anchor] -or
                -not [HYPReadOnly]::Descendant($script:familyHwnd,$anchor)) {throw 'owner-family-rejected'}
        }
    }
    function Same-NodeOwner($element, $node) {
        Recheck-OwnerFamily
        $current=$element.Current
        if($current.ProcessId -ne $node.uia.ownerPid) {throw 'action-boundary-changed'}
        $proof=Node-OwnerProof $element $current ($node.parent -eq -1)
        if((ConvertTo-Json -InputObject $proof -Depth 4 -Compress) -cne
            (ConvertTo-Json -InputObject $node.uia.ownerIdentity -Depth 4 -Compress)) {throw 'action-boundary-changed'}
    }
'''
