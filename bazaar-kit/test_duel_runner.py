"""Offline integration checks: shared ownership, no writes in analysis, acceptance budget."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import duel_runner as runner
import duels
from bazaar_sdk import BazaarError
from negotiation import InstanceLock


class DuelSafety(unittest.TestCase):
    def test_day3_preset_enables_verified_two_issue_policy_without_writes_in_analysis(self):
        saved = dict(duels.PARAMS)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(runner, 'DATA', Path(tmp)), patch.object(runner, 'log'), \
                     patch.object(runner, 'run') as run, patch.dict(os.environ, {'BAZAAR_KEY': 'test-key'}), \
                     patch('sys.argv', ['duel_runner.py', '--day3']):
                    runner.main()
                    self.assertTrue(duels.PARAMS['PLAY_DAYS'])
                    self.assertTrue(duels.PARAMS['PROFILES'])
                    self.assertTrue(duels.PARAMS['LADDER'])
                    self.assertFalse(duels.PARAMS['LOGROLL'])
                    self.assertEqual(run.call_args.args[1], False)
                    self.assertEqual(run.call_args.kwargs['reconcile'], True)
                    self.assertEqual(run.call_args.kwargs['verify_accept'], True)
        finally:
            duels.PARAMS.clear()
            duels.PARAMS.update(saved)

    def test_expired_and_days_duels_are_skipped(self):
        d = dict(duel=1, status='live', role='buyer', your_limit=100,
                 deadline_tick=10, rival_offer={'price': 1}, messages=[])
        self.assertEqual(duels.duel_candidates([d], 10), [])
        self.assertEqual(duels.duel_candidates([dict(d, issues=['price', 'days'])], 9), [])

    def test_shared_lock_rejects_existing_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = InstanceLock(str(Path(tmp) / 'agent.lock'), {'version': 'coordinator'})
            self.assertIsNone(owner.acquire())
            try:
                with patch.object(runner, 'DATA', Path(tmp)), patch.object(runner, 'log'), \
                     patch.object(runner, 'run') as run, \
                     patch.dict(os.environ, {'BAZAAR_KEY': 'test-key'}), \
                     patch('sys.argv', ['duel_runner.py', '--execute']):
                    with self.assertRaises(SystemExit):
                        runner.main()
                    run.assert_not_called()
                self.assertTrue((Path(tmp) / 'agent.lock').exists())
            finally:
                owner.release()

    def test_lock_released_on_runner_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, 'DATA', Path(tmp)), patch.object(runner, 'log'), \
                 patch.object(runner, 'run', side_effect=RuntimeError('stop')), \
                 patch.dict(os.environ, {'BAZAAR_KEY': 'test-key'}), \
                 patch('sys.argv', ['duel_runner.py', '--execute']):
                with self.assertRaises(RuntimeError):
                    runner.main()
            self.assertFalse((Path(tmp) / 'agent.lock').exists())

    def exercise_tick(self, execute, error=None):
        api = Mock()
        api.clock.side_effect = [{'tick': 9}, KeyboardInterrupt()]
        api.duels.return_value = {'duels': [{'status': 'live'}]}
        api.duel_accept.side_effect = error
        candidates = [dict(type='duel_accept', duel=i, score=10, du=5, why='test') for i in (1, 2)]
        with patch.object(runner.dl, 'duel_candidates', return_value=candidates), \
             patch.object(runner, 'log'), patch.object(runner, 'retune') as retune:
            runner.run(api, execute)
            retune.assert_not_called()
        return api

    def test_analysis_sends_nothing(self):
        api = self.exercise_tick(False)
        api.duel_accept.assert_not_called()
        api.duel_say.assert_not_called()

    def test_one_acceptance_per_tick(self):
        self.assertEqual(self.exercise_tick(True).duel_accept.call_count, 1)

    def test_used_tick_is_not_retried_for_second_duel(self):
        api = self.exercise_tick(True, BazaarError('wait_for_tick'))
        self.assertEqual(api.duel_accept.call_count, 1)

    def test_ambiguous_write_stops(self):
        with self.assertRaises(SystemExit):
            self.exercise_tick(True, BazaarError('network'))
