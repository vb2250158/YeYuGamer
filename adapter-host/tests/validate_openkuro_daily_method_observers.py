"""Offline contract replay for passive Endfield/GF2 method observers."""
from __future__ import annotations
import importlib.util, json, os, sys, tempfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adapter-host/openkuro-runner'))

def load(name, path):
    spec=importlib.util.spec_from_file_location(name, path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def stages(path):
    # This replay checks method results; PNGs and unavailable buffers have
    # their own bound-official-writer verification.
    return [event for line in path.read_text(encoding="utf-8").splitlines()
            if (event := json.loads(line))["state"] not in {"progress", "step_artifact"}]

def install_module(path, cls, module_name, installer):
    modules={}
    parts=module_name.split('.')
    for index in range(1,len(parts)+1):
        name='.'.join(parts[:index]); modules[name]=sys.modules.get(name) or ModuleType(name); sys.modules[name]=modules[name]
    setattr(modules[module_name], 'DailyTask', cls)
    bridge=load('fixture_'+installer, path)
    bridge._install_methods(cls)
    return bridge

def main():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw); stage=root/'stage.jsonl'; stage.write_text('',encoding='utf-8')
        os.environ['YEYU_GAMER_STAGE_FILE']=str(stage)
        os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=json.dumps(['attach-world','mail','spend-sanity','delivery-commission','collect-credit','dijiang-harvest','claim-daily-reward'])
        class Ef:
            def active_and_send_mouse_delta(self, **kw): return getattr(self,'active_result',None)
            def ensure_main(self, **kw):
                if getattr(self, 'main_error', None): raise self.main_error
                return getattr(self,'main_result',None)
            def claim_mail(self): return getattr(self,'mail_result',None)
            def battle(self): return None
            def delivery_send_others(self): return None
            def collect_credit(self): return None
            def boat_claim_rewards(self): return None
            def claim_daily_rewards(self): return None
            def build_task_plan(self): return [(name,getattr(self,method)) for name,method in [('m','claim_mail'),('s','battle'),('d','delivery_send_others'),('c','collect_credit'),('b','boat_claim_rewards'),('r','claim_daily_rewards')]]
        bridge=install_module(ROOT/'adapter-host/openkuro-runner/EndfieldYeYuBridge.py',Ef,'src.tasks.onetime.DailyTask','install_yeyu_gamer_bridge')
        config = {'supported_resolution': {'ratio': '16:9', 'min_size': (1920, 1080)}}
        bridge._configure_resolution(config)
        assert config['supported_resolution'] == {'ratio': '16:9', 'min_size': (1920, 1080), 'resize_to': [(1920, 1080)]}
        for policy in ([], [(2560, 1440)], None):
            config['supported_resolution']['resize_to'] = policy
            bridge._configure_resolution(config)
            assert config['supported_resolution']['resize_to'] is policy
        stage.write_text('', encoding='utf-8')
        task=Ef(); task.active_and_send_mouse_delta(); assert stages(stage) == []
        task.ensure_main(); task.ensure_main(); [fn() for _,fn in task.build_task_plan()]
        events=stages(stage)
        assert {x['operation'] for x in events if x['state']=='completed'} == {'attach-world','mail','spend-sanity','delivery-commission','collect-credit','dijiang-harvest','claim-daily-reward'}
        assert [x['operation'] for x in events if x['state']=='started'].count('attach-world') == 1
        stage.write_text('',encoding='utf-8'); os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=json.dumps(['mail'])
        task.mail_result=False; task.build_task_plan()[0][1](); assert [x['state'] for x in stages(stage)] == ['started','failed']
        stage.write_text('',encoding='utf-8'); os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=json.dumps(['attach-world'])
        task=Ef(); task.main_error=RuntimeError('official world check failed')
        try: task.ensure_main()
        except RuntimeError as error: assert error is task.main_error
        else: raise AssertionError('official error must propagate')
        assert [x['state'] for x in stages(stage)] == ['started','failed']
        (root/'YeYuGamerRun.json').write_text(json.dumps({'selectedOperations':['mail']}),encoding='utf-8')
        os.environ['YEYU_GAMER_SELECTED_OPERATIONS']='invalid-json'
        assert bridge._context()[1] == set(), 'stale sidecar must not supply this run scope'
        stage.write_text('',encoding='utf-8'); os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=json.dumps(['attach-home','mail','public-area-dispatch','spend-stamina','squad-tasks','claim-daily-missions','battle-pass-free-track'])
        class Gf:
            def ensure_main(self,*a,**k): return getattr(self,'home_result',None)
            def mail(self): return None
            def gongongqu(self): return None
            def battle(self): return getattr(self,'battle_result',None)
            def guild(self): return None
            def claim_quest(self): return None
            def xunlu(self): return None
        gf_bridge=install_module(ROOT/'adapter-host/openkuro-runner/Gf2YeYuObserver.py',Gf,'src.tasks.DailyTask','install')
        task=Gf(); task.ensure_main(); task.ensure_main(); [getattr(task,name)() for name in ('mail','gongongqu','battle','guild','claim_quest','xunlu')]
        events=stages(stage); completed=[x['operation'] for x in events if x['state']=='completed']
        assert completed.count('attach-home') == 1 and set(completed) == {'attach-home','mail','public-area-dispatch','spend-stamina','squad-tasks','claim-daily-missions','battle-pass-free-track'}
        assert [x['operation'] for x in events if x['state']=='started'].count('attach-home') == 1
        stage.write_text('',encoding='utf-8'); os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=json.dumps(['spend-stamina'])
        task.battle_result=False; task.battle(); assert [x['state'] for x in stages(stage)] == ['started','failed']
        stage.write_text('',encoding='utf-8'); os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=json.dumps(['attach-home'])
        task=Gf(); task.home_result=False; task.ensure_main(); assert [x['state'] for x in stages(stage)] == ['started','completed']
        for observer in (bridge, gf_bridge):
            for invalid in ('invalid-json', '{}', '[1]', ''):
                os.environ['YEYU_GAMER_SELECTED_OPERATIONS']=invalid
                assert observer._context()[1] == set(), 'invalid scope must not read historical sidecar'
            os.environ.pop('YEYU_GAMER_SELECTED_OPERATIONS')
            assert observer._context()[1] == set()
        stage.write_text('',encoding='utf-8')
        task=Ef(); task.mail_result=False
        assert task.build_task_plan()[0][1]() is False
        task=Gf(); task.battle_result=False
        assert task.battle() is False
        assert stages(stage) == [], 'missing telemetry scope must not change official results or invent steps'
    print(json.dumps({'status':'passed','gameStarted':False,'toolStarted':False,'staleScopeRejected':True}))

if __name__=='__main__': main()
