from contextlib import nullcontext
import io,json,os,subprocess,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase,mock
from yeyu_gamer_manager.services import hoyoplay_owner_diagnostics as diagnostics
from yeyu_gamer_manager.services import hoyoplay_native_observation as observation
from yeyu_gamer_manager.services import hoyoplay_native_execution as execution
from yeyu_gamer_manager.services.game_launcher import GameLaunchError

TASK='00000000-0000-0000-0000-000000000071'
PS=r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

def record(**changes):
    return dict(contract=diagnostics.CONTRACT,source='execution',taskId=TASK,helperPid=71,sequence=1,
        expectedPid=91,observedPid=0,nodeIndex=7,parentIndex=3,depth=2,relation='descendant',
        controlTypeId=50030,rootHwnd=123456,nodeHwnd=0,**changes)


class DiagnosticValidationTests(TestCase):
    def read(self,value):
        return diagnostics.owner_diagnostic(value,source='execution',task_id=TASK,helper_pid=71,sequence=1)

    def test_zero_foreign_and_unknown_pid_are_evidence_only(self):
        for pid in (0,92,-1):
            value=record();value['observedPid']=pid
            self.assertEqual(self.read(value),value)
            self.assertNotIn('actions',self.read(value))

    def test_root_and_descendant_identity_are_distinct(self):
        value=record();value.update(nodeIndex=0,parentIndex=-1,depth=0,relation='root')
        self.assertEqual(self.read(value),value)
        value['relation']='descendant';self.assertIsNone(self.read(value))
        value=record();value['parentIndex']=7;self.assertIsNone(self.read(value))

    def test_foreign_task_helper_sequence_and_source_are_rejected(self):
        for key,value in [('taskId','foreign'),('helperPid',72),('sequence',2),('source','observation')]:
            original=record();original[key]=value;self.assertIsNone(self.read(original))

    def test_untrusted_text_unknown_fields_types_and_overflow_never_escape(self):
        for key,value in [('name','private-input'),('url','https://example.test/?token=private'),
                          ('controlTypeId','provider string'),('expectedPid',True),('nodeIndex',256),
                          ('nodeHwnd',2**63),('rootHwnd',0),('controlTypeId',49999)]:
            original=record();original[key]=value;self.assertIsNone(self.read(original))


class OwnerFailureBoundaryTests(TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.output=Path(temp.name)
        self.events=[]

    def session(self,value):
        context=execution.NativeContext(TASK,'fixture',self.output,nullcontext,self.events.append)
        session=execution.NativeHoYoPlaySession(context,lambda:False)
        session.process=mock.Mock(pid=71,stdin=io.BytesIO());session.closed=False
        session.job=mock.Mock();session.job.members.return_value=[71]
        session.stderr_path=self.output/'empty.log';session.stderr_path.write_bytes(b'')
        session._responses.put({'ok':False,'sequence':1,'reason':'owner-changed','ownerDiagnostic':value})
        return session

    def test_execution_preserves_rejection_with_no_invocation_and_poisoned_retry(self):
        for pid in (0,92):
            value=record();value['observedPid']=pid;session=self.session(value)
            with self.assertRaises(GameLaunchError) as error:session.request('observe')
            self.assertEqual(error.exception.reason_code,'zzz_launcher_owner_changed')
            self.assertEqual(error.exception.detail,{'ownerDiagnostic':value})
            self.assertEqual(session._record['nativeInvocationCount'],0)
            self.assertFalse(session._record['gameLaunchRequested'])
            before=session.process.stdin.getvalue()
            with self.assertRaises(GameLaunchError):session.request('observe')
            self.assertEqual(before,session.process.stdin.getvalue())
            self.assertEqual(self.events,[])

    def test_malformed_diagnostic_cannot_turn_failure_into_success_or_disclose_text(self):
        value=record();value['name']='private';session=self.session(value)
        with self.assertRaises(GameLaunchError) as error:session.request('observe')
        self.assertEqual(error.exception.reason_code,'zzz_launcher_owner_changed')
        self.assertNotIn('private',repr(error.exception.detail))
        self.assertTrue(session._poisoned)

    def test_observation_failure_remains_unqualified_without_capture_or_control(self):
        value=record();value.update(source='observation',sequence=0)
        sampler=observation.NativeHoYoPlaySampler(observation.FIXED_GAME_EXECUTABLE,
            cancelled=lambda:False,register_helper=lambda _:None,task_id=TASK,manager_id='fixture',launch_guard=nullcontext)
        failed=GameLaunchError('owner mismatch',reason_code='zzz_observation_ax_owner_mismatch',detail={'ownerDiagnostic':value})
        with mock.patch.object(sampler,'_validate_inputs',side_effect=failed):result=sampler.sample(self.output,allow_start=False)
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['ownerDiagnostic'],value)
        for field in ('qualification','selectedGameVerified','productionUpdateAllowed'):self.assertFalse(result[field])
        self.assertEqual(result['actions'],[]);self.assertEqual(result['screenshots'],[]);self.assertEqual(result['launches'],[])


class InertPowerShellOwnerTests(TestCase):
    def run_script(self,body):
        # Only the pure diagnostic function and exact rejection block execute.
        # No native helper, UIAutomation assembly, window or process API exists.
        declaration='function Owner-Diagnostic'+observation._NATIVE_WORKER.split('function Owner-Diagnostic',1)[1].split('function Read-UIA',1)[0]
        script=f"$sampleId='{TASK}';"+declaration+'\n'+body
        self.assertNotIn('Add-Type',script);self.assertNotIn('FromHandle',script);self.assertNotIn('Read-UIA',script)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'inert-owner.ps1';path.write_text(script,encoding='utf-8')
            result=subprocess.run([PS,'-Mta','-NoProfile','-NonInteractive','-Command',
                '& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:TEST_SCRIPT,[Text.Encoding]::UTF8)))'],
                env=dict(os.environ,TEST_SCRIPT=str(path)),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            return json.loads(result.stdout)

    def test_actual_execution_rejection_block_keeps_zero_and_foreign_pid_closed(self):
        block='$observedPid=$current.ProcessId'+execution._SESSION_WORKER.split('$observedPid=$current.ProcessId',1)[1].split('        $index=$nodes.Count;',1)[0]
        body=r'''
$number=91;$hwnd=123456;$nodes=@(0,1,2,3,4,5,6);$item=@{parent=3;depth=2};$lastSequence=1
function Node-OwnerProof($element,$current,$rootNode) {if($current.ProcessId -ne $number){throw 'owner-family-rejected'}}
$results=@()
foreach($observed in @(0,92,91)) {
 $script:ownerDiagnostic=$null;$current=[pscustomobject]@{ProcessId=$observed;ControlType=[pscustomobject]@{Id=50030};NativeWindowHandle=0;Name='private-url-or-input'}
 try {
'''+block+r'''
  $results+=@{rejected=$false;ownerDiagnostic=$script:ownerDiagnostic}
 } catch { $results+=@{rejected=$true;reason=$_.Exception.Message;ownerDiagnostic=$script:ownerDiagnostic} }
}
ConvertTo-Json -InputObject $results -Depth 4 -Compress
'''
        result=self.run_script(body)
        self.assertEqual([v['rejected'] for v in result],[True,True,False])
        for row in result[:2]:
            self.assertEqual(row['reason'],'owner-changed');self.assertEqual(row['ownerDiagnostic']['nodeIndex'],7)
            self.assertEqual(set(row['ownerDiagnostic']),diagnostics.FIELDS)
        self.assertNotIn('private-url-or-input',json.dumps(result))

    def test_missing_root_and_root_owner_mismatch_have_safe_root_diagnostics(self):
        tail=observation._NATIVE_WORKER.split('$element = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)',1)[1]
        block=tail.split('        $queue = ',1)[0]
        body=r'''
$number=91;$hwnd=123456;$results=@()
foreach($pidValue in @(-1,0,92,91)) {
 $script:ownerDiagnostic=$null
 $element=if($pidValue -eq -1){$null}else{[pscustomobject]@{Current=[pscustomobject]@{ProcessId=$pidValue;ControlType=[pscustomobject]@{Id=50032};NativeWindowHandle=123456}}}
 try {
'''+block+r'''
  $results+=@{rejected=$false}
 } catch { $results+=@{rejected=$true;reason=$_.Exception.Message;ownerDiagnostic=$script:ownerDiagnostic} }
}
ConvertTo-Json -InputObject $results -Depth 4 -Compress
'''
        result=self.run_script(body);self.assertEqual([r['rejected'] for r in result],[True,True,True,False])
        for row in result[:3]:
            value=row['ownerDiagnostic'];self.assertEqual(value['relation'],'root');self.assertEqual(value['nodeIndex'],0)
            self.assertEqual(row['reason'],'zzz_observation_ax_owner_mismatch')
        self.assertEqual(result[0]['ownerDiagnostic']['observedPid'],-1)
        self.assertEqual(result[0]['ownerDiagnostic']['controlTypeId'],-1)
