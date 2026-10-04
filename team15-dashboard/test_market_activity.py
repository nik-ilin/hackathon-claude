import unittest

import market_activity


class MarketActivityTests(unittest.TestCase):
    def test_separates_our_posts_from_incoming_offers(self):
        venues = [{"venue": "v15", "owner": "t15", "name": "Puesto de Team 15", "fee_bps": 0}]
        own = [
            {"id": 1, "venue": "v05", "maker": "t15", "status": "open",
             "give": {"cards": ["CHA-09"]}, "want": {"cash": 65}},
            {"id": 2, "venue": "v15", "maker": "bank", "to": "t15", "status": "open",
             "give": {"cash": 114}, "want": {"cards": ["RET-11"]}},
        ]
        result = market_activity.build(venues, {}, own, tick=100)

        self.assertEqual([row["id"] for row in result["my_open_offers"]], [1])
        self.assertEqual([row["id"] for row in result["incoming_offers"]], [2])
        self.assertEqual([row["id"] for row in result["my_active_sales"]], [1])
        self.assertEqual(result["summary"]["my_open_offers"], 1)
        self.assertEqual(result["summary"]["incoming_offers"], 1)
        self.assertEqual(result["own_market_offers"][0]["id"], 2)

    def test_group_copy_contains_only_public_offers_from_our_market(self):
        venue = [{"venue": "v15", "owner": "t15", "name": "El Duende"}]
        board = {"v15": [
            {"id": "public", "venue": "v15", "maker": "t03", "status": "open",
             "give": {"cards": ["CHA-06"]}, "want": {"cash": 20}, "expires_tick": 120},
            {"id": "directed", "venue": "v15", "maker": "t04", "to": "t02", "status": "open",
             "give": {"cards": ["RET-01"]}, "want": {"cash": 10}},
        ]}
        own = [{"id": "private", "venue": "v05", "maker": "t15", "to": "t15", "status": "open",
                "give": {"cash": 999}, "want": {"cards": ["SECRET-CARD"]}}]
        activity = market_activity.build(venue, board, own, tick=100, clock={"tick": 100},
                                         captured_at=1_800_000_000)
        message = market_activity.group_share_message(activity)
        self.assertIn("CHA-06", message)
        self.assertIn("20 ticks left", message)
        self.assertNotIn("RET-01", message)
        self.assertNotIn("SECRET-CARD", message)
        self.assertNotIn("999", message)
        self.assertIn("card-for-card swaps welcome", message)


if __name__ == "__main__":
    unittest.main()
