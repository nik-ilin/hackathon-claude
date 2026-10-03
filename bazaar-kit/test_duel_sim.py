"""Pruebas offline del simulador de rivales, de las correcciones de duels.py / duel_runner.py y de los opt-in.

    python3 -m unittest test_duel_sim
"""
import os
import unittest
from unittest.mock import Mock, patch

import duel_runner as runner
import duel_sim as sim
import duels as dl
from bazaar_sdk import Bazaar, BazaarError


def live(i, deadline, price, role="buyer", limit=100, **kw):
    msgs = [{"tick": deadline - 6 + k, "from": "R", "price": price + 2 - k} for k in range(3)]
    return {"duel": i, "status": "live", "role": role, "your_limit": limit, "rival": "R", "deadline_tick": deadline,
            "rival_offer": {"id": i * 10, "price": price}, "your_offer": None, "messages": msgs, **kw}


class Params(unittest.TestCase):
    def setUp(self):
        self.saved = dict(dl.PARAMS)

    def tearDown(self):
        dl.PARAMS.clear()
        dl.PARAMS.update(self.saved)


class Simulator(Params):
    def test_never_outside_limit_in_any_variant(self):
        for days in (False, True):
            res = sim.simulate(n=40, seed=3, days=days)
            for v, row in res.items():
                self.assertEqual(row["TOTAL"]["outside"], 0, (days, v))

    def test_default_variant_is_untouched_params(self):
        self.assertEqual(sim.VARIANTS["por defecto"], {})
        sim.simulate(n=5, seed=1)
        self.assertEqual(dl.PARAMS, self.saved)          # el simulador restaura PARAMS

    def test_ladder_helps_against_mute_rivals_only(self):
        res = sim.simulate(n=60, seed=5, mix=["mudo", "cedente"],
                           variants={"d": {}, "l": {"LADDER": runner.LADDER}})
        self.assertGreater(res["l"]["mudo"]["capture"], res["d"]["mudo"]["capture"])
        self.assertGreaterEqual(res["l"]["cedente"]["capture"], res["d"]["cedente"]["capture"] - 0.01)

    def test_days_duels_score_zero_without_flag(self):
        res = sim.simulate(n=20, seed=2, days=True, variants={"off": {"PLAY_DAYS": False}, "on": {}})
        self.assertEqual(res["off"]["TOTAL"]["deals"], 0)
        self.assertGreater(res["on"]["TOTAL"]["capture"], 0.2)   # con el signo correcto de los días (antes inflado)

    def test_silence_beats_early_acceptance_on_ceding_rivals(self):
        res = sim.simulate(n=40, seed=4, mix=["cedente"],
                           variants={"d": {}, "e": {"EARLY_ACCEPT_RATIO": 0.0, "MID_ACCEPT_RATIO": 0.0}})
        self.assertGreater(res["d"]["cedente"]["capture"], res["e"]["cedente"]["capture"])


class DaysFormat(Params):
    def test_formats(self):
        lst = list(range(11))
        self.assertEqual(dl.days_table({"your_days_weight": lst}), [float(x) for x in lst])
        self.assertEqual(dl.days_table({"your_days_weight": 2})[3], 6.0)
        self.assertEqual(dl.days_table({"your_days_weight": "2"})[3], 6.0)
        self.assertEqual(dl.days_table({"your_days_weight": {"per_day": -1}})[4], -4.0)
        self.assertEqual(dl.days_table({"your_days_weight": {"3": 12, "4": 1}})[3], 12.0)
        self.assertEqual(dl.days_table({"your_days_weight": {3: 12}})[3], 12.0)
        for bad in (None, True, "x", {"a": 1, "b": 2}, {"12": 1}, [1, "x"], []):
            self.assertIsNone(dl.days_table({"your_days_weight": bad}), bad)

    def test_bad_day_values_are_zero(self):
        d = {"your_days_weight": list(range(11))}
        for k in (None, -1, 11, 2.5, "x"):
            self.assertEqual(dl._days_value(d, k), 0.0)
        self.assertEqual(dl._days_value(d, "4"), 4.0)

    def test_best_day_ties_go_to_rival(self):
        flat = {"your_days_weight": [0] * 11, "rival_offer": {"price": 1, "days": 7}}
        self.assertEqual(dl._best_days(flat), 7)
        self.assertEqual(dl._best_days(dict(flat, your_days_weight=list(range(11)))), 10)
        self.assertEqual(dl._best_days({"your_days_weight": None, "rival_offer": None}), 0)

    def test_days_duel_skipped_by_default_played_with_flag(self):
        d = {"duel": 1, "status": "live", "role": "seller", "your_limit": 100, "rival": "R", "deadline_tick": 20,
             "issues": ["price", "days"], "your_days_weight": [5 - k for k in range(11)],
             "rival_offer": None, "messages": [], "your_offer": None}
        self.assertEqual(dl.duel_candidates([d], 17), [])
        dl.PARAMS["PLAY_DAYS"] = True
        c = dl.duel_candidates([d], 17)
        self.assertEqual(c[0]["type"], "duel_say")
        # vendedor con días que valen (lista firmada, día 0 el mejor): ancla REALISTA (a 3 ticks del deadline, 0,18 del límite)
        self.assertEqual((c[0]["price"], c[0]["days"]), (118, 0))

    def test_days_never_turn_out_of_limit_price_into_accept(self):
        dl.PARAMS["PLAY_DAYS"] = True
        d = live(1, 10, 104, issues=["price", "days"], your_days_weight=[100] * 11)
        d["rival_offer"]["days"] = 3
        self.assertFalse([c for t in range(10) for c in dl.duel_candidates([d], t) if c["type"] == "duel_accept"])

    def test_days_value_hurting_us_blocks_accept(self):
        dl.PARAMS["PLAY_DAYS"] = True
        d = live(1, 10, 95, issues=["price", "days"], your_days_weight=[0] * 10 + [-50])
        d["rival_offer"]["days"] = 10
        self.assertFalse([c for c in dl.duel_candidates([d], 9) if c["type"] == "duel_accept"])
        d["rival_offer"]["days"] = 2
        self.assertTrue([c for c in dl.duel_candidates([d], 9) if c["type"] == "duel_accept"])


class Ordering(Params):
    def test_earliest_deadline_accepted_first(self):
        # duelo 1 vence en 1 tick; duelo 2 está en una oleada de 3 (deadline 52) y también es urgente. Antes ganaba
        # el desempate el rival que menos cede (el 2) y el duelo 1 se quedaba sin aceptación: 0 puntos.
        a, b = live(1, 50, 90), live(2, 52, 90)
        b["messages"] = [{"tick": 46, "from": "R", "price": 90}]
        others = [live(i, 52, 150) for i in (3, 4)]                # fuera de límite: no compiten, pero alargan la oleada
        c = [x for x in dl.duel_candidates([b, a] + others, 49) if x["type"] == "duel_accept"]
        self.assertEqual([x["duel"] for x in c], [1, 2])

    def test_overlapping_waves_get_enough_ticks(self):
        # 3 duelos con deadline 50 y 3 con 51: seis aceptaciones necesitan empezar ≥ 6 ticks antes de 51
        wave = [live(i, 50 + (i >= 3), 90) for i in range(6)]
        self.assertTrue([c for c in dl.duel_candidates(wave, 45) if c["type"] == "duel_accept"])

    def test_ladder_steps_only_for_silent_rival(self):
        dl.PARAMS["LADDER"] = runner.LADDER
        d = {"duel": 1, "status": "live", "role": "buyer", "your_limit": 100, "rival": "R", "deadline_tick": 20,
             "rival_offer": None, "messages": [], "your_offer": None}
        prices = []
        for t in range(15, 20):
            c = [x for x in dl.duel_candidates([d], t) if x["type"] == "duel_say"]
            if c:
                prices.append(c[0]["price"])
                d["your_offer"] = {"price": c[0]["price"]}
        self.assertEqual(prices, [70, 80, 88, 94])
        self.assertTrue(all(p < 100 for p in prices))
        talking = dict(d, rival_offer={"price": 140}, messages=[{"tick": 18, "from": "R", "price": 140}])
        self.assertEqual(dl.duel_candidates([talking], 19), [])

    def test_probe_is_now_default_counter_for_stalled_rival(self):
        # Hotfix de duelos: la contraoferta a un rival estancado es política por defecto (PROBE ya no cambia nada);
        # tras nuestra contraoferta, en la fase media se cierra el trato razonable en lugar de esperar indefinidamente.
        d = live(1, 30, 90)
        d["messages"] = [{"tick": t, "from": "R", "price": 90} for t in range(10, 14)]
        c = dl.duel_candidates([d], 14)[0]
        self.assertEqual(c["type"], "duel_say")
        dl.PARAMS["PROBE"] = True
        self.assertEqual(dl.duel_candidates([d], 14)[0]["price"], c["price"], "PROBE sin efecto")
        d["your_offer"] = {"price": c["price"]}
        self.assertEqual(dl.duel_candidates([d], 23)[0]["type"], "duel_accept")


def api_for(duels_seq, ticks=(9,)):
    api = Mock()
    api.clock.side_effect = [{"tick": t, "tick_seconds": 10} for t in ticks] + [KeyboardInterrupt()]
    api.duels.side_effect = duels_seq
    return api


class Runner(Params):
    def play(self, api, **kw):
        with patch.object(runner, "log"), patch.object(runner.time, "sleep"), \
             patch.object(runner, "LOG", runner.Path("/nonexistent/duels_log.jsonl")):
            runner.run(api, True, **kw)

    def test_read_failure_does_not_skip_the_tick(self):
        d = live(1, 10, 90)
        api = api_for([BazaarError("rate_limited"), {"duels": [d]}], ticks=(9, 9))
        self.play(api)
        api.duel_accept.assert_called_once_with(1)

    def test_accepted_duel_does_not_take_next_ticks_slot(self):
        a, b = live(1, 12, 90), live(2, 12, 90)
        api = api_for([{"duels": [a, b]}, {"duels": [a, b]}], ticks=(10, 11))
        self.play(api)
        self.assertEqual([c.args[0] for c in api.duel_accept.call_args_list], [1, 2])

    def test_unexpected_error_does_not_stop_runner(self):
        api = api_for([{"duels": [{"status": "live", "duel": 1, "deadline_tick": 10}]}, {"duels": [live(2, 10, 90)]}],
                      ticks=(8, 9))
        self.play(api)                                          # el primero no tiene role/your_limit: KeyError
        api.duel_accept.assert_called_once_with(2)

    def test_reconcile_keeps_running_after_ambiguous_write(self):
        a, b = live(1, 12, 90), live(2, 12, 90)
        api = api_for([{"duels": [a, b]}, {"duels": [a, b]}], ticks=(10, 11))
        api.duel_accept.side_effect = [BazaarError("network"), {}]
        self.play(api, reconcile=True)
        self.assertEqual([c.args[0] for c in api.duel_accept.call_args_list], [1, 2])

    def test_verify_accept_skips_worsened_offer(self):
        d = live(1, 10, 90)
        worse = dict(d, rival_offer={"id": 99, "price": 120})
        api = api_for([{"duels": [d]}, {"duels": [worse]}])
        self.play(api, verify_accept=True)
        api.duel_accept.assert_not_called()

    def test_verify_accept_allows_same_offer(self):
        d = live(1, 10, 90)
        api = api_for([{"duels": [d]}, {"duels": [d]}])
        self.play(api, verify_accept=True)
        api.duel_accept.assert_called_once_with(1)

    def test_days_say_always_carries_days(self):
        dl.PARAMS["PLAY_DAYS"] = True
        d = {"duel": 1, "status": "live", "role": "buyer", "your_limit": 100, "rival": "R", "deadline_tick": 12,
             "issues": ["price", "days"], "your_days_weight": {"per_day": 1}, "rival_offer": None, "messages": [],
             "your_offer": None}
        api = api_for([{"duels": [d]}])
        self.play(api)
        kw = api.duel_say.call_args.kwargs
        # comprador: cada día cuesta (peso positivo + rol) ⇒ día 0; antes elegía el 10 porque sumaba los días al comprador
        self.assertEqual(kw["days"], 0)
        self.assertEqual(kw["price"], 82)

    def test_timeout_follows_tick(self):
        self.assertEqual(runner._timeout_for({"tick_seconds": 5}), 3.0)
        self.assertEqual(runner._timeout_for({"tick_seconds": 15}), 7.5)
        self.assertEqual(runner._timeout_for({}), 15.0)

    def test_flags_set_params(self):
        with patch.object(runner, "run") as run, patch.object(runner, "log"), \
             patch.dict(os.environ, {"BAZAAR_KEY": "test-key"}), \
             patch("sys.argv", ["duel_runner.py", "--days", "--ladder"]):
            runner.main()
        self.assertTrue(dl.PARAMS["PLAY_DAYS"])
        self.assertEqual(dl.PARAMS["LADDER"], runner.LADDER)
        self.assertFalse(dl.PARAMS["PROBE"])
        self.assertEqual(run.call_args.kwargs, {"reconcile": False, "verify_accept": False, "track": True, "learner": None})
        self.assertFalse(run.call_args.args[1])                    # sin --execute: análisis

    def test_defaults_unchanged(self):
        self.assertEqual({k: self.saved[k] for k in ("PLAY_DAYS", "LADDER", "PROBE")},
                         {"PLAY_DAYS": False, "LADDER": (), "PROBE": False})


class Sdk(unittest.TestCase):
    def test_priced_days_message_has_days_at_top_level(self):
        b = Bazaar("http://offline.invalid", "k")
        with patch.object(b, "_call") as call:
            b.duel_say(3, "hola", price=60, days=4)
            b.duel_say(3, "hola", price=60)
        body = call.call_args_list[0].args[2]
        self.assertEqual((body["price"], body["days"]), (60, 4))   # nunca un precio de nivel superior sin días
        self.assertEqual(call.call_args_list[1].args[2], {"text": "hola", "price": 60})


if __name__ == "__main__":
    unittest.main()
