"""Integración offline de Banco en el coordinador único; no llama a la API."""
import copy
import unittest
from collections import Counter

import bank_dealer as bank
import negotiation as neg
import trading as tr
from test_ernesto import BANCO, world
from test_dealer_ladder import bid, thread
from test_trading import asset


class BancoPlanning(unittest.TestCase):
    def setUp(self):
        self.s = world([asset(1, "LAT-03"), asset(2, "LAT-03")])
        self.s["clock"].update(tick=50, tick_seconds=30, limits={"max_open_threads_per_team": 6})
        self.s["dealers"]["banco"]["menu"]["sells"].append(
            {"pack": "sobre_plata", "list_price": 75, "opening_ask": 90, "per_team_per_hour": 2})
        self.val = tr.Valuation(self.s["catalog"], self.s["me"]["affinity"])
        self.counts = tr.counts_of(self.s["me"]["assets"])

    def plan(self, **kw):
        args = dict(margin=2, free_cash=0, budget_left=0, open_count=0, committed=set(), alternatives=[])
        args.update(kw)
        return bank.plan(self.s, {"actions": []}, self.val, self.counts, **args)

    def test_api_menu_unlock_and_eligible_free_copy(self):
        cands, report = self.plan(free_cash=38, budget_left=100)
        sales = [c for c in cands if c["type"] == "dealer_sell_open"]
        self.assertEqual({c["asset"] for c in sales}, {1, 2})
        self.assertEqual(report["status"], "operación de venta viable")
        self.assertEqual(report["quota"]["limit"], 4)
        self.assertIn("no hay una copia sin abrir", report["silver_pack"])

    def test_exact_no_operation_status_and_no_capital_reservation(self):
        self.s["me"]["assets"] = [asset(1, "LAT-01")]
        self.counts = tr.counts_of(self.s["me"]["assets"])
        cands, report = self.plan(free_cash=0, budget_left=0)
        self.assertEqual(report["status"], "banco desbloqueado, sin operación viable")
        self.assertEqual(report["cash_room"], 0)
        info = next(c for c in cands if c["type"] == "info")
        self.assertIn("no se reserva capital", " ".join(info["blockers"]))

    def test_asset_committed_or_other_executable_buyer_prevents_bank_contact(self):
        self.s["me"]["assets"] = [asset(2, "LAT-03")]
        self.counts = tr.counts_of(self.s["me"]["assets"])
        _, report = self.plan(committed={2})
        self.assertEqual(report["candidates"], 0)
        competitor = {"type": "accept", "cash": 200, "deliver": {"LAT-03": 1}, "blockers": [], "maker": "t13"}
        _, report = self.plan(alternatives=[competitor])
        self.assertEqual(report["candidates"], 0)
        self.assertTrue(any("otro comprador ejecutable" in x for x in report["reasons"]))

    def test_diminishing_concessions_and_floor_close(self):
        d = bank.sell_decision(neg.NegState(item="card:LAT-03", current=neg.Ask(200, 7, False, True, 50)), 220, 8)
        self.assertEqual((d.action, d.price), ("counter", 242))
        state = neg.NegState(item="card:LAT-03", current=neg.Ask(200, 7, False, True, 50),
                             rounds=[neg.Round(242, 200, 50)])
        d2 = bank.sell_decision(state, 220, 8)
        self.assertEqual(d2.price, 231)
        self.assertEqual(bank.sell_decision(neg.NegState(item="card:LAT-03", current=neg.Ask(80, 9, False, True, 50),
                                                         rounds=[neg.Round(90, 100, 50)]),
                                            80, 8).action, "accept")
        self.assertEqual(bank.sell_decision(neg.NegState(item="card:LAT-03", current=neg.Ask(79, 9, True, True, 50),
                                                         rounds=[neg.Round(90, 100, 50)]),
                                            80, 8).action, "abandon")

    def test_pending_bank_thread_is_continued_without_second_contact(self):
        t = thread(5, "banco", [bid("banco", 1, 51, 1, 2)], {"sell": {"assets": [2]}}, created=50)
        self.s["clock"]["tick"] = 51
        self.s["threads"]["open"] = [t]
        c = bank.thread_candidate(self.s, t, self.val, self.counts, margin=2, alternatives=[], led={"actions": []})
        self.assertEqual(c["type"], "dealer_sell_counter")
        self.assertEqual(c["dealer"], "banco")
        self.assertGreaterEqual(c["price"], c["floor"])


if __name__ == "__main__":
    unittest.main()
