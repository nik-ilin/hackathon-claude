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

    def test_one_acceptance_per_tick_is_explicit(self):
        candidates = [dict(type="duel_accept", duel=i, score=10, du=5, why="ok")
                      for i in (1, 2)]
        with patch.object(duels, "duel_candidates", return_value=candidates):
            result = duel_tree.plan([{"duel": 1}, {"duel": 2}], 10)
        self.assertEqual([step["action"] for step in result], ["accept", "defer"])

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
