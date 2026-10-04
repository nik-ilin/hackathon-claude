import json
import os
import tempfile
import unittest
from pathlib import Path

import strategy_health


class StrategyHealthTest(unittest.TestCase):
    def test_practice_excluded_and_negative_deals_counted(self):
        rows = [dict(session=1, status='no_deal'),
                dict(session=2, status='deal', result=15),
                dict(session=2, status='deal', result=-4),
                dict(session=2, status='no_deal')]
        with tempfile.TemporaryDirectory() as tmp:
            report = strategy_health.build(rows, [tmp], 100)
        self.assertEqual(report['duels']['sessions'][0]['total'], 3)
        self.assertEqual(report['duels']['negative'], 1)
        self.assertEqual(report['duels']['no_deal'], 1)

    def test_memory_requires_recent_report_not_just_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            data.mkdir()
            (data / 'agent_memory.sqlite3').touch()
            self.assertEqual(strategy_health.build([], [tmp], 100)['memory']['status'], 'database_only')
            (data / 'agent_memory_report.json').write_text(json.dumps({'tick': 60, 'stored_events': 12}))
            self.assertEqual(strategy_health.build([], [tmp], 100)['memory']['status'], 'fresh')
            self.assertEqual(strategy_health.build([], [tmp], 101)['memory']['status'], 'stale')

    def test_saved_memory_and_model_do_not_mean_an_agent_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            data.mkdir()
            (data / 'agent_memory_report.json').write_text(json.dumps({'tick': 100, 'stored_events': 8}))
            (data / 'duel_learning.json').write_text(json.dumps({'facts': {'duels_done': 12}}))
            report = strategy_health.build([], [tmp], 100)
            self.assertEqual(report['execution']['status'], 'stopped')
            self.assertEqual(report['duel_learning']['status'], 'saved_report')
            (data / 'agent.lock').write_text(json.dumps({'pid': os.getpid(), 'version': 'duels-1.1'}))
            report = strategy_health.build([], [tmp], 100)
            self.assertEqual(report['execution']['mode'], 'duels')
            (data / 'agent.lock').write_text(json.dumps({'pid': 999999999, 'version': 'duels-1.1'}))
            self.assertEqual(strategy_health.build([], [tmp], 100)['execution']['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
