"""Offline fixtures only: no native session, launcher, UIA provider or desktop."""
import copy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from yeyu_gamer_manager.services.game_launcher import GameLaunchError
from yeyu_gamer_manager.services.hoyoplay_launcher import HoYoPlayLauncher
from yeyu_gamer_manager.services.hoyoplay_native_execution import native_script, NativeContext, NativeHoYoPlaySession, _SESSION_WORKER
from yeyu_gamer_manager.services.hoyoplay_native_observation import FIXED_GAME_EXECUTABLE as GAME
from yeyu_gamer_manager.services.hoyoplay_ui_contract import interpret
from yeyu_gamer_manager.services.zzz_launcher_preflight import ZzzLauncherPreflight


def view(state="ready", *, menu=False, percent=3.89):
    nodes = []
    def node(role, name, parent, **fields):
        i = len(nodes)
        value = dict(index=i, parent=parent, controlType="ControlType."+role, name=name, enabled=True, offscreen=False,
                     uia=dict(ownerPid=71, ownerHwnd=91, runtimeId=[42,71,i], invokePatternAvailable=True,
                              expandCollapsePatternAvailable=True, expandCollapseState=0))
        value.update(fields)
        nodes.append(value)
        return i
    node("Window", "米哈游启动器", -1)
    d = node("Document", "", 0, documentLocation=dict(status="unqualified", origin="https://hyp-api.mihoyo.com",
             path="/hypfe/index.html", route="/nap_cn", queryOmitted=True), documentText="fixture")
    main = node("Group", "", d)
    if state in {"ready", "checking", "check-complete", "repair-confirm"}:
        node("Button", "开始游戏", main)
        node("Button", "开始游戏", main)
        node("CheckBox", "使用DX12启动", main)
        node("MenuItem", "", main)
    if menu:
        m = node("Menu", "", d)
        node("MenuItem", "检查更新", m)
        node("MenuItem", "一键修复", m)
    if state == "checking":
        nodes[d]["documentText"] = "正在解析下载内容，请稍候"
    if state == "check-complete":
        dlg = node("Window", "提示", d)
        node("Text", "当前已是最新版本，无需更新", dlg)
        node("Button", "知道了", dlg)
    if state == "repair-confirm":
        dlg = node("Window", "一键修复提醒", d)
        node("Button", "取消", dlg)
        node("Button", "开始修复", dlg)
    if state == "verifying":
        b = node("Button", "3.49185 3 校验并修复 00:05:18", main)
        node("Text", "校验并修复", b)
        progress = node("ProgressBar", "", b)
        nodes[progress]["uia"].update(rangeValuePatternAvailable=True, rangeValue=dict(value=percent, minimum=0, maximum=100))
        node("ProgressBar", "", d)  # unrelated duplicate must never win
    if state == "waiting":
        node("Button", "0 等待中", main, enabled=False)
    return dict(nonce="fixture-nonce", truncated=False, selectedDocumentCount=1, hwnd=91, nodes=nodes,gameExecutablePresent=True,
                installationAssociation=dict(primaryGame="nap_cn", gameExecutable=str(GAME), configSha256="a"*64),
                process=dict(pid=71, createdAt=12345678, imagePath=r"D:\Game\Launchers\HoYoPlay\1.0.0.0\HYP.exe",
                             signatureStatus="Valid", signer="miHoYo Co.,Ltd.", productName="米哈游启动器"))


class FakeSession:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []
        self.started = self.closed = False
    def start(self):
        self.started = True
    def close(self):
        self.closed = True
    def request(self, operation, **fields):
        self.calls.append((operation, fields))
        if operation == "observe":
            return {"view": self.frames.pop(0)}
        return dict(invoked=True, action=fields["action"])


class ContractTests(unittest.TestCase):
    def test_recorded_states_and_progress_parent(self):
        for state in ("ready", "checking", "check-complete", "repair-confirm", "verifying", "waiting"):
            with self.subTest(state=state):
                result=interpret(view(state))
                self.assertEqual(state, result.state)
        self.assertEqual(("repair-basis-points",389), interpret(view("verifying")).progress)

    def test_mutated_identity_route_binding_completeness_refuse(self):
        changes = [lambda p:p.update(truncated=True), lambda p:p.update(selectedDocumentCount=2),
                   lambda p:p["process"].update(signer="wrong"), lambda p:p["process"].update(createdAt=0),
                   lambda p:p["installationAssociation"].update(gameExecutable=r"C:\wrong.exe"),
                   lambda p:p["nodes"][1]["documentLocation"].update(route="/other"),
                   lambda p:p["nodes"][2]["uia"].update(ownerPid=72),
                   lambda p:p["nodes"][2].update(parent=2), lambda p:p["nodes"][1].update(documentText="unknown")]
        for mutation in changes:
            packet=view();mutation(packet)
            with self.subTest(mutation=mutation),self.assertRaises(GameLaunchError):interpret(packet)

    def test_url_alone_never_ready(self):
        packet=view();packet["nodes"]=packet["nodes"][:2]
        with self.assertRaises(GameLaunchError):interpret(packet)

    def test_no_coordinate_or_arbitrary_icon_fallback(self):
        packet=view();packet["nodes"][-1]["uia"]["expandCollapsePatternAvailable"]=False
        with self.assertRaisesRegex(GameLaunchError,"合同"):interpret(packet).opener()

    def test_duplicate_named_action_or_disabled_pattern_refuses(self):
        for kind in ("duplicate", "disabled", "pattern"):
            packet=view(menu=True)
            target=packet["nodes"][-2]
            if kind=="duplicate":
                duplicate=copy.deepcopy(target);duplicate["index"]=len(packet["nodes"]);packet["nodes"].append(duplicate)
            elif kind=="disabled":target["enabled"]=False
            else:target["uia"]["invokePatternAvailable"]="unknown"
            with self.subTest(kind=kind),self.assertRaises(GameLaunchError):
                current=interpret(packet);current.target("检查更新","MenuItem",current.menu())

    def test_update_button_not_invented(self):
        packet=view();packet["nodes"][3]["name"]="更新游戏"
        with self.assertRaises(GameLaunchError):interpret(packet)

    def test_unknown_dialog_or_input_cannot_click_ready_controls_behind_it(self):
        for role,fields in [('Window',{}),('Pane',{'dialog':True}),('Edit',{}),('Pane',{'privateInput':True})]:
            packet=view()
            node=copy.deepcopy(packet['nodes'][-1])
            node.update(index=len(packet['nodes']),parent=1,controlType='ControlType.'+role,name='unknown',**fields)
            packet['nodes'].append(node)
            with self.subTest(role=role,fields=fields),self.assertRaises(GameLaunchError):interpret(packet)

    def test_progress_requires_range_not_button_number(self):
        packet=view("verifying")
        packet["nodes"][-2]["uia"]["rangeValue"]="unknown"
        with self.assertRaises(GameLaunchError):interpret(packet)


class DriverTests(unittest.TestCase):
    def driver(self, frames):
        session=FakeSession(frames)
        context=NativeContext("00000000-0000-0000-0000-000000000071","fixture",Path("unused"),nullcontext,Mock())
        driver=HoYoPlayLauncher(object(),GAME,Mock(),lambda:False,context=context)
        with patch("yeyu_gamer_manager.services.hoyoplay_launcher.NativeHoYoPlaySession",return_value=session):
            driver.prepare()
        return driver,session

    def test_real_driver_check_repair_sequence_through_gate(self):
        frames=[view(),view(),view(menu=True),view("checking"),view("check-complete"),
                view(),view(),view(menu=True),view("repair-confirm"),view("waiting"),view("verifying"),
                view("verifying",percent=98.34),view()]
        driver,session=self.driver(frames)
        gate=ZzzLauncherPreflight(driver,GAME,cancel_requested=lambda:False,authority_valid=lambda:True,
            deadline_at=datetime.now(timezone.utc)+timedelta(minutes=5),notify=Mock(),sleep=lambda _:None)
        # Already prepared by test harness; production wrapper prepares exactly once.
        with patch.object(driver,"prepare"):
            result=gate.run()
        driver.close()
        self.assertEqual(dict(state="launcher-verified",updates=0,checks=1,verifications=1),result)
        self.assertEqual(["expand-menu","check","ack-check","expand-menu","verify","confirm-repair"],
                         [f["action"] for op,f in session.calls if op=="invoke"])
        self.assertTrue(session.closed)

    def test_fast_explicit_check_completion_has_owned_receipt(self):
        driver,session=self.driver([view(),view(menu=True),view("check-complete"),view()])
        snapshot=driver.observe();receipt=driver.invoke("check",snapshot);result=driver.observe()
        self.assertEqual(receipt,result.completion_receipt)
        self.assertEqual("up-to-date",result.state)

    def test_preexisting_dialog_does_not_ack(self):
        driver,session=self.driver([view("check-complete")])
        with self.assertRaises(GameLaunchError):driver.observe()
        self.assertEqual(["observe"],[op for op,_ in session.calls])

    def test_unknown_receipt_poisoned_never_replayed(self):
        driver,session=self.driver([view(),view(menu=True)])
        snapshot=driver.observe();original=session.request
        def lost(op,**kw):
            if op=="invoke":raise OSError("lost receipt")
            return original(op,**kw)
        with patch.object(session,"request",side_effect=lost):
            with self.assertRaises(OSError):driver.invoke("check",snapshot)
            with self.assertRaises(GameLaunchError):driver.invoke("check",snapshot)

    def test_forged_snapshot_no_native_action(self):
        driver,session=self.driver([view()]);snapshot=driver.observe()
        with self.assertRaises(GameLaunchError):driver.invoke("verify",copy.copy(snapshot))
        self.assertEqual(["observe"],[op for op,_ in session.calls])

    def test_update_requires_current_owned_snapshot(self):
        driver,session=self.driver([])
        with self.assertRaises(GameLaunchError) as caught:driver.invoke("update",None)
        self.assertEqual("zzz_launcher_stale_observation",caught.exception.reason_code)
        self.assertEqual([],session.calls)

    def test_no_context_or_stop_cannot_start_process(self):
        driver=HoYoPlayLauncher(object(),GAME,Mock(),lambda:False)
        with patch("subprocess.Popen",side_effect=AssertionError("native forbidden")),self.assertRaises(GameLaunchError):
            driver.prepare()


class ScriptTests(unittest.TestCase):
    def test_missing_exe_exception_is_owned_update_only(self):
        # Only the small path branch is run against this private empty fixture;
        # never evaluate Install-Association or read the real launcher config.
        script=native_script()
        block=script.split('    if ($script:ownedUpdate -and',1)[1].split('\n    $config =',1)[0]
        block='if ($script:ownedUpdate -and'+block
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[3]) as temporary:
            game=Path(temporary)/'fixture.exe'
            import base64
            encoded=base64.b64encode(str(game).encode()).decode()
            source="""$ErrorActionPreference='Stop'
$game=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'))
$seen=[Collections.Generic.List[string]]::new()
function Local-Path($path){if(-not (Test-Path -LiteralPath $path)){throw 'missing'};$seen.Add($path);return $path}
$script:ownedUpdate=$false;$denied=$false
try{__BLOCK__}catch{$denied=$true}
if(-not $denied -or $seen.Count -ne 0){throw 'unowned missing accepted'}
$script:ownedUpdate=$true
__BLOCK__
if($seen.Count -ne 1 -or $seen[0] -cne [IO.Path]::GetDirectoryName($game)){throw 'parent not checked'}
[IO.File]::WriteAllText($game,'private inert fixture')
__BLOCK__
if($seen.Count -ne 2 -or $seen[1] -cne $game){throw 'restored file not checked'}
'OWNED_PATH_OK 3 cases'
""".replace('__PATH__',encoded).replace('__BLOCK__',block)
            command="[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))"
            result=subprocess.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe","-NoProfile","-NonInteractive","-Command",command],
                input=source.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
            self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
            self.assertIn(b'OWNED_PATH_OK 3 cases',result.stdout)

    def test_suspended_assignment_failure_kills_only_owned_helper(self):
        events=[]
        with tempfile.TemporaryDirectory() as temporary:
            context=NativeContext('00000000-0000-0000-0000-000000000071','fixture',Path(temporary)/'task',nullcontext,events.append)
            session=NativeHoYoPlaySession(context,lambda:False)
            process=Mock(pid=123,stdin=io.BytesIO(),stdout=io.BytesIO())
            process.poll.side_effect=[None,0]
            job=Mock();job.assign.side_effect=OSError('assignment failed');job.finish.return_value=(True,[])
            sampler=Mock();sampler._run_worker.return_value={'processes':[{'pid':71}]};sampler._helper_closed=True
            def create(*args,**kwargs):
                self.assertEqual('native-start-issued',events[0]['event'])
                self.assertTrue(kwargs['creationflags'] & 4)
                self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)
                return process
            with patch('yeyu_gamer_manager.services.hoyoplay_native_execution.NativeHoYoPlaySampler',return_value=sampler), \
                 patch('yeyu_gamer_manager.services.hoyoplay_native_execution._PinnedLauncher',return_value=nullcontext(Mock())), \
                 patch('yeyu_gamer_manager.services.hoyoplay_native_execution._HelperJob',return_value=job), \
                 patch('yeyu_gamer_manager.services.hoyoplay_native_execution.GameLaunchService._require_queue_local_path') as validate_path, \
                 patch('subprocess.Popen',side_effect=create):
                with self.assertRaises(OSError):session.start()
            self.assertEqual([context.output_dir.parent.parent,context.output_dir.parent,context.output_dir],
                             [call.args[0] for call in validate_path.call_args_list])
            self.assertEqual(1,process.kill.call_count)
            job.resume.assert_not_called()
            sampler._start_launcher.assert_not_called()
            self.assertEqual('native-closed',events[-1]['event'])
            self.assertTrue(events[-1]['helperClosed'])
            session.close()
            self.assertEqual(1,process.kill.call_count)

    def test_dispatch_with_fake_elements_only(self):
        # Execute only these two pure dispatch routines with fake providers.
        # No Live-View implementation, process query or real UIA element runs.
        functions=_SESSION_WORKER.split('function Node-Key',1)[1].split('\ntry {\n    while',1)[0]
        functions='function Node-Key'+functions
        setup=r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type 'public static class HYPReadOnly { public static int Owner(long h) { return 71; } }'
function Session-Guard {}
function Install-Association {}
function Same-Identity($value) {}
function Same-NodeOwner($element,$node) {if($element.Current.ProcessId -ne $node.uia.ownerPid){throw 'action-boundary-changed'}}
function Local-Path($value) { return $value }
function No-Games($phase) {}
function Runtime-Identity($element) { return ,@($element.Rid) }
function Live-View { return $script:fixture }
$script:invocations=0
$pattern=New-Object PSObject
$pattern | Add-Member ScriptMethod Invoke { $script:invocations++ }
$pattern | Add-Member ScriptMethod Expand { $script:invocations++ }
$element=New-Object PSObject -Property @{Pattern=$pattern; Supported=$true;Rid=@(42,71,1);
    Current=@{ProcessId=71;IsEnabled=$true;IsOffscreen=$false;Name='检查更新';ControlType=@{ProgrammaticName='ControlType.MenuItem'}}}
$element | Add-Member ScriptMethod TryGetCurrentPattern {param($kind,$reference)
    if(-not $this.Supported){return $false};$reference.Value=$this.Pattern;return $true }
$script:liveElements=@($element)
$node=@{index=0;parent=-1;name='检查更新';controlType='ControlType.MenuItem';enabled=$true;offscreen=$false;
    uia=@{runtimeId=@(42,71,1);ownerPid=71;ownerHwnd=91;expandCollapseState=0}}
$script:fixture=@{nonce='fresh';process=@{pid=71;createdAt=123};hwnd=91;
    installationAssociation=@{configSha256='same'};nodes=@($node)}
$previous=ConvertFrom-Json ($script:fixture | ConvertTo-Json -Depth 8)
$previous.nonce='prior'
$request=@{nonce='prior';action='check';index=0}
$lastObservedTicks=[Diagnostics.Stopwatch]::GetTimestamp()
'''
        assertions=r'''
$answer=Dispatch-Action $request $previous
if(-not $answer.invoked -or $script:invocations -ne 1){throw 'invoke did not execute'}
$failures=0
$request.action='update';try{$null=Dispatch-Action $request $previous}catch{$failures++};$request.action='check'
$request.nonce='forged';try{$null=Dispatch-Action $request $previous}catch{$failures++};$request.nonce='prior'
$element.Supported=$false;try{$null=Dispatch-Action $request $previous}catch{$failures++};$element.Supported=$true
$element.Rid=@(42,71,2);try{$null=Dispatch-Action $request $previous}catch{$failures++};$element.Rid=@(42,71,1)
$script:fixture.process.createdAt=124;try{$null=Dispatch-Action $request $previous}catch{$failures++};$script:fixture.process.createdAt=123
$node.name='开始游戏';try{$null=Dispatch-Action $request $previous}catch{$failures++};$node.name='检查更新'
$node.enabled=$false;try{$null=Dispatch-Action $request $previous}catch{$failures++};$node.enabled=$true
$script:fixture.nodes=@($node,$node);try{$null=Dispatch-Action $request $previous}catch{$failures++};$script:fixture.nodes=@($node)
if($failures -ne 8 -or $script:invocations -ne 1){throw ('refusal mismatch '+$failures+' calls '+$script:invocations)}
$node.name='';$previous.nodes[0].name='';$request.action='expand-menu'
$element.Current.Name=''
$null=Dispatch-Action $request $previous
if($script:invocations -ne 2){throw 'expand did not execute'}
$node.name='更新';$previous.nodes[0].name='更新';$element.Current.Name='更新'
$node.controlType='ControlType.Button';$previous.nodes[0].controlType='ControlType.Button'
$element.Current.ControlType.ProgrammaticName='ControlType.Button';$request.action='update'
$null=Dispatch-Action $request $previous
if($script:invocations -ne 3 -or -not $script:ownedUpdate){throw 'update invocation missing'}
'FAKE_DISPATCH_OK 11 cases'
'''
        command="[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))"
        result=subprocess.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe","-NoProfile","-NonInteractive","-Command",command],
            input=(setup+functions+assertions).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
        self.assertIn(b'FAKE_DISPATCH_OK 11 cases',result.stdout)

    def test_full_script_ps5_parse_only(self):
        # Parser only; no script invocation, UIA provider or native helper.
        script=native_script()
        command="[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$s=[Console]::In.ReadToEnd();$t=$null;$e=$null;$null=[Management.Automation.Language.Parser]::ParseInput($s,[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.Message};exit 1};'PARSE_OK'"
        result=subprocess.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe","-NoProfile","-NonInteractive","-Command",command],
                              input=script.encode("utf-8"),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout.decode(errors="replace"))
        self.assertIn(b"PARSE_OK",result.stdout)

    def test_request_loss_poison_and_durable_issue_precede_pipe(self):
        events=[]
        context=NativeContext("00000000-0000-0000-0000-000000000071","fixture",Path("unused"),nullcontext,events.append)
        session=NativeHoYoPlaySession(context,lambda:False)
        session.process=Mock();session.closed=False
        def broken(data):
            self.assertEqual("action-issued",events[-1]["event"])
            raise OSError("broken pipe")
        session.process.stdin.write.side_effect=broken
        with self.assertRaises(OSError):session.request("invoke",action="check",nonce="x",index=1)
        with self.assertRaises(GameLaunchError):session.request("invoke",action="check",nonce="x",index=1)
        self.assertEqual(1,session.process.stdin.write.call_count)


if __name__=="__main__":unittest.main()
