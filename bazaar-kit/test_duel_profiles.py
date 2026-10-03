"""Pruebas offline de los perfiles de bots de la casa (--profiles), el logrolling de días (--logroll) y su medición en
duel_sim.py --house. Sin red, sin claves.

    python3 -m unittest test_duel_profiles
"""
import json
import os
import random
import unittest
from unittest.mock import Mock, patch

import duel_runner as runner
import duel_sim as sim
import duel_tree
import duels as dl

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "duels_fixture_practice.json")))


def duel(rival="Rival Plata", role="buyer", limit=100, prices=(), deadline=16, days=None, weight=None, **kw):
    """Duelo vivo con el rival publicando `prices` en los ticks 0, 1, 2…"""
    msgs = [{"tick": i, "from": rival, "price": p, "days": days} for i, p in enumerate(prices)]
    d = {"duel": kw.pop("duel", 1), "status": "live", "role": role, "your_limit": limit, "rival": rival,
         "deadline_tick": deadline, "decay_per_round": 0.08, "messages": msgs, "your_offer": None,
         "issues": ["price", "days"] if days is not None or weight is not None else ["price"],
         "your_days_weight": weight,
         "rival_offer": {"price": prices[-1], "days": days} if prices else None}
    d.update(kw)
    return d


def kinds(cands):
    return [c["type"] for c in cands]


class Params(unittest.TestCase):
    def setUp(self):
        self.saved = dict(dl.PARAMS)

    def tearDown(self):
        dl.PARAMS.clear()
        dl.PARAMS.update(self.saved)

    def flags(self, **kw):
        dl.PARAMS.update(kw)


class DefaultsUnchanged(Params):
    def test_flags_off_by_default(self):
        self.assertFalse(dl.PARAMS["PROFILES"])
        self.assertFalse(dl.PARAMS["LOGROLL"])

    def test_flags_off_is_original_policy_on_practice(self):
        base = dl.replay(FIXTURE)
        self.flags(PROFILES=False, LOGROLL=True)            # logroll sin días no toca nada
        self.assertEqual(dl.replay(FIXTURE), base)

    def test_generic_rivals_untouched_except_mutes(self):
        # rivales sin alias conocido ⇒ política original; solo cambia el trato a los mudos
        res = sim.simulate(n=40, seed=2, variants={"a": {}, "b": {"PROFILES": True}}, mix=["cedente", "firme"])
        self.assertEqual(res["a"]["TOTAL"], res["b"]["TOTAL"])


class Profiles(Params):
    def setUp(self):
        super().setUp()
        self.flags(PROFILES=True)

    def test_name_lookup(self):
        self.assertEqual(dl.rival_profile({"rival": "Rival Plata"}), "cede")
        self.assertEqual(dl.rival_profile({"rival": "Rival Rojo"}), "empeora")
        self.assertEqual(dl.rival_profile({"rival": "Rival Luna"}), "salto")
        self.assertEqual(dl.rival_profile({"rival": "Rival Sol"}), "desconocido")
        self.assertEqual(dl.rival_profile({"rival": "R"}), "desconocido")

    def test_worsening_rival_is_accepted_at_once(self):
        d = duel("Rival Rojo", prices=[80])
        self.assertEqual(kinds(dl.duel_candidates([d], 0)), ["duel_accept"])
        self.flags(PROFILES=False)
        self.assertEqual(kinds(dl.duel_candidates([d], 0)), [])         # la política original espera

    def test_steady_conceder_is_not_accepted_on_a_stall(self):
        d = duel("Rival Plata", prices=[90, 90, 90])
        self.assertEqual(kinds(dl.duel_candidates([d], 2)), [])
        self.assertEqual(kinds(dl.duel_candidates([d], 15)), ["duel_accept"])   # cierre seguro
        self.flags(PROFILES=False)
        # Con perfiles desactivados aplica la política adaptativa local: sondea al rival estancado.
        self.assertEqual(kinds(dl.duel_candidates([d], 2)), ["duel_say"])

    def test_jump_then_accept(self):
        self.assertEqual(kinds(dl.duel_candidates([duel("Rival Oro", prices=[95, 95, 95])], 2)), [])
        self.assertEqual(kinds(dl.duel_candidates([duel("Rival Oro", prices=[95, 95, 75])], 2)), ["duel_accept"])

    def test_mixed_rival_accepted_when_it_worsens(self):
        self.assertEqual(kinds(dl.duel_candidates([duel("Rival Noche", prices=[80, 82])], 1)), ["duel_accept"])
        self.assertEqual(kinds(dl.duel_candidates([duel("Rival Noche", prices=[82, 80])], 1)), [])

    def test_never_accepts_outside_limit(self):
        self.flags(PLAY_DAYS=True, LOGROLL=True)
        for c in sim.HOUSE.values():
            for w in (None, -3):
                d = duel(c.alias, prices=[130, 140], days=None if w is None else 5, weight=w)
                for t in range(16):
                    self.assertNotIn("duel_accept", kinds(dl.duel_candidates([d], t)))

    def test_mute_rival_gets_early_stepped_offers(self):
        d = duel("Rival Sol", role="seller", limit=100)
        self.assertEqual(dl.duel_candidates([d], 2), [])
        self.assertEqual([(c["type"], c["price"]) for c in dl.duel_candidates([d], 3)], [("duel_say", 130)])
        d["messages"] = [{"tick": 3, "from": "us", "price": 130}]
        d["your_offer"] = {"price": 130}
        self.assertEqual(dl.duel_candidates([d], 5), [])                 # MUTE_GAP
        self.assertEqual(dl.duel_candidates([d], 6)[0]["price"], 120)
        d["messages"] += [{"tick": 6, "from": "us", "price": 120}, {"tick": 9, "from": "us", "price": 110}]
        self.assertEqual(dl.duel_candidates([d], 13), [])                # MUTE_MAX
        self.flags(PROFILES=False)
        self.assertEqual(dl.duel_candidates([duel("Rival Sol")], 3), [])  # sin flag: espera a SPEAK_AT

    def test_only_one_accept_per_tick_in_plan(self):
        wave = [duel("Rival Rojo", prices=[80], duel=i) for i in range(3)]
        actions = [s["action"] for s in duel_tree.plan(wave, 0)]
        self.assertEqual((actions.count("accept"), actions.count("defer")), (1, 2))
        self.assertEqual(duel_tree.plan(wave, 0)[0]["facts"]["rival_profile"], "empeora")

    def test_days_duels_still_need_days_flag(self):
        d = duel("Rival Rojo", prices=[80], days=3, weight=-1)
        self.assertEqual(dl.duel_candidates([d], 0), [])
        self.flags(PLAY_DAYS=True)
        self.assertEqual(kinds(dl.duel_candidates([d], 0)), ["duel_accept"])


class Logroll(Params):
    def setUp(self):
        super().setUp()
        self.flags(PLAY_DAYS=True, LOGROLL=True)

    def test_rival_day_inferred_from_offers(self):
        d = duel(prices=[90, 88, 86], days=2)
        d["messages"][-1]["days"] = 4
        self.assertEqual(dl.rival_days(d), 2)
        self.assertIsNone(dl.rival_days(duel(prices=[90])))

    def test_concedes_cheap_day_for_price(self):
        d = duel("Rival Plata", prices=[150, 149], days=8, weight=[-10] + [0] * 10)   # solo el día 0 nos cuesta
        lr = dl.logroll_offer(d, 70, 0)
        self.assertEqual(lr["days"], 8)
        self.assertLess(lr["price"], 80)
        self.assertGreater(lr["gain"], 0)

    def test_uses_days_table_formats_and_falls_back_to_price(self):
        for w in ([0, -1, -2, -3, -4, -5, -6, -7, -8, -9, -10], {str(k): -k for k in range(11)}, -1, {"per_day": -1}):
            d = duel(prices=[90], days=6, weight=w)
            self.assertEqual(dl.days_table(d)[10], -10.0, w)
        self.assertIsNone(dl.logroll_offer(duel(prices=[90], days=3, weight="raro"), 90, 3))
        self.assertIsNone(dl.logroll_offer(duel(prices=[90], weight=[0] * 11), 90, 5))   # rival sin día

    def test_unknown_format_plays_price_only(self):
        self.flags(PROFILES=True)
        d = duel("Rival Rojo", prices=[80], days=7, weight={"raro": [1, 2]})
        c = dl.duel_candidates([d], 0)
        self.assertEqual((kinds(c), c[0]["du"]), (["duel_accept"], 20))
        self.assertEqual(duel_tree.plan([d], 0)[0]["facts"]["days_weight_format"], "desconocido_solo_precio")

    def test_never_outside_limit(self):
        rng = random.Random(1)
        for _ in range(400):
            role = rng.choice(["buyer", "seller"])
            lim = rng.randint(50, 150)
            d = duel(role=role, limit=lim, prices=[lim + rng.randint(-40, 40)], days=rng.randint(0, 10),
                     weight=[rng.uniform(-8, 8) for _ in range(11)])
            lr = dl.logroll_offer(d, lim + rng.randint(-30, 30), rng.randint(0, 10))
            if lr:
                self.assertTrue(lr["price"] <= lim if role == "buyer" else lr["price"] >= lim, (lr, role, lim))

    def test_every_offer_carries_days(self):
        self.flags(PROFILES=True)
        for w in (-1, [0] * 11, "x"):
            d = duel("Rival Sol", weight=w)
            for t in range(16):
                for c in dl.duel_candidates([d], t):
                    if c["type"] == "duel_say":
                        self.assertIn(c.get("days"), range(11))


class HouseSimulator(Params):
    def tearDown(self):
        super().tearDown()
        sim.TICKS, sim.DECAY, sim.WAVE = 12, 0.08, 6

    def test_never_outside_limit_and_restores_params(self):
        sim.configure("II")
        for days in (False, True):
            res = sim.simulate(n=20, seed=3, days=days, house=True)
            for v, row in res.items():
                self.assertEqual(row["TOTAL"]["outside"], 0, (days, v))
        self.assertEqual(dl.PARAMS, self.saved)

    def test_profiles_beat_ladder_on_house_bots(self):
        for session in ("II", "III"):
            sim.configure(session)
            res = sim.simulate(n=80, seed=5, days=True, house=True)
            self.assertGreater(res["--ladder --profiles"]["TOTAL"]["capture"], res["--ladder"]["TOTAL"]["capture"])
            self.assertGreater(res["--profiles"]["verde"]["capture"], res["por defecto"]["verde"]["capture"])
            self.assertGreaterEqual(res["--profiles"]["rojo"]["capture"], res["por defecto"]["rojo"]["capture"])

    def test_house_bots_follow_duelos_i(self):
        rng = random.Random(4)
        rojo = sim.Rojo("seller", 100.0, rng)
        prices = [rojo.act(t, None, False) for t in range(10)]
        self.assertGreater(prices[-1], prices[0])                      # Rojo vendedor empeora
        plata = sim.Plata("seller", 100.0, rng)
        p = [plata.act(t, None, False) for t in range(6)]
        self.assertTrue(all(1 <= a - b <= 3.5 for a, b in zip(p, p[1:])))


class RunnerFlags(Params):
    def test_flags_set_params_and_coexist(self):
        with patch.object(runner, "run"), patch.object(runner, "log"), \
             patch.dict(os.environ, {"BAZAAR_KEY": "test-key"}), \
             patch("sys.argv", ["duel_runner.py", "--days", "--ladder", "--profiles", "--logroll"]):
            runner.main()
        self.assertTrue(dl.PARAMS["PLAY_DAYS"] and dl.PARAMS["PROFILES"] and dl.PARAMS["LOGROLL"])
        self.assertEqual(dl.PARAMS["LADDER"], runner.LADDER)

    def test_flags_absent_stay_off(self):
        with patch.object(runner, "run"), patch.object(runner, "log"), \
             patch.dict(os.environ, {"BAZAAR_KEY": "test-key"}), patch("sys.argv", ["duel_runner.py", "--days"]):
            runner.main()
        self.assertFalse(dl.PARAMS["PROFILES"] or dl.PARAMS["LOGROLL"])

    def test_mute_offer_sent_with_days(self):
        self.flags(PLAY_DAYS=True, PROFILES=True)
        api = Mock()
        api.clock.side_effect = [{"tick": 3}, KeyboardInterrupt()]
        api.duels.return_value = {"duels": [duel("Rival Sol", weight=-1)]}
        with patch.object(runner, "log"):
            runner.run(api, True, None)
        api.duel_accept.assert_not_called()
        self.assertEqual(api.duel_say.call_args.kwargs["days"], 0)     # peso −1/día ⇒ nuestro mejor día es el 0


if __name__ == "__main__":
    unittest.main()
