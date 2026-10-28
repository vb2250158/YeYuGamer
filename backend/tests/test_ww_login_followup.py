"""Offline source-method regressions, not Manager/SDK integration acceptance."""
import ast
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from uuid import UUID


QA = Path(__file__).resolve().parents[2]
SCOPE_RELATIVE = Path('backend/yeyu_gamer_manager/services/ww_multi_account_scope.py')
MANAGER_RELATIVE = Path('backend/yeyu_gamer_manager/services/manager.py')
SCOPE_BASE_SHA = '8b2560faf338d41eb25555ed4c913e8dd7f8219c30e97b9fbd8b47ab50670e90'
SCOPE_CANDIDATE_SHA = 'adc98819bab5c78c5997f52ce0928635da5ae0ba0c76e61aaf6178ed767f5cf0'
MANAGER_SHA = '279ccef22f0916d45821ef2d9693957e6a4a06f3ec8fff4536f71a7360202244'
LOGIN_SHA = '890d2fe4eea471cc54ae651da6545d27e079db289875eee77833ae37061ed9a6'
PATCH_SHA = '0d96d5a943820bbfdb1f19a38ade42330e6e411882d6597a2274b03d39705d3b'
LOGIN_OBJECT = '20d2a0fbd5cbe257d80cc2de5f70d369ba25d07f:src/task/MultiAccountDailyTask.py'
SOURCE = None
SCOPE = None
LOGIN = None
WORK = None
OPERATIONS = ('attach-world', 'inspect-daily-progress', 'farm-nightmare-daily-echo',
              'spend-waveplates', 'claim-daily-reward', 'claim-mail', 'claim-battle-pass')


def checked_bytes(data, expected, label):
    if hashlib.sha256(data).hexdigest() != expected:
        raise AssertionError(f'{label}: pinned SHA256 mismatch')
    return data


def manager_text():
    return checked_bytes((SOURCE / MANAGER_RELATIVE).read_bytes(), MANAGER_SHA,
                         'reference manager').decode('utf-8-sig')


def setUpModule():
    global SOURCE, SCOPE, LOGIN, WORK
    roots = []
    for name in ('WW_FOLLOWUP_REFERENCE_ROOT', 'WW_FOLLOWUP_FORK_ROOT'):
        value = os.environ.get(name)
        if not value or not Path(value).is_absolute() or not Path(value).is_dir():
            raise RuntimeError(f'{name} must explicitly name an existing absolute read-only reference root')
        roots.append(Path(value).resolve())
    SOURCE, fork = roots
    variant = os.environ.get('WW_FOLLOWUP_VARIANT', 'candidate')
    if variant not in {'base', 'candidate'}:
        raise RuntimeError('WW_FOLLOWUP_VARIANT must be base or candidate')
    base = checked_bytes((SOURCE / SCOPE_RELATIVE).read_bytes(), SCOPE_BASE_SHA, 'reference WW scope')
    manager_text()
    patch = checked_bytes((QA / 'integration-patches/ww-login-followup.patch').read_bytes(),
                          PATCH_SHA, 'delivery patch')
    login = subprocess.run(['git', '-C', str(fork), 'show', LOGIN_OBJECT],
                           check=True, capture_output=True, timeout=15).stdout
    checked_bytes(login, LOGIN_SHA, 'reference login Git object')
    WORK = tempfile.TemporaryDirectory(prefix='ww-followup-', dir=QA)
    try:
        temporary = Path(WORK.name)
        candidate = temporary / SCOPE_RELATIVE
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(base.replace(b'\r\n', b'\n'))
        environment = dict(os.environ, GIT_CEILING_DIRECTORIES=str(QA))
        for arguments in (['--check'], []):
            subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', *arguments, '-'],
                           input=patch, cwd=temporary, env=environment, capture_output=True,
                           check=True, timeout=15)
        candidate.write_bytes(candidate.read_bytes().replace(b'\n', b'\r\n'))
        checked_bytes(candidate.read_bytes(), SCOPE_CANDIDATE_SHA, 'patched temporary WW scope')
        baseline = temporary / 'ww_multi_account_scope.base.py'
        baseline.write_bytes(base)
        SCOPE = baseline if variant == 'base' else candidate
        LOGIN = temporary / 'MultiAccountDailyTask.py'
        LOGIN.write_bytes(login)
    except BaseException:
        WORK.cleanup()
        raise


def tearDownModule():
    if WORK is not None:
        WORK.cleanup()


class WaitFailedException(Exception):
    pass


class ManagerConflict(Exception):
    pass


class Profile:
    @classmethod
    def model_validate(cls, value):
        return SimpleNamespace(model_dump=lambda **kwargs: deepcopy(value))


def methods(path, names, namespace):
    text = manager_text() if path == SOURCE / MANAGER_RELATIVE else path.read_text(encoding='utf-8-sig')
    tree = ast.parse(text)
    selected = [node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(selected) != len(names):
        raise AssertionError('source method missing or ambiguous')
    for node in selected:
        for child in ast.walk(node):
            if isinstance(getattr(child, 'body', None), list):
                child.body = [statement for statement in child.body
                              if not isinstance(statement, ast.ImportFrom) or not statement.level]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace


def scope_module():
    return methods(SCOPE, {'digest', '_uuid', '_label', 'scope_digest', 'validate_scope', 'build_for_plan'},
                   dict(deepcopy=deepcopy, hashlib=hashlib, json=json, UUID=UUID,
                        OPERATIONS=OPERATIONS, CONTRACT='ww-multi-account@1',
                        OKWWProfileConfig=Profile,
                        frozen_run_tool_profiles=lambda run, *args: run['account_snapshot']['daily_tool_profiles']))


def uid(number):
    return str(UUID(int=number))


def cohort_fixture():
    runs, todos = [], {}
    for ordinal in range(2):
        account_id = uid(10 + ordinal)
        todo_ids = ['todo-instance-' + uid(100 + ordinal * 10 + step) for step in range(7)]
        snapshot = dict(saved_account_label=f'fixture{ordinal}****',
                        daily_todo_selection=list(OPERATIONS), daily_tool_profiles={'ok_ww': {}})
        runs.append(dict(run_id=uid(20 + ordinal), account_id=account_id, game_id='WW',
                         cadence='daily', account_snapshot=snapshot, todo_instance_ids=todo_ids))
        for todo_id, operation in zip(todo_ids, OPERATIONS):
            todos[todo_id] = SimpleNamespace(operation=operation, game_id='WW', account_id=account_id,
                                             period_key='fixture-day', status='pending')
    batch = dict(batch_id=uid(1), result={'accountTargets': [dict(gameId='WW', runId=run['run_id'],
                 accountId=run['account_id'], accountSnapshot=deepcopy(run['account_snapshot'])) for run in runs]})
    memberships = [dict(batch_id=batch['batch_id'], run_id=run['run_id'], ordinal=ordinal)
                   for ordinal, run in enumerate(runs)]
    attempts = {}
    store = SimpleNamespace(list_batch_run_memberships=lambda **kwargs: memberships,
                            get_game_run=lambda run_id: next(run for run in runs if run['run_id'] == run_id),
                            list_run_attempts=lambda run_id, **kwargs: attempts.get(run_id, []),
                            list_todo_attempts=lambda **kwargs: [])
    manager = SimpleNamespace(store=store, _latest_batch_for_run=lambda run_id: batch,
                              get_todo_instance=todos.__getitem__, _validate_execution_period=lambda value: None)

    def plan_for(index):
        run = runs[index]
        document = dict(gameId='WW', cadence='daily', runId=run['run_id'], runAttemptId=uid(30 + index),
                        accountId=run['account_id'], accountSnapshot=deepcopy(run['account_snapshot']),
                        selectedOperations=list(OPERATIONS), executableTodoInstanceIds=run['todo_instance_ids'],
                        todos=[dict(operation=operation) for operation in OPERATIONS])
        return SimpleNamespace(game_id='WW', observation_only_operations=(), run_attempt_id=uid(30 + index),
                               to_document=lambda: deepcopy(document))

    return SimpleNamespace(manager=manager, runs=runs, attempts=attempts, batch=batch, plan_for=plan_for)


class LoginWaitTests(unittest.TestCase):
    def task(self, state=None):
        namespace = methods(LOGIN, {'_prepare_first_account_login'},
                            dict(WaitFailedException=WaitFailedException))
        trace = []
        task = SimpleNamespace(_login_combo=lambda: state == 'login', do_find_account_drop_down=lambda: False,
                               in_team_and_world=lambda: state == 'world',
                               _title_account_entry=lambda: None, _title_logout_confirmation=lambda: None,
                               _title_logged_out_login_entry=lambda: None,
                               ensure_main=lambda **kwargs: trace.append(('ensure', kwargs)),
                               _switch_to_login=lambda: trace.append(('switch',)),
                               click=lambda *args, **kwargs: self.fail('unexpected input'),
                               log_info=lambda *args: None)

        def wait_until(callback, **kwargs):
            trace.append(('budget', kwargs))
            for tick in range(kwargs['time_out']):
                result = callback()
                if result:
                    return result
            return None

        task.wait_until = wait_until
        return namespace['_prepare_first_account_login'], task, trace

    def test_unknown_or_invisible_screen_expires_without_input(self):
        method, task, trace = self.task()
        with self.assertRaisesRegex(WaitFailedException, '^account_login_screen_unavailable:'):
            method(task)
        self.assertEqual(trace, [('budget', dict(time_out=100, raise_if_not_found=False))])

    def test_recognized_picker_returns_without_world_navigation(self):
        method, task, trace = self.task('login')
        method(task)
        self.assertEqual(len(trace), 1)

    def test_world_keeps_existing_switch_path(self):
        method, task, trace = self.task('world')
        method(task)
        self.assertEqual(trace[1:], [('ensure', dict(time_out=100)), ('switch',)])

    def test_cancellation_propagates(self):
        method, task, trace = self.task()

        def cancelled():
            raise InterruptedError('fixture cancellation')

        task._login_combo = cancelled
        with self.assertRaises(InterruptedError):
            method(task)


class ReplayDependencyTests(unittest.TestCase):
    def setUp(self):
        self.module = scope_module()
        self.fixture = cohort_fixture()

    def build(self, index=1):
        fixture = self.fixture
        return self.module['build_for_plan'](fixture.manager, fixture.runs[index], fixture.plan_for(index))

    def test_missing_source_attempt_is_explicit_rejection(self):
        with self.assertRaisesRegex(ValueError, '^ww_account_replay_source_missing$'):
            self.build()

    def test_malformed_dependency_plan_has_same_explicit_rejection(self):
        for plan in (None, [], 'corrupt-plan'):
            with self.subTest(plan_type=type(plan).__name__):
                self.fixture.attempts[self.fixture.runs[0]['run_id']] = [
                    dict(run_attempt_id=uid(30), state='failed', plan=plan)]
                with self.assertRaisesRegex(ValueError, '^ww_account_replay_source_missing$'):
                    self.build()

    def test_failed_parent_alone_does_not_discard_valid_scoped_observations(self):
        scope = self.build(0)
        before = deepcopy(scope)
        self.fixture.attempts[self.fixture.runs[0]['run_id']] = [
            dict(run_attempt_id=uid(30), state='failed', plan={'wwAccountScope': scope})]
        replay = self.build()
        self.assertEqual(replay, dict(before, mode='replay'))
        self.assertEqual(scope, before)

    def test_foreign_attempt_cannot_supply_dependency(self):
        scope = self.build(0)
        self.fixture.attempts[self.fixture.runs[0]['run_id']] = [
            dict(run_attempt_id=uid(999), plan={'wwAccountScope': scope})]
        with self.assertRaisesRegex(ValueError, '^ww_account_replay_source_missing$'):
            self.build()


class ObservationFileTests(unittest.TestCase):
    def setUp(self):
        namespace = methods(SOURCE / 'backend/yeyu_gamer_manager/services/manager.py',
                            {'_ww_official_multi_observation'}, dict(Path=Path, ManagerConflict=ManagerConflict))
        self.method = namespace['_ww_official_multi_observation']
        self.fixture = cohort_fixture()
        self.temporary = tempfile.TemporaryDirectory(prefix='file-seam-', dir=WORK.name)
        self.addCleanup(self.temporary.cleanup)
        self.fixture.manager.settings = SimpleNamespace(data_dir=Path(self.temporary.name))
        self.path = Path(self.temporary.name) / 'ww-official-multi-account' / (uid(1) + '.jsonl')

    def observe(self, mode='replay', **kwargs):
        return self.method(self.fixture.manager, self.fixture.runs[1], self.fixture.batch,
                           account_scope=dict(batchId=uid(1), mode=mode, sourceRunId=uid(20)), **kwargs)

    def test_missing_file_fails_once_without_creating_or_launching(self):
        with self.assertRaisesRegex(ManagerConflict, '^ww_account_replay_source_missing$'):
            self.observe()
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_existing_source_is_read_only_and_not_a_success_receipt(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b'fixture-not-valid-observation\n')
        before = self.path.read_bytes()
        self.assertEqual(self.observe(), (self.path, 'read'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_directory_is_not_a_replay_source(self):
        self.path.mkdir(parents=True)
        with self.assertRaisesRegex(ManagerConflict, '^ww_account_replay_source_missing$'):
            self.observe()

    def test_observation_successor_does_not_reuse_daily_file(self):
        self.assertEqual(self.observe(observation_only_operations=('inspect-daily-progress',)), (None, None))

    def test_replay_cannot_fall_back_to_execute(self):
        with self.assertRaisesRegex(ManagerConflict, '^ww_account_source_run_mismatch$'):
            self.observe(mode='execute')
        self.assertFalse(self.path.parent.exists())


class FailureTerminalTests(unittest.TestCase):
    def test_existing_launch_failure_body_closes_attempt_lease_and_membership(self):
        source = SOURCE / 'backend/yeyu_gamer_manager/services/manager.py'
        tree = ast.parse(manager_text())
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
                    and isinstance(node.type, ast.Name) and node.type.id == 'Exception'
                    and any(isinstance(child, ast.Constant) and child.value == 'adapter_start_failed'
                            for child in ast.walk(node))]
        self.assertEqual(len(handlers), 1)
        transactions = [node for node in handlers[0].body if isinstance(node, ast.With)]
        self.assertEqual(len(transactions), 1)
        for starting_state in ('starting', 'running', 'cancelling'):
            with self.subTest(starting_state=starting_state):
                attempt = dict(state=starting_state)
                recorded = {}
                store = SimpleNamespace(atomic=nullcontext, get_run_attempt=lambda attempt_id: attempt,
                    list_batch_run_memberships=lambda **kwargs: [dict(latest_run_attempt_id=uid(31), batch_id=uid(1))],
                    update_run_attempt=lambda attempt_id, **kwargs: attempt.update(kwargs),
                    update_game_run=lambda run_id, **kwargs: recorded.update(run=kwargs))
                manager = SimpleNamespace(store=store, _terminal_game_cleanup_gate=lambda *args: None,
                    _end_controller_lease=lambda attempt_id, **kwargs: recorded.update(lease=kwargs),
                    _terminalize_unsealed_batch_memberships=lambda **kwargs: recorded.update(membership=kwargs))
                namespace = dict(self=manager, plan=SimpleNamespace(run_attempt_id=uid(31), run_id=uid(21)),
                    run_id=uid(21), preserved_state=None, failure_code='adapter_start_failed', cleanup=None,
                    error=ManagerConflict('ww_account_replay_source_missing'), launch_failure={},
                    EntityState=SimpleNamespace(FAILED='failed'), LeaseState=SimpleNamespace(REVOKED='revoked'),
                    GameLaunchHumanRequired=RuntimeError, GameLaunchCancelled=InterruptedError)
                exec(compile(ast.Module(body=transactions, type_ignores=[]), str(source), 'exec'), namespace)
                self.assertEqual(attempt['state'], 'failed')
                self.assertTrue(attempt['completed'])
                self.assertEqual(attempt['result']['message'], 'ManagerConflict: ww_account_replay_source_missing')
                self.assertEqual(recorded['run']['state'], 'failed')
                self.assertEqual(recorded['lease']['state'], 'revoked')
                self.assertEqual(recorded['membership']['outcome'], 'failed')


class SourceIntegrityTests(unittest.TestCase):
    def test_pinned_inputs_and_candidate_only_change(self):
        expected = {SOURCE / SCOPE_RELATIVE: SCOPE_BASE_SHA, LOGIN: LOGIN_SHA,
                    SOURCE / MANAGER_RELATIVE: MANAGER_SHA,
                    QA / 'integration-patches/ww-login-followup.patch': PATCH_SHA}
        for path, digest in expected.items():
            with self.subTest(source=path.name):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertIn(hashlib.sha256(SCOPE.read_bytes()).hexdigest(), {SCOPE_BASE_SHA, SCOPE_CANDIDATE_SHA})


if __name__ == '__main__':
    unittest.main()
