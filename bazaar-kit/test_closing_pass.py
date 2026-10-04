"""Pasada de cierre del último día: suelo de 150 P con el presupuesto, bono de página contado una vez, comisión anunciada en las ventas.
Complementa test_last_hour / test_page_campaign / test_market_intel sin duplicarlas. Fixtures; sin red."""
import tempfile
import unittest
from collections import Counter
from types import SimpleNamespace

import coordinator as co
import fast_sales as fs
import negotiation as neg
import trading as tr
from test_coordinator import args as base_args
from test_dealer_ladder import led0
from test_fast_sales import bid, run, world
from test_trading import asset, catalog, offer


class PageBonus(unittest.TestCase):
    def test_completing_card_carries_the_page_bonus_exactly_once(self):
        val = tr.Valuation(catalog(), {"LAT": 1.0})
        two = Counter({"LAT-01": 1, "LAT-02": 1})
        three = Counter({"LAT-01": 1, "LAT-02": 1, "LAT-03": 1})
        last = val.delta(two, Counter({"LAT-03": 1}), Counter())[0]
        plain = val.delta(Counter({"LAT-01": 1}), Counter({"LAT-03": 1}), Counter())[0]
        self.assertGreater(last, plain)                                           # la última carta incluye el bono de página
        self.assertAlmostEqual(val.total(three) - val.total(two), last, places=6)   # y el bono no se cuenta dos veces
        sequential = sum(val.delta(Counter(), Counter({r: 1}), Counter())[0] for r in ("LAT-01", "LAT-02", "LAT-03"))
        self.assertLess(sequential, val.total(three))                             # sumar valores sueltos subestima la página

    def test_a_duplicate_is_worth_less_than_the_first_copy(self):
        val = tr.Valuation(catalog(), {"LAT": 1.0})
        first = val.delta(Counter(), Counter({"LAT-01": 1}), Counter())[0]
        second = val.delta(Counter({"LAT-01": 1}), Counter({"LAT-01": 1}), Counter())[0]
        self.assertLess(second, first)


class AnnouncedFees(unittest.TestCase):
    def test_pending_fee_lowers_the_net_of_accepting_a_bid_and_can_make_it_fall_short(self):
        s = world(offers=[bid(50, 14)])
        venues = {"rastro": SimpleNamespace(fee=lambda p, n: 3 + 0)}
        cfg = fs.Config(refs=("LAT-01",))
        before = fs.evidence(s, "LAT-01", cfg, venues)["bids"][0]
        higher = {"rastro": SimpleNamespace(fee=lambda p, n: 9)}                   # comisión ANUNCIADA mayor que la vigente
        after = fs.evidence(s, "LAT-01", cfg, higher)["bids"][0]
        self.assertEqual((before["net"], after["net"]), (11, 5))
        pr = {"minimum": 6, "objective": 10, "quick_close": after["net"]}
        self.assertLess(after["net"], pr["minimum"])                               # con la comisión anunciada ya no llega al mínimo

    def test_market_intel_venue_uses_the_larger_announced_fee(self):
        import market_intel as mi
        v = mi.Venue("rastro", "Rastro", 500, 1, "posted", "world", pending={"fee_bps": 1000, "fee_per_card": 5})
        self.assertEqual(v.fee(20, 1), 7)                                           # 10 % de 20 + 5 P/carta
        self.assertEqual(v.effective_fees(), (1000, 5))


class FloorWithBudget(unittest.TestCase):
    def cands(self, spent=0, **kw):
        s = world(cash=kw.pop("cash", 635))
        led = led0()
        led["spent_confirmed"] = spent
        a = base_args(final_floor=150, max_spend=kw.pop("max_spend", 1100), reserve=5, per_card=95, margin=2.0, dealer_sell_dups=False,
                      duende_venue="rastro", **kw)
        out, pl, _ = co.candidates(s, led, a, neg.Journal(tempfile.mkdtemp()))
        return out, pl

    def test_floor_and_spend_limit_are_independent_gates(self):
        out, pl = self.cands(cash=200)                                             # 200 − 150 = 50 P gastables aunque el límite sea 1100
        self.assertEqual(pl["last_hour"]["floor"], 150)
        buys = [c for c in out if c["type"] in ("bid", "dealer_open", "dealer_counter") and (c.get("price") or 0) > 50 and not c.get("blockers")]
        self.assertEqual(buys, [])
        out, pl = self.cands(spent=618, cash=635, max_spend=620)                              # caja de sobra, pero el límite de gasto (618 ya gastados) manda
        self.assertEqual(pl["budget"]["remaining"], 2)
        self.assertEqual([c for c in out if c["type"] in ("bid", "dealer_open") and (c.get("price") or 0) > 2 and not c.get("blockers")], [])

    def test_unverifiable_balance_blocks_all_new_purchases(self):
        s = world(cash=635)
        view = SimpleNamespace(cash=999, market_reserved_cash=0, dealer_exposure=0, pending_cash=0)
        ok, why = co.balance_reliable(s, {"actions": []}, view, 635)
        self.assertFalse(ok)
        self.assertIn("distinto del servidor", why)


if __name__ == "__main__":
    unittest.main()


class DealersOnly(unittest.TestCase):
    def test_team_side_actions_are_blocked_and_dealer_actions_are_not(self):
        s = world(offers=[bid(50, 40)])
        a = base_args(dealers_only=True, max_spend=1100, reserve=5, per_card=95, margin=2.0, dealer_sell_dups=True, duende_venue="rastro",
                      fast_sales="LAT-01")
        out, pl, _ = co.candidates(s, led0(), a, neg.Journal(tempfile.mkdtemp()))
        team = [c for c in out if c["type"] in co.TEAM_SIDE_TYPES]
        self.assertTrue(team)
        self.assertTrue(all(any("--dealers-only" in b for b in c["blockers"]) for c in team))
        self.assertFalse([c for c in co.select(out, led0(), 500, 6) if c["type"] in co.TEAM_SIDE_TYPES])
        self.assertTrue(all(not any("--dealers-only" in b for b in c.get("blockers") or [])
                            for c in out if str(c["type"]).startswith("dealer_") or c["type"] == "cancel"))
