import unittest

import strategy_v3


class StrategyV3Tests(unittest.TestCase):
    def test_stable_sections_keep_snapshot_sources_and_read_only_marker(self):
        data = {"tick": 25, "built_at": 1234, "verified": False,
                "feed_health": {"status": "fresh", "last_tick": 25},
                "market_activity": {"own_venue": "v15", "own_venue_name": "Puesto Team 15",
                                    "my_open_offers": [{"id": 1}], "incoming_offers": [{"id": 2}],
                                    "own_market_offers": [{"id": 3, "to": "t04"}, {"id": 4}], "summary": {"my_open_offers": 1}},
                "clock": {"tick": 25, "tick_seconds": 15},
                "tick_time_context": {"tick": 25, "captured_at": 1234, "samples": []},
                "trades": [], "history": []}
        v2 = {"operations": {}, "scoring": {}, "ranking": {"position": 4},
              "strategy_health": {}, "market_activity": data["market_activity"],
              "cards": [{"ref": "LAT-01"}], "definitions": {"safe": "meaning"}}
        result = strategy_v3.export(data, lambda _data: v2)
        self.assertEqual(result["schema"], "team15.dashboard.v3")
        self.assertTrue(result["snapshot"]["read_only"])
        self.assertEqual(result["sections"]["overview"]["my_offers"], [{"id": 1}])
        self.assertEqual(result["sections"]["overview"]["incoming_offers"], [{"id": 2}])
        self.assertEqual(result["sections"]["overview"]["public_offers"], [{"id": 3, "to": "t04"}, {"id": 4}])
        self.assertEqual(result["sections"]["overview"]["cards"], [{"ref": "LAT-01"}])
        self.assertEqual(result["sections"]["operations"]["my_offers"], [{"id": 1}])
        self.assertEqual(result["sections"]["operations"]["incoming_offers"], [{"id": 2}])
        self.assertIn("TEAM 15 MARKET · v15", result["sections"]["overview"]["group_share_message"])
        self.assertNotIn("t04", result["sections"]["overview"]["group_share_message"])
        self.assertEqual(result["sections"]["market"]["snapshot"]["tick_time_context"]["tick"], 25)


if __name__ == "__main__":
    unittest.main()
