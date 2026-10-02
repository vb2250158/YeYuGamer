from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yeyu_gamer_manager.store.sqlite_store import SqliteStore


class HumanGuardHistoryTests(unittest.TestCase):
    def test_filter_history_before_expanding_batches_and_preserve_all_gates(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        store = SqliteStore(Path(directory) / 'test.db')
        store.initialize()
        self.addCleanup(store.close)
        store.import_legacy(config_values={}, games=[{
            'game_id': 'WW', 'display_name': 'WW', 'order_index': 1,
            'enabled': True, 'policy': {},
        }], source_info={'test': True}, policy_projection={})
        def batch(state, result):
            return store.create_batch(dict(cadence='daily', mode='execute',
                state=state, game_ids=['WW'], requested_by='test', result=result))
        open_gate = batch('human_required', {})
        running = batch('running', {})
        # Historical rows must not invoke the relational/JSON mapper.
        for _ in range(12):
            batch('failed', {'sealVersion': 1, 'diagnostic': 'x' * 10000})
            batch('cancelled', {})
        with patch.object(store, '_batch', wraps=store._batch) as decode:
            candidates = store.list_unsealed_batches()
            self.assertEqual(decode.call_count, 2)
        self.assertEqual({row['batch_id'] for row in candidates},
                         {open_gate['batch_id'], running['batch_id']})
        gate = store.create_game_run(dict(game_id='WW', cadence='daily',
            state='human_required', mode='execute', requested_by='test',
            todo_instance_ids=[]))
        for _ in range(12):
            store.create_game_run(dict(game_id='WW', cadence='daily',
                state='completed', mode='execute', requested_by='test',
                todo_instance_ids=[]))
        with patch.object(store, '_game_run', wraps=store._game_run) as decode:
            gates = store.list_human_game_runs()
            self.assertEqual(decode.call_count, 1)
        self.assertEqual([row['run_id'] for row in gates], [gate['run_id']])

if __name__ == '__main__':
    unittest.main()
