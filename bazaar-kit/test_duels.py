"""Pruebas offline de duels.py con los 24 duelos reales de la sesión de práctica del viernes.

    python3 -m unittest test_duels
"""
import json
import os
import unittest

import duels as dl

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "duels_fixture_practice.json")))


class TestDuels(unittest.TestCase):
    def test_replay_captures_most_of_the_margin(self):
        r = dl.replay(FIXTURE)
        self.assertEqual(r["outside_limit"], 0)
        self.assertGreaterEqual(r["captured"] / r["best"], 0.85, r)

    def test_beats_accepting_early(self):
        default = dl.replay(FIXTURE)["captured"]
        early = dl.replay(FIXTURE, {"GOOD_SHARE": 0.3})["captured"]
        self.assertGreater(default, early)

    def test_margin_signs(self):
        buyer = {"role": "buyer", "your_limit": 100}
        seller = {"role": "seller", "your_limit": 100}
        self.assertEqual(dl.margin(buyer, 80), 20)
        self.assertEqual(dl.margin(seller, 120), 20)
        self.assertLess(dl.margin(buyer, 120), 0)
        self.assertLess(dl.margin(seller, 80), 0)

    def test_days_never_excuse_a_price_outside_the_limit(self):
        d = {"role": "buyer", "your_limit": 100, "your_days_weight": [50] * 11}
        self.assertLess(dl.margin(d, 120, 5), 0)

    def test_never_accepts_outside_limit(self):
        d = {"duel": 1, "status": "live", "role": "buyer", "your_limit": 100, "rival": "R", "deadline_tick": 10,
             "rival_offer": {"price": 130}, "messages": [{"tick": 9, "from": "R", "price": 130}], "your_offer": None}
        for t in range(0, 10):
            self.assertFalse([c for c in dl.duel_candidates([d], t) if c["type"] == "duel_accept"])

    def test_silent_rival_gets_one_anchor(self):
        d = {"duel": 2, "status": "live", "role": "seller", "your_limit": 100, "rival": "R", "deadline_tick": 20,
             "rival_offer": None, "messages": [], "your_offer": None, "issues": ["price"]}
        self.assertFalse(dl.duel_candidates([d], 10))
        c = dl.duel_candidates([d], 16)
        self.assertEqual([x["type"] for x in c], ["duel_say"])
        self.assertEqual(c[0]["price"], 130)

    def test_wave_is_staggered(self):
        wave = [{"duel": i, "status": "live", "role": "buyer", "your_limit": 100, "rival": "R", "deadline_tick": 50,
                 "rival_offer": {"price": 90}, "your_offer": None,
                 "messages": [{"tick": 40 + k, "from": "R", "price": 99 - k} for k in range(5)]} for i in range(3)]
        # tres duelos con el mismo deadline: empiezan a aceptarse 3 ticks antes (uno por tick)
        self.assertTrue(any(c["type"] == "duel_accept" for c in dl.duel_candidates(wave, 47)))
        self.assertFalse(any(c["type"] == "duel_accept" for c in dl.duel_candidates(wave, 45)))


if __name__ == "__main__":
    unittest.main()
