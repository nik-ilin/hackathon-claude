"""Precio + días: convención de signos, calibración con el servidor, los duelos del log (5842, 5694, 5762, 5919), elección
de día, logroll, perfiles, replay, prioridad de aceptaciones y reconciliación. Offline; no envía nada."""
import json
import os
import random
import unittest
from unittest.mock import Mock, patch

import duel_runner as runner
import duel_tree
import duels as dl

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "duels_fixture_days.json")))["duels"]
BUY = "each delivery day costs you this much cash"
SELL = "each delivery day adds this much cash to your side"


def duel(role="buyer", limit=100, w=5.0, price=None, days=None, deadline=30, **kw):
    d = {"duel": kw.pop("duel", 1), "status": "live", "role": role, "your_limit": limit, "rival": kw.pop("rival", "Rival X"),
         "deadline_tick": deadline, "decay_per_round": 0.08, "rounds": 0, "issues": ["price", "days"], "your_days_weight": w,
         "days_meaning": BUY if role == "buyer" else SELL, "your_offer": None, "messages": [],
         "rival_offer": None if price is None else {"id": 9, "price": price, "days": days, "tick": 0}}
    if price is not None:
        d["messages"] = [{"tick": 0, "from": d["rival"], "price": price, "days": days}]
    d.update(kw)
    return d


class Params(unittest.TestCase):
    def setUp(self):
        self.saved = dict(dl.PARAMS)
        dl.PARAMS["PLAY_DAYS"] = True

    def tearDown(self):
        dl.PARAMS.clear()
        dl.PARAMS.update(self.saved)


class Signs(Params):
    def test_buyer_pays_for_days_and_seller_collects(self):
        b = duel("buyer", 100, 5.0)
        s = duel("seller", 100, 5.0)
        self.assertEqual(dl.margin(b, 80, 4), 20 - 20)         # (100−80) − 5·4
        self.assertEqual(dl.margin(s, 120, 4), 20 + 20)        # (120−100) + 5·4
        self.assertEqual(dl.margin(b, 80, 0), 20)              # día 0: sin efecto de días
        self.assertEqual(dl.margin(s, 120, 0), 20)
        self.assertEqual((dl.days_sign(b)[0], dl.days_sign(s)[0]), (-1, 1))
        self.assertEqual(dl.days_utility(b, 10), -50.0)
        self.assertEqual(dl.days_utility(s, 10), 50.0)

    def test_price_limit_is_independent_of_days(self):
        b = duel("buyer", 100, 5.0)
        s = duel("seller", 100, 5.0)
        self.assertEqual(dl.margin(b, 110, 0), -10)            # fuera de límite: los días 0 no lo arreglan
        self.assertEqual(dl.margin(s, 90, 10), -10)            # ni los días que cobra un vendedor
        self.assertLess(dl.price_margin(b, 101), 0)

    def test_total_negative_although_price_is_inside_the_limit(self):
        b = duel("buyer", 100, 5.0, price=90, days=10)
        self.assertEqual(dl.price_margin(b, 90), 10)
        self.assertEqual(dl.margin(b, 90, 10), -40)
        self.assertFalse([c for t in range(0, 29) for c in dl.duel_candidates([b], t) if c["type"] == "duel_accept"])
        f = dl.analyze(b, 20)
        self.assertEqual(dl.decide(b, f)[0], "wait")
        self.assertIn("días", dl.decide(b, f)[1])

    def test_meaning_text_overrides_role_and_unverified_formats_never_reward_the_buyer(self):
        odd = duel("buyer", 100, 5.0, days_meaning=SELL)       # el servidor manda sobre el rol
        self.assertEqual(dl.days_sign(odd), (1, "days_meaning"))
        nomean = duel("buyer", 100, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10], days_meaning=None)
        self.assertEqual(dl.days_format(nomean), "por_rol")
        self.assertEqual(dl.days_utility(nomean, 10), -10.0)   # magnitudes sin frase: signo del ROL
        signed = duel("buyer", 100, [0, -1, -2, -3, -4, -5, -6, -7, -8, -9, -10], days_meaning=None)
        self.assertEqual(dl.days_format(signed), "firmado")
        self.assertEqual(dl.days_utility(signed, 10), -10.0)
        self.assertEqual(dl.days_format(duel(w="raro")), "desconocido")

    def test_unknown_format_blocks_acceptance(self):
        d = duel("buyer", 100, {"raro": [1, 2]}, price=50, days=0)
        self.assertFalse(dl.days_known(d))
        self.assertEqual(dl.decide(d, dl.analyze(d, 25))[0], "wait")
        self.assertFalse([c for c in dl.duel_candidates([d], 29) if c["type"] == "duel_accept"])

    def test_missing_day_in_rival_offer_is_valued_at_the_worst_day(self):
        b = duel("buyer", 100, 5.0, price=90, days=None)
        self.assertEqual(dl.analyze(b, 20)["surplus_now"], 10 - 50)


class ServerCalibration(Params):
    def test_expected_result_matches_every_settled_days_duel(self):
        deals = [x for x in FIX if x["status"] == "deal"]
        self.assertGreaterEqual(len(deals), 15)
        for x in deals:
            d = dict(x, status="deal", messages=[])
            got = dl.expected_result(d, x["price"], x["days"])
            self.assertAlmostEqual(got, x["result"], delta=0.15, msg=x["duel"])

    def test_the_old_formula_would_have_been_wrong_for_buyers(self):
        buyers = [x for x in FIX if x["status"] == "deal" and x["role"] == "buyer" and (x["days"] or 0) > 0]
        self.assertTrue(buyers)
        wrong = 0
        for x in buyers:
            pm = x["your_limit"] - x["price"]
            old = (pm + x["your_days_weight"] * x["days"]) * (1 - x["decay_per_round"]) ** x["rounds"]
            wrong += abs(old - x["result"]) > 1
        self.assertGreaterEqual(wrong, len(buyers) - 1)


class LogDuels(Params):
    """Los cuatro duelos que la política anterior aceptó con «excedentes altísimos» y que el servidor liquidó en negativo."""
    CASES = {5842: (187, 7.78, 142), 5694: (66, 4.48, 63), 5762: (135, 5.07, 104), 5919: (98, 5.68, 55)}

    def duel_for(self, i):
        lim, w, p = self.CASES[i]
        return duel("buyer", lim, w, price=p, days=10, duel=i, deadline=1280)

    def test_old_surplus_was_hugely_positive_new_one_is_negative_and_matches_the_server(self):
        server = {x["duel"]: x["result"] for x in FIX}
        old = {5842: 122.8, 5694: 47.8, 5762: 81.7, 5919: 99.8}
        for i, (lim, w, p) in self.CASES.items():
            d = self.duel_for(i)
            self.assertAlmostEqual((lim - p) + w * 10, old[i], delta=0.01)          # lo que la política anterior calculaba
            m = dl.margin(d, p, 10)
            self.assertLess(m, 0, i)
            self.assertAlmostEqual(m, (lim - p) - w * 10, delta=0.01)
            if i in server:
                self.assertLess(server[i], 0, i)                                    # el servidor lo liquidó en negativo

    def test_corrected_policy_never_accepts_them_at_any_tick(self):
        for i in self.CASES:
            d = self.duel_for(i)
            for t in range(1250, 1280):
                self.assertFalse([c for c in dl.duel_candidates([d], t) if c["type"] == "duel_accept"], (i, t))

    def test_when_it_speaks_its_offer_has_a_positive_net_utility(self):
        for i in self.CASES:
            d = self.duel_for(i)
            say = [c for t in (1277, 1278) for c in dl.duel_candidates([d], t) if c["type"] == "duel_say"]
            self.assertTrue(say, i)
            for c in say:
                self.assertEqual(c["days"], 0, i)                                   # el día que maximiza NUESTRA utilidad
                self.assertGreater(dl.margin(d, c["price"], c["days"]), 0, i)
                self.assertGreaterEqual(dl.price_margin(d, c["price"]), 0, i)

    def test_a_price_and_day_that_would_have_been_positive(self):
        d = self.duel_for(5919)                                                     # 43 de margen de precio, 5,68 por día
        self.assertGreater(dl.margin(d, 55, 7), 0)                                  # día 7: 43 − 39,8 = 3,2
        self.assertLess(dl.margin(d, 55, 8), 0)
        self.assertEqual(dl._best_days(d), 0)

    def test_runner_tree_reports_the_reason(self):
        d = self.duel_for(5842)
        step = [x for x in duel_tree.plan([d], 1270) if x["duel"] == 5842][0]
        self.assertNotEqual(step["action"], "accept")
        self.assertEqual(step["facts"]["days_weight_format"], "verificado")
        self.assertLess(step["facts"]["margin"], 0)


class DayChoice(Params):
    def test_best_day_maximises_our_net_utility(self):
        self.assertEqual(dl._best_days(duel("buyer", 100, 3.0)), 0)
        self.assertEqual(dl._best_days(duel("seller", 100, 3.0)), 10)
        flat = duel("buyer", 100, 0.0, price=80, days=6)
        self.assertEqual(dl._best_days(flat), 6)                                    # empate: el del rival

    def test_every_own_offer_uses_our_best_day(self):
        for role, best in (("buyer", 0), ("seller", 10)):
            d = duel(role, 100, 4.0, deadline=12)
            say = [c for t in range(0, 12) for c in dl.duel_candidates([d], t) if c["type"] == "duel_say"]
            self.assertTrue(say)
            self.assertTrue(all(c["days"] == best for c in say), role)


class Logroll(Params):
    def setUp(self):
        super().setUp()
        dl.PARAMS["LOGROLL"] = True

    def test_logroll_never_rewards_a_buyer_for_more_days_without_a_net_gain(self):
        rng = random.Random(3)
        n = 0
        for _ in range(500):
            role = rng.choice(["buyer", "seller"])
            lim = rng.randint(40, 150)
            w = rng.uniform(0.5, 8)
            ref = rng.randint(0, 10)
            price = lim + rng.randint(-40, 40)
            d = duel(role, lim, round(w, 2), price=price, days=rng.randint(0, 10))
            lr = dl.logroll_offer(d, price, ref)
            if not lr:
                continue
            n += 1
            before = dl.price_margin(d, price) + dl.days_utility(d, ref)
            after = dl.margin(d, lr["price"], lr["days"])
            self.assertGreater(after, 0, (role, lim, price, ref, lr))              # total positivo
            self.assertGreater(after, before - 1e-9, (role, lim, price, ref, lr))  # y mejor que lo que dejamos
            self.assertGreaterEqual(dl.price_margin(d, lr["price"]), 0)            # dentro del límite de precio
            self.assertAlmostEqual(lr["gain"], after - before, delta=0.01)
        self.assertGreater(n, 20)

    def test_buyer_concedes_a_day_only_when_the_price_discount_pays_for_it(self):
        d = duel("buyer", 100, 2.0, price=70, days=8)
        d["messages"] = [{"tick": 0, "from": "Rival X", "price": 70, "days": 8}]
        lr = dl.logroll_offer(d, 70, 0)
        if lr:
            self.assertGreater(dl.margin(d, lr["price"], lr["days"]), dl.price_margin(d, 70))

    def test_logroll_offer_is_used_for_counters_and_stays_in_limit(self):
        d = duel("buyer", 100, 2.0, price=95, days=9, deadline=16)
        for t in range(0, 16):
            for c in dl.duel_candidates([d], t):
                if c["type"] == "duel_say":
                    self.assertLessEqual(c["price"], 100)
                    self.assertGreater(dl.margin(d, c["price"], c["days"]), 0)


class Profiles(Params):
    def test_profile_accepts_cannot_override_a_negative_total(self):
        dl.PARAMS["PROFILES"] = True
        for rival in ("Rival Rojo", "Rival Oro", "Rival Noche", "Rival Plata", "Rival Verde"):
            d = duel("buyer", 100, 5.0, price=70, days=10, rival=rival, deadline=20)
            for t in range(5, 20):
                self.assertFalse([c for c in dl.duel_candidates([d], t) if c["type"] == "duel_accept"], (rival, t))

    def test_profile_accepts_when_total_is_positive(self):
        dl.PARAMS["PROFILES"] = True
        d = duel("buyer", 100, 1.0, price=70, days=3, rival="Rival Rojo", deadline=20)
        self.assertTrue([c for c in dl.duel_candidates([d], 5) if c["type"] == "duel_accept"])

    def test_mute_offer_carries_our_best_day(self):
        dl.PARAMS["PROFILES"] = True
        d = duel("buyer", 100, 5.0, deadline=20, start_tick=0)
        c = [x for x in dl.duel_candidates([d], 4) if x["type"] == "duel_say"]
        self.assertTrue(c and c[0]["days"] == 0)
        self.assertGreater(dl.margin(d, c[0]["price"], c[0]["days"]), 0)


class Replay(Params):
    def test_replay_never_captures_a_negative_total(self):
        d = duel("buyer", 100, 5.0, price=90, days=10, deadline=30, status="no_deal")
        d["messages"] = [{"tick": t, "from": "Rival X", "price": 90, "days": 10} for t in range(0, 28)]
        r = dl.replay([d])
        self.assertEqual(r["captured"], 0.0)
        self.assertEqual(r["best"], 0.0)
        self.assertEqual(r["outside_limit"], 0)

    def test_replay_captures_when_total_is_positive(self):
        d = duel("buyer", 100, 1.0, price=80, days=2, deadline=30, status="deal")
        d["messages"] = [{"tick": t, "from": "Rival X", "price": 80, "days": 2} for t in range(0, 28)]
        self.assertAlmostEqual(dl.replay([d])["captured"], 18.0, delta=0.01)

    def test_simulator_marks_negative_totals_as_outside(self):
        import duel_sim
        res = duel_sim.simulate(n=30, seed=4, days=True, variants={"d": {}})
        self.assertEqual(res["d"]["TOTAL"]["outside"], 0)


class AcceptOrder(Params):
    def test_a_mediocre_offer_expiring_now_does_not_beat_a_much_better_one(self):
        good = duel("buyer", 100, 1.0, price=40, days=0, duel=1, deadline=40)
        poor = duel("buyer", 100, 1.0, price=96, days=0, duel=2, deadline=31)
        poor["messages"] = [{"tick": t, "from": "Rival X", "price": 96, "days": 0} for t in range(0, 30)]
        good["messages"] = [{"tick": t, "from": "Rival X", "price": 40, "days": 0} for t in range(0, 30)]
        c = [x for x in dl.duel_candidates([poor, good], 30) if x["type"] == "duel_accept"]
        self.assertEqual([x["duel"] for x in c], [1, 2])                            # 60 de excedente × riesgo > 4 × 1
        self.assertGreater(c[0]["score"], c[1]["score"])

    def test_expiring_offer_goes_first_when_values_are_comparable(self):
        a = duel("buyer", 100, 1.0, price=80, days=0, duel=1, deadline=33)
        b = duel("buyer", 100, 1.0, price=80, days=0, duel=2, deadline=31)
        for d in (a, b):
            d["messages"] = [{"tick": t, "from": "Rival X", "price": 80, "days": 0} for t in range(0, 30)]
        c = [x for x in dl.duel_candidates([a, b], 30) if x["type"] == "duel_accept"]
        self.assertEqual(c[0]["duel"], 2)
        self.assertGreater(dl.vanish_risk(c[0]["facts"]), dl.vanish_risk(c[1]["facts"]))

    def test_one_accept_per_tick_is_kept_by_the_runner_plan(self):
        a = duel("buyer", 100, 1.0, price=80, days=0, duel=1, deadline=31)
        b = duel("buyer", 100, 1.0, price=80, days=0, duel=2, deadline=31)
        steps = duel_tree.plan([a, b], 30)
        self.assertEqual(sorted(s["action"] for s in steps if s["action"] in ("accept", "defer")), ["accept", "defer"])


class Anchors(Params):
    def test_anchor_gets_closer_to_the_limit_near_the_deadline_and_stays_inside_it(self):
        d = duel("buyer", 100, 1.0, deadline=30)
        far, near = dl.realistic_share(d, 5), dl.realistic_share(d, 1)
        self.assertLess(near, far)
        self.assertGreater(near, 0)
        for left in range(1, 6):
            self.assertLessEqual(dl._own_price(d, dl.realistic_share(d, left)), 100)

    def test_history_needs_enough_comparable_cases(self):
        hist = [{"status": "deal", "role": "buyer", "issues": ["price", "days"], "your_limit": 100, "price": 100 - 20 - i}
                for i in range(7)]
        d = duel("buyer", 100, 1.0, deadline=30)
        self.assertIsNone(dl.history_share(hist, "buyer", True))                    # 7 < 8: no se usa
        base = dl.realistic_share(d, 5)
        self.assertEqual(dl.realistic_share(d, 5, hist), base)
        hist.append({"status": "deal", "role": "buyer", "issues": ["price", "days"], "your_limit": 100, "price": 90})
        share = dl.history_share(hist, "buyer", True)
        self.assertAlmostEqual(share, 0.23, delta=0.03)
        self.assertLessEqual(dl.realistic_share(d, 5, hist), base)
        self.assertIsNone(dl.history_share(hist, "seller", True))                   # otro rol: no comparable


class Measurement(Params):
    def test_score_snapshot_reads_duel_points_and_negotiating_and_reports_a_delta(self):
        b = Mock()
        b.me.return_value = {"score": {"duel_points": 14.6, "negotiating": 18.3, "score": 25.8, "rank": 9}}
        first = runner.score_snapshot(b, 1260, None)
        b.me.return_value = {"score": {"duel_points": 17.1, "negotiating": 19.0, "score": 27.0, "rank": 8}}
        second = runner.score_snapshot(b, 1275, first)
        self.assertEqual(second["delta"], {"duel_points": 2.5, "negotiating": 0.7, "score": 1.2})
        self.assertIn("no atribuida", second["note"])
        self.assertIsNone(runner.score_snapshot(Mock(me=Mock(side_effect=RuntimeError)), 1, None))

    def test_accept_is_logged_as_requested_not_scored_and_reconciled_next_tick(self):
        d = duel("buyer", 100, 1.0, price=60, days=2, deadline=12)
        d["messages"] = [{"tick": t, "from": "Rival X", "price": 60, "days": 2} for t in range(0, 8)]
        done = dict(d, status="deal", price=60, days=2, result=36.8, rounds=0)
        api = Mock()
        api.clock.side_effect = [{"tick": 10, "tick_seconds": 10}, {"tick": 11, "tick_seconds": 10}, KeyboardInterrupt()]
        api.duels.side_effect = [{"duels": [d]}, {"duels": []}, {"duels": []}, {"duels": [done]}, {"duels": [done]}]
        api.me.return_value = {"score": {"duel_points": 10.0, "negotiating": 5.0, "score": 20.0}}
        logs = []
        with patch.object(runner, "log", side_effect=logs.append), patch.object(runner.time, "sleep"), \
                patch.object(runner, "LOG", runner.Path("/nonexistent/x.jsonl")):
            runner.run(api, True, track=True)
        api.duel_accept.assert_called_once_with(1)
        req = [x for x in logs if x.get("event") == "accept_requested"]
        self.assertEqual((len(req), req[0]["sent"], req[0]["scored"]), (1, True, False))
        self.assertAlmostEqual(req[0]["prediction"]["expected_result"], 38.0 - 2.0 * 1.0 + 0.0, delta=3)
        rec = [x for x in logs if x.get("event") == "reconciled"]
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["status"], rec[0]["price"], rec[0]["days"], rec[0]["role"]), ("deal", 60, 2, "buyer"))
        self.assertAlmostEqual(rec[0]["predicted_result"], 38.0, delta=0.01)
        self.assertAlmostEqual(rec[0]["prediction_error"], 36.8 - 38.0, delta=0.01)
        self.assertTrue([x for x in logs if x.get("event") == "score_snapshot"])


class OracleRoute(unittest.TestCase):
    def test_oracle_view_signs_the_server_weight_by_role(self):
        import oracle_duels as od
        buyer = od.DuelView.from_api({"duel": 1, "role": "buyer", "your_limit": 100, "your_days_weight": 4.48, "days_meaning": BUY,
                                      "issues": ["price", "days"]})
        seller = od.DuelView.from_api({"duel": 2, "role": "seller", "your_limit": 100, "your_days_weight": 4.39,
                                       "days_meaning": SELL, "issues": ["price", "days"]})
        self.assertLess(buyer.days_weight, 0)             # comprador: quiere pronto (cada día cuesta)
        self.assertGreater(seller.days_weight, 0)
        self.assertEqual(od.preferred_day(buyer.days_weight), 0)
        self.assertEqual(od.preferred_day(seller.days_weight), 10)


if __name__ == "__main__":
    unittest.main()
