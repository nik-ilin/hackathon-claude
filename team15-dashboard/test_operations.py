"""Contratos del monitor operativo; no toca la red."""
import unittest

import operations
from app import render_dashboard_overview, render_operations, strategy_export


class OperationsTests(unittest.TestCase):
    def test_duel_queue_marks_same_deadline_and_rejects_bad_price(self):
        base = {"status": "live", "role": "buyer", "your_limit": 100, "deadline_tick": 104,
                "issues": ["price", "days"], "your_days_weight": [0] * 11}
        ds = [dict(base, duel=1, rival_offer={"price": 80, "days": 3}),
              dict(base, duel=2, rival_offer={"price": 101, "days": 3}),
              dict(base, duel=3, rival_offer={"price": 90, "days": 4})]
        result = operations.duel_watch(ds, 101)
        self.assertEqual(result["live"], 3)
        self.assertEqual(result["safe"], 2)
        self.assertEqual(result["urgent"], 2)
        self.assertEqual(result["rows"][0]["state"], "close_now")
        self.assertEqual(result["rows"][1]["state"], "unsafe")
        self.assertFalse(result["rows"][1]["safe_to_accept"])

    def test_unknown_day_weight_never_claims_safe_acceptance(self):
        result = operations.duel_watch([{"duel": 4, "status": "live", "role": "seller",
                                       "your_limit": 50, "deadline_tick": 13,
                                       "issues": ["price", "days"], "your_days_weight": None,
                                       "rival_offer": {"price": 70, "days": 2}}], 10)
        self.assertIsNone(result["rows"][0]["total_margin"])
        self.assertEqual(result["rows"][0]["state"], "unsafe")

    def test_buyer_delivery_days_can_turn_good_price_into_bad_deal(self):
        result = operations.duel_watch([{"duel": 5, "status": "live", "role": "buyer",
                                       "your_limit": 100, "deadline_tick": 13,
                                       "issues": ["price", "days"], "your_days_weight": 4,
                                       "days_meaning": "each delivery day costs you this much cash",
                                       "rival_offer": {"price": 80, "days": 6}}], 10)
        self.assertEqual(result["rows"][0]["price_margin"], 20)
        self.assertEqual(result["rows"][0]["total_margin"], -4)
        self.assertFalse(result["rows"][0]["safe_to_accept"])

    def test_capital_and_market_signal_do_not_claim_broker_liveness(self):
        result = operations.build(duels=[], duel_error=None, tick=100, cash=500,
                                  offers=[{"status": "open", "maker": "t15", "give": {"cash": 90}}], reserve=100,
                                  venues=[{"venue": "v15", "owner": "t15", "status": "open",
                                           "rules": {"mechanism": "board"}}],
                                  feed_health={"status": "fresh"}, verified=True)
        self.assertEqual(result["capital"]["uncommitted_cash"], 410)
        self.assertTrue(result["capital"]["board_cash_ready"])
        self.assertFalse(result["market"]["broker_health_verified"])
        self.assertEqual(result["queue"][0]["topic"], "market")
        self.assertIn('Mesa de mando', render_operations({"operations": result}))
        self.assertEqual(strategy_export({"operations": result})["operations"]["source_tick"], 100)

    def test_incoming_counterparty_bid_does_not_reserve_team_cash(self):
        result = operations.capital_watch(360, [
            {"status": "open", "maker": "banco", "to": "t15", "give": {"cash": 113}},
            {"status": "open", "maker": "t15", "to": None, "give": {"cash": 40}},
            {"status": "open", "maker": "t15", "to": None, "give": {"assets": [{"ref": "LAT-11"}]},
             "want": {"cash": 248}},
        ])
        self.assertEqual(result["open_bid_commitments"], 40)
        self.assertEqual(result["uncommitted_cash"], 320)
        self.assertEqual(result["spendable_after_reserve"], 220)

    def test_public_only_view_does_not_invent_missing_cards_or_cash_value(self):
        view = render_dashboard_overview({"verified": False, "catalog_rows": [
            {"ref": "MAL-01", "set": "Malasaña", "released": True, "stock": 0,
             "free": 0, "buy_ceiling": None, "sold_median": None, "held_by": [], "wanted_by": []}]})
        self.assertIn("La ausencia en el feed no prueba que falte", view)
        self.assertIn("Requiere valoración privada", view)
        self.assertNotIn("1 cartas faltantes", view)


if __name__ == "__main__":
    unittest.main()
