"""Pruebas offline de la escalera calibrada (--ladder-calibrated): perfiles medidos, decisiones con paciencia hasta
final:true y anti-farol, plan de 3 tratos por vendedor, integración en el coordinador y simulador de vendedores.
Sin red ni claves: instantáneas sintéticas."""
import copy
import json
import os
import random
import tempfile
import unittest

import coordinator as co
import dealer_sim as ds
import ladder_calibrated as lcal
import negotiation as neg
from test_coordinator import dealer_thread, her, ours
from test_ladder_plus import PILAR_T, ask, cands, led0, pil, pilar_thread, sal_snap
from test_trading import TEAM, asset, offer

PROF = lcal.PROFILES


def chato_her(oid, tick, price, status="open", final=False):
    m = her(oid, tick, price, status, final)
    return dict(m, sender="chato", offer=dict(m["offer"], maker="chato"))


def buy_st(msgs, now, dealer="abuela"):
    t = dict(dealer_thread(7, msgs), **{"with": dealer})
    return neg.state_from_thread(t, dealer, now, neg.Config())


def sell_st(msgs, now):
    return neg.state_from_thread(pilar_thread(msgs), "pilar", now, neg.Config(), side="sell")


def ladder_snap(assets, cash=200, tick=50):
    """sal_snap con los tres vendedores y sus menús reales (forma de /api/dealers)."""
    s = sal_snap(assets, cash=cash, tick=tick)
    s["dealers"] = {
        "abuela": {"open_to_all": True, "menu": {"buys": [{"rarity": "common", "sets": "released"},
                                                          {"rarity": "uncommon", "sets": "released"}],
                                                 "sells": [{"rarity": "common", "list_price": 10}]}},
        "chato": {"open_to_all": True, "menu": {"buys": [{"rarity": "uncommon", "sets": "released"},
                                                         {"rarity": "rare", "sets": "released"}],
                                                "sells": [{"rarity": "rare", "list_price": 77}]}},
        "pilar": {"open_to_all": True, "menu": {"buys": [{"rarity": "uncommon", "sets": ["SAL", "RET"]},
                                                         {"rarity": "uncommon", "sets": "released"},
                                                         {"rarity": "rare", "sets": "released"}]}}}
    s["me"]["unlocked"] = ["abuela", "chato", "pilar"]
    return s


# ------------------------------------------------------------------ perfiles medidos

class Profiles(unittest.TestCase):
    def test_targets_are_the_best_observed_deals(self):
        t = lambda k, o: PROF[k].target_price(o, k.split("|")[1])
        self.assertEqual(t("abuela|buy|common", 12), 8, "t03: 8")
        self.assertEqual(t("abuela|buy|uncommon", 29), 20, "t07: 20")
        self.assertEqual(t("chato|buy|rare", 97), 82, "t04 81, t09 84")
        self.assertEqual(t("chato|buy|uncommon", 33), 26, "t13: final 27, contraoferta 26 aceptada")
        self.assertEqual(t("abuela|sell|common", 5), 6)
        self.assertEqual(t("chato|sell|uncommon", 13), 15, "t02: 15")
        self.assertEqual(t("pilar|sell|uncommon", 16), 21, "t09: 21")
        self.assertEqual(t("pilar|sell|uncommon|fav", 22), 25, "t04/t10/t12: 25")

    def test_capture_is_the_share_of_the_range(self):
        p = PROF["chato|buy|rare"]
        self.assertEqual(p.capture(97, 97, "buy"), 0.0, "a su apertura no puntúa")
        self.assertEqual(p.capture(97, None, "buy"), 0.0)
        self.assertGreater(p.capture(97, 81, "buy"), 0.9)
        self.assertLess(p.capture(97, 96, "buy"), 0.1, "nuestro 96 de la ronda pasada")
        self.assertEqual(PROF["abuela|sell|common"].capture(5, 6, "sell"), 1.0)

    def test_lookup_fallbacks(self):
        self.assertEqual(lcal.profile_for(PROF, "pilar", "sell", "uncommon", True)[0], "pilar|sell|uncommon|fav")
        self.assertEqual(lcal.profile_for(PROF, "pilar", "sell", "uncommon", False)[0], "pilar|sell|uncommon")
        self.assertEqual(lcal.profile_for(PROF, "chato", "buy", "XXX-01")[0], "chato|buy|*")
        self.assertEqual(lcal.profile_for(PROF, "picaros", "sell", "rare")[0], "*|sell|*")

    def test_fav_sets_from_the_menu(self):
        row = {"menu": {"buys": [{"rarity": "uncommon", "sets": ["SAL", "RET"]}, {"rarity": "rare", "sets": "released"}]}}
        self.assertEqual(lcal.fav_sets(row), frozenset({"SAL", "RET"}))
        self.assertTrue(lcal.is_fav("pilar", "RET", row))
        self.assertFalse(lcal.is_fav("pilar", "LAV", row))
        self.assertFalse(lcal.is_fav("chato", "SAL", row), "solo Pilar tiene favoritos")

    def test_profile_file_overrides_and_rejects_typos(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w") as fh:
                json.dump({"chato|buy|rare": {"step": 4}, "picaros|sell|*": {"first": 2.0}}, fh)
            ps = lcal.load_profiles(path)
            self.assertEqual(ps["chato|buy|rare"].step, 4)
            self.assertEqual(ps["chato|buy|rare"].target, PROF["chato|buy|rare"].target, "el resto, medido")
            self.assertEqual(ps["picaros|sell|*"].first, 2.0)
            self.assertEqual(PROF["chato|buy|rare"].step, 5, "los perfiles medidos no se tocan")
            with open(path, "w") as fh:
                json.dump({"chato|buy|rare": {"stpe": 4}}, fh)
            with self.assertRaises(ValueError):
                lcal.load_profiles(path)


# ------------------------------------------------------------------ compra

class DecideBuy(unittest.TestCase):
    p = PROF["chato|buy|rare"]

    def d(self, msgs, now, ceiling=95, left=20):
        return lcal.decide_buy(buy_st(msgs, now, "chato"), self.p, ceiling, left, now)

    def test_extreme_opening_then_fixed_steps(self):
        d = self.d([chato_her(1, 41, 97)], 41)
        self.assertEqual((d.action, d.price), ("counter", 49), "0,50 × 97 (t04 abrió 55)")
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 49), chato_her(3, 42, 96)]
        self.assertEqual(self.d(msgs, 42).price, 54, "paso de 5: el Chato copia nuestro paso")

    def test_never_accepts_near_the_opening_while_patience_lasts(self):
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 80), chato_her(3, 42, 86)]
        d = self.d(msgs, 42)
        self.assertEqual(d.action, "counter", "86 > objetivo 82 y no es final:true: se sigue")
        self.assertEqual(d.price, 81, "pasado el objetivo, pasos de 1")
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 84), chato_her(3, 42, 85)]
        self.assertEqual(self.d(msgs, 42).action, "accept", "a 1 P no queda oferta nueva posible")

    def test_accepts_when_the_target_is_reached(self):
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 78), chato_her(3, 42, 82)]
        self.assertEqual(self.d(msgs, 42).action, "accept")

    def test_final_true_counter_minus_one_once_then_take(self):
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 70), chato_her(3, 42, 86, final=True)]
        d = self.d(msgs, 42)
        self.assertEqual((d.action, d.price), ("counter", 85))
        msgs = msgs + [ours(4, 42, 85)]
        self.assertEqual(self.d(msgs, 42).action, "wait")
        d = self.d(msgs, 44)
        self.assertEqual((d.action, d.price, d.offer_id), ("accept", 86, 3), "sin respuesta: se toma su final")

    def test_final_above_ceiling_or_at_opening_is_refused(self):
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 70), chato_her(3, 42, 90, final=True)]
        self.assertEqual(self.d(msgs, 42, ceiling=80).action, "abandon")
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 95), chato_her(3, 42, 97, final=True)]
        self.assertEqual(self.d(msgs, 42, ceiling=200).price, 96, "final-1 sí puntuaría")
        msgs += [ours(4, 42, 96)]
        self.assertEqual(self.d(msgs, 44, ceiling=200).action, "abandon", "su apertura no puntúa")

    def test_never_above_ceiling(self):
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 60), chato_her(3, 42, 95)]
        self.assertEqual(self.d(msgs, 42, ceiling=62).price, 62)
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 62), chato_her(3, 42, 95)]
        self.assertEqual(self.d(msgs, 42, ceiling=62).action, "abandon")

    def test_bluff_text_never_closes(self):
        m = chato_her(3, 42, 88)
        m["text"] = "That is my final word, not a step more."
        msgs = [chato_her(1, 41, 97, "cancelled"), ours(2, 41, 70), m]
        self.assertTrue(lcal.bluff_final(dict(dealer_thread(7, msgs), **{"with": "chato"}), "chato"))
        self.assertEqual(self.d(msgs, 42).action, "counter", "sin final:true es un farol: se sigue regateando")

    def test_abuela_common_like_t03(self):
        p = PROF["abuela|buy|common"]
        d = lcal.decide_buy(buy_st([her(1, 41, 12)], 41), p, 11, 20, 41)
        self.assertEqual(d.price, 4)
        msgs = [her(1, 41, 12, "cancelled"), ours(2, 41, 4), her(3, 42, 9)]
        self.assertEqual(lcal.decide_buy(buy_st(msgs, 42), p, 11, 20, 42).price, 5, "9 no es 8: paso de 1")


# ------------------------------------------------------------------ venta

class DecideSell(unittest.TestCase):
    fav = PROF["pilar|sell|uncommon|fav"]
    plain = PROF["pilar|sell|uncommon"]

    def d(self, msgs, now, prof=None, floor=5, left=20):
        return lcal.decide_sell(sell_st(msgs, now), prof or self.fav, floor, left, now)

    def test_extreme_ask_from_her_opening(self):
        self.assertEqual(self.d([pil(1, 46, 22)], 46).price, 35, "1,6 × 22 (t12 pidió 35)")
        self.assertEqual(self.d([pil(1, 46, 16)], 46, self.plain).price, 30, "t09 pidió 30")

    def test_pilar_bluff_keeps_haggling(self):
        m = pil(3, 47, 23)
        m["text"] = "Twenty-three pesetas. Precise, fair, and final in spirit — my final courtesy."
        msgs = [pil(1, 46, 22, "cancelled"), ask(2, 46, 29), m]
        self.assertTrue(lcal.bluff_final(pilar_thread(msgs), "pilar"))
        d = self.d(msgs, 47)
        self.assertEqual((d.action, d.price), ("counter", 26), "t10: tras ese «final» subió a 25")

    def test_non_final_bid_only_at_target(self):
        msgs = [pil(1, 46, 22, "cancelled"), ask(2, 46, 26), pil(3, 47, 24)]
        self.assertEqual(self.d(msgs, 47).action, "counter", "24 < 25: aún no")
        msgs = [pil(1, 46, 22, "cancelled"), ask(2, 46, 27), pil(3, 47, 25)]
        self.assertEqual(self.d(msgs, 47).action, "accept")

    def test_final_true_is_taken_only_above_private_value(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 30), pil(3, 47, 19, final=True)]
        self.assertEqual(self.d(msgs, 47, self.plain).action, "accept")
        self.assertEqual(self.d(msgs, 47, self.plain, floor=20).action, "abandon", "nunca bajo el valor privado")

    def test_never_asks_below_floor_or_at_her_opening(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 19), pil(3, 47, 16)]
        d = self.d(msgs, 47, self.plain, floor=18)
        self.assertEqual((d.action, d.price), ("counter", 18))
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 17), pil(3, 47, 16)]
        self.assertEqual(self.d(msgs, 47, self.plain, floor=1).action, "abandon", "16 = su apertura: no puntúa")

    def test_out_of_rounds_takes_a_bid_above_floor(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 30), pil(3, 47, 18)]
        self.assertEqual(self.d(msgs, 47, self.plain, left=0).action, "accept")

    def test_abuela_common_dup_at_six(self):
        p = PROF["abuela|sell|common"]
        msgs = [pil(1, 46, 5, "cancelled"), ask(2, 46, 7), pil(3, 47, 6)]
        self.assertEqual(lcal.decide_sell(sell_st(msgs, 47), p, 2, 20, 47).action, "accept", "6 = su máximo")


# ------------------------------------------------------------------ plan

class Plan(unittest.TestCase):
    def opt(self, dealer, ref, mode="sell", price=20, cash=0, du=5.0, capture=1.0, asset=None):
        return lcal.Option(dealer, mode, ref, asset if asset is not None else hash(ref) % 1000, price, cash, du,
                           capture, f"{dealer}|{mode}")

    def test_shares_cards_so_every_level_gets_deals(self):
        # SAL-07 vale para Pilar o para el Chato; LAV-06/LAV-08 solo para Pilar: el óptimo da SAL-07 al Chato.
        opts = [self.opt("pilar", "SAL-06"), self.opt("pilar", "SAL-07"), self.opt("chato", "SAL-07", du=2.5),
                self.opt("pilar", "LAV-06", du=2), self.opt("pilar", "LAV-08", du=2)]
        p = lcal.plan(opts, {"abuela": 3}, budget=0)
        got = sorted((o.dealer, o.ref) for o in p["picks"])
        self.assertIn(("chato", "SAL-07"), got)
        self.assertEqual(sum(1 for o in p["picks"] if o.dealer == "pilar"), 3)
        self.assertEqual(p["gain"], 3 * 3 + 2)

    def test_respects_filled_slots_and_budget(self):
        opts = [self.opt("abuela", "RET-05", "buy", 8, 8), self.opt("chato", "RET-10", "buy", 82, 82, du=30),
                self.opt("abuela", "LAT-05", du=3)]
        p = lcal.plan(opts, {"abuela": 2}, budget=50)
        self.assertEqual([(o.dealer, o.ref) for o in p["picks"]], [("abuela", "LAT-05")], "un hueco y 82 > 50")
        p = lcal.plan(opts, {}, budget=100)
        self.assertEqual(p["cash"], 90)
        self.assertEqual(lcal.plan(opts, {}, budget=0)["cash"], 0)

    def test_never_plans_value_losing_deals(self):
        p = lcal.plan([self.opt("pilar", "RET-07", du=-15)], {}, budget=0)
        self.assertEqual(p["picks"], [])


# ------------------------------------------------------------------ coordinador

class Coordinator(unittest.TestCase):
    def test_flag_off_is_neutral(self):
        s = ladder_snap([asset(1, "LAT-01"), asset(2, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")])
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 22)]), dealer_thread(7, [her(1, 41, 12)])]
        led = led0(threads=[7, PILAR_T])
        strip = lambda cs: [{k: v for k, v in c.items() if k != "opp"} for c in cs]
        base, pl0 = cands(copy.deepcopy(s), copy.deepcopy(led))
        neutral, pl1 = cands(copy.deepcopy(s), copy.deepcopy(led), ladder_calibrated=False, ladder_profile=None,
                             ladder_budget=None)
        self.assertEqual(strip(base), strip(neutral))
        self.assertNotIn("ladder_cal", pl1)

    def test_buy_thread_uses_the_calibrated_profile(self):
        s = ladder_snap([asset(1, "LAT-01"), asset(2, "LAT-02")])
        s["threads"]["open"] = [dealer_thread(7, [her(1, 41, 12)])]
        s["clock"]["tick"] = 41
        c = next(c for c in cands(s, led0(threads=[7]), ladder_calibrated=True)[0] if c["type"] == "dealer_counter")
        self.assertEqual(c["price"], 4, "0,34 × 12: apertura extrema (t03)")
        self.assertTrue(any("calibrada abuela|buy|common" in n for n in c["notes"]))

    def test_sell_threads_with_abuela_and_pilar_bluff(self):
        s = ladder_snap([asset(1, "LAT-01"), asset(2, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")])
        m = pil(3, 47, 23)
        m["text"] = "my final courtesy"
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 22, "cancelled"), ask(2, 46, 30), m], aid=12)]
        s["clock"]["tick"] = 47
        out, pl = cands(s, led0(threads=[PILAR_T]), ladder_calibrated=True)
        c = next(c for c in out if c["type"] == "dealer_sell_counter")
        self.assertEqual((c["price"], c["blockers"]), (27, []))
        self.assertTrue(any("FAROL" in n for n in c["notes"]))

    def test_sell_deals_fill_the_ladder_and_the_plan(self):
        s = ladder_snap([asset(1, "LAT-01"), asset(2, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")])
        # una venta de duplicado a la Abuela por encima de su puja de apertura cuenta como trato negociado
        sold = dict(pilar_thread([pil(1, 40, 5, "cancelled"), ask(2, 40, 7), pil(3, 41, 6, "settled")], status="deal",
                                 tid=90), **{"with": "abuela"})
        for m in sold["messages"]:
            if m.get("offer", {}).get("maker") == "pilar":
                m["sender"], m["offer"]["maker"] = "abuela", "abuela"
        s["threads"]["deal"] = [sold]
        out, pl = cands(s, ladder_calibrated=True)
        self.assertEqual(pl["ladder"]["abuela"], 1, "qualifying_sell_deals: venta a 6 sobre su apertura de 5")
        self.assertEqual(pl["ladder_cal"]["filled"]["abuela"], 1)
        picks = " | ".join(pl["ladder_cal"]["picks"])
        self.assertIn("pilar: vender SAL-06 a ~25 P", picks, "SAL es favorito de Pilar: 25, no 15 del Chato")
        self.assertIn("abuela: vender LAT-01 a ~6 P", picks)
        ab = [c for c in out if c["type"] == "dealer_sell_open" and c["dealer"] == "abuela"]
        self.assertTrue(ab and not ab[0]["blockers"])
        self.assertEqual(ab[0]["asset"], 2)
        pil_open = next(c for c in out if c["type"] == "dealer_sell_open" and c["dealer"] == "pilar")
        self.assertEqual((pil_open["ref"], pil_open["price"]), ("card:SAL-06", 25))
        self.assertEqual(pl["pilar"]["next"]["first_ask"], 35)
        self.assertTrue(any("ESCALERA CALIBRADA" in x for x in co.ladder_cal_lines(pl["ladder_cal"])))

    def test_last_copies_and_complete_pages_stay_out_of_the_plan(self):
        page = [asset(1, "SAL-01"), asset(2, "SAL-02"), asset(11, "SAL-06"), asset(5, "SAL-09")]
        _, pl = cands(ladder_snap(page), ladder_calibrated=True, pilar_sell="SAL", pilar_last_copy=True)
        self.assertFalse([x for x in pl["ladder_cal"]["picks"] if "vender" in x], "página SAL completa: intocable")
        _, pl = cands(ladder_snap([asset(1, "LAT-01"), asset(11, "SAL-06")]), ladder_calibrated=True)
        self.assertFalse([x for x in pl["ladder_cal"]["picks"] if "vender" in x], "últimas copias sin permiso")

    def test_last_copy_permissions_per_dealer(self):
        def picks(**kw):
            s = ladder_snap([asset(1, "LAT-01"), asset(11, "SAL-06")])
            s["me"]["affinity"]["SAL"] = 0.5  # SAL-06 vale 10 P: Pilar (25) y el Chato (15) pagan más
            return [x for x in cands(s, ladder_calibrated=True, **kw)[1]["ladder_cal"]["picks"] if "vender" in x]
        self.assertTrue(any(x.startswith("pilar: vender SAL-06") for x in picks(pilar_sell="SAL", pilar_last_copy=True)))
        self.assertFalse(picks(pilar_sell="LAT", pilar_last_copy=True), "Pilar solo los barrios de --pilar-sell; "
                                                                        "--pilar-last-copy no vale para el Chato")
        self.assertTrue(any(x.startswith("chato: vender SAL-06")
                            for x in picks(pilar_sell="LAT", allow_last_copy="SAL-06")))

    def test_planned_buy_gets_priority_and_budget_flag(self):
        s = ladder_snap([asset(1, "LAT-01"), asset(2, "LAT-02")], cash=300)
        out, pl = cands(s, ladder_calibrated=True, reserve=0)
        self.assertIn("abuela: comprar LAT-03 a ~8 P", " | ".join(pl["ladder_cal"]["picks"]))
        best = max((c for c in out if c["type"] == "dealer_open"), key=lambda c: c["score"])
        self.assertEqual((best["dealer"], best["ref"]), ("abuela", "card:LAT-03"))
        _, pl = cands(s, ladder_calibrated=True, reserve=0, ladder_budget=0)
        self.assertFalse([x for x in pl["ladder_cal"]["picks"] if "comprar" in x])


# ------------------------------------------------------------------ simulador

class Simulator(unittest.TestCase):
    def test_calibrated_beats_main_where_it_matters(self):
        res = ds.compare(n=300, seed=5)
        for k in ("chato|buy|rare", "chato|buy|uncommon", "chato|sell|uncommon"):
            self.assertGreater(res[k]["después"]["captura"], res[k]["antes"]["captura"] + 0.2, k)
        for k, v in res.items():
            self.assertGreaterEqual(v["después"]["captura"], v["antes"]["captura"] - 0.05, k)
            self.assertGreaterEqual(v["después"]["captura"], 0.6, k)
        self.assertLess(res["chato|buy|rare"]["sin flags"]["captura"], 0.1, "como nuestro 96 frente a 97")

    def test_bluffs_do_not_stop_the_calibrated_policy(self):
        spec = ds.Spec(**{**ds.SPECS["pilar|sell|uncommon"].__dict__, "p_bluff": 1.0})
        rng = random.Random(1)
        res = [ds.run(ds.SimDealer("pilar", spec, rng), ds.after_policy("pilar|sell|uncommon", 0, 1))
               for _ in range(50)]
        self.assertTrue(all(r["status"] == "deal" for r in res))
        self.assertTrue(all(r["price"] > 16 for r in res), "nunca a su apertura")

    def test_replay_reproduces_a_known_thread(self):
        rows = [{"dealer": "chato", "mode": "buy", "kind": "uncommon", "fav": False, "dealer_open": 33,
                 "team_prices": [9, 11, 14, 17, 20, 23, 25, 26], "deal": 26, "final": 27}]
        out = ds.replay(rows, reps=10)
        self.assertLessEqual(out["chato|buy|uncommon"]["error_medio_P"], 2)


if __name__ == "__main__":
    unittest.main()
