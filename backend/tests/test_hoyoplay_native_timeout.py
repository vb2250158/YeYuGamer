"""Native protocol tests with inert providers; never acquire desktop elements."""
from contextlib import nullcontext
import io, json, os
from pathlib import Path
from queue import Empty
import subprocess, tempfile, time, unittest
from unittest.mock import Mock, patch
from yeyu_gamer_manager.services import hoyoplay_native_execution as native
from yeyu_gamer_manager.services.game_launcher import GameLaunchError

TASK='00000000-0000-0000-0000-000000000071'
PS=r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

class NativeTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.output=Path(self.temporary.name);self.events=[]
        self.context=native.NativeContext(TASK,'fixture',self.output,nullcontext,self.events.append)

    def phase(self,**changes):
        value=dict(taskId=TASK,pid=71,sequence=1,phase='uia-from-handle',node=-1,apartment='MTA')
        value.update(changes)
        (self.output/'session.phase.json').write_text(json.dumps(value),encoding='utf-8')

    def session(self):
        session=native.NativeHoYoPlaySession(self.context,lambda:False)
        session.process=Mock(pid=71,stdin=io.BytesIO());session.closed=False;session.sequence=1
        return session

    def test_phase_reader_rejects_foreign_stale_oversized_and_private_payloads(self):
        session=self.session();self.phase(private='secret-url-or-input')
        self.assertEqual({'phase':'uia-from-handle','node':-1,'apartment':'MTA'},session._native_phase())
        for changes in ({'taskId':'foreign'},{'pid':72},{'sequence':2},{'phase':'secret-user-text'},
                        {'node':True},{'node':257},{'apartment':'unknown'}):
            self.phase(**changes);self.assertIsNone(session._native_phase(),changes)
        (self.output/'session.phase.json').write_bytes(b' '*2049)
        self.assertIsNone(session._native_phase())

    def test_timeout_retains_phase_and_poison_without_replay(self):
        with patch.object(native.time,'monotonic',return_value=0) as clock:
            session=self.session();session.sequence=0;session.job=Mock();session.job.members.return_value=[]
            session.stderr_path=self.output/'stderr';session.stderr_path.touch()
            self.phase()
            def pending(**kwargs):
                clock.return_value=16
                raise Empty
            session._responses=Mock();session._responses.get.side_effect=pending
            with self.assertRaises(GameLaunchError) as raised:session.request('observe')
        self.assertEqual('zzz_launcher_native_request_timeout',raised.exception.reason_code)
        self.assertEqual('uia-from-handle',raised.exception.detail['nativePhase']['phase'])
        self.assertEqual(15,raised.exception.detail['requestLimitSeconds'])
        before=session.process.stdin.getvalue()
        with self.assertRaises(GameLaunchError):session.request('observe')
        self.assertEqual(before,session.process.stdin.getvalue())
        self.assertEqual(0,session._record['nativeInvocationCount'])

    def test_owned_native_start_selects_real_mta_without_changing_job_or_identity_policy(self):
        context=native.NativeContext(TASK,'fixture',self.output/'task',nullcontext,self.events.append)
        session=native.NativeHoYoPlaySession(context,lambda:False)
        process=Mock(pid=123,stdin=io.BytesIO(),stdout=io.BytesIO());process.poll.side_effect=[None,0]
        job=Mock();job.assign.side_effect=OSError('inert assignment boundary');job.finish.return_value=(True,[])
        sampler=Mock();sampler._run_worker.return_value={'processes':[{'pid':71}]};sampler._helper_closed=True
        captured=[]
        def create(args,**kwargs):
            captured.append((args,kwargs))
            self.assertTrue(kwargs['creationflags'] & 4)
            self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)
            return process
        with patch.object(native,'NativeHoYoPlaySampler',return_value=sampler),\
             patch.object(native,'_PinnedLauncher',return_value=nullcontext(Mock())),\
             patch.object(native,'_HelperJob',return_value=job),\
             patch.object(native.GameLaunchService,'_require_queue_local_path'),\
             patch.object(native.subprocess,'Popen',side_effect=create):
            with self.assertRaises(OSError):session.start()
        # Run only the apartment query with the exact captured native startup flags.
        args=captured[0][0][:captured[0][0].index('-Command')+1]+['[Threading.Thread]::CurrentThread.GetApartmentState().ToString()']
        result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
        self.assertEqual(0,result.returncode)
        self.assertEqual('MTA',result.stdout.decode().strip())
        self.assertEqual(str(context.output_dir/'session.phase.json'),captured[0][1]['env']['YEYU_HYP_PHASE'])
        self.assertTrue(self.events[-1]['helperClosed']);job.resume.assert_not_called()
        sampler._start_launcher.assert_not_called()

    def test_session_protocol_runs_in_mta_and_refuses_sta_with_inert_provider(self):
        prefix,marker,tail=native._SESSION_WORKER.partition('\ntry {\n')
        self.assertTrue(marker)
        # Override the provider before the actual request loop. No UIA assembly
        # or AutomationElement is loaded, and invocation is never requested.
        source=(f"$sampleId='{TASK}';$timer=[Diagnostics.Stopwatch]::StartNew();function Limit-Time {{}}\n"+prefix+
            "\nfunction Close-OwnerFamily {}\nfunction Live-View {Session-Phase 'uia-from-handle';return @{waiting=$true}}\n"+marker+tail)
        script=self.output/'fake-worker.ps1';script.write_text(source,encoding='utf-8')
        env=dict(os.environ,YEYU_HYP_PHASE=str(self.output/'session.phase.json'),TEST_SCRIPT=str(script))
        for apartment in ('-Mta','-Sta'):
            result=subprocess.run([PS,apartment,'-NoProfile','-NonInteractive','-Command',
                '& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:TEST_SCRIPT,[Text.Encoding]::UTF8)))'],
                input=b'{"sequence":1,"operation":"observe"}\n',stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env=env,creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
            response=json.loads(result.stdout)
            if apartment=='-Mta':
                self.assertEqual({'ok':True,'sequence':1,'view':{'waiting':True}},response)
                phase=json.loads((self.output/'session.phase.json').read_text(encoding='utf-8'))
                self.assertEqual(('response-write','MTA',1),(phase['phase'],phase['apartment'],phase['sequence']))
                self.assertLess((self.output/'session.phase.json').stat().st_size,2048)
            else:
                self.assertFalse(response['ok']);self.assertEqual('native-apartment-invalid',response['reason'])

    def test_phase_record_has_bounded_space_and_time_at_full_tree_limit(self):
        declaration='function Session-Phase'+native._SESSION_WORKER.split('function Session-Phase',1)[1].split('function Session-Guard',1)[0]
        source=(f"$sampleId='{TASK}';$lastSequence=1;"+declaration+r'''
$watch=[Diagnostics.Stopwatch]::StartNew()
for($i=0;$i -lt 256;$i++) {
  foreach($phase in @('uia-current','uia-properties','uia-document-text','uia-first-child','uia-next-sibling')) {
    Session-Phase $phase $i
  }
}
@{elapsedSeconds=$watch.Elapsed.TotalSeconds;bytes=(Get-Item -LiteralPath $env:YEYU_HYP_PHASE).Length}|ConvertTo-Json -Compress
''')
        script=self.output/'phase-cost.ps1';script.write_text(source,encoding='utf-8')
        result=subprocess.run([PS,'-Mta','-NoProfile','-NonInteractive','-Command',
            '& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:TEST_SCRIPT,[Text.Encoding]::UTF8)))'],
            env=dict(os.environ,TEST_SCRIPT=str(script),YEYU_HYP_PHASE=str(self.output/'session.phase.json')),
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
        value=json.loads(result.stdout);print('PHASE_COST '+json.dumps(value))
        self.assertLess(value['bytes'],2048)
        self.assertLess(value['elapsedSeconds'],1,'Phase diagnostics must leave headroom inside the existing two-second freshness limit')

    def test_phase_is_readable_while_inert_worker_waits_and_survives_owned_cleanup(self):
        declaration='function Session-Phase'+native._SESSION_WORKER.split('function Session-Phase',1)[1].split('function Session-Guard',1)[0]
        source=f"$sampleId='{TASK}';$lastSequence=1;"+declaration+"\nSession-Phase 'uia-from-handle';Start-Sleep -Seconds 4"
        script=self.output/'inert-wait.ps1';script.write_text(source,encoding='utf-8')
        process=subprocess.Popen([PS,'-Mta','-NoProfile','-NonInteractive','-Command',
            '& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:TEST_SCRIPT,[Text.Encoding]::UTF8)))'],
            env=dict(os.environ,TEST_SCRIPT=str(script),YEYU_HYP_PHASE=str(self.output/'session.phase.json')),
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            session=self.session();session.process=process
            deadline=time.monotonic()+2
            phase=None
            while phase is None and time.monotonic()<deadline:
                phase=session._native_phase();time.sleep(.01)
            self.assertIsNone(process.poll())
            self.assertEqual({'phase':'uia-from-handle','node':-1,'apartment':'MTA'},phase)
            process.kill();process.wait(timeout=2)
            self.assertEqual(phase,session._native_phase())
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)
            process.stdout.close();process.stderr.close()

if __name__=='__main__':unittest.main()
