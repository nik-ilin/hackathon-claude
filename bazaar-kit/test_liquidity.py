"""Campaña de liquidez: inventario sin confundir físicas/distintas, escenarios y viabilidad, lotes con un solo bono,
ofertas excepcionales nunca ejecutadas, reventa con salida desaparecida. Fixtures; sin red."""
import tempfile
import unittest
from collections import Counter

import coordinator as co
import liquidity as liq
import negotiation as neg
import phases as ph
import trading as tr
from test_coordinator import args as base_args
from test_dealer_ladder import led0
from test_fast_sales import world, bid, run
from test_trading import asset, catalog, offer, TEAM


class Inventory(unittest.TestCase):
    def test_selling_a_duplicate_keeps_pages_and_the_distinct_count(self):
        s = world()                                   # LAT-01 ×2, LAT-02, LAT-03: página LAT completa
        s["me"]["score"] = {"album_filled": 3, "album_slots": 4}
        inv = liq.inventory(s["me"], s["catalog"], set())
        self.assertEqual((inv["physical"], inv["distinct"], inv["surplus_copies"], inv["album_gaps"]), (4, 3, 1, 1))
        after = dict(s["me"], assets=[a for a in s["me"]["assets"] if a["id"] != 2])
        inv2 = liq.inventory(after, s["catalog"], set())
        self.assertEqual((inv2["distinct"], inv2["pages_complete"]), (3, inv["pages_complete"]))   # el contador no baja
        self.assertEqual(inv["free_surplus"], {"LAT-01": [2]})

    def test_committed_surplus_is_not_free(self):
        s = world()
        self.assertEqual(liq.inventory(s["me"], s["catalog"], {2})["free_surplus"], {})


class Scenarios(unittest.TestCase):
    ROWS = [{"ref": "MAL-07", "authorized": True, "free": [898], "locked": {},
             "prices": {"minimum": 9, "objective": 18, "quick_close": 12}},
            {"ref": "SAL-03", "authorized": True, "free": [1051], "locked": {},
             "prices": {"minimum": 7, "objective": 7, "quick_close": 5}},
            {"ref": "LAT-09", "authorized": False, "free": [], "locked": {}, "prices": {"minimum": 180, "objective": 180, "quick_close": None}}]

    def test_three_scenarios_without_protected_cards_or_promises(self):
        sc = liq.scenarios(85, self.ROWS)
        self.assertEqual((sc["confirmed"], sc["conservative"], sc["optimistic"]), (85, 97, 110))  # 5 < mínimo 7: no cuenta
        self.assertNotIn("LAT-09", [x["ref"] for x in sc["items"]])

    def test_unreachable_target_is_reported_not_forced(self):
        v = liq.viability(liq.scenarios(85, self.ROWS))
        self.assertFalse(v["reachable_min"])
        self.assertEqual((v["min"]["deficit_now"], v["min"]["gap_optimistic"], v["stretch"]["gap_optimistic"]), (65, 40, 90))
        self.assertIn("no se liquidan páginas", v["note"])


class Exceptional(unittest.TestCase):
    def test_lot_loses_the_page_bonus_once(self):
        val = tr.Valuation(catalog(), {"LAT": 1.3})
        c = Counter({"LAT-01": 1, "LAT-02": 1, "LAT-03": 1})
        single = [liq.lot_loss(val, c, [r]) for r in ("LAT-01", "LAT-02")]
        lot = liq.lot_loss(val, c, ["LAT-01", "LAT-02"])
        self.assertLess(lot, sum(single))                                       # sumar pérdidas duplicaría el bono

    def test_exceptional_bid_is_only_information_and_needs_authorization(self):
        s = world([asset(1, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")],
                  offers=[bid(50, 200, ref="LAT-02"), bid(51, 3, ref="LAT-03", maker="t08")])
        a = base_args(dealer_sell_dups=False, fast_sales="LAT-02", margin=2.0, duende_venue="rastro", liquidity_report=True)
        cands, pl, _ = co.candidates(s, led0(), a, neg.Journal(tempfile.mkdtemp()))
        exc = pl["liquidity"]["exceptional"]
        self.assertEqual(len(exc), 1)                                           # una página, un lote
        self.assertEqual(sorted(exc[0]["refs"]), ["LAT-02", "LAT-03"])
        self.assertTrue(exc[0]["justified"])                                    # 200 P compensa la pérdida del lote
        info = [c for c in cands if c.get("module") == "excepcional"]
        self.assertTrue(info and all(c["type"] == "info" and c["blockers"] for c in info))
        self.assertFalse([c for c in co.select(cands, led0(), 500, 6) if c.get("module") == "excepcional"])
        self.assertFalse([c for c in cands if c["type"] == "accept" and c.get("offer") == 50 and not c.get("blockers")])


class Resale(unittest.TestCase):
    def test_resale_without_a_live_exit_is_not_backed(self):
        cfg = ph.PhaseConfig(enabled=True)
        c = {"type": "accept", "cash": -10, "receive": {"MAL-01": 1}, "ref": "card:MAL-01"}
        ok, why = ph.resale_backed(c, {}, 50, cfg, 2.0)
        self.assertFalse(ok)
        self.assertIn("sin puja de otro equipo", why)


class Report(unittest.TestCase):
    def test_report_lines_render_and_separate_confirmed_from_pending(self):
        s = world()
        s["me"]["score"] = {"album_filled": 3, "album_slots": 4}
        a = base_args(dealer_sell_dups=False, fast_sales="LAT-01", margin=2.0, duende_venue="rastro", liquidity_report=True)
        cands, pl, _ = co.candidates(s, led0(), a, neg.Journal(tempfile.mkdtemp()))
        txt = "\n".join(liq.lines(pl["liquidity"]))
        self.assertIn("no es efectivo", txt)
        self.assertIn("ESCENARIOS", txt)
        self.assertIn("vendible LAT-01", txt)


if __name__ == "__main__":
    unittest.main()
