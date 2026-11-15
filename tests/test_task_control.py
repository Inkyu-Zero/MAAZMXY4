"""Bound only remote polling; cancellation at an input boundary stays fresh."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from task_control import StopQuery, stopping


class StopQueryTests(unittest.TestCase):
    def test_busy_wait_polls_at_most_once_per_interval(self):
        reads = []
        now = [0.0]
        query = StopQuery(lambda: reads.append(now[0]) or False, clock=lambda: now[0])
        for tick in range(1000):
            now[0] = tick / 10000
            self.assertFalse(query())
        self.assertEqual(reads, [0.0, .05])

    def test_input_refresh_sees_cancel_inside_cached_interval_and_latches_it(self):
        state = [False]
        query = StopQuery(lambda: state[0], clock=lambda: 0)
        self.assertFalse(query())
        state[0] = True
        self.assertFalse(query())
        self.assertTrue(query.refresh())
        state[0] = False
        self.assertTrue(query.refresh())

    def test_rpc_error_is_not_interpreted_as_permission_to_continue(self):
        def broken():
            raise RuntimeError('channel closed')
        query = StopQuery(broken)
        with self.assertRaisesRegex(RuntimeError, 'channel closed'):
            query()

    def test_contexts_do_not_share_latched_cancellation(self):
        tasker = SimpleNamespace(stopping=True)
        self.assertTrue(stopping(SimpleNamespace(tasker=tasker)))
        tasker.stopping = False
        self.assertFalse(stopping(SimpleNamespace(tasker=tasker)))


if __name__ == '__main__':
    unittest.main()
