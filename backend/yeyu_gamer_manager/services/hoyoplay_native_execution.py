"""One bounded native UIA session owned by an existing Manager operation.

No API, profile switch, network port, game launch or observer mutation is added.
The readonly observer's identity/property routines are reused without calling
its acquisition entry point. Only this module has UIA Invoke/Expand capability.
"""
from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
import time
import uuid
from typing import Callable

from .game_launcher import GameLaunchCancelled, GameLaunchError, GameLaunchService
from .hoyoplay_native_observation import (
    _NATIVE_WORKER, _HelperJob, _PinnedLauncher, NativeHoYoPlaySampler, _known_game_names, FIXED_GAME_EXECUTABLE,
    MAX_AX_BYTES, MAX_HELPER_BYTES,
)

_NATIVE_PHASES = frozenset({"association", "launcher-signature", "process-inventory",
    "launcher-identity", "window-enumeration", "window-identity", "uia-from-handle",
    "uia-current", "uia-properties", "uia-document-value", "uia-document-text",
    "uia-first-child", "uia-next-sibling", "view-final-identity", "response-serialization", "response-write"})


@dataclass(frozen=True)
class NativeContext:
    task_id: str
    manager_id: str
    output_dir: Path
    fence: Callable[[], AbstractContextManager]
    record: Callable[[dict], None]


def error(reason, *, detail=None):
    return GameLaunchError("受限 HoYoPlay 会话未取得可确认结果。", reason_code="zzz_launcher_" + reason,
                           detail=detail)


class NativeHoYoPlaySession:
    def __init__(self, context, cancelled):
        if not isinstance(context, NativeContext):
            raise error("native_context_missing")
        self.context, self.cancelled = context, cancelled
        self.task_id = str(uuid.UUID(context.task_id))
        self.process = self.job = self.reader = self.stderr = None
        self._assigned = False
        self.started = time.monotonic()
        self.sequence = 0
        self.closed = True
        self._journal_started = self._finalized = False
        self._poisoned = False
        self._responses = Queue(maxsize=2)
        self._record = {"taskId": self.task_id, "managerId": context.manager_id,
                        "purpose": "hoyoplay-official-preflight", "parentPid": os.getpid(),
                        "source": __name__, "consoleHidden": True, "helperClosed": True,
                        "gameLaunchRequested": False, "nativeInvocationCount": 0}

    def guard(self):
        if self.cancelled():
            raise GameLaunchCancelled("HoYoPlay 会话已被 Manager 取消。", reason_code="zzz_launcher_cancelled")
        if time.monotonic() - self.started >= 3600:
            raise error("hard_timeout")
        if self._poisoned:
            raise error("unknown_action_outcome")

    def _read(self):
        try:
            while True:
                raw = self.process.stdout.readline(MAX_AX_BYTES + 1)
                if not raw or len(raw) > MAX_AX_BYTES or not raw.endswith(b"\n"):
                    self._responses.put_nowait(error("native_response_invalid"))
                    return
                self._responses.put_nowait(json.loads(raw.decode("utf-8-sig")))
        except Exception:
            try:
                self._responses.put_nowait(error("native_response_invalid"))
            except Exception:
                pass

    def start(self):
        self.guard()
        if self.process is not None or os.name != "nt":
            raise error("native_session_unavailable")
        output = self.context.output_dir
        # data_dir exists; launcher-executions may be created for the first run.
        # Validate the existing ancestor before mkdir, then the new directory.
        GameLaunchService._require_queue_local_path(output.parent.parent)
        output.parent.mkdir(exist_ok=True)
        GameLaunchService._require_queue_local_path(output.parent)
        output.mkdir(exist_ok=False)
        GameLaunchService._require_queue_local_path(output)
        # Reuse signed, pinned cold-start admission. Never assign the official
        # launcher to the helper Job or create a new game-start path.
        self.context.record({**self._record, "event": "native-start-issued", "helperClosed": False})
        self._journal_started = True
        admission_dir = output / "admission"
        admission_dir.mkdir()
        def registered(entry):
            self.context.record({**self._record, "event": "native-admission-record", "helperClosed": False, "child": entry})
        sampler = NativeHoYoPlaySampler(FIXED_GAME_EXECUTABLE, cancelled=self.cancelled,
            register_helper=registered, task_id=self.task_id, manager_id=self.context.manager_id,
            launch_guard=self.context.fence)
        sampler._deadline = time.monotonic() + 15
        try:
            sampler._validate_inputs(admission_dir)
            with _PinnedLauncher() as pinned:
                admitted = sampler._run_worker("admit", admission_dir)
                sampler._validate_processes(admitted.get("processes"))
                if not admitted["processes"]:
                    self.context.record({**self._record, "event": "launcher-start-issued", "helperClosed": False})
                    sampler._start_launcher(admission_dir, pinned)
        except BaseException:
            self.closed = sampler._helper_closed
            self.context.record({**self._record, "event": "native-closed", "helperClosed": self.closed,
                                 "remainingHelperPids": sorted(sampler._remaining_helpers)})
            self._finalized = True
            raise
        script = output / "session.ps1"
        script.write_text(native_script(), encoding="utf-8")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        names = _known_game_names()
        encoded = json.dumps(names)
        env = dict(os.environ)
        env.update(PSModulePath=str(powershell.parent / "Modules"), YEYU_HYP_SCRIPT=str(script),
                   YEYU_HYP_PHASE=str(output / "session.phase.json"),
                   YEYU_HYP_SAMPLE_ID=self.task_id, YEYU_HYP_GAME=str(FIXED_GAME_EXECUTABLE),
                   YEYU_HYP_GAME_NAMES=encoded, YEYU_HYP_GAME_NAMES_COUNT=str(len(names)),
                   YEYU_HYP_GAME_NAMES_SHA256=hashlib.sha256(encoded.encode()).hexdigest())
        self.stderr_path = output / "session.stderr.log"
        self.stderr = self.stderr_path.open("xb")
        try:
            self.job = _HelperJob()
            # Persist intent before creation. A crash from here never replays.
            with self.context.fence():
                self.guard()
                # Keep all UIA elements on one dedicated MTA for the session.
                self.process = subprocess.Popen([str(powershell), "-Mta", "-NoProfile", "-NonInteractive", "-Command",
                    "& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:YEYU_HYP_SCRIPT,[Text.Encoding]::UTF8)))"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW | 4, bufsize=0)
            self.closed = False
            self.job.assign(self.process)
            self._assigned = True
            identity = GameLaunchService._queue_process_identity(self.process.pid)
            if identity is None or Path(identity.executable) != powershell:
                raise error("helper_identity_missing")
            self._record.update(pid=self.process.pid, createdAt=identity.created_at, helperClosed=False,
                                logPath=str(self.stderr_path), scriptPath=str(script),
                                hardJobMemoryLimitBytes=MAX_HELPER_BYTES, jobAssigned=True)
            self.context.record({**self._record, "event": "native-registered"})
            with self.context.fence():
                self.guard()
                self.job.resume(self.process)
            self.reader = threading.Thread(target=self._read, name="hoyoplay-owned-response", daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise

    def request(self, operation, **fields):
        self.guard()
        if self.process is None or self.closed or operation not in {"observe", "invoke"}:
            raise error("native_session_unavailable")
        self.sequence += 1
        message = {"sequence": self.sequence, "operation": operation, **fields}
        encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        if len(encoded) > 8192:
            raise error("request_limit")
        invocation = operation == "invoke"
        if (set(fields) != {"action", "nonce", "index"} if invocation else bool(fields)) or len(encoded) > 1024:
            raise error("request_contract_invalid")
        if invocation:
            self._record["nativeInvocationCount"] += 1
            self.context.record({**self._record, "event": "action-issued", "sequence": self.sequence,
                                 "action": fields.get("action"), "outcome": "unknown"})
        started = time.monotonic()
        try:
            # Atomic with Stop only while writing the small pipe request, never
            # while waiting for a UI provider. Native code remains killable.
            with self.context.fence():
                self.guard()
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            while True:
                self.guard()
                if time.monotonic() - started > 15 or self.stderr_path.stat().st_size > 65536:
                    raise error("native_request_timeout", detail={"requestSequence": self.sequence,
                        "requestOperation": operation, "requestLimitSeconds": 15,
                        "nativePhase": self._native_phase()})
                for pid in self.job.members():
                    if pid == self.process.pid or pid in self._record.get("descendantPids", []):
                        continue
                    identity = GameLaunchService._queue_process_identity(pid)
                    parent = self.job.parent_pid(pid)
                    if identity and parent:
                        self._record.setdefault("descendantPids", []).append(pid)
                        self.context.record({**self._record, "event": "native-descendant", "child": {
                            "pid": pid, "parentPid": parent, "createdAt": identity.created_at,
                            "source": str(identity.executable), "ownership": "inherited-helper-job"}})
                try:
                    response = self._responses.get(timeout=.05)
                except Empty:
                    continue
                if isinstance(response, Exception):
                    raise response
                if not isinstance(response, dict) or response.get("sequence") != self.sequence:
                    raise error("native_sequence_invalid")
                if response.get("ok") is not True:
                    reason = response.get("reason", "")
                    if not isinstance(reason, str) or not re.fullmatch(r"[a-z_-]{1,80}", reason):
                        reason = "native_contract_rejected"
                    detail = None
                    if reason == 'owner-changed':
                        from .hoyoplay_owner_diagnostics import owner_diagnostic
                        value = owner_diagnostic(response.get('ownerDiagnostic'), source='execution',
                            task_id=self.task_id, helper_pid=self.process.pid, sequence=self.sequence)
                        if value is not None:
                            detail = {'ownerDiagnostic':value}
                    raise error(reason.replace("-", "_"), detail=detail)
                if invocation:
                    if response.get("invoked") is not True or response.get("action") != fields.get("action"):
                        raise error("action_receipt_missing")
                    self.context.record({**self._record, "event": "action-returned", "sequence": self.sequence,
                                         "action": fields.get("action"), "outcome": "invoked",
                                         "nativeEvidence": response.get("evidence")})
                else:
                    # One bounded latest sample; no unbounded snapshot history.
                    (self.context.output_dir / "latest.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
                return response
        except BaseException:
            self._poisoned = True  # a partial write or lost response is terminal
            raise

    def _native_phase(self):
        """Bounded enum/counter diagnostics only; never UI text or provider errors."""
        try:
            with (self.context.output_dir / "session.phase.json").open("rb") as stream:
                raw = stream.read(2049)
            if len(raw) > 2048:
                return None
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("taskId") != self.task_id
                    or value.get("pid") != self.process.pid or value.get("sequence") != self.sequence
                    or not isinstance(value.get("phase"), str)
                    or value["phase"] not in _NATIVE_PHASES
                    or type(value.get("node")) is not int or not -1 <= value["node"] <= 256
                    or value.get("apartment") not in {"STA", "MTA"}):
                return None
            return {key: value[key] for key in ("phase", "node", "apartment")}
        except (OSError, ValueError, TypeError):
            return None

    def close(self):
        if self._finalized:
            return
        if self.job is None:
            if self.stderr:
                self.stderr.close()
            if self._journal_started:
                self.context.record({**self._record, "event": "native-closed", "helperClosed": self.closed,
                                     "remainingHelperPids": []})
            self._finalized = True
            return
        closed, remaining = False, []
        try:
            if self.process is not None and not self._assigned and self.process.poll() is None:
                # Only the Popen handle we created suspended, never a PID from
                # a snapshot or any launcher/client process.
                self.process.kill()
                self.process.wait(timeout=2)
            closed, remaining = self.job.finish()
            if self.process:
                self.process.wait(timeout=2)
        finally:
            self.job.close()
            self.job = None
            if self.process:
                for pipe in (self.process.stdin, self.process.stdout):
                    if pipe:
                        pipe.close()
            if self.reader:
                self.reader.join(timeout=2)
                closed = closed and not self.reader.is_alive()
            if self.stderr:
                self.stderr.close()
            self.closed = closed
            self._record.update(helperClosed=closed, remainingHelperPids=remaining,
                                exitCode=self.process.poll() if self.process else None)
            (self.context.output_dir / "session.process.json").write_text(json.dumps(self._record), encoding="utf-8")
            self.context.record({**self._record, "event": "native-closed"})
            self._finalized = True
        if not closed:
            raise error("helper_cleanup_unconfirmed")


def native_script():
    # Delimited, reviewed common identity and readonly property routines. A
    # changed layout fails closed in tests/import, rather than running the
    # readonly acquisition entry point or maintaining a second signature policy.
    prefix, marker, _ = _NATIVE_WORKER.partition("\ntry {\n    Install-Association\n")
    _, assembly_marker, tail = _NATIVE_WORKER.partition("    Add-Type -AssemblyName UIAutomationClient\n")
    shared, end_marker, _ = tail.partition("    $windows = @(); $shots = @(); $truncated = $false; $nodeTotal = 0\n")
    if not marker or not assembly_marker or not end_marker:
        raise error("shared_native_contract_changed")
    game_check = "    $null = Local-Path $game\n"
    if prefix.count(game_check) != 1:
        raise error("shared_native_contract_changed")
    # Only after this very worker has returned from its own Update invocation
    # may patching temporarily remove the fixed EXE. Keep its fixed directory,
    # no-reparse and configuration checks. Every action still requires the EXE.
    prefix = prefix.replace(game_check, """    if ($script:ownedUpdate -and -not [IO.File]::Exists($game)) {
        $null = Local-Path ([IO.Path]::GetDirectoryName($game))
    } else { $null = Local-Path $game }
""", 1)
    return prefix + "\nAdd-Type -AssemblyName UIAutomationClient\n" + shared + _SESSION_WORKER


_SESSION_WORKER = r'''
$sessionTimer=[Diagnostics.Stopwatch]::StartNew()
$last=$null; $lastElements=@(); $lastSequence=0
$lastObservedTicks=0
$script:ownedUpdate=$false
function Session-Phase([string]$phase, [int]$node=-1) {
    # One overwritten, bounded record. No names, URLs, input or provider strings.
    # Keep one owned handle: reopening per property can consume the entire
    # freshness budget on Windows. Flush managed bytes before a provider call.
    if($null -eq $script:phaseStream) {
        $script:phaseStream=[IO.File]::Open($env:YEYU_HYP_PHASE,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite)
    }
    $phaseRecord=@{taskId=$sampleId;pid=$PID;sequence=$lastSequence;phase=$phase;node=$node;
        apartment=[Threading.Thread]::CurrentThread.GetApartmentState().ToString()}
    $bytes=[Text.Encoding]::UTF8.GetBytes(($phaseRecord|ConvertTo-Json -Compress))
    $script:phaseStream.Position=0;$script:phaseStream.Write($bytes,0,$bytes.Length)
    $script:phaseStream.SetLength($bytes.Length);$script:phaseStream.Flush()
}
function Session-Guard {
    Limit-Time
    if ($sessionTimer.Elapsed.TotalSeconds -ge 3600) { throw 'session-deadline' }
}
function Live-View {
    Session-Guard; Session-Phase 'association'; $inventoryChecks.Clear(); Install-Association
    Session-Phase 'launcher-signature'
    $null=Signed-Image (Join-Path $root 'launcher.exe')
    Session-Phase 'process-inventory'
    $all=@(No-Games 'execution'); $processes=@()
    foreach($p in $all) {
        if($p.Name -in @('launcher.exe','HYP.exe') -and $p.ExecutablePath -and
            $p.ExecutablePath.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) {
            Session-Phase 'launcher-identity'; $processes+=Identity $p.ProcessId }
    }
    if($processes.Count -gt 32) { throw 'launcher-process-limit' }
    if($processes.Count -eq 0) { return @{waiting=$true} }
    Session-Phase 'window-enumeration'
    $handles=@([HYPReadOnly]::Windows([int[]]@($processes | ForEach-Object { $_.pid })))
    # An extra native window is not assumed to be a harmless popup.
    if($handles.Count -eq 0) { return @{waiting=$true} }
    if($handles.Count -ne 1) { throw 'window-ambiguous' }
    $hwnd=$handles[0]; $number=[HYPReadOnly]::Owner($hwnd)
    $identity=@($processes | Where-Object {$_.pid -eq $number})[0]
    Session-Phase 'window-identity'; Same-Identity $identity
    Begin-OwnerFamily $identity $hwnd
    Session-Phase 'uia-from-handle'
    $rootElement=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
    $queue=[Collections.Generic.Queue[object]]::new(); $nodes=[Collections.Generic.List[object]]::new()
    $viewStarted=[Diagnostics.Stopwatch]::GetTimestamp()
    $elements=[Collections.Generic.List[object]]::new(); $budget=@{used=0;limit=196608}
    $queue.Enqueue(@{element=$rootElement;parent=-1;depth=0})
    $walker=[Windows.Automation.TreeWalker]::RawViewWalker
    while($queue.Count -gt 0) {
        Session-Guard
        if($nodes.Count -ge 256) { throw 'tree-limit' }
        Session-Phase 'uia-current' $nodes.Count
        $item=$queue.Dequeue(); $current=$item.element.Current
        $observedPid=$current.ProcessId
        try {$ownerProof=Node-OwnerProof $item.element $current ($item.parent -eq -1)} catch {
            $script:ownerDiagnostic=Owner-Diagnostic $current $number $observedPid $hwnd $nodes.Count $item.parent $item.depth 'execution' $lastSequence
            throw 'owner-changed'
        }
        $index=$nodes.Count; $private=$current.IsPassword -or $current.ControlType -eq [Windows.Automation.ControlType]::Edit
        Session-Phase 'uia-properties' $index
        $node=@{index=$index;parent=$item.parent;controlType=$current.ControlType.ProgrammaticName;
            name='';enabled=$current.IsEnabled;offscreen=$current.IsOffscreen;privateInput=$private;
            dialog=($current.ControlType -eq [Windows.Automation.ControlType]::Window -or $current.LocalizedControlType -in @('dialog','对话框'));
            uia=(Read-UIA $item.element $current $hwnd $ownerProof)}
        if(-not $private) {
            if($current.ControlType -eq [Windows.Automation.ControlType]::Document) {
                Session-Phase 'uia-document-value' $index
                $value=Optional-Property $item.element ([Windows.Automation.ValuePattern]::ValueProperty)
                if($value -isnot [string]) {$value=''}
                $projection=Project-Document $current.Name $value '' $false
                $pattern=$null; $text=''; $available=$false
                if($null -ne $projection.location -and $projection.location.route -ceq '/nap_cn' -and
                    $projection.location.origin -ceq 'https://hyp-api.mihoyo.com') {
                    Session-Phase 'uia-document-text' $index
                    if($item.element.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern,[ref]$pattern)) {
                        $text=$pattern.DocumentRange.GetText(4097);$available=$true
                    }
                }
                $projection=Project-Document $current.Name $value $text $available
                if($projection.truncated) {throw 'document-limit'}
                $node.documentLocation=$projection.location;$node.documentText=$projection.text
                if($projection.nameRedacted) {$node.name=$projection.nameLabel} else {$node.name=$current.Name}
            } else { $node.name=$current.Name }
        }
        if($node.name.Length -gt 128 -or -not (Take-NodeBudget $budget $node)) {throw 'node-limit'}
        $nodes.Add($node);$elements.Add($item.element)
        Session-Phase 'uia-first-child' $index
        $child=$walker.GetFirstChild($item.element)
        if($null -ne $child -and $item.depth -ge 12) {throw 'depth-limit'}
        while($null -ne $child) {
            Session-Guard
            if($queue.Count+$nodes.Count -ge 256) {throw 'tree-limit'}
            $queue.Enqueue(@{element=$child;parent=$index;depth=$item.depth+1})
            Session-Phase 'uia-next-sibling' $index
            $child=$walker.GetNextSibling($child)
        }
    }
    $docs=@($nodes | Where-Object {$_.controlType -ceq 'ControlType.Document' -and -not $_.offscreen})
    if($docs.Count -ne 1 -or $docs[0].documentLocation.origin -cne 'https://hyp-api.mihoyo.com' -or
       $docs[0].documentLocation.path -cne '/hypfe/index.html' -or $docs[0].documentLocation.route -cne '/nap_cn' -or
       $docs[0].documentLocation.status -cne 'unqualified' -or $docs[0].documentText -ceq 'unknown') {throw 'selection-unknown'}
    Session-Phase 'view-final-identity'; Install-Association; Same-Identity $identity; Recheck-OwnerFamily
    if([HYPReadOnly]::Owner($hwnd) -ne $number) {throw 'window-changed'}
    if(([Diagnostics.Stopwatch]::GetTimestamp()-$viewStarted)/[Diagnostics.Stopwatch]::Frequency -gt 2) {throw 'live-view-stale'}
    $script:liveElements=$elements.ToArray()
    return @{nonce=([Guid]::NewGuid().ToString());process=$identity;hwnd=$hwnd;nodes=@($nodes.ToArray());
        truncated=$false;selectedDocumentCount=1;installationAssociation=$installationAssociation;
        gameExecutablePresent=[IO.File]::Exists($game)}
}
function Node-Key($node) {
    return (@{name=$node.name;parent=$node.parent;role=$node.controlType;enabled=$node.enabled;offscreen=$node.offscreen;
        dialog=$node.dialog;privateInput=$node.privateInput;documentText=$node.documentText;
        runtimeId=$node.uia.runtimeId;ownerPid=$node.uia.ownerPid;ownerHwnd=$node.uia.ownerHwnd;ownerIdentity=$node.uia.ownerIdentity} | ConvertTo-Json -Depth 6 -Compress)
}
function Dispatch-Action($request,$previous) {
    if($null -eq $previous -or $request.nonce -cne $previous.nonce) {throw 'stale-request'}
    if(([Diagnostics.Stopwatch]::GetTimestamp()-$lastObservedTicks)/[Diagnostics.Stopwatch]::Frequency -gt 2) {throw 'stale-request'}
    $allowed=@{'check'=@('检查更新');'verify'=@('一键修复');'confirm-repair'=@('开始修复');'ack-check'=@('知道了');'expand-menu'=@('');'update'=@('更新','下载')}
    if(-not $allowed.ContainsKey([string]$request.action)) {throw 'forbidden-action'}
    $index=[int]$request.index
    if($index -lt 0 -or $index -ge $previous.nodes.Count) {throw 'invalid-target'}
    $old=$previous.nodes[$index]
    if($old.name -cnotin $allowed[$request.action]) {throw 'label-changed'}
    $expectedRole=if($request.action -in @('confirm-repair','ack-check','update')) {'ControlType.Button'} else {'ControlType.MenuItem'}
    if($old.controlType -cne $expectedRole) {throw 'role-changed'}
    $fresh=Live-View
    if($fresh.process.pid -ne $previous.process.pid -or $fresh.process.createdAt -ne $previous.process.createdAt -or
        $fresh.hwnd -ne $previous.hwnd -or $fresh.installationAssociation.configSha256 -cne $previous.installationAssociation.configSha256 -or
        $fresh.nodes.Count -ne $previous.nodes.Count) {throw 'identity-or-tree-changed'}
    # All structural/semantic properties must survive the re-observation.
    # Runtime IDs belong to this session; never persist them as selectors.
    for($i=0;$i -lt $fresh.nodes.Count;$i++) {
        if((Node-Key $fresh.nodes[$i]) -cne (Node-Key $previous.nodes[$i])) {throw 'tree-changed'}
    }
    $node=$fresh.nodes[$index];$element=$script:liveElements[$index]
    if(-not $node.enabled -or $node.offscreen) {throw 'target-unavailable'}
    $duplicates=@($fresh.nodes | Where-Object {$_.controlType -ceq $node.controlType -and $_.name -ceq $node.name -and $_.parent -eq $node.parent -and -not $_.offscreen})
    if($duplicates.Count -ne 1) {throw 'ambiguous-target'}
    $pattern=$null
    if($request.action -ceq 'expand-menu') {
        if($node.uia.expandCollapseState -ne 0 -or -not $element.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern,[ref]$pattern)) {throw 'expand-unsupported'}
    } elseif(-not $element.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) {throw 'invoke-unsupported'}
    Session-Guard; Install-Association; Same-Identity $fresh.process
    $null = Local-Path $game
    $null=No-Games 'before-action'
    Same-NodeOwner $element $node
    if([HYPReadOnly]::Owner($fresh.hwnd) -ne $fresh.process.pid -or $element.Current.ProcessId -ne $node.uia.ownerPid -or
       -not $element.Current.IsEnabled -or $element.Current.IsOffscreen -or
       $element.Current.Name -cne $node.name -or $element.Current.ControlType.ProgrammaticName -cne $node.controlType -or
       (ConvertTo-Json -InputObject (Runtime-Identity $element) -Compress) -cne (ConvertTo-Json -InputObject $node.uia.runtimeId -Compress)) {throw 'action-boundary-changed'}
    if($request.action -ceq 'expand-menu') {$pattern.Expand()} else {$pattern.Invoke()}
    if($request.action -ceq 'update') {$script:ownedUpdate=$true}
    return @{invoked=$true;action=$request.action;evidence=@{process=$fresh.process;hwnd=$fresh.hwnd;
        configSha256=$fresh.installationAssociation.configSha256;runtimeId=$node.uia.runtimeId;ownerIdentity=$node.uia.ownerIdentity;
        role=$node.controlType;name=$node.name;pattern=([string]$(if($request.action -ceq 'expand-menu'){'ExpandCollapse'}else{'Invoke'}));
        origin='https://hyp-api.mihoyo.com';route='/nap_cn';sourceNonce=$request.nonce}}
}
try {
    while($null -ne ($line=[Console]::In.ReadLine())) {
        if([Threading.Thread]::CurrentThread.GetApartmentState() -ne [Threading.ApartmentState]::MTA) {throw 'native-apartment-invalid'}
        if($line.Length -gt 8192) {throw 'request-limit'}
        $timer.Restart();Session-Guard
        $request=ConvertFrom-Json $line
        if($request.sequence -ne $lastSequence+1 -or $lastSequence -ge 10000) {throw 'sequence-invalid'}
        $lastSequence=[int]$request.sequence
        try {
        if($request.operation -ceq 'observe') {$last=Live-View;$lastObservedTicks=[Diagnostics.Stopwatch]::GetTimestamp();$result=@{view=$last}}
        elseif($request.operation -ceq 'invoke') {$result=Dispatch-Action $request $last;$last=$null}
        else {throw 'operation-forbidden'}
        $result.ok=$true;$result.sequence=$lastSequence
        Session-Phase 'response-serialization'
        $json=ConvertTo-Json -InputObject $result -Depth 16 -Compress
        if([Text.Encoding]::UTF8.GetByteCount($json) -gt 262143) {throw 'response-limit'}
        Session-Phase 'response-write'
        [Console]::Out.WriteLine($json);[Console]::Out.Flush()
        } finally {Close-OwnerFamily}
    }
} catch {
    # Do not serialize provider exception strings or a raw Document URI.
    $reason=$_.Exception.Message
    if($reason -cnotmatch '^[a-z_-]{1,80}$') {$reason='native-contract-rejected'}
    $failure=@{ok=$false;sequence=$lastSequence;reason=$reason}
    if($reason -ceq 'owner-changed' -and $null -ne $script:ownerDiagnostic) {
        $failure.ownerDiagnostic=$script:ownerDiagnostic
    }
    [Console]::Out.WriteLine(($failure | ConvertTo-Json -Depth 3 -Compress))
    exit 2
}
'''
