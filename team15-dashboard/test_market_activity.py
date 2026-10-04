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


if __name__ == "__main__":
    unittest.main()
