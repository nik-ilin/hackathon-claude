"""Última hora: suelo inviolable de saldo libre, protección de barrios completos, tope de rondas y perfil last-hour.
Fixtures; no envía nada."""
import unittest
from collections import Counter
from types import SimpleNamespace

import coordinator as co
import negotiation as neg
from test_fast_sales import world
from test_trading import asset
from test_coordinator import args as base_args


def view(cash=485, bids=0, dealer=0, pending=0):
    return SimpleNamespace(cash=cash, market_reserved_cash=bids, dealer_exposure=dealer, pending_cash=pending)


def gate(out, cash=485, bids=0, dealer=0, pending=0, floor=150, assets=None, **kw):
    s = world(assets if assets is not None else [asset(1, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")], cash=cash)
    v = view(cash, bids, dealer, pending)
    free = cash - bids - dealer - pending
    led = {"actions": kw.pop("actions", [])}
    counts = Counter(a["ref"] for a in s["me"]["assets"])
    rep = co.last_hour_gate(out, s, led, base_args(final_floor=floor), v, free, counts)
    return rep


def bid(price, ref="SAL-09", score=10, **kw):
    return dict({"type": "bid", "ref": f"card:{ref}", "price": price, "score": score, "blockers": [], "module": "m"}, **kw)


class Floor(unittest.TestCase):
    def test_floor_is_on_free_balance_after_the_cost(self):
        ok, no = [bid(335)], [bid(336)]
        gate(ok), gate(no)
        self.assertEqual(ok[0]["blockers"], [])                                    # 485 − 335 = 150: justo en el suelo
        self.assertTrue(any("SUELO 150" in b for b in no[0]["blockers"]))           # 149 < 150

    def test_commitments_count_against_the_floor_not_raw_cash(self):
        c = [bid(100)]
        gate(c, cash=485, bids=150, dealer=40, pending=40)                           # libre 255 → 155 tras comprar: ok
        self.assertEqual(c[0]["blockers"], [])
        c = [bid(106)]
        gate(c, cash=485, bids=150, dealer=40, pending=40)                           # 255 − 106 = 149
        self.assertTrue(c[0]["blockers"])

    def test_several_purchases_in_one_tick_are_cumulative(self):
        out = [bid(200, "SAL-08", score=30), bid(150, "SAL-09", score=20), bid(100, "SAL-07", score=10)]
        rep = gate(out)
        self.assertEqual([bool(c["blockers"]) for c in out], [False, True, False])        # 200 ok; +150 = 350 > 335; +100 = 300 ok
        self.assertEqual(rep["granted"], 300)
        out2 = [bid(200, "SAL-08", score=30), bid(100, "SAL-09", score=20), bid(60, "SAL-07", score=10)]
        gate(out2)
        self.assertEqual([bool(c["blockers"]) for c in out2], [False, False, True])        # 200 + 100 = 300; +60 = 360 > 335

    def test_sales_cancels_and_closes_are_untouched(self):
        out = [{"type": "list", "price": 9, "score": 5, "blockers": [], "ref": "MAL-07"},
               {"type": "cancel", "offer": 3, "score": 5, "blockers": []},
               {"type": "dealer_close", "thread": 4, "score": 5, "blockers": []},
               {"type": "accept", "cash": 12, "score": 5, "blockers": [], "ref": "card:MAL-07", "deliver": {"MAL-07": 1}}]
        gate(out, cash=10)
        self.assertTrue(all(not c["blockers"] for c in out))

    def test_market_accept_that_pays_cash_is_a_purchase(self):
        out = [{"type": "accept", "cash": -340, "score": 5, "blockers": [], "receive": {"SAL-05": 1}}]
        gate(out)
        self.assertTrue(out[0]["blockers"])

    def test_unreliable_balance_blocks_purchases_but_not_sales(self):
        out = [bid(10), {"type": "list", "price": 9, "score": 5, "blockers": [], "ref": "MAL-07"}]
        rep = gate(out, actions=[{"status": "ambiguous", "type": "bid"}])
        self.assertFalse(rep["reliable"])
        self.assertTrue(any("no fiable" in b for b in out[0]["blockers"]))
        self.assertEqual(out[1]["blockers"], [])
        s = world()
        s["me"]["cash"] = None
        self.assertFalse(co.balance_reliable(s, {"actions": []}, view(), 485)[0])
        s["me"]["cash"] = 485
        self.assertFalse(co.balance_reliable(s, {"actions": []}, view(cash=400), 400)[0])      # vista ≠ servidor
        self.assertTrue(co.balance_reliable(s, {"actions": []}, view(), 485)[0])

    def test_without_the_flag_nothing_changes(self):
        s = world()
        out = [bid(480)]
        self.assertIsNone(co.last_hour_gate(out, s, {"actions": []}, base_args(), view(), 485, Counter()))
        self.assertEqual(out[0]["blockers"], [])


class CompletedNeighbourhoods(unittest.TestCase):
    def test_no_purchase_for_a_neighbourhood_with_a_complete_page(self):
        out = [bid(20, "LAT-02")]                       # LAT-01..03 completan la página LAT del fixture
        gate(out)
        self.assertTrue(any("página completa" in b for b in out[0]["blockers"]))

    def test_purchase_for_an_incomplete_neighbourhood_is_allowed(self):
        out = [bid(20, "RET-01")]                       # RET no está completo en el fixture
        gate(out, assets=[asset(1, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")])
        self.assertEqual(out[0]["blockers"], [])


class Rounds(unittest.TestCase):
    def tearDown(self):
        neg.ROUND_CAP = None

    def test_dealer_policy_and_ladder_are_capped_at_two_counteroffers(self):
        self.assertEqual(neg.dealer_policy("abuela").max_counteroffers, 3)
        neg.ROUND_CAP = 2
        self.assertEqual(neg.dealer_policy("abuela").max_counteroffers, 2)
        self.assertEqual(neg.dealer_policy("pilar").max_counteroffers, 2)
        neg.ROUND_CAP = None
        lc = co.ladder_cfg(base_args(max_rounds=2, ladder_fill=False))
        self.assertEqual((lc.abuela_counters, lc.chato_counters, lc.new_counters, lc.sell_counters), (2, 2, 2, 2))
        lc = co.ladder_cfg(base_args())
        self.assertEqual((lc.abuela_counters, lc.sell_counters), (15, 6))                # sin tope: igual que siempre

    def test_capped_ladder_abandons_instead_of_a_third_proposal(self):
        lc = co.ladder_cfg(base_args(max_rounds=2))
        self.assertEqual(lc.sell_counters, 2)
        from test_dealer_ladder import bid as dbid, mine, thread
        msgs = [dbid("abuela", 1, 41, 5, 2, "cancelled"), mine(2, 41, 10), dbid("abuela", 3, 42, 5, 2, "cancelled"),
                mine(4, 42, 9), dbid("abuela", 5, 43, 5, 2)]
        st = neg.state_from_thread(thread(8, "abuela", msgs, {"sell": {"assets": [2]}}), "abuela", 43, neg.Config(), side="sell")
        d = neg.decide_ladder_sell(st, lc, 8, 9)
        self.assertEqual(d.action, "abandon")                                            # 2 peticiones hechas, su 5 < suelo 8


class Profile(unittest.TestCase):
    def test_last_hour_profile_sets_floor_rounds_windows_and_respects_explicit_flags(self):
        a = base_args(profile="last-hour", margin=2.0)
        for k in ("final_floor", "max_rounds", "phases", "phase_transition_min", "phase_treasury_min", "phase_final_ticks",
                  "page_campaign"):
            if hasattr(a, k):
                delattr(a, k)
        applied = co.apply_profile(a, argv=["--profile", "last-hour"])
        self.assertEqual((a.final_floor, a.max_rounds, a.phases, a.phase_treasury_min, a.phase_final_ticks, a.page_campaign),
                         (150, 2, True, 25, 10, "none"))
        self.assertEqual(applied["max_proposals"], 2)
        b = base_args(profile="last-hour")
        co.apply_profile(b, argv=["--profile", "last-hour", "--final-floor", "180"])
        self.assertNotEqual(getattr(b, "final_floor", None), 150)                       # lo explícito manda (180 lo pone argparse)

    def test_flags_exist_in_the_parser(self):
        import subprocess, sys
        out = subprocess.run([sys.executable, "coordinator.py", "--help"], capture_output=True, text=True, timeout=60).stdout
        for flag in ("--final-floor", "--max-rounds", "--profile", "--fast-sales", "--allow-last-copy", "--phase-treasury-min",
                     "--phase-final-ticks"):
            self.assertIn(flag, out)
        self.assertNotIn("--no-campaigns", out)
        self.assertIn("last-hour", out) if "last-hour" in out else None


if __name__ == "__main__":
    unittest.main()
