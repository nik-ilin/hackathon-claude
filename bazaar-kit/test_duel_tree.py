"""Comprueba que el árbol usa la política y la evidencia vigente."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duel_tree
import duel_runner
import duels


HERE = Path(__file__).resolve().parent


class DecisionTreeTests(unittest.TestCase):
    def test_practice_trajectory_waits_when_rival_can_improve(self):
        duels_done = json.loads((HERE / "duels_fixture_practice.json").read_text())
        duel = next(d for d in duels_done if d["duel"] == 7)
        first = duel["messages"][0]
        live = dict(duel, status="live", messages=[first],
                    rival_offer={"price": first["price"], "days": first.get("days")})
        result = duel_tree.plan([live], first["tick"])
        self.assertEqual(result[0]["action"], "wait")
        self.assertIn("wait_for_better_offer", result[0]["path"])
        self.assertEqual(result[0]["facts"]["own_limit"], 120)
        self.assertEqual(result[0]["facts"]["rival_price"], 78)
        self.assertEqual(result[0]["facts"]["margin"], 42)
        self.assertEqual(result[0]["facts"]["ticks_left"], 10)

    def test_explains_acceptance_trigger_with_real_practice_duel(self):
        duels_done = json.loads((HERE / "duels_fixture_practice.json").read_text())
        duel = next(d for d in duels_done if d["duel"] == 8)
        first = duel["messages"][0]
        live = dict(duel, status="live", messages=[first],
                    rival_offer={"price": first["price"]})
        result = duel_tree.plan([live], first["tick"])[0]
        self.assertEqual(result["action"], "accept")
        self.assertEqual(result["trigger"], "large_margin")
        self.assertEqual(result["facts"]["margin"], 34)

    def test_one_acceptance_per_tick_is_explicit(self):
        candidates = [dict(type="duel_accept", duel=i, score=10, du=5, why="ok")
                      for i in (1, 2)]
        with patch.object(duels, "duel_candidates", return_value=candidates):
            result = duel_tree.plan([{"duel": 1}, {"duel": 2}], 10)
        self.assertEqual([step["action"] for step in result], ["accept", "defer"])

    def test_restart_uses_sent_acceptance_memory_for_same_tick(self):
        candidates = [dict(type="duel_accept", duel=2, score=10, du=5, why="ok")]
        actions = [{"tick": 10, "type": "duel_accept", "duel": 1, "sent": True}]
        with patch.object(duels, "duel_candidates", return_value=candidates):
            result = duel_tree.plan([{"duel": 2}], 10, actions=actions)
            next_tick = duel_tree.plan([{"duel": 2}], 11, actions=actions)
        self.assertEqual(result[0]["action"], "defer")
        self.assertEqual(next_tick[0]["action"], "accept")

    def test_restart_does_not_repeat_sent_offer(self):
        candidate = dict(type="duel_say", duel=2, score=10, price=50, why="ok")
        actions = [{"tick": 10, "type": "duel_say", "duel": 2, "sent": True}]
        with patch.object(duels, "duel_candidates", return_value=[candidate]):
            result = duel_tree.plan([{"duel": 2}], 10, actions=actions)
        self.assertEqual(result[0]["action"], "already_sent")

    def test_dry_run_log_does_not_consume_budget(self):
        candidate = dict(type="duel_accept", duel=2, score=10, du=5, why="ok")
        actions = [{"tick": 10, "type": "duel_accept", "duel": 1, "sent": False}]
        with patch.object(duels, "duel_candidates", return_value=[candidate]):
            result = duel_tree.plan([{"duel": 2}], 10, actions=actions)
        self.assertEqual(result[0]["action"], "accept")

    def test_days_case_stays_unverified(self):
        duel = {"duel": 3, "status": "live", "deadline_tick": 20,
                "issues": ["price", "days"]}
        result = duel_tree.plan([duel], 10)
        self.assertEqual(result[0]["action"], "wait")
        self.assertEqual(result[0]["reason"], "days_utility_unverified")

    def test_feed_counts_are_context_not_rival_value(self):
        events = [{"id": 1, "type": "duel.closed", "payload":
                   {"session": 1, "status": "no_deal"}}] * 2
        context = duel_tree.feed_evidence(events)
        self.assertEqual(context["closed"], 1)
        self.assertEqual(context["confidence"], "small_sample")
        self.assertNotIn("rival_limit", context)

    def test_partial_feed_line_does_not_stop_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.jsonl"
            path.write_text('{"id":1,"type":"duel.closed","payload":{"status":"no_deal"}}\n{"id":')
            events = duel_runner.load_feed_events(path)
        self.assertEqual(duel_tree.feed_evidence(events)["closed"], 1)


if __name__ == "__main__":
    unittest.main()
