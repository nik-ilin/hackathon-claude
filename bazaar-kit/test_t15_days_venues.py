"""Pruebas offline: duelos con días (opt-in --days), cuerpo de duel_say con días y bloqueo de venues rivales.

    python3 -m unittest test_t15_days_venues
"""
import unittest
from unittest import mock

import bazaar_sdk
import coordinator as co
import duel_tree
import duels as dl


def days_duel(price=80, weight=None, left=5):
    return {"duel": 7, "status": "live", "role": "buyer", "your_limit": 100, "rival": "R", "deadline_tick": 10 + left,
            "issues": ["price", "days"], "your_days_weight": weight,
            "rival_offer": {"price": price, "days": 3},
            "messages": [{"tick": 9, "from": "R", "price": price}], "your_offer": None}


class TestDays(unittest.TestCase):
    def tearDown(self):
        dl.PARAMS["PLAY_DAYS"] = False

    def test_default_skips_days_duels(self):
        self.assertEqual(dl.duel_candidates([days_duel(price=30, left=1)], 10), [])

    def test_play_days_accepts_inside_limit(self):
        dl.PARAMS["PLAY_DAYS"] = True
        c = dl.duel_candidates([days_duel(price=30, left=1)], 10)
        self.assertEqual([x["type"] for x in c], ["duel_accept"])

    def test_play_days_never_accepts_outside_limit(self):
        dl.PARAMS["PLAY_DAYS"] = True
        c = dl.duel_candidates([days_duel(price=130, left=1)], 10)
        self.assertNotIn("duel_accept", [x["type"] for x in c])

    def test_own_offer_carries_days(self):
        dl.PARAMS["PLAY_DAYS"] = True
        d = days_duel(price=130, weight=[0, 0, 5, 0, 0, 0, 0, 0, 0, 0, 0], left=2)
        says = [x for x in dl.duel_candidates([d], 10) if x["type"] == "duel_say"]
        self.assertEqual(says[0]["days"], 2)

    def test_tree_no_longer_waits_on_days_when_enabled(self):
        dl.PARAMS["PLAY_DAYS"] = True
        reason, _ = duel_tree._waiting_reason(days_duel(price=30), 10)
        self.assertNotEqual(reason, "days_utility_unverified")

    def test_duel_say_sends_days_next_to_price(self):
        b = bazaar_sdk.Bazaar.__new__(bazaar_sdk.Bazaar)
        with mock.patch.object(bazaar_sdk.Bazaar, "_call", return_value={}) as call:
            b.duel_say(7, text="hola", price=60, days=3)
        body = call.call_args[0][2]
        self.assertEqual((body["price"], body["days"]), (60, 3))

    def test_duel_say_price_only_unchanged(self):
        b = bazaar_sdk.Bazaar.__new__(bazaar_sdk.Bazaar)
        with mock.patch.object(bazaar_sdk.Bazaar, "_call", return_value={}) as call:
            b.duel_say(7, text="hola", price=60)
        self.assertEqual(call.call_args[0][2], {"text": "hola", "price": 60})


class TestRivalVenues(unittest.TestCase):
    S = {"me": {"id": "t15"}, "venues": {"venues": [
        {"venue": "rastro", "owner": "world"}, {"venue": "v14", "owner": "t14"}, {"venue": "v15", "owner": "t15"}]}}

    def test_blocks_posts_and_accepts_on_rival_venue(self):
        for t in ("bid", "list", "swap_list", "accept"):
            self.assertIn("t14", co.rival_venue_blocker({"type": t, "venue": "v14"}, self.S))

    def test_allows_world_own_and_cancels(self):
        self.assertIsNone(co.rival_venue_blocker({"type": "bid", "venue": "rastro"}, self.S))
        self.assertIsNone(co.rival_venue_blocker({"type": "bid"}, self.S))
        self.assertIsNone(co.rival_venue_blocker({"type": "list", "venue": "v15"}, self.S))
        self.assertIsNone(co.rival_venue_blocker({"type": "cancel", "venue": "v14"}, self.S))


if __name__ == "__main__":
    unittest.main()
