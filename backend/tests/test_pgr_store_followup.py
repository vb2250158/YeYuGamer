from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from yeyu_gamer_manager.store.sqlite_store import SqliteStore
from yeyu_gamer_manager.services.todo_catalog import catalog
from yeyu_gamer_manager.domain.todos import todo_period


class PgrStoreFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pgr-store-followup-')
        self.addCleanup(self.temporary.cleanup)
        self.store = SqliteStore(Path(self.temporary.name) / 'manager.sqlite3')
        self.store.initialize()
        self.addCleanup(self.store.close)
        self.store.import_legacy(config_values={}, games=[{
            'game_id': 'PGR', 'display_name': 'Offline PGR', 'order_index': 0, 'enabled': True}],
            source_info={'fixture': 'pgr-store-followup'}, policy_projection={})
        definition = next(item for item in catalog() if item['game_id'] == 'PGR' and item['cadence'] == 'daily')
        self.store.sync_todo_definitions([definition])
        period = todo_period(definition['reset_rule'])
        self.store.reconcile_todo_instances([{'todo_instance_id': 'offline-todo',
            'todo_definition_id': definition['todo_definition_id'], 'game_id': 'PGR', 'cadence': 'daily',
            'period_key': period['periodKey'], 'period_starts_at': period['startsAt'],
            'period_ends_at': period['endsAt'], 'definition_snapshot': definition, 'status': 'pending'}],
            intent='reconcile', requested_by='fixture', reason='offline-store-test')
        self.batch = self.new_batch()
        self.owner = self.new_run()
        self.store.add_batch_run_membership(self.batch['batch_id'], self.owner['run_id'], ordinal=0, role='initial')
        self.sibling = self.new_run()
        self.store.add_batch_run_membership(self.batch['batch_id'], self.sibling['run_id'], ordinal=1, role='initial')

    def new_batch(self):
        return self.store.create_batch({'cadence': 'daily', 'mode': 'execute', 'state': 'running',
            'game_ids': ['PGR'], 'requested_by': 'fixture', 'result': {}})

    def new_run(self, **changes):
        todo_ids = [] if changes.get('cadence', 'daily') != 'daily' else ['offline-todo']
        return self.store.create_game_run({'game_id': 'PGR', 'cadence': 'daily', 'mode': 'execute',
            'state': 'pending_execution', 'requested_by': 'fixture',
            'todo_instance_ids': todo_ids, 'completion_todo_instance_ids': todo_ids, **changes})

    def attempt(self, run=None):
        run = run or self.sibling
        return self.store.create_run_attempt({'run_attempt_id': str(uuid4()), 'run_id': run['run_id'],
            'game_id': 'PGR', 'cadence': 'daily', 'fencing_token_hash': 'a' * 64,
            'cancel_authority_hash': 'sha256:' + 'b' * 64,
            'plan': {'executableTodoInstanceIds': ['offline-todo'], 'todos': [{'todoInstanceId': 'offline-todo'}]}})

    def summary(self, **changes):
        return self.store.active_execution_summary(owned_batch_id=self.batch['batch_id'],
            current_run_id=self.owner['run_id'], **changes)

    def assert_sibling_blocks(self):
        self.assertIn(self.sibling['run_id'], [row['runId'] for row in self.summary()['gameRuns']])

    def test_fresh_pending_and_queued_siblings_are_excluded_read_only(self):
        for state in ('pending_execution', 'queued'):
            with self.subTest(state=state):
                self.store.update_game_run(self.sibling['run_id'], state=state)
                before = self.store.latest_event_sequence(), self.store.connection.total_changes
                summary = self.summary()
                self.assertNotIn(self.sibling['run_id'], [row['runId'] for row in summary['gameRuns']])
                self.assertIn(self.owner['run_id'], [row['runId'] for row in summary['gameRuns']])
                self.assertIn(self.batch['batch_id'], [row['batchId'] for row in summary['batches']])
                self.assertEqual(before, (self.store.latest_event_sequence(), self.store.connection.total_changes))
                self.assertEqual(self.store.get_game_run(self.sibling['run_id'])['state'], state)

    def test_without_context_and_incomplete_context_still_block(self):
        for context in ({}, {'owned_batch_id': self.batch['batch_id']}, {'current_run_id': self.owner['run_id']},
                        {'owned_batch_id': self.batch['batch_id'], 'current_run_id': 'not-member'}):
            with self.subTest(context=context):
                self.assertIn(self.sibling['run_id'], [row['runId'] for row in self.store.active_execution_summary(**context)['gameRuns']])

    def test_orphan_fresh_pending_is_not_a_sibling(self):
        orphan = self.new_run()
        self.assertIn(orphan['run_id'], [row['runId'] for row in self.summary()['gameRuns']])

    def test_running_and_cancelling_are_not_fresh(self):
        for state in ('running', 'cancelling'):
            with self.subTest(state=state):
                self.store.update_game_run(self.sibling['run_id'], state=state)
                self.assert_sibling_blocks()

    def test_nonqueued_membership_blocks(self):
        self.store.update_batch_run_membership(self.batch['batch_id'], self.sibling['run_id'], state='active')
        self.assert_sibling_blocks()

    def test_terminal_attempt_is_not_fresh(self):
        attempt = self.attempt()
        self.store.update_run_attempt(attempt['run_attempt_id'], state='failed', completed=True)
        self.assert_sibling_blocks()

    def test_starting_attempt_blocks(self):
        self.attempt()
        self.assert_sibling_blocks()

    def test_queued_membership_with_terminal_outcome_is_not_fresh(self):
        self.store.update_batch_run_membership(self.batch['batch_id'], self.sibling['run_id'],
            state='queued', terminal_outcome='failed')
        self.assert_sibling_blocks()

    def test_another_live_membership_blocks_sibling(self):
        other = self.new_batch()
        self.store.add_batch_run_membership(other['batch_id'], self.sibling['run_id'], ordinal=0, role='initial')
        self.assert_sibling_blocks()

    def test_ambiguous_current_run_does_not_authorize_exemption(self):
        other = self.new_batch()
        self.store.add_batch_run_membership(other['batch_id'], self.owner['run_id'], ordinal=0, role='initial')
        self.assert_sibling_blocks()

    def test_cancelled_batch_with_nonterminal_prior_member_still_blocks(self):
        other = self.new_batch()
        self.store.add_batch_run_membership(other['batch_id'], self.owner['run_id'], ordinal=0, role='initial')
        self.store.update_batch(other['batch_id'], state='cancelled', result={})
        self.assert_sibling_blocks()

    def test_formally_retired_prior_membership_preserves_unique_owner(self):
        other = self.new_batch()
        self.store.add_batch_run_membership(other['batch_id'], self.owner['run_id'], ordinal=0, role='initial')
        request, _ = self.store.create_or_get_batch_cancel_request({'batch_id': other['batch_id'],
            'reason': 'offline-retirement', 'requested_by': 'fixture'})
        self.store.update_batch_run_membership(other['batch_id'], self.owner['run_id'], state='cancelled',
            terminal_outcome='cancelled')
        self.store.update_batch_cancel_request(request['cancel_request_id'], state='sealed', active_run_attempt_ids=[])
        sealed = self.store.seal_batch(other['batch_id'], state='cancelled',
            result={'notificationOutcome': 'blocked', 'cancelRequestId': request['cancel_request_id']})
        self.assertIsInstance(sealed['result']['sealVersion'], int)
        self.assertEqual(self.store.get_batch_cancel_request(request['cancel_request_id'])['state'], 'sealed')
        self.assertNotIn(self.sibling['run_id'], [row['runId'] for row in self.summary()['gameRuns']])

    def test_completed_current_member_can_check_fresh_siblings_after_step(self):
        attempt = self.attempt(self.owner)
        self.store.update_run_attempt(attempt['run_attempt_id'], state='completed', completed=True, exit_code=0)
        self.store.update_batch_run_membership(self.batch['batch_id'], self.owner['run_id'], state='terminal',
            latest_run_attempt_id=attempt['run_attempt_id'], terminal_outcome='completed')
        self.assertNotIn(self.sibling['run_id'], [row['runId'] for row in self.summary()['gameRuns']])

    def test_terminal_current_member_without_exact_completed_attempt_cannot_exempt(self):
        self.store.update_batch_run_membership(self.batch['batch_id'], self.owner['run_id'], state='terminal',
            terminal_outcome='completed')
        self.assert_sibling_blocks()

    def test_sealed_batch_does_not_authorize_exemption(self):
        self.store.seal_batch(self.batch['batch_id'], state='blocked', result={'notificationOutcome': 'blocked'})
        self.assert_sibling_blocks()

    def test_active_cancel_request_does_not_authorize_exemption(self):
        self.store.create_or_get_batch_cancel_request({'batch_id': self.batch['batch_id'],
            'reason': 'operator-cancel', 'requested_by': 'fixture'})
        self.assert_sibling_blocks()

    def test_cross_cadence_or_plan_run_does_not_get_exemption(self):
        for changes in ({'cadence': 'weekly'}, {'mode': 'plan'}):
            with self.subTest(changes=changes):
                foreign = self.new_run(**changes)
                members = self.store.list_batch_run_memberships(batch_id=self.batch['batch_id'])
                self.store.add_batch_run_membership(self.batch['batch_id'], foreign['run_id'], ordinal=len(members), role='initial')
                self.assertIn(foreign['run_id'], [row['runId'] for row in self.summary()['gameRuns']])
