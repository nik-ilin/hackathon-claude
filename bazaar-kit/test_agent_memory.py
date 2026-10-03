"""Offline regressions for persistent, causal evidence and coordinator adapter."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_memory as am
from test_feed_oracle import dealer_msg


def snap(tick=20, events=()):
    return {'clock': {'tick': tick, 'round': 2}, 'me': {'id': 't15', 'cash': 200,
            'collection_value': 100, 'starter_broker_key': 'secret', 'score': {}, 'album': {}},
            'feed': {'events': list(events)}, 'catalog': {'sets': []},
            'board': {'offers': [{'id': 99}]}, 'venues': {'venues': []}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'memory.db'
        self.m = am.Memory(self.path)

    def tearDown(self):
        self.m.close()
        self.tmp.cleanup()

    def test_restart_deduplicates_and_recovers_historical_feed(self):
        e = dealer_msg(1, 10, 'abuela', 't15', 'LAV-01', 8)
        self.assertEqual(self.m.capture(snap(events=[e, e])), 1)
        self.m.close()
        self.m = am.Memory(self.path)
        self.assertEqual(self.m.capture(snap(events=[e])), 0)
        self.assertEqual(self.m.enrich(snap())['feed']['events'], [e])

    def test_incremental_import_keeps_partial_and_future_events(self):
        path = Path(self.tmp.name) / 'feed_history.jsonl'
        first = dealer_msg(1, 10, 'abuela', 't15', 'LAV-01', 8)
        second = dealer_msg(2, 21, 'abuela', 't15', 'LAV-01', 7)
        raw = json.dumps(second).encode()
        path.write_bytes(json.dumps(first).encode() + b'\n' + raw[:20])
        self.assertEqual(self.m.import_history(path, 20), 1)
        with path.open('ab') as f:
            f.write(raw[20:] + b'\n')
        self.assertEqual(self.m.import_history(path, 20), 0)
        self.assertEqual(self.m.import_history(path, 21), 1)
        self.assertEqual(self.m.import_history(path, 21), 0)
        self.assertEqual(len(self.m.events(21)), 2)

    def test_window_and_future_excluded(self):
        evs = [dealer_msg(i, i, 'abuela', 't15', 'LAV-01', 8) for i in (1, 10, 21)]
        self.m.capture(snap(events=evs))
        self.assertEqual([e['id'] for e in self.m.events(20, 12)], [10])
        self.assertEqual([e['id'] for e in self.m.events(9, 12)], [1])

    def test_no_credentials_or_unbounded_action_payload(self):
        e = {'id': 1, 'tick': 10, 'type': 'test', 'payload': {'broker_key': 'secret', 'value': 2}}
        a = {'key': 'a', 'tick': 10, 'type': 'list', 'status': 'submitted', 'broker_key': 'secret',
             'response': {'starter_broker_key': 'secret'}}
        self.m.capture(snap(events=[e]), [a])
        dump = '\n'.join(self.m.db.iterdump())
        self.assertNotIn('secret', dump)
        self.assertNotIn('broker_key', dump)

    def test_quotes_not_reported_as_settled(self):
        e = dealer_msg(1, 10, 'abuela', 't15', 'LAV-01', 8, final=True)
        s = snap(events=[e])
        self.m.capture(s)
        row = self.m.report(s)['dealer_evidence'][0]
        self.assertEqual(row['settled_count'], 0)
        self.assertIsNone(row['best_settled_observed'])
        self.assertEqual(row['final_quotes'], [8])

    def test_action_transition_replaces_snapshot_not_new_sample(self):
        a = {'key': 'a', 'tick': 10, 'type': 'list', 'status': 'submitted'}
        self.m.capture(snap(), [a])
        self.m.capture(snap(), [{**a, 'status': 'settled', 'settled_tick': 19}])
        self.m.capture(snap(), [a])  # older snapshot cannot undo a confirmed settlement
        self.assertEqual(self.m.report(snap())['recorded_action_states'], {'settled': 1})
        self.assertEqual(self.m.db.execute('SELECT count(*) FROM action_history').fetchone()[0], 2)

    def test_enrich_preserves_authoritative_live_state(self):
        self.m.capture(snap(events=[dealer_msg(1, 10, 'abuela', 't15', 'LAV-01', 8)]))
        s = snap()
        enriched = self.m.enrich(s)
        self.assertIs(enriched['me'], s['me'])
        self.assertIs(enriched['board'], s['board'])
        self.assertEqual(s['feed']['events'], [])

    def test_adapter_delivers_history_and_restores_original_functions(self):
        import memory_coordinator as mc
        self.m.capture(snap(events=[dealer_msg(1, 10, 'abuela', 't15', 'LAV-01', 8)]))
        saved = mc.coordinator.candidates
        captured = []
        def plan(s, *_):
            captured.extend(s['feed']['events'])
            return [], {}, 0
        with patch.object(mc.coordinator, 'candidates', plan), patch.object(mc.coordinator.ma, 'save'), \
             patch.object(mc.coordinator.ma, 'load_json', return_value={}):
            with mc.integrated(self.path):
                mc.coordinator.candidates(snap(), {'actions': []}, SimpleNamespace(history_window=240), None)
            self.assertIs(mc.coordinator.candidates, plan)
        self.assertIs(mc.coordinator.candidates, saved)
        self.assertEqual(len(captured), 1)

    def test_memory_failure_falls_back_to_live_without_changing_guards(self):
        import memory_coordinator as mc
        s = snap()
        with patch.object(mc.coordinator, 'candidates', return_value=([], {}, 0)) as original, \
             patch.object(am, 'prepare', side_effect=OSError('unavailable')):
            with mc.integrated(self.path):
                mc.coordinator.candidates(s, {'actions': []}, SimpleNamespace(), None)
        self.assertIs(original.call_args.args[0], s)
