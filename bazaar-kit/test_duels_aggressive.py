"""Perfil AGRESIVO de Duelos III (duels.decide_aggressive, activado por duel_runner --day3): comprador y vendedor, días con signo,
rival que mejora rápido, rival estancado, cierre cerca del deadline, total negativo, cola con el mismo deadline, caso 15830 y
barreras duras (precio fuera de límite o total ≤ 0 nunca se aceptan). Offline; no envía nada."""
import json
import os
import random
import unittest
from unittest.mock import patch

import duel_runner as runner
import duel_tree
import duels as dl

BUY = "each delivery day costs you this much cash"
SELL = "each delivery day adds this much cash to your side"


def duel(role="buyer", limit=100, prices=(), days=0, w=None, start=0, deadline=16, duel_id=1, rival="Rival X", ours=()):
    msgs = [{"tick": start + k, "from": rival, "price": p, "days": days} for k, p in enumerate(prices)]
    msgs += [{"tick": t, "from": "us", "price": p, "days": dd} for t, p, dd in ours]
    issues = ["price", "days"] if w is not None else ["price"]
    d = {"duel": duel_id, "status": "live", "role": role, "your_limit": limit, "rival": rival, "deadline_tick": deadline,
         "decay_per_round": 0.1, "rounds": 0, "issues": issues, "your_days_weight": w,
         "days_meaning": (BUY if role == "buyer" else SELL) if w is not None else None, "messages": msgs,
         "your_offer": {"price": ours[-1][1], "days": ours[-1][2]} if ours else None,
         "rival_offer": {"id": 9, "price": prices[-1], "days": days if w is not None else None, "tick": start + len(prices) - 1} if prices else None}
    return d


class Params(unittest.TestCase):
    def setUp(self):
        self.saved = dict(dl.PARAMS)
        dl.PARAMS.update(PLAY_DAYS=True, PROFILES=True, LADDER=runner.LADDER, AGGRESSIVE=True, LEARN=False, LOGROLL=False)

    def tearDown(self):
        dl.PARAMS.clear()
        dl.PARAMS.update(self.saved)

    def cands(self, ds, tick):
        return dl.duel_candidates(ds, tick)

    def accepts(self, ds, tick):
        return [c for c in self.cands(ds, tick) if c["type"] == "duel_accept"]


class Phases(Params):
    def test_rapidly_improving_rival_is_not_accepted_on_a_mediocre_surplus_early(self):
        d = duel(prices=[95, 90, 85, 80], deadline=16)            # +5 P/tick, total 20 % con 12 ticks
        self.assertEqual(self.accepts([d], 3), [])
        self.assertIn("esperar", dl.NOTES[1]["reason"])

    def test_good_surplus_is_accepted_even_if_the_rival_is_still_improving(self):
        d = duel(prices=[65, 60, 55, 50], deadline=16)            # 50 % en EARLY con mejora fuerte (umbral EARLY 45 %)
        self.assertTrue(self.accepts([d], 3))
        self.assertIn("ya es bueno", self.accepts([d], 3)[0]["facts"]["reason"])

    def test_old_policy_waits_where_the_aggressive_one_closes(self):
        d = duel(prices=[65, 60, 55, 50], deadline=16)
        self.assertTrue(self.accepts([d], 3))
        dl.PARAMS["AGGRESSIVE"] = False
        self.assertEqual(self.accepts([d], 3), [])                # la anterior: «mejora con fuerza: esperar» en EARLY

    def test_stalled_rival_in_mid_closes_a_small_positive_surplus(self):
        d = duel(prices=[95] * 10, deadline=16)                   # total 5 %, plantado; t=9 → MID
        acc = self.accepts([d], 9)
        self.assertTrue(acc)
        self.assertIn("stalled", acc[0]["facts"]["reason"])

    def test_early_stall_is_treated_as_a_pause_not_a_reason_to_close(self):
        d = duel(prices=[95, 95, 95, 95], deadline=16)            # 5 % con 12 ticks
        self.assertEqual(self.accepts([d], 3), [])

    def test_near_deadline_any_positive_total_is_closed(self):
        d = duel(prices=[99, 99], start=12, deadline=16)          # 1 P, quedan 3
        self.assertTrue(self.accepts([d], 13))

    def test_wait_reason_shows_both_expected_values(self):
        d = duel(prices=[95, 90, 85], deadline=16)
        self.cands([d], 2)
        self.assertRegex(dl.NOTES[1]["reason"], r"valor [\d.]+ > [\d.]+")
        self.assertIn("p_pérdida", dl.NOTES[1]["brief"])


class Days(Params):
    def test_buyer_with_costly_days_uses_the_total_not_the_price_margin(self):
        d = duel(prices=[70, 70], days=8, w=4.0, start=10, deadline=16)   # precio +30, días −32 → total −2
        self.assertEqual(self.accepts([d], 13), [])
        d0 = duel(prices=[70, 70], days=0, w=4.0, start=10, deadline=16)  # día 0: total +30
        self.assertTrue(self.accepts([d0], 13))

    def test_seller_with_valuable_days_counts_them_in_favour(self):
        d = duel("seller", 100, prices=[101, 101], days=10, w=3.0, start=12, deadline=16)   # precio +1, días +30
        acc = self.accepts([d], 13)
        self.assertTrue(acc)
        self.assertAlmostEqual(acc[0]["du"], 31.0)

    def test_zone_counter_lands_inside_our_zone_with_our_best_day(self):
        for role, w, best in (("buyer", 3.0, 0), ("seller", 3.0, 10)):
            d = duel(role, 100, prices=[130 if role == "buyer" else 70] * 2, days=5, w=w, start=4, deadline=16)
            says = [c for c in self.cands([d], 5) if c["type"] == "duel_say"]
            self.assertTrue(says, role)
            c = says[0]
            self.assertEqual(c["days"], best)
            self.assertGreater(dl.margin(d, c["price"], c["days"]), 0)
            self.assertGreaterEqual(dl.price_margin(d, c["price"]), 0)

    def test_negative_total_without_time_is_left_to_expire_for_economic_reasons(self):
        d = duel(prices=[120, 120], start=14, deadline=16, ours=((10, 80, None), (12, 85, None)))
        self.cands([d], 15)
        self.assertEqual(dl.NOTES[1]["action"], "expire")
        self.assertIn("POR ECONOMÍA", dl.NOTES[1]["reason"])


class HardLimits(Params):
    def test_out_of_limit_or_non_positive_total_is_never_accepted(self):
        rng = random.Random(7)
        for _ in range(600):
            role = rng.choice(["buyer", "seller"])
            lim = rng.randint(40, 260)
            w = rng.choice([None, round(rng.uniform(0.5, 9), 2)])
            dd = rng.randint(0, 10)
            seq = [lim + rng.randint(-60, 60) for _ in range(rng.randint(1, 6))]
            start = rng.randint(0, 10)
            d = duel(role, lim, prices=seq, days=dd, w=w, start=start, deadline=start + len(seq) + rng.randint(1, 8))
            t = start + len(seq) - 1
            for c in self.accepts([d], t):
                ro = d["rival_offer"]
                days = ro.get("days") if w is not None else None
                self.assertGreaterEqual(dl.price_margin(d, ro["price"]), 0)
                self.assertGreater(dl.margin(d, ro["price"], days), 0)
            for c in self.cands([d], t):
                if c["type"] == "duel_say":
                    self.assertGreaterEqual(dl.price_margin(d, c["price"]), 0)

    def test_profile_rules_cannot_delay_an_aggressive_accept_past_the_queue(self):
        d = duel(prices=[70, 67, 64, 61, 58, 55, 52, 49], deadline=12, rival="Rival Plata")   # «cede»: antes esperaba al 90 %
        self.assertTrue(self.accepts([d], 7))                                       # MID, 51 % ≥ 32 %: el perfil no lo retrasa


class Queue(Params):
    def test_same_deadline_priority_is_explicit_and_not_iteration_order(self):
        a = duel(prices=[95, 95, 95, 95, 95, 95], deadline=12, duel_id=1)          # 5 P
        b = duel(prices=[60, 60, 60, 60, 60, 60], deadline=12, duel_id=2)          # 40 P
        c = duel(prices=[80, 80, 80, 80, 80, 80], deadline=12, duel_id=3)          # 20 P
        for order in ([a, b, c], [c, a, b], [b, c, a]):
            acc = self.accepts(order, 9)
            self.assertEqual([x["duel"] for x in acc][:1], [2])
            self.assertEqual([x["duel"] for x in acc], sorted([x["duel"] for x in acc], key=lambda k: {2: 0, 3: 1, 1: 2}[k]))
        plan = duel_tree.plan([a, b, c], 9)
        self.assertEqual(sorted(s["action"] for s in plan if s["action"] in ("accept", "defer")).count("accept"), 1)

    def test_queue_pressure_closes_before_the_last_tick(self):
        wave = [duel(prices=[85, 84, 83, 82, 81, 80, 80], deadline=12, duel_id=i) for i in range(4)]
        self.assertTrue(self.accepts(wave, 6))          # 4 duelos, quedan 6: no se espera al último tick (antes se difería)

    def test_expiring_mediocre_offer_does_not_jump_a_much_better_one(self):
        poor = duel(prices=[97] * 12, deadline=13, duel_id=1)                       # 3 P, vence ya
        good = duel(prices=[50] * 12, deadline=16, duel_id=2)                       # 50 P
        acc = self.accepts([poor, good], 11)
        self.assertEqual(acc[0]["duel"], 2)


class Case15830(Params):
    """Duelo real 15830 (Rival Azul, comprador, límite 256, peso de días 0,93): 238, 233, 227, 221, 214, 210, 204 a día 0."""
    PRICES = [238, 233, 227, 221, 214, 210, 204]

    def d(self, n):
        return duel("buyer", 256, prices=self.PRICES[:n], days=0, w=0.93, start=2582, deadline=2594, duel_id=15830, rival="Rival Azul")

    def test_offer_210_surplus_46_is_waited_on_because_waiting_is_worth_more(self):
        d = self.d(6)                                                             # 210 → total 46 (18 %), +~5,6 P/tick
        self.assertEqual(self.accepts([d], 2587), [])
        self.assertRegex(dl.NOTES[15830]["reason"], r"esperar: valor 4[6-9]\.\d+ > 46\.0")

    def test_closes_at_204_one_tick_earlier_than_the_previous_policy(self):
        d = self.d(7)
        first_new = next(t for t in range(2588, 2594) if self.accepts([d], t))
        dl.PARAMS["AGGRESSIVE"] = False
        first_old = next(t for t in range(2588, 2594) if self.accepts([d], t))
        self.assertLess(first_new, first_old)
        self.assertEqual(dl.margin(d, 204, 0), 52.0)


class Runner(unittest.TestCase):
    def setUp(self):
        self.saved = dict(dl.PARAMS)

    def tearDown(self):
        dl.PARAMS.clear()
        dl.PARAMS.update(self.saved)

    def run_main(self, argv):
        logs = []
        with patch.object(runner, "run") as run, patch.object(runner, "log", side_effect=logs.append), \
                patch.dict(os.environ, {"BAZAAR_KEY": "test-key"}), patch("sys.argv", ["duel_runner.py", *argv]):
            runner.main()
        return logs

    def test_day3_enables_the_aggressive_profile_and_logs_its_parameters(self):
        logs = self.run_main(["--day3"])
        self.assertTrue(dl.PARAMS["AGGRESSIVE"])
        start = logs[0]
        self.assertTrue(start["params"]["AGGRESSIVE"])
        for k in ("AGG_EARLY_RATIO", "AGG_MID_RATIO", "AGG_GOOD_RATIO", "AGG_RISK_URGENCY", "AGG_ZONE_FRAC"):
            self.assertIn(k, start["params"])

    def test_no_aggressive_and_other_modes_keep_the_previous_policy(self):
        self.run_main(["--day3", "--no-aggressive"])
        self.assertFalse(dl.PARAMS["AGGRESSIVE"])
        dl.PARAMS["AGGRESSIVE"] = False
        self.run_main(["--days", "--ladder"])
        self.assertFalse(dl.PARAMS["AGGRESSIVE"])                                   # fuera de --day3 nada cambia
        self.run_main(["--days", "--aggressive"])
        self.assertTrue(dl.PARAMS["AGGRESSIVE"])


if __name__ == "__main__":
    unittest.main()
