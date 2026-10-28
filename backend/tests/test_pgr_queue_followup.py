"""Explicit offline overlay integration; never imports installed runtime state."""
import ast
import atexit
from copy import deepcopy
from contextlib import ExitStack
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


QA_ROOT = Path(__file__).resolve().parents[2]
if 'PGR_FOLLOWUP_SOURCE' not in os.environ:
    raise unittest.SkipTest('Set PGR_FOLLOWUP_SOURCE explicitly to run the offline source-overlay integration')
SOURCE_ROOT = Path(os.environ['PGR_FOLLOWUP_SOURCE']).resolve()
BASE_SHA256 = {
    'serial_observation_recovery': 'df40c76229405a024d1f75462f867fd1a59109ca0596a7b47027d8578daf09db',
    'pgr_daily_admission': '8149569a560f3512b21499b4448680f4471126c75a651c6b57daf70d184df909',
    'pgr_unused_reservation': 'de1a474b47cf45ef74297e067126d754009b525b1b2b0d778e7ba9492f0af245',
    'pgr_daily_successor': '8e0d2f7e200b83f6b37f00f755f6a860963c6ab5f3bda1d4992644ba380c0c1e',
}
for module_name, expected_sha in BASE_SHA256.items():
    source_file = SOURCE_ROOT / 'backend/yeyu_gamer_manager/services' / (module_name + '.py')
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != expected_sha:
        raise RuntimeError('PGR followup reference SHA mismatch: ' + str(source_file))
cache_root = QA_ROOT / '.cache' / 'pgr-followup'
cache_root.mkdir(parents=True, exist_ok=True)
temporary_overlay = tempfile.TemporaryDirectory(prefix='replay-', dir=cache_root)
atexit.register(temporary_overlay.cleanup)
OVERLAY = Path(temporary_overlay.name)
for module_name in BASE_SHA256:
    shutil.copyfile(SOURCE_ROOT / 'backend/yeyu_gamer_manager/services' / (module_name + '.py'),
                    OVERLAY / (module_name + '.py'))
store_reference = SOURCE_ROOT / 'backend/yeyu_gamer_manager/store/sqlite_store.py'
if hashlib.sha256(store_reference.read_bytes()).hexdigest() != 'db85cd6497eac66109528b64f848725a969b3b2b7bd71c56fcdaefa6f2c56e45':
    raise RuntimeError('PGR followup Store reference SHA mismatch: ' + str(store_reference))
shutil.copyfile(store_reference, OVERLAY / 'sqlite_store.py')
variant = os.environ.get('PGR_FOLLOWUP_VARIANT', 'candidate')
if variant not in {'base', 'candidate'}:
    raise ValueError('PGR_FOLLOWUP_VARIANT must be base or candidate')
if variant == 'candidate':
    subprocess.run(['git', 'apply', '-p4', '--directory=' + OVERLAY.relative_to(QA_ROOT).as_posix(),
                    str(QA_ROOT / 'integration-patches/pgr-queue-followup.patch')], cwd=QA_ROOT, check=True)
    subprocess.run(['git', 'apply', '-p4', '--directory=' + OVERLAY.relative_to(QA_ROOT).as_posix(),
                    str(QA_ROOT / 'integration-patches/pgr-owned-resume-store.patch')], cwd=QA_ROOT, check=True)
sys.dont_write_bytecode = True
sys.path[:0] = [str(SOURCE_ROOT / 'backend'), str(SOURCE_ROOT / 'backend' / 'tests')]

for module_name in BASE_SHA256:
    qualified_name = 'yeyu_gamer_manager.services.' + module_name
    specification = importlib.util.spec_from_file_location(qualified_name, OVERLAY / (module_name + '.py'))
    module = importlib.util.module_from_spec(specification)
    sys.modules[qualified_name] = module
    specification.loader.exec_module(module)
    import yeyu_gamer_manager.services as services
    setattr(services, module_name, module)

import test_pgr_daily_manager as daily_fixtures
import test_pgr_unused_reservation as unused_fixtures
import test_serial_observation_recovery as chain_fixtures
from yeyu_gamer_manager.services import pgr_daily_admission as admission
from yeyu_gamer_manager.services import pgr_daily_selection as selection
from yeyu_gamer_manager.services import pgr_unused_reservation as unused
from yeyu_gamer_manager.services import serial_observation_recovery as chain
from yeyu_gamer_manager.services.manager_errors import ManagerConflict
from yeyu_gamer_manager.services import manager as manager_module
from yeyu_gamer_manager.store import sqlite_store


def qa_method(relative_path, class_name, method_name, source_module):
    source_path = QA_ROOT / relative_path
    syntax = ast.parse(source_path.read_text(encoding='utf-8-sig'))
    source_class = next(node for node in syntax.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in source_class.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    namespace = dict(vars(source_module))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source_path), 'exec'), namespace)
    return namespace[method.name]


class OverlaySuite(unittest.TestSuite):
    def run(self, result, debug=False):
        with ExitStack() as patches:
            for method_name in ('active_execution_summary', 'orphan_pending_execution_candidates'):
                patches.enter_context(mock.patch.object(sqlite_store.SqliteStore, method_name,
                    qa_method('backend/yeyu_gamer_manager/store/sqlite_store.py', 'SqliteStore', method_name, sqlite_store),
                    create=True))
            patches.enter_context(mock.patch.object(sqlite_store.SqliteStore, 'serial_observation_execution_blockers',
                qa_method((OVERLAY / 'sqlite_store.py').relative_to(QA_ROOT), 'SqliteStore',
                    'serial_observation_execution_blockers', sqlite_store), create=True))
            patches.enter_context(mock.patch.object(manager_module.ManagerService, 'execution_occupancy',
                qa_method('backend/yeyu_gamer_manager/services/manager.py', 'ManagerService', 'execution_occupancy', manager_module),
                create=True))
            return super().run(result, debug)


class PgrQueueFollowupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = daily_fixtures.DailyManagerTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.m
        self.store = self.fixture.s
        self.batch, self.run = self.fixture.f.admitted()
        host = self.manager.adapter_host
        host.active_execution = type(host).active_execution.__get__(host, type(host))

    def sibling(self):
        candidates = self.manager._todo_instance_candidates(['GF2'], 'daily')
        self.store.reconcile_todo_instances(candidates, intent='reconcile', requested_by='fixture', reason='offline-sibling')
        todo_ids = [candidates[0]['todo_instance_id']]
        run = self.store.create_game_run({
            'game_id': 'GF2', 'cadence': 'daily', 'mode': 'execute', 'state': 'queued',
            'requested_by': 'fixture', 'todo_instance_ids': todo_ids, 'completion_todo_instance_ids': todo_ids})
        self.store.add_batch_run_membership(self.batch['batch_id'], run['run_id'], ordinal=1, role='initial')
        return run

    def cancel(self):
        self.manager.cancel_batch(self.batch['batch_id'],
            {'reason': 'operator-cancel', 'requestedBy': 'webgui'}, idempotency_key='followup-cancel',
            request_id=None, path='/offline/batch/cancel',
            expected_state_version=self.store.latest_event_sequence())
        self.batch = self.store.get_batch(self.batch['batch_id'])
        self.run = self.store.get_game_run(self.run['run_id'])
        reservation = self.batch['result']['pgrPristineObservation']['pgrFormalAdmission']['admissionId']
        self.resource = self.store.get_resource(selection.TYPE, reservation)
        self.rows = [self.store.get_todo_instance(item) for item in self.run['todo_instance_ids']]
        return reservation

    def test_fresh_sibling_allows_actual_pgr_preparation_not_global_idle(self):
        sibling = self.sibling()
        before = deepcopy(self.store.get_game_run(sibling['run_id']))
        summary = self.store.active_execution_summary()
        self.assertIn(sibling['run_id'], [row['runId'] for row in summary['gameRuns']])
        plan = self.manager._prepare_run_attempt(self.run)
        self.assertEqual(plan.pgr_preparation['operation'], 'attach-home')
        self.assertFalse(plan.pgr_preparation['nativePermitIssued'])
        self.assertEqual(before, self.store.get_game_run(sibling['run_id']))
        self.assertEqual(self.store.list_run_attempts(run_id=sibling['run_id']), [])

    def orphan(self):
        sibling = self.sibling()
        attempt = self.store.create_run_attempt({'run_attempt_id': 'followup-orphan-attempt', 'run_id': sibling['run_id'],
            'game_id': 'GF2', 'cadence': 'daily', 'fencing_token_hash': 'a' * 64,
            'cancel_authority_hash': 'sha256:' + 'b' * 64,
            'plan': {'executableTodoInstanceIds': sibling['todo_instance_ids'],
                     'todos': [{'todoInstanceId': item} for item in sibling['todo_instance_ids']]}})
        self.store.update_run_attempt(attempt['run_attempt_id'], state='failed', completed=True)
        self.store.create_or_get_resume_intent({'run_id': sibling['run_id'],
            'predecessor_run_attempt_id': attempt['run_attempt_id'], 'source_run_revision': 1,
            'source_attempt_revision': 1, 'decision': {'fixture': 'failed-orphan'}, 'state': 'successor_failed'})
        self.store.update_batch_run_membership(self.batch['batch_id'], sibling['run_id'], state='terminal',
            latest_run_attempt_id=attempt['run_attempt_id'], terminal_outcome='failed')
        self.store.update_game_run(sibling['run_id'], state='pending_execution')
        return sibling

    def test_real_host_idle_and_real_sql_exclude_failed_successor_orphan(self):
        orphan = self.orphan()
        self.assertIsNone(self.manager.adapter_host.active_execution())
        self.assertIn(orphan['run_id'], [row['runId'] for row in self.store.active_execution_summary()['gameRuns']])
        occupancy = self.manager.execution_occupancy(owned_batch_id=self.batch['batch_id'], current_run_id=self.run['run_id'])
        self.assertNotIn(orphan['run_id'], [row['runId'] for row in occupancy['gameRuns']])
        plan = self.manager._prepare_run_attempt(self.run)
        self.assertEqual(plan.pgr_preparation['operation'], 'attach-home')

    def test_unknown_host_does_not_exclude_failed_successor_orphan(self):
        orphan = self.orphan()
        with mock.patch.object(self.manager.adapter_host, 'active_execution', return_value={}):
            occupancy = self.manager.execution_occupancy(owned_batch_id=self.batch['batch_id'], current_run_id=self.run['run_id'])
        self.assertIn(orphan['run_id'], [row['runId'] for row in occupancy['gameRuns']])

    def test_host_owning_failed_successor_orphan_stays_reported(self):
        orphan = self.orphan()
        with mock.patch.object(self.manager.adapter_host, 'active_execution', return_value={'runId': orphan['run_id']}):
            occupancy = self.manager.execution_occupancy(owned_batch_id=self.batch['batch_id'], current_run_id=self.run['run_id'])
        self.assertIn(orphan['run_id'], [row['runId'] for row in occupancy['gameRuns']])

    def pending_successor(self):
        from yeyu_gamer_manager.services import pgr_daily_successor as successor
        self.store.update_batch(self.batch['batch_id'], state='running', result=self.batch['result'])
        preparation = self.manager._prepare_run_attempt(self.run)
        self.manager.adapter_host._artifact_staging_dir(preparation).mkdir(parents=True, exist_ok=True)
        self.store.update_batch_run_membership(self.batch['batch_id'], self.run['run_id'], state='active',
            latest_run_attempt_id=preparation.run_attempt_id)
        final = daily_fixtures.lifecycle.AdmissionTests.finalize_fixture(self.fixture.f, self.run, preparation)
        result = self.fixture.close_synthetic_step(self.batch, final)
        following = successor.prepare(self.manager, self.batch['batch_id'], final, result)
        self.assertIsNotNone(following)
        self.assertEqual(self.store.get_resume_intent(following['intentId'])['state'], 'successor_pending')
        return following

    def test_mixed_all_fifteen_steps_preserve_fresh_siblings_and_one_reservation(self):
        from yeyu_gamer_manager.services import pgr_daily_successor as successor
        sibling = self.sibling()
        self.store.update_game_run(sibling['run_id'], state='pending_execution')
        self.store.update_batch(self.batch['batch_id'], state='running', result=self.batch['result'])
        following = None
        observed = []
        with mock.patch.object(admission, 'recheck_inputs'):
            for ordinal in range(15):
                options = {} if following is None else {'todo_instance_ids': following['todoInstanceIds'],
                                                       'attempt_ordinal': following['attemptOrdinal']}
                preparation = self.manager._prepare_run_attempt(self.store.get_game_run(self.run['run_id']), **options)
                self.manager.adapter_host._artifact_staging_dir(preparation).mkdir(parents=True, exist_ok=True)
                self.store.update_batch_run_membership(self.batch['batch_id'], self.run['run_id'], state='active',
                    latest_run_attempt_id=preparation.run_attempt_id)
                if following:
                    self.store.update_resume_intent(following['intentId'], state='successor_dispatched',
                        successor_run_attempt_id=preparation.run_attempt_id)
                final = daily_fixtures.lifecycle.AdmissionTests.finalize_fixture(self.fixture.f, self.run, preparation)
                observed.append((final.pgr_daily_step['operation'], final.pgr_daily_step['phase']))
                result = self.fixture.close_synthetic_step(self.batch, final)
                if following:
                    self.store.update_resume_intent(following['intentId'], state='successor_terminal')
                following = successor.prepare(self.manager, self.batch['batch_id'], final, result)
                self.fixture.f.clock.at += timedelta(seconds=1)
        self.assertEqual(len(observed), 15)
        self.assertIsNone(following)
        self.assertEqual(len(self.store.list_run_attempts(run_id=self.run['run_id'])), 15)
        resource = self.store.get_resource(selection.TYPE, final.pgr_daily_step['reservationId'])
        self.assertEqual(resource['document']['dailyLedger']['state'], 'complete')
        self.assertEqual(len(resource['document']['dailyLedger']['spent']), 6)
        self.assertFalse(resource['document']['businessAccepted'])
        self.assertEqual(self.store.list_run_attempts(run_id=sibling['run_id']), [])

    def test_pending_intent_decision_mutations_fail_closed(self):
        following = self.pending_successor()
        original = self.store.get_resume_intent(following['intentId'])
        for change in ({'kind': 'unknown'}, {'batchId': 'foreign'}, {'reservationId': 'foreign'},
                       {'nextStepIndex': 9}, {'operation': 'dorm'}, {'phase': 'execute'},
                       {'sourceRunAttemptId': 'foreign'}, {'eligibleTodoInstanceIds': ['foreign']}):
            with self.subTest(change=change):
                with self.store.atomic():
                    self.store.connection.execute('UPDATE resume_intents SET decision_json=? WHERE resume_intent_id=?',
                        (json.dumps({**original['decision'], **change}), following['intentId']))
                with self.assertRaises(ManagerConflict):
                    self.manager._prepare_run_attempt(self.store.get_game_run(self.run['run_id']),
                        todo_instance_ids=following['todoInstanceIds'], attempt_ordinal=following['attemptOrdinal'])
                self.assertEqual(len(self.store.list_run_attempts(run_id=self.run['run_id'])), 1)
        with self.store.atomic():
            self.store.connection.execute('UPDATE resume_intents SET decision_json=? WHERE resume_intent_id=?',
                (json.dumps(original['decision']), following['intentId']))

    def test_nonlatest_or_duplicate_own_intent_rejects(self):
        following = self.pending_successor()
        original = self.store.get_resume_intent(following['intentId'])
        self.store.create_or_get_resume_intent({'run_id': self.run['run_id'],
            'predecessor_run_attempt_id': original['predecessor_run_attempt_id'],
            'source_run_revision': original['source_run_revision'] + 1,
            'source_attempt_revision': original['source_attempt_revision'],
            'decision': original['decision'], 'state': 'successor_pending'})
        with self.assertRaisesRegex(ManagerConflict, 'resume_not_unique_latest'):
            admission.owned_resume(self.manager, self.run['run_id'])

    def test_wrong_member_intent_rejects(self):
        following = self.pending_successor()
        with self.store.atomic():
            self.store.connection.execute('UPDATE batch_run_memberships SET resume_intent_id=NULL WHERE batch_id=? AND run_id=?',
                (self.batch['batch_id'], self.run['run_id']))
        with self.assertRaisesRegex(ManagerConflict, 'resume_owner_changed'):
            admission.owned_resume(self.manager, self.run['run_id'])

    def test_pending_with_extra_unowned_attempt_rejects(self):
        self.pending_successor()
        self.store.create_run_attempt({'run_attempt_id': 'unowned-after-resume', 'run_id': self.run['run_id'],
            'game_id': 'PGR', 'cadence': 'daily', 'attempt_ordinal': 2, 'fencing_token_hash': 'a' * 64,
            'cancel_authority_hash': 'sha256:' + 'b' * 64,
            'plan': {'executableTodoInstanceIds': self.run['todo_instance_ids'],
                     'todos': [{'todoInstanceId': item} for item in self.run['todo_instance_ids']]}})
        with self.assertRaisesRegex(ManagerConflict, 'resume_attempt_changed'):
            admission.owned_resume(self.manager, self.run['run_id'])

    def test_failed_latest_intent_does_not_authorize_ready_ledger(self):
        following = self.pending_successor()
        self.store.update_resume_intent(following['intentId'], state='successor_failed')
        with self.assertRaisesRegex(ManagerConflict, 'resume_missing_active_intent'):
            self.manager._prepare_run_attempt(self.store.get_game_run(self.run['run_id']),
                todo_instance_ids=following['todoInstanceIds'], attempt_ordinal=following['attemptOrdinal'])
        self.assertEqual(len(self.store.list_run_attempts(run_id=self.run['run_id'])), 1)

    def test_pending_with_lingering_predecessor_lease_rejects(self):
        following = self.pending_successor()
        predecessor = self.store.get_resume_intent(following['intentId'])['predecessor_run_attempt_id']
        with self.store.atomic():
            changed = self.store.connection.execute('UPDATE controller_leases SET state=? WHERE run_attempt_id=?',
                ('active', predecessor)).rowcount
        self.assertEqual(changed, 1)
        with self.assertRaisesRegex(ManagerConflict, 'resume_effect_owned'):
            admission.owned_resume(self.manager, self.run['run_id'])

    def test_revoked_intent_does_not_authorize_prepared_grant(self):
        following = self.pending_successor()
        preparation = self.manager._prepare_run_attempt(self.store.get_game_run(self.run['run_id']),
            todo_instance_ids=following['todoInstanceIds'], attempt_ordinal=following['attemptOrdinal'])
        self.store.update_batch_run_membership(self.batch['batch_id'], self.run['run_id'], state='active',
            latest_run_attempt_id=preparation.run_attempt_id)
        self.store.update_resume_intent(following['intentId'], state='successor_failed')
        with self.assertRaisesRegex(ManagerConflict, 'resume_missing_active_intent'):
            admission.guard_dispatch(self.manager, self.store.get_game_run(self.run['run_id']),
                dispatch_attempt_id=preparation.run_attempt_id, _check_environment=False)

    def test_store_does_not_exempt_stale_intent_snapshot(self):
        following = self.pending_successor()
        qualified = admission.owned_resume(self.manager, self.run['run_id'])
        self.assertFalse(self.store.serial_observation_execution_blockers(self.run['run_id'], owned_pgr_resume=qualified)['resumes'])
        self.store.update_resume_intent(following['intentId'], state='successor_dispatched')
        self.assertTrue(self.store.serial_observation_execution_blockers(self.run['run_id'], owned_pgr_resume=qualified)['resumes'])

    def test_foreign_pending_intent_still_blocks_qualified_pgr(self):
        self.pending_successor()
        orphan = self.orphan()
        foreign = self.store.list_resume_intents(run_id=orphan['run_id'])[0]
        self.store.update_resume_intent(foreign['resume_intent_id'], state='successor_pending')
        qualified = admission.owned_resume(self.manager, self.run['run_id'])
        self.assertTrue(self.store.serial_observation_execution_blockers(self.run['run_id'], owned_pgr_resume=qualified)['resumes'])
        with self.assertRaises(ManagerConflict):
            self.manager._prepare_run_attempt(self.store.get_game_run(self.run['run_id']))

    def test_resume_qualification_and_blockers_share_atomic_writer_boundary(self):
        following = self.pending_successor()
        original_qualification = admission.owned_resume
        original_blockers = self.store.serial_observation_execution_blockers
        observations = []
        writer_outcomes = []

        def external_writer():
            connection = sqlite3.connect(self.store.path, timeout=0, isolation_level=None)
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('UPDATE batch_run_memberships SET resume_intent_id=NULL WHERE batch_id=? AND run_id=?',
                    (self.batch['batch_id'], self.run['run_id']))
                connection.execute('COMMIT')
                writer_outcomes.append('committed')
            except sqlite3.OperationalError as error:
                writer_outcomes.append('locked' if 'locked' in str(error) else str(error))
            finally:
                connection.close()

        def qualification(*args):
            observations.append(self.store.connection.in_transaction)
            qualified = original_qualification(*args)
            writer = threading.Thread(target=external_writer)
            writer.start()
            writer.join(timeout=2)
            self.assertFalse(writer.is_alive())
            return qualified

        def blockers(*args, **kwargs):
            observations.append(self.store.connection.in_transaction)
            return original_blockers(*args, **kwargs)

        with mock.patch.object(admission, 'owned_resume', side_effect=qualification), \
                mock.patch.object(self.store, 'serial_observation_execution_blockers', side_effect=blockers):
            chain._current_ownership(self.manager, child_run_id=self.run['run_id'],
                allow_stopped=False, allow_queued_siblings=True)
        self.assertEqual(observations, [True, True])
        self.assertEqual(writer_outcomes, ['locked'])
        self.assertEqual(self.store.get_batch_run_membership(self.batch['batch_id'], self.run['run_id'])['resume_intent_id'],
                         following['intentId'])
        with self.store.atomic():
            self.store.connection.execute('UPDATE batch_run_memberships SET resume_intent_id=NULL WHERE batch_id=? AND run_id=?',
                (self.batch['batch_id'], self.run['run_id']))
        with self.assertRaisesRegex(ManagerConflict, 'resume_owner_changed'):
            chain._current_ownership(self.manager, child_run_id=self.run['run_id'],
                allow_stopped=False, allow_queued_siblings=True)

    def test_ledger_change_without_intent_change_is_requalified(self):
        following = self.pending_successor()
        original = self.store.get_resume_intent(following['intentId'])
        resource = self.store.get_resource(selection.TYPE, original['decision']['reservationId'])
        changed = deepcopy(resource['document'])
        changed['dailyLedger']['pending'][0]['phase'] = 'execute'
        self.store.update_resource(selection.TYPE, resource['resource_id'], state=resource['state'], document=changed)
        self.assertEqual(self.store.get_resume_intent(following['intentId']), original)
        with self.assertRaisesRegex(ManagerConflict, 'resume_decision_changed'):
            chain._current_ownership(self.manager, child_run_id=self.run['run_id'],
                allow_stopped=False, allow_queued_siblings=True)

    def test_default_observation_guard_still_rejects_sibling(self):
        self.sibling()
        with self.assertRaisesRegex(ManagerConflict, 'execution_active'):
            chain.guard(self.manager, child_run_id=self.run['run_id'])

    def test_other_batch_queued_member_still_blocks(self):
        sibling = self.sibling()
        other = self.store.create_batch({'cadence': 'daily', 'mode': 'execute', 'state': 'queued',
            'game_ids': ['GF2'], 'requested_by': 'fixture', 'result': {}})
        self.store.add_batch_run_membership(other['batch_id'], sibling['run_id'], ordinal=0, role='initial')
        with self.assertRaises(ManagerConflict):
            self.manager._prepare_run_attempt(self.run)
        self.assertEqual(self.store.list_run_attempts(run_id=self.run['run_id']), [])

    def test_sibling_attempt_still_blocks(self):
        sibling = self.sibling()
        self.store.create_run_attempt({'run_attempt_id': 'followup-other-attempt', 'run_id': sibling['run_id'],
            'game_id': 'GF2', 'cadence': 'daily', 'fencing_token_hash': 'a' * 64,
            'cancel_authority_hash': 'sha256:' + 'b' * 64,
            'plan': {'executableTodoInstanceIds': sibling['todo_instance_ids'],
                     'todos': [{'todoInstanceId': item} for item in sibling['todo_instance_ids']]}})
        with self.assertRaises(ManagerConflict):
            self.manager._prepare_run_attempt(self.run)
        self.assertEqual(self.store.list_run_attempts(run_id=self.run['run_id']), [])

    def test_sibling_running_without_attempt_still_blocks(self):
        sibling = self.sibling()
        self.store.update_game_run(sibling['run_id'], state='running')
        with self.assertRaisesRegex(ManagerConflict, 'execution_active'):
            self.manager._prepare_run_attempt(self.run)

    def test_second_guard_read_rejects_sibling_becoming_running(self):
        sibling = self.sibling()
        original = self.store.active_execution_summary
        calls = []

        def changed_summary(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                self.store.update_game_run(sibling['run_id'], state='running')
            return original(**kwargs)

        with mock.patch.object(self.store, 'active_execution_summary', side_effect=changed_summary):
            with self.assertRaisesRegex(ManagerConflict, 'execution_active'):
                admission.guard_dispatch(self.manager, self.run)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call['owned_batch_id'] == self.batch['batch_id'] for call in calls))

    def test_guard_without_child_keeps_global_exclusion(self):
        self.sibling()
        with self.assertRaisesRegex(ManagerConflict, 'execution_active'):
            chain.guard(self.manager)

    def test_operator_cancelled_zero_attempt_reuses_exact_reservation_on_new_start(self):
        reservation = self.cancel()
        before = deepcopy((self.batch, self.run, self.resource, self.rows))
        version = self.store.latest_event_sequence()
        self.assertTrue(unused.validate(self.manager.manager_todos, self.resource, self.rows))
        self.assertEqual(version, self.store.latest_event_sequence())
        result = self.fixture.f.start('followup-restart')
        self.assertEqual(result.status_code, 202, result.text)
        updated = self.store.get_resource(selection.TYPE, reservation)
        self.assertNotEqual(updated['document']['runId'], self.run['run_id'])
        self.assertEqual(updated['document']['dailyLedger']['steps'], [])
        self.assertEqual(updated['document']['dailyLedger']['spent'], {})
        self.assertEqual(updated['document']['dailyOwners'][-1]['unusedDailyLedger'], before[2]['document']['dailyLedger'])
        self.assertEqual(self.store.get_batch(self.batch['batch_id']), before[0])
        self.assertEqual(self.store.get_game_run(self.run['run_id']), before[1])
        self.assertEqual([self.store.get_todo_instance(row['todo_instance_id']) for row in self.rows], before[3])
        self.assertFalse(updated['document']['businessAccepted'])

    def test_cancel_validation_does_not_restart_stopped_intent(self):
        self.cancel()
        before = deepcopy(self.manager.daily_execution.read())
        self.assertEqual(before['state'], 'stopped')
        self.assertTrue(unused.validate(self.manager.manager_todos, self.resource, self.rows))
        self.assertEqual(self.manager.daily_execution.read(), before)

    def test_forged_or_unsealed_durable_cancel_rejects(self):
        self.cancel()
        request = self.store.get_batch_cancel_request(self.batch['result']['cancelRequestId'])
        for change in ({'state': 'acknowledged'}, {'batch_id': 'other'}, {'reason': 'changed'},
                       {'requested_by': 'manager'}, {'active_run_attempt_ids': ['unknown']}):
            with self.subTest(change=change), mock.patch.object(self.store, 'get_batch_cancel_request',
                                                               return_value={**request, **change}):
                with self.assertRaises(ManagerConflict):
                    unused.validate(self.manager.manager_todos, self.resource, self.rows)

    def test_cancelled_reservation_with_any_attempt_rejects(self):
        self.cancel()
        self.store.create_run_attempt({'run_attempt_id': 'followup-late-attempt', 'run_id': self.run['run_id'],
            'game_id': 'PGR', 'cadence': 'daily', 'fencing_token_hash': 'a' * 64,
            'cancel_authority_hash': 'sha256:' + 'b' * 64,
            'plan': {'executableTodoInstanceIds': self.run['todo_instance_ids'],
                     'todos': [{'todoInstanceId': item} for item in self.run['todo_instance_ids']]}})
        with self.assertRaisesRegex(ManagerConflict, 'attempt_exists'):
            unused.validate(self.manager.manager_todos, self.resource, self.rows)


def load_tests(loader, tests, pattern):
    suite = OverlaySuite([loader.loadTestsFromTestCase(PgrQueueFollowupTests)])
    if os.environ.get('PGR_FOLLOWUP_ORIGINAL_TESTS') == '1':
        for module in (daily_fixtures, unused_fixtures, chain_fixtures):
            suite.addTests(loader.loadTestsFromModule(module))
    return suite
