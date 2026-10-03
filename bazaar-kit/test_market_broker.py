"""Broker del Market Test (market_broker.py) y banco de pruebas (sim_bench.py); sin acceso a red."""
import itertools
import random
import unittest
from unittest.mock import patch

import market_broker as mb
import sim_bench as sim
import starter_broker as sb


def offer(oid, quote, buyer):
    card = {"cash": 0, "assets": [], "types": ["card:SIM-01"]}
    cash = {"cash": quote, "assets": [], "types": []}
    return {"id": oid, "give": cash if buyer else card, "want": card if buyer else cash}


def book(bids, asks, run="b1"):
    return {"bench_offers": [offer(f"{run}-b{i}", q, True) for i, q in enumerate(bids)]
            + [offer(f"{run}-a{i}", q, False) for i, q in enumerate(asks)], "offers": []}


def random_book(r):
    offers = []
    for run in range(r.randint(1, 3)):
        offers += book([r.randint(5, 30) for _ in range(r.randint(0, 7))],
                       [r.randint(5, 30) for _ in range(r.randint(0, 7))], f"b{run}")["bench_offers"]
    r.shuffle(offers)
    return {"bench_offers": offers, "offers": []}


def quotes(b):
    return {o["id"]: o["want"]["cash"] or o["give"]["cash"] for o in b["bench_offers"]}


class Matching(unittest.TestCase):
    def test_assign_max_equals_brute_force(self):
        r = random.Random(1)
        for _ in range(200):
            n, m = r.randint(1, 4), r.randint(1, 5)
            if n > m:
                n, m = m, n
            w = [[r.choice([0, r.uniform(0, 10)]) for _ in range(m)] for _ in range(n)]
            res = mb.assign_max(w)
            self.assertEqual(len(set(res)), n)
            best = max(sum(w[i][p[i]] for i in range(n)) for p in itertools.permutations(range(m), n))
            self.assertAlmostEqual(sum(w[i][res[i]] for i in range(n)), best)

    def test_best_matching_only_uses_valid_edges(self):
        pairs = mb.best_matching([10, 8], [7, 9], lambda i, j: 1.0, lambda i, j: [10, 8][i] >= [7, 9][j])
        self.assertEqual(sorted(pairs), [(0, 1), (1, 0)])  # 10×9 y 8×7: dos cruces donde el puesto ve uno


class Floor(unittest.TestCase):
    def test_every_offer_the_stall_crosses_is_crossed(self):
        r = random.Random(2)
        for _ in range(300):
            b = random_book(r)
            plan = mb.smart_plan(b, mb.Tracker(r.choice([0.0, 0.2, 0.4])), 0)
            ours = {x for m in plan for x in (m.sell, m.buy)}
            self.assertLessEqual({x for s, y, _ in sb.bench_plan(b) for x in (s, y)}, ours)
            self.assertEqual(len(ours), 2 * len(plan), "una oferta en dos pares")
            q = quotes(b)
            for m in plan:
                self.assertEqual(m.sell.split("-")[0], m.buy.split("-")[0])
                self.assertIn("-a", m.sell)
                self.assertTrue(q[m.sell] <= m.price <= q[m.buy], "el servidor exige ask <= precio <= bid")

    def test_extra_pair_needs_estimated_surplus(self):
        b = book([10, 8], [7, 9])
        self.assertEqual(len(sb.bench_plan(b)), 1)
        self.assertEqual(len(mb.smart_plan(b, mb.Tracker(0.2), 0)), 2)  # 8/0,8 − 9/1,2 = +2,5: compensa
        self.assertEqual(len(mb.smart_plan(b, mb.Tracker(0.0), 0)), 1)  # 8 − 9 < 0: solo el cruce del puesto

    def test_midpoint_price(self):
        self.assertEqual([m.price for m in mb.smart_plan(book([20], [11]), mb.Tracker(), 0)], [15])

    def test_relaxed_quote_estimates_limit_from_first_quote(self):
        t = mb.Tracker(0.2)
        t.update(book([8], [], "b1"), 0)
        t.update(book([10], [], "b1"), 2)
        self.assertAlmostEqual(t.limit("b1-b0", True, 10), 11.0)  # 10 + 2 por cada 2 ticks > 8/0,8
        self.assertEqual(t.limit("b9-b9", True, 12), 15)           # sin historia: prior


class Learning(unittest.TestCase):
    def test_shade_learned_from_relaxed_offers_that_left(self):
        t = mb.Tracker(0.2)
        for i in range(6):
            t.update(book([100], [], f"b{i}"), 0)
            t.update(book([101], [], f"b{i}"), 1)
            t.update({"bench_offers": []}, 2)
        self.assertEqual(len(t.moves), 6)
        self.assertLess(t.shade, 0.07)

    def test_matched_offers_are_not_evidence(self):
        t = mb.Tracker(0.2)
        t.update(book([100], []), 0)
        t.update(book([101], []), 1)
        t.update({"bench_offers": []}, 2, matched={"b1-b0"})
        self.assertEqual(t.moves, [])


class Watchdog(unittest.TestCase):
    def test_planner_error_falls_back_to_stall(self):
        bb, b = mb.BenchBroker(), book([10, 8], [7, 9])
        with patch.object(mb, "smart_plan", side_effect=ValueError("x")):
            plan = bb.plan(b, 0)
        self.assertEqual([(m.sell, m.buy, m.price) for m in plan], sb.bench_plan(b))
        self.assertEqual(bb.dog.mode, "stall")
        self.assertEqual(len(bb.plan(b, 1)), 1, "el resto de la sesión, plan del puesto")

    def test_plan_missing_a_stall_cross_trips(self):
        bb = mb.BenchBroker()
        with patch.object(mb, "smart_plan", return_value=[]):
            self.assertEqual(len(bb.plan(book([10], [7]), 0)), 1)
        self.assertEqual(bb.dog.mode, "stall")

    def test_refusals_trip(self):
        dog = mb.Watchdog()
        for ok in (True, False, False, False):
            dog.feedback(mb.Match("a", "b", 1), ok)
        self.assertEqual(dog.mode, "stall")

    def test_low_learned_shade_trips(self):
        bb = mb.BenchBroker()
        bb.tracker.moves = [0.01] * mb.MIN_MOVES
        self.assertEqual(len(bb.plan(book([10, 8], [7, 9]), 0)), 1)
        self.assertEqual(bb.dog.mode, "stall")

    def test_new_session_resets(self):
        bb = mb.BenchBroker()
        bb.plan(book([10], [7]), 0)
        bb.dog.trip("x")
        bb.plan({"bench_offers": []}, 1)
        self.assertEqual((bb.dog.mode, bb.tracker.seen), ("smart", {}))

    def test_sent_matches_are_not_resent_before_settling(self):
        bb, b = mb.BenchBroker(), book([10], [7])
        (m,) = bb.plan(b, 0)
        bb.feedback(m, True)
        self.assertEqual(bb.plan(b, 0), [])


class Probe(unittest.TestCase):
    def test_probes_only_non_crossing_leftovers(self):
        plan = mb.smart_plan(book([10, 9], [7, 11]), mb.Tracker(0.2), 0, probe=3)
        smart = [m for m in plan if m.kind == "smart"]
        probe = [m for m in plan if m.kind == "probe"]
        self.assertEqual(len(smart), 1)
        self.assertEqual(len(probe), 1)
        self.assertNotIn(probe[0].sell, {smart[0].sell})
        self.assertGreater(probe[0].price, 9)  # entre límites estimados, fuera de cotización

    def test_probe_switches_off_after_refusals_without_success(self):
        bb = mb.BenchBroker(probe=3)
        for _ in range(mb.PROBE_STRIKES):
            bb.feedback(mb.Match("a", "b", 1, "probe"), False)
        self.assertFalse(bb.dog.probing)
        self.assertEqual(bb.dog.mode, "smart", "un sondeo rechazado no es un fallo del broker")

    def test_probe_stays_on_after_one_success(self):
        dog = mb.Watchdog()
        dog.feedback(mb.Match("a", "b", 1, "probe"), True)
        for _ in range(5):
            dog.feedback(mb.Match("a", "b", 1, "probe"), False)
        self.assertTrue(dog.probing)


class Bench(unittest.TestCase):
    def test_starter_broker_is_the_stall(self):
        r = random.Random(3)
        for _ in range(50):
            b = random_book(r)
            self.assertEqual([(m.sell, m.buy, m.price) for m in sim.Stall().plan(b, 0)], sb.bench_plan(b))

    def test_new_broker_beats_stall_on_average(self):
        mechs = {"puesto": lambda r, s: sim.Stall(), "nuevo": lambda r, s: mb.BenchBroker(probe=3)}
        for server in ("quote", "limit"):
            out = sim.bench(["normal", "dificil", "denso"], 20, 5, server, mechs)
            for name, rows in out.items():
                self.assertGreaterEqual(rows["nuevo"]["ratio"], 1.0, f"{name} / {server}")
        self.assertGreater(out["dificil"]["nuevo"]["ratio"], 1.1, "con validación por límites el sondeo gana")

    def test_play_rejects_invalid_matches(self):
        runs = sim.session(1, sim.SCENARIOS["normal"])
        bad = type("Bad", (), {"plan": lambda self, b, t: [mb.Match(o["id"], o["id"], 1) for o in b["bench_offers"]][:1],
                               "feedback": lambda self, m, ok: None})()
        self.assertEqual(sim.play(runs, bad, 5)["matches"], 0)


class Loop(unittest.TestCase):
    def test_main_runs_offline_with_a_fake_broker(self):
        sent = []

        class Fake:
            def __init__(self, url, key):
                pass

            def clock(self):
                return {"tick": 1}

            def book(self):
                return book([10, 8], [7, 9])

            def match(self, sell, buy, price):
                sent.append((sell, buy, price))
                return {}

        with patch("bazaar_sdk.Broker", Fake), patch.dict("os.environ", {"BROKER_KEY": "bk_test"}), \
                patch("sys.argv", ["market_broker.py"]), patch.object(mb.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                mb.main()
        self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
