"""Offline owner-family fixtures: no launcher, desktop or process discovery."""
import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from yeyu_gamer_manager.services import hoyoplay_owner_family as family
from yeyu_gamer_manager.services import hoyoplay_native_observation as observation
from yeyu_gamer_manager.services import hoyoplay_native_execution as execution
from yeyu_gamer_manager.services.game_launcher import GameLaunchError
from yeyu_gamer_manager.services.hoyoplay_ui_contract import interpret
from yeyu_gamer_manager.services.hoyoplay_launcher import HoYoPlayLauncher
from test_hoyoplay_execution import view
from test_hoyoplay_update_branch import available, progress


def proof(process, *, owner=72, hwnd=91):
    return dict(schemaVersion=1, profile=family.PROFILE, sampleId=process['sampleId'],
        rootPid=process['pid'], rootCreatedAt=process['createdAt'], rootSessionId=1,
        ownerPid=owner, ownerCreatedAt=process['createdAt']+100, ownerParentPid=process['pid'],
        ownerSessionId=1, workerSessionId=1, rootHwnd=hwnd, anchorHwnd=92,
        rootSha256=family.ROOT_SHA256, ownerSha256=family.HELPER_SHA256,
        ownerImagePath=family.HELPER_IMAGE, signatureStatus='Valid', signer='miHoYo Co.,Ltd.')


def helper_view(packet=None):
    packet=copy.deepcopy(packet or view())
    packet['process'].update(imagePath=family.ROOT_IMAGE, sampleId='fixture-task', sessionId=1)
    for node in packet['nodes'][1:]:
        node['uia'].update(ownerPid=72, ownerIdentity=proof(packet['process']))
    return packet


class FamilyContractTests(unittest.TestCase):
    def test_same_owner_and_reviewed_helper_keep_semantics(self):
        self.assertEqual(interpret(view()).state,interpret(helper_view()).state)
        for state in ('ready','checking','check-complete','repair-confirm','verifying','waiting'):
            self.assertEqual(state,interpret(helper_view(view(state))).state)

    def test_unknown_foreign_root_and_missing_proof_fail(self):
        for pid in (0,-1,999,True):
            packet=helper_view();packet['nodes'][1]['uia']['ownerPid']=pid
            with self.subTest(pid=pid),self.assertRaises(GameLaunchError):interpret(packet)
        for mutate in (lambda p:p['nodes'][1]['uia'].pop('ownerIdentity'),
                       lambda p:p['nodes'][0]['uia'].update(ownerPid=72,ownerIdentity=proof(p['process'])),
                       lambda p:p['nodes'][1]['uia'].update(ownerPid=71)):
            packet=helper_view();mutate(packet)
            with self.assertRaises(GameLaunchError):interpret(packet)

    def test_every_proof_binding_fails_closed(self):
        changes={'schemaVersion':True,'profile':'different','sampleId':'foreign','rootPid':99,
            'rootCreatedAt':0,'rootSessionId':2,'ownerPid':99,'ownerCreatedAt':1,'ownerParentPid':99,
            'ownerSessionId':2,'workerSessionId':2,'rootHwnd':90,'anchorHwnd':91,
            'rootSha256':'0'*64,'ownerSha256':'0'*64,'ownerImagePath':family.ROOT_IMAGE,
            'signatureStatus':'NotSigned','signer':'other','unexpected':'private'}
        for key,value in changes.items():
            packet=helper_view();packet['nodes'][1]['uia']['ownerIdentity'][key]=value
            with self.subTest(key=key),self.assertRaises(GameLaunchError):interpret(packet)

    def test_different_same_directory_image_and_version_are_not_a_family(self):
        for key,value in [('imagePath',family.ROOT_IMAGE.replace('HYP.exe','launcher.exe')),
                          ('imagePath',family.ROOT_IMAGE.replace('1.18.0.380','1.18.0.381')),
                          ('sessionId',2),('sampleId','foreign')]:
            packet=helper_view();packet['process'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(GameLaunchError):interpret(packet)

    def test_numeric_proofs_reject_boolean_negative_and_overflow(self):
        for key,value in [('ownerCreatedAt',True),('anchorHwnd',2**63),('ownerPid',2**31),
                          ('ownerParentPid',-1),('workerSessionId',2**31)]:
            packet=helper_view();packet['nodes'][1]['uia']['ownerIdentity'][key]=value
            with self.subTest(key=key),self.assertRaises(GameLaunchError):interpret(packet)

    def test_worker_integration_is_shared_and_action_recheck_precedes_input(self):
        self.assertEqual(1,observation._NATIVE_WORKER.count('function Node-OwnerProof'))
        self.assertEqual(1,execution.native_script().count('function Node-OwnerProof'))
        self.assertIn('Recheck-OwnerFamily\n        $shots +=',observation._NATIVE_WORKER)
        dispatch=execution._SESSION_WORKER.split('function Dispatch-Action',1)[1]
        self.assertLess(dispatch.index('Same-NodeOwner $element $node'),dispatch.index('$pattern.Expand()'))
        self.assertIn('} finally {Close-OwnerFamily}',execution._SESSION_WORKER)

    def test_update_scope_binds_helper_birth_and_anchor_between_views(self):
        first=interpret(helper_view(available()));_,scope=first.update_target()
        self.assertEqual('updating',interpret(helper_view(progress(10)),update_scope=scope).state)
        for key in ('ownerCreatedAt','anchorHwnd'):
            packet=helper_view(progress(10));packet['nodes'][1]['uia']['ownerIdentity'][key]+=1
            with self.subTest(key=key),self.assertRaises(GameLaunchError):interpret(packet,update_scope=scope)

    def test_settling_surface_identity_includes_helper_birth(self):
        first=interpret(helper_view(view('check-complete')))
        packet=helper_view(view('check-complete'));packet['nodes'][1]['uia']['ownerIdentity']['ownerCreatedAt']+=1
        self.assertNotEqual(HoYoPlayLauncher._surface_identity(first),HoYoPlayLauncher._surface_identity(interpret(packet)))

    def test_observation_uses_same_proof_and_rejects_detached_root_process(self):
        from test_hoyoplay_native_observation import NativeHoYoPlaySamplerTests
        fixture=NativeHoYoPlaySamplerTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        payload=fixture.fixture();payload['processes'][0]['sessionId']=1
        ax=fixture.read_ax();window=ax['windows'][0];window['ownerProcess']=copy.deepcopy(payload['processes'][0])
        root=fixture.uia_node();child=fixture.uia_node(ownerPid=72,ownerIdentity=proof(window['ownerProcess']),selectionContainer='unknown')
        child.update(index=1,parent=0);window['nodes']=[root,child];fixture.write_ax(ax)
        fixture.sampler._validate_result(payload,fixture.output)
        self.assertFalse(ax['qualification']);self.assertEqual([],ax['actions'])
        window['ownerProcess']['sessionId']=2;fixture.write_ax(ax)
        with self.assertRaises(GameLaunchError):fixture.sampler._validate_result(payload,fixture.output)


PS=r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
FAKE_IO=r'''
$ErrorActionPreference='Stop';$sampleId='fixture-task';$root='D:\Game\Launchers\HoYoPlay'
Add-Type 'public static class HYPReadOnly {
 public static int RootOwner=71, ChildOwner=72; public static bool Related=true;
 public static int Owner(long h) {return h==91?RootOwner:(h==92||h==93?ChildOwner:0);}
 public static bool Descendant(long r,long c) {return Related&&r==91&&(c==92||c==93);}
}'
function Limit-Time {}
function Pin-FamilyImages {$script:pinsChecked++}
function Family-Session {return 1}
function Owner-Parent($element) {return $element.Parent}
function Read-FamilyProcess([int]$number) {
 if(-not $script:records.ContainsKey($number)){throw 'fixture-unknown-process'}
 return $script:records[$number].Clone()
}
$identity=@{pid=71;createdAt=100;imagePath=($root+'\1.18.0.380\HYP.exe');sessionId=1}
$script:records=@{
 71=@{pid=71;createdAt=100;imagePath=$identity.imagePath;sessionId=1;parentPid=9};
 72=@{pid=72;createdAt=200;imagePath=($root+'\1.18.0.380\HYPHelper.exe');sessionId=1;parentPid=71}}
$anchor=[pscustomobject]@{Current=@{ProcessId=72;NativeWindowHandle=92};Parent=$null}
$element=[pscustomobject]@{Current=@{ProcessId=72;NativeWindowHandle=0};Parent=$anchor}
$script:pinsChecked=0
Begin-OwnerFamily $identity 91
'''


class InertFamilyNativeTests(unittest.TestCase):
    def run_ps(self,body,setup=FAKE_IO):
        # Production functions, but native/file/process I/O is replaced AFTER
        # declarations. Only in-memory fake processes/elements can be accessed.
        script=family.OWNER_FAMILY_NATIVE+'\n'+setup+'\n'+body
        command='[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))'
        result=subprocess.run([PS,'-Mta','-NoProfile','-NonInteractive','-Command',command],
            input=script.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def test_direct_and_virtual_helper_nodes_have_same_proof(self):
        result=json.loads(self.run_ps(r'''
$direct=Node-OwnerProof $anchor $anchor.Current $false
$virtual=Node-OwnerProof $element $element.Current $false
Recheck-OwnerFamily
@{direct=$direct;virtual=$virtual;pinChecks=$script:pinsChecked} | ConvertTo-Json -Depth 5 -Compress
'''))
        self.assertEqual(result['direct'],result['virtual'])
        raw=dict(ownerPid=72,ownerHwnd=91,ownerIdentity=result['direct'])
        p=dict(pid=71,createdAt=100,sessionId=1,sampleId='fixture-task',imagePath=family.ROOT_IMAGE)
        self.assertTrue(family.valid_node_owner(raw,p,91));self.assertLess(len(json.dumps(result)),2048)

    def test_actual_pair_and_anchor_checks_reject_foreign_reused_or_unknown(self):
        result=self.run_ps(r'''
$rejected=0
$mutations=@(
 { $script:records[72].parentPid=999 },{ $script:records[72].sessionId=2 },
 { $script:records[72].imagePath=$root+'\1.18.0.381\HYPHelper.exe' },
 { $script:records[72].createdAt=99 },{ $script:records[71].createdAt=101 },
 { [HYPReadOnly]::Related=$false },{ [HYPReadOnly]::ChildOwner=999 },
 { $element.Current.ProcessId=0 },{ $element.Current.ProcessId=999 },
 { $element.Parent=$null },{ $anchor.Current.ProcessId=71 },
 { $anchor.Current.NativeWindowHandle=91 },{ $identity.sessionId=2 })
foreach($mutation in $mutations) {
 $script:records[72]=@{pid=72;createdAt=200;imagePath=($root+'\1.18.0.380\HYPHelper.exe');sessionId=1;parentPid=71}
 $script:records[71].createdAt=100;$identity.sessionId=1
 [HYPReadOnly]::Related=$true;[HYPReadOnly]::ChildOwner=72
 $anchor.Current.ProcessId=72;$anchor.Current.NativeWindowHandle=92
 $element.Current.ProcessId=72;$element.Parent=$anchor
 Begin-OwnerFamily $identity 91
 & $mutation
 try {$null=Node-OwnerProof $element $element.Current $false} catch {$rejected++}
}
if($rejected -ne $mutations.Count){throw ('refusals '+$rejected)}
Begin-OwnerFamily $identity 91
try {$null=Node-OwnerProof $anchor $anchor.Current $true;throw 'accepted-root'} catch {if($_.Exception.Message -ceq 'accepted-root'){throw}}
'REFUSALS_OK '+$rejected
''')
        self.assertEqual('REFUSALS_OK 13',result)

    def test_same_pid_new_birth_is_rejected_after_tree(self):
        self.assertEqual('REUSED_PID_REFUSED',self.run_ps(r'''
$null=Node-OwnerProof $element $element.Current $false
$script:records[72].createdAt=201
try {Recheck-OwnerFamily;throw 'reused-pid-accepted'} catch {if($_.Exception.Message -ceq 'reused-pid-accepted'){throw}}
'REUSED_PID_REFUSED'
'''))

    def test_file_pins_deny_write_and_signature_hash_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            version=Path(temp)/'1.18.0.380';version.mkdir()
            for name in ('HYP.exe','HYPHelper.exe'):(version/name).write_bytes(b'abc')
            setup=r'''
$ErrorActionPreference='Stop'
function Limit-Time {}
function Local-Path($p) {return $p}
$root='''+"'"+str(Path(temp)).replace("'","''")+"'"+r'''
$identity=@{imagePath=($root+'\1.18.0.380\HYP.exe')}
$script:mode='valid'
function Get-AuthenticodeSignature($LiteralPath) {
 $cert=New-Object PSObject
 $cert | Add-Member ScriptMethod GetNameInfo {param($kind,$flag) if($script:mode -ceq 'signer'){return 'foreign'};return 'miHoYo Co.,Ltd.'}
 return @{Status=$(if($script:mode -ceq 'signature'){'NotSigned'}else{'Valid'});SignerCertificate=$cert}
}
'''
            body=r'''
$stream=[IO.File]::OpenRead($identity.imagePath)
try {$digest=Family-Digest $stream} finally {$stream.Dispose()}
if($digest -cne 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'){throw 'digest-wrong'}
function Family-Digest($stream) {
 if($script:mode -ceq 'hash'){return 'wrong'}
 if($stream.Name.EndsWith('HYPHelper.exe')){return '870501007ce67af0931ab576c9b06335cdc6126a5f898dadbb3fda816113989d'}
 return '554c578e5ff0510e459cd2dd8ba9be3a25eb712dc01c1e10dce20decaaef831a'
}
Begin-OwnerFamily $identity 91;Pin-FamilyImages
if($script:familyPins.Count -ne 2){throw 'missing-file-pin'}
$denied=0
foreach($path in @($identity.imagePath,($root+'\1.18.0.380\HYPHelper.exe'))) {
 try {$write=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite);$write.Dispose()} catch {$denied++}
}
if($denied -ne 2){throw 'mutable-pinned-image'}
Close-OwnerFamily
$write=[IO.File]::OpenWrite($identity.imagePath);$write.Dispose()
$refused=0
foreach($mode in @('hash','signature','signer')) {
 $script:mode=$mode;Begin-OwnerFamily $identity 91
 try {Pin-FamilyImages} catch {$refused++} finally {Close-OwnerFamily}
 if($script:familyPins.Count -ne 0){throw 'leaked-pin'}
}
if($refused -ne 3){throw 'invalid-pin-accepted'}
'PINS_OK 2 held 3 refusals released'
'''
            self.assertEqual('PINS_OK 2 held 3 refusals released',self.run_ps(body,setup=setup))

    def test_full_native_action_boundary_calls_no_pattern_on_family_change(self):
        functions='function Node-Key'+execution._SESSION_WORKER.split('function Node-Key',1)[1].split('\ntry {\n    while',1)[0]
        body=r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Session-Guard {}
function Install-Association {}
function Same-Identity($p) {}
function Local-Path($p) {return $p}
function Runtime-Identity($e) {return ,@($e.Rid)}
function No-Games($phase) {if($script:mutation){& $script:mutation}}
function Live-View {return $script:fixture}
$script:invocations=0;$script:mutation=$null
$pattern=New-Object PSObject
$pattern | Add-Member ScriptMethod Invoke {$script:invocations++}
$pattern | Add-Member ScriptMethod Expand {$script:invocations++}
$element | Add-Member NoteProperty Pattern $pattern
$element | Add-Member NoteProperty Rid @(42,72,2)
$element | Add-Member ScriptMethod TryGetCurrentPattern {param($kind,$reference) $reference.Value=$this.Pattern;return $true}
$element.Current.IsEnabled=$true;$element.Current.IsOffscreen=$false;$element.Current.Name='检查更新'
$element.Current.ControlType=@{ProgrammaticName='ControlType.MenuItem'}
$node=@{index=1;parent=0;name='检查更新';controlType='ControlType.MenuItem';enabled=$true;offscreen=$false;
 uia=@{ownerPid=72;ownerHwnd=91;ownerIdentity=(Node-OwnerProof $element $element.Current $false);runtimeId=@(42,72,2);expandCollapseState=0}}
$rootNode=@{index=0;parent=-1;name='root';controlType='ControlType.Window';enabled=$true;offscreen=$false;
 uia=@{ownerPid=71;ownerHwnd=91;runtimeId=@(42,71,0)}}
$script:liveElements=@($null,$element)
$script:fixture=@{nonce='fresh';process=$identity;hwnd=91;installationAssociation=@{configSha256='same'};nodes=@($rootNode,$node)}
$previous=ConvertFrom-Json ($script:fixture | ConvertTo-Json -Depth 12);$previous.nonce='prior'
$request=@{nonce='prior';action='check';index=1};$lastObservedTicks=[Diagnostics.Stopwatch]::GetTimestamp()
'''+functions+r'''
$null=Dispatch-Action $request $previous
if($script:invocations -ne 1){throw 'valid-helper-not-invoked'}
$mutations=@(
 {$script:records[72].createdAt=201},{$script:records[72].parentPid=999},
 {$script:records[72].sessionId=2},{$script:records[72].imagePath=$root+'\foreign.exe'},
 {[HYPReadOnly]::Related=$false},{[HYPReadOnly]::ChildOwner=999},
 {$script:records[71].createdAt=101},{$anchor.Current.NativeWindowHandle=93},
 {$element.Current.ProcessId=999},{$element.Rid=@(42,72,99)})
$rejected=0
foreach($change in $mutations) {
 $script:records[72]=@{pid=72;createdAt=200;imagePath=$root+'\1.18.0.380\HYPHelper.exe';sessionId=1;parentPid=71}
 $script:records[71].createdAt=100;[HYPReadOnly]::Related=$true;[HYPReadOnly]::ChildOwner=72
 $anchor.Current.NativeWindowHandle=92;$element.Current.ProcessId=72;$element.Rid=@(42,72,2)
 Begin-OwnerFamily $identity 91;$null=Node-OwnerProof $element $element.Current $false
 $script:mutation=$change;$lastObservedTicks=[Diagnostics.Stopwatch]::GetTimestamp()
 try {$null=Dispatch-Action $request $previous} catch {$rejected++}
}
if($rejected -ne 10 -or $script:invocations -ne 1){throw ('action leak '+$rejected+' / '+$script:invocations)}
'ACTION_BOUNDARY_OK 10 refusals 1 owned invocation'
'''
        self.assertEqual('ACTION_BOUNDARY_OK 10 refusals 1 owned invocation',self.run_ps(body))


class NativeSourceSyntaxTests(unittest.TestCase):
    def test_both_complete_powershell_workers_parse_without_invocation(self):
        command='[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);$tokens=$null;$errors=$null;$null=[Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors);if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}'
        for source in (observation._NATIVE_WORKER,execution.native_script()):
            result=subprocess.run([PS,'-NoProfile','-NonInteractive','-Command',command],input=source.encode(),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(0,result.returncode,(result.stdout+result.stderr).decode(errors='replace'))

    def test_window_class_compiles_without_calling_native_methods(self):
        source=observation._NATIVE_WORKER.split("Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'",1)[1].split("\n'@",1)[0]
        command='[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);Add-Type -AssemblyName System.Drawing;Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition ([Console]::In.ReadToEnd());if(-not ([HYPReadOnly].GetMethod("Descendant"))){exit 1}'
        result=subprocess.run([PS,'-NoProfile','-NonInteractive','-Command',command],input=source.encode(),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(0,result.returncode,(result.stdout+result.stderr).decode(errors='replace'))
