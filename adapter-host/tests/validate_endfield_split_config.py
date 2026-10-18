"""Production C# projection replay against pinned official v1.1.8 predicates."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / '.cache/upstream-checkouts/endfield-v1.1.8-20261002'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_plan(projected, account=None):
    feature = load('src.tasks.daily.daily_feature', SOURCE / 'src/tasks/daily/daily_feature.py').DailyFeature
    # Load exact official plan and wrapper assignments, excluding GUI imports
    # and constructor initialization unrelated to the plan under test.
    tree = ast.parse((SOURCE / 'src/tasks/onetime/DailyTask.py').read_text(encoding='utf-8'))
    task = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DailyTask')
    init = next(node for node in task.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    assignments = [node for node in init.body if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                   and node.value.func.id == 'DailyFeature']
    plan = next(node for node in task.body if isinstance(node, ast.FunctionDef) and node.name == 'build_task_plan')
    calls = []
    class Config(dict):
        def get(self, key, default=None):
            return self.overrides.get(key, super().get(key, default))
    class Impl:
        def set_current_account(self, user, account_id): pass
        def __getattr__(self, name):
            return lambda: calls.append(type(self).__name__)
    namespace = {'DailyFeature': feature}
    impls = []
    for assignment in assignments:
        name = assignment.value.args[1].id
        cls = type(name, (Impl,), {})
        namespace[name] = cls
        impl = cls()
        impl.config = Config(projected.get(name, {}))
        impl.config.overrides = (projected['account_scoped_overrides']['accounts'][account].get(name, {})
                                 if account else {})
        impls.append(impl)
    constructor = ast.FunctionDef(name='initialize_features', args=plan.args,
        body=assignments, decorator_list=[], returns=None, type_comment=None)
    grouped = any(isinstance(node, ast.FunctionDef) and node.name == '_task_selected' for node in task.body)
    constants = [node for node in task.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and (target.id.startswith('CFG_')
                     or target.id.endswith('_TASKS') or target.id == '_LEGACY_TASK_KEYS')
                     for target in node.targets)] if grouped else []
    methods = [node for node in task.body if isinstance(node, ast.FunctionDef)
               and node.name == '_task_selected'] if grouped else []
    definition = ast.ClassDef(name='OfficialPlan', bases=[], keywords=[],
        body=constants + methods + [constructor, plan], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[]))
    exec(compile(module, str(SOURCE / 'src/tasks/onetime/DailyTask.py'), 'exec'), namespace)
    config = Config(projected['DailyTask'])
    config.overrides = (projected['account_scoped_overrides']['accounts'][account].get('DailyTask', {})
                        if account else {})
    host = namespace['OfficialPlan']()
    host.__dict__.update(dict(executor=SimpleNamespace(onetime_tasks=impls), config=config,
        current_user='', current_account_id='', debug=False, ensure_main=lambda: None,
        send_key=lambda *a: None, log_info=lambda *a, **k: None, tr=lambda x: x,
        screenshot=lambda *a: None, _is_account_override_enabled=lambda: True,
        _account_override_for=lambda _name: config.overrides))
    host.initialize_features()
    official_ok = ModuleType('ok')
    official_ok.TaskDisabledException = type('TaskDisabledException', (Exception,), {})
    previous = sys.modules.get('ok')
    sys.modules['ok'] = official_ok
    try:
        runner = load('split_replay_runner', SOURCE / 'src/tasks/daily/daily_task_runner.py').DailyTaskRunner
    finally:
        if previous is None: sys.modules.pop('ok', None)
        else: sys.modules['ok'] = previous
    plan_items = host.build_task_plan()
    host.daily_runner = runner(host, plan_items)
    for item in plan_items:
        host.daily_runner.execute_task(*item)
    return calls


def main():
    global SOURCE
    replay, assembly = map(Path, sys.argv[1:3])
    if len(sys.argv) > 3:
        SOURCE = Path(sys.argv[3])
    (ROOT / '.cache/verification').mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / '.cache/verification') as raw:
        root = Path(raw)
        (root / 'src/tasks/daily').mkdir(parents=True)
        (root / 'src/tasks/daily/daily_feature.py').write_text('# fixture layout marker', encoding='utf-8')
        (root / 'src/tasks/onetime').mkdir(parents=True)
        daily_source = (SOURCE / 'src/tasks/onetime/DailyTask.py').read_text(encoding='utf-8')
        (root / 'src/tasks/onetime/DailyTask.py').write_text(daily_source, encoding='utf-8')
        config = root / 'configs'
        config.mkdir()
        defaults = {
            'DailyTask': {'⭐收邮件': True, '⭐刷体力': True, '⭐日常奖励': True,
                          '⭐转交运送委托': True, '⭐收信用': True,
                          '⭐送礼': True, '⭐造装备': True, '⭐买信用商店': True,
                          '⭐自动送货': True, '⭐演算': True, '⭐传送到帝江号右侧传送点': True},
            'BoatOrganizeTask': {'⭐帝江号一键存放': True, '⭐简易制作': True, 'privateParameter': 'keep'},
            'BoatHarvestTask': {'⭐帝江号收菜': ['收集线索']},
            'RegionalBuildTask': {'⭐地区建设': ['据点交易'], '购物白名单': ['fixture-user-choice']},
            'ActivityRewardTask': {'⭐活动奖励': ['fixture-user-choice']},
            'DailyBattleTask': {'体力本': '超距辉映管', '刷本序列': ['超距辉映管'], '限时体力药': False},
        }
        defaults['account_scoped_overrides'] = {'accounts': {
            'fixture-A': {name: dict(value) for name, value in defaults.items()},
            'fixture-B': {'BoatHarvestTask': {'⭐帝江号收菜': ['使用制造舱助力']},
                          'DailyBattleTask': {'体力本': 'fixture-B-stage'}},
        }, 'account_registry': {'fixture-A': {'username': 'fixture-user'}}}
        if 'CFG_BOAT_TASKS' in daily_source:
            # Persisted new lists must be narrowed even when old bool values
            # are present and per-account groups disagree with global groups.
            groups = {'帝江号任务': ['送礼', '帝江号整理', '帝江号收菜'],
                '其他任务': ['收邮件', '转交运送委托', '地区建设', '造装备', '收信用', '买信用商店'],
                '战斗任务': ['刷体力', '演算'], '奖励任务': ['活动奖励', '日常奖励'],
                '自动送货任务': ['自动送货'], '收尾任务': ['传送到帝江号右侧传送点']}
            defaults['DailyTask'].update(groups)
            for account in defaults['account_scoped_overrides']['accounts'].values():
                account.setdefault('DailyTask', {}).update(groups)
        for name, value in defaults.items():
            (config / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        original = {p.name: p.read_bytes() for p in config.glob('*.json')}
        def invoke(mode, selected='', profile=None):
            args = [str(replay), str(assembly), str(root), mode, selected]
            if profile: args.append(str(profile))
            result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8')
            if result.returncode != 0:
                raise RuntimeError(f"Endfield split replay {mode} exited {result.returncode}: {result.stderr}")
            return result.stdout.strip()
        assert invoke('reject-migration') == 'endfield_official_config_migration_required'
        assert original == {p.name: p.read_bytes() for p in config.glob('*.json')}
        marker = config / '_daily_split_migrations.json'
        marker.write_text(json.dumps({'completed_batches': ['daily_split_pilot_v1', 'daily_split_v2', 'daily_split_v3']}), encoding='utf-8')
        for selected, expected in (
            ('', []), ('mail', ['MailTask']),
            ('mail,dijiang-harvest,spend-sanity,claim-daily-reward',
             ['BoatHarvestTask', 'MailTask', 'DailyBattleTask', 'DailyRewardTask']),
        ):
            projected = json.loads(invoke('restore', selected))
            for account in (None, 'fixture-A', 'fixture-B'):
                assert check_plan(projected, account) == expected, (selected, account)
            assert projected['RegionalBuildTask']['购物白名单'] == ['fixture-user-choice']
            assert projected['BoatOrganizeTask']['privateParameter'] == 'keep'
            if 'dijiang-harvest' in selected:
                assert projected['BoatHarvestTask']['⭐帝江号收菜'] == ['收集线索']
                assert projected['account_scoped_overrides']['accounts']['fixture-B']['BoatHarvestTask']['⭐帝江号收菜'] == ['使用制造舱助力']
            for name, content in original.items():
                if name != 'DailyTask.json': assert (config / name).read_bytes() == content, name
            assert not list(config.glob('*.yeyu-restore-pending'))
        profile = root / 'profile.json'
        profile.write_text(json.dumps({'staminaStage': '超距辉映管', 'rewardTier': '高阶',
            'staminaRotationStartDate': '2026-10-02', 'staminaRotation': ['超距辉映管'], 'teamSlot': '不换队伍'}), encoding='utf-8')
        projected = json.loads(invoke('pending', 'spend-sanity', profile))
        assert projected['DailyBattleTask']['体力本奖励档位'] == '高阶'
        assert '体力本奖励档位' not in projected['DailyTask']
        assert projected['account_scoped_overrides']['accounts']['fixture-B']['DailyBattleTask']['体力本'] == 'fixture-B-stage'
        assert len(list(config.glob('*.yeyu-restore-pending'))) == 6
        # The next preflight recovers durable originals, then snapshots again.
        invoke('restore', '')
        for name, content in original.items():
            if name != 'DailyTask.json': assert (config / name).read_bytes() == content, name
        assert not list(config.glob('*.yeyu-restore-pending'))
    print(json.dumps({'status': 'passed', 'officialSource': SOURCE.name,
                      'accountCases': 9, 'gameStarted': False, 'toolStarted': False}))


if __name__ == '__main__': main()
