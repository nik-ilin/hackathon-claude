"""Decisión bajo información incompleta: cerrar frente a esperar, alternativas por activo, concesiones, selector por niveles,
exclusión estratégica evaluable, tesorería con ventas lentas y modo sombra. Fixtures; sin red ni operaciones."""
import unittest
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import coordinator as co
import decision as dec
import fast_sales as fs
import phases as ph
import shadow
import trading as tr
import v10_commission as v10c
from test_fast_sales import world, run, bid, CFG
from test_trading import asset, catalog, offer, TEAM


def cmp(**kw):
    base = dict(loss=5.0, minimum=9, objective=14, best={"net": 11, "expires_in": 5}, alts=[], ticks_left=4)
    base.update(kw)
    return dec.compare(**base)


class CloseVsWait(unittest.TestCase):
    def test_four_options_are_always_compared_with_ranges_and_confidence(self):
        r = cmp()
        self.assertEqual([o["code"] for o in r["options"]], ["A", "B", "C", "D"])
        a = r["options"][0]
        self.assertEqual((a["surplus"], a["confidence"], a["cash_now"]), ([6.0, 6.0], "alta", 11))
        self.assertIsNone(r["options"][2]["surplus"])                    # sin alternativas visibles: no se inventan
        self.assertEqual(r["options"][1]["confidence"], "baja")

    def test_executable_offer_is_not_penalised_by_a_hypothetical_alternative(self):
        for alts in ([], [{"kind": "comprador con puja reciente", "net_lo": None, "net_hi": None, "confidence": "baja"}],
                     [{"kind": "cierres comparables", "net_lo": 14, "net_hi": 16, "n": 2, "confidence": "baja"}]):
            self.assertEqual(cmp(alts=alts)["action"], "accept")

    def test_only_evidenced_better_alternative_with_time_justifies_waiting(self):
        good = [{"kind": "cierres comparables", "net_lo": 14, "net_hi": 16, "n": 3, "confidence": "media"}]
        self.assertEqual(cmp(alts=good)["action"], "wait_list")
        self.assertEqual(cmp(alts=good, ticks_left=1)["action"], "accept")                        # sin tiempo
        self.assertEqual(cmp(alts=good, w=0.5)["action"], "accept")                               # urgencia de caja
        self.assertEqual(cmp(alts=good, best={"net": 11, "expires_in": 1})["action"], "accept")    # caduca ya
        self.assertEqual(cmp(alts=good, deadline=True)["action"], "accept")
        near = [{"kind": "cierres comparables", "net_lo": 12, "net_hi": 13, "n": 5, "confidence": "media"}]
        self.assertEqual(cmp(alts=near)["action"], "accept")                                       # <min_step P: no se prolonga

    def test_at_or_above_objective_closes_without_waiting_and_below_minimum_is_not_accepted(self):
        self.assertEqual(cmp(best={"net": 14, "expires_in": 5})["action"], "accept")
        self.assertNotEqual(cmp(best=None)["action"], "accept")
        self.assertEqual(cmp(best=None, own_offer=True)["action"], "hold")                          # silencio ≠ rechazo

    def test_live_alternative_bids_are_listed_with_age_and_next_alternative(self):
        ev = {"bids": [{"offer": 1, "team": "t07", "net": 20, "expires": 90}, {"offer": 2, "team": "t08", "net": 17, "expires": 80}],
              "closes": [{"price": p, "tick": 490 + p} for p in (14, 15, 16)], "recent_buyers": [{"team": "t09", "last_bid_tick": 480}]}
        alts = dec.alternatives(ev, selected_offer=1, tick=500)
        self.assertEqual([a["kind"] for a in alts], ["puja vigente", "cierres comparables", "comprador con puja reciente"])
        self.assertEqual((alts[0]["team"], alts[0]["executable"], alts[1]["n"], alts[1]["confidence"]), ("t08", True, 3, "media"))
        self.assertEqual(alts[2]["age"], 20)


class Concessions(unittest.TestCase):
    def test_no_reprice_for_less_than_min_step_nor_on_silence_alone(self):
        d = fs.Config(refs=("LAT-01",), ticks=6, counters=2, margin=2.0)
        rep, own = {"ref": "LAT-01", "loss": 3.25}, {"id": 9}
        ev = {"bids": [], "closes": [], "asks": [], "recent_buyers": []}
        st = {"first_tick": 500, "age": 4, "asks": [11], "last_ask_tick": 500, "cooling_until": None}
        pr = {"minimum": 6, "objective": 10, "quick_close": None, "basis": []}
        self.assertEqual(fs.decide(rep, 2, ev, pr, st, own, d, 504, False, "rastro")["action"], "wait")     # 11→10: solo 1 P
        pr["objective"] = 9
        self.assertEqual(fs.decide(rep, 2, ev, pr, st, own, d, 504, False, "rastro")["action"], "cancel")   # 2 P y evidencia
        pr["objective"] = 11
        self.assertEqual(fs.decide(rep, 2, ev, pr, st, own, d, 504, False, "rastro")["action"], "wait")     # silencio: sin cambio


class OffersAndRestarts(unittest.TestCase):
    def test_wait_list_keeps_one_exit_per_asset_and_evidenced_better_closes(self):
        closes = [{"type": "settlement", "tick": 490 + i, "payload": {"settlement": 700 + i, "tick": 490 + i, "price": 30,
                   "items": [{"kind": "card", "ref": "LAT-01"}]}} for i in range(3)]
        mine, pl, _ = run(world(offers=[bid(50, 12)], feed=closes))
        self.assertEqual([c["type"] for c in mine], ["list"])                                       # una sola salida
        row = pl["fast_sales_report"]["refs"][0]["assets"][0]
        self.assertIn("evidencia de mejora", row["reason"])
        self.assertEqual(row["next_alternative"]["kind"], "cierres comparables")
        self.assertEqual([o["code"] for o in row["compare"]], ["A", "B", "C", "D"])

    def test_offer_expiring_this_tick_is_closed_not_waited_on(self):
        closes = [{"type": "settlement", "tick": 490 + i, "payload": {"settlement": 700 + i, "tick": 490 + i, "price": 30,
                   "items": [{"kind": "card", "ref": "LAT-01"}]}} for i in range(3)]
        mine, _, _ = run(world(offers=[bid(50, 12, exp=501)], feed=closes))                            # caduca en 1 tick
        self.assertEqual([c["type"] for c in mine], ["accept"])

    def test_page_bonus_is_part_of_the_marginal_loss(self):
        val = tr.Valuation(catalog(), {"LAT": 1.3})
        full = Counter({"LAT-01": 1, "LAT-02": 1, "LAT-03": 1})
        loss_last = -val.delta(full, Counter(), Counter({"LAT-01": 1}))[0]
        loss_dup = -val.delta(Counter({"LAT-01": 2, "LAT-02": 1, "LAT-03": 1}), Counter(), Counter({"LAT-01": 1}))[0]
        self.assertGreater(loss_last, loss_dup + 3)                                                    # el bono de página se pierde


class Selector(unittest.TestCase):
    def cands(self):
        return [
            {"type": "list", "module": "mercado", "ref": "A", "asset": 1, "du": 40.0, "expected_du": 2.0, "score": 3 * 10 ** 5, "blockers": []},
            {"type": "accept", "module": "mercado", "ref": "B", "offer": 7, "du": 6.0, "expected_du": 6.0, "score": 10 ** 3, "blockers": []},
            {"type": "cancel", "module": "seguridad", "offer": 9, "ref": "C", "score": 10 ** 7, "blockers": []},
            {"type": "dealer_sell_accept", "module": "vendedores", "thread": 3, "ref": "D", "du": 1.0, "score": 3 * 10 ** 5, "blockers": []},
        ]

    def test_tiers_separate_safety_commitments_and_commercial(self):
        c = self.cands()
        self.assertEqual([co.priority_tier(x) for x in c], [2, 2, 0, 1])
        self.assertEqual([co.priority_tier(x) for x in [{"type": "cancel", "capital_cancel": True}, {"type": "list", "v10_approval": True}]], [1, 1])

    def test_economic_ranking_ignores_arbitrary_priority_bonuses(self):
        c = self.cands()
        legacy = sorted(c, key=lambda x: co.select_key(x, "legacy"))
        econ = sorted(c, key=lambda x: co.select_key(x, "economic"))
        self.assertEqual(legacy[1]["ref"], "A")                       # el bono 3·10**5 colocaba la publicación con p desconocida
        self.assertEqual([x["ref"] for x in econ], ["C", "D", "B", "A"])
        self.assertEqual(co.economic_value(c[0]), 2.0)
        self.assertEqual(co.economic_value({"type": "list", "du": 99.0}), 0.0)                      # sin probabilidad: no se inventa

    def test_cash_urgency_is_explicit_and_select_respects_limits(self):
        a = {"type": "accept", "module": "m", "du": 5.0, "score": 1, "blockers": [], "cash_urgent": 0.0, "ref": "X", "deliver": {"X": 1}}
        b = {"type": "accept", "module": "m", "du": 2.0, "score": 1, "blockers": [], "cash_urgent": 0.9, "ref": "Y", "deliver": {"Y": 1}}
        led = {"class_tick": {}, "class_count": {}, "blocked": {}}
        self.assertEqual(co.select([a, b], led, 10, 3, "economic")[0]["ref"], "Y")                  # urgencia de caja explícita
        self.assertEqual(len(co.select([a, b], led, 10, 3, "economic")), 1)                         # una aceptación por tick
        self.assertEqual(co.select([a, b], led, 10, 3)[0]["ref"], "X")                              # legado intacto por defecto

    def test_phases_mark_cash_urgency_on_sales(self):
        cfg = ph.PhaseConfig(enabled=True)
        st = ph.state({"closes": "2026-10-03T23:00:00+02:00"}, cfg, ph.scenarios(100, 0, 0), datetime.fromisoformat("2026-10-03T22:10:00+02:00"))
        sale = {"type": "list", "price": 9, "blockers": [], "score": 1}
        ph.apply([sale], st, cfg, free_cash=100, states={}, tick_seconds=30.0, expiry_ratio=1.0, margin=2.0, open_cash_offers=[])
        self.assertGreater(sale["cash_urgent"], 0)


class StrategicExclusion(unittest.TestCase):
    def cfg(self, **kw):
        return fs.Config(refs=("LAT-01",), soft_denied=frozenset({"t13"}), **kw)

    def test_hard_denial_is_untouched_and_soft_denial_is_evaluated(self):
        hard = fs.Config(refs=("LAT-01",), denied=frozenset({"t13"}))
        ev = fs.evidence(world(offers=[bid(50, 40, maker="t13")]), "LAT-01", hard, {"rastro": SimpleNamespace(fee=lambda p, n: 3)})
        self.assertEqual(ev["bids"], [])                                                            # acuerdo expreso: siempre
        self.assertIn("deny-teams", ev["rejected_bids"][0]["problems"][0])

    def test_soft_denial_blocks_only_when_rival_has_no_visible_alternative_and_cost_is_small(self):
        cfg = self.cfg()
        e = fs.soft_deny_eval("t13", 8, 5.0, [], cfg)
        self.assertTrue(e["deny"])
        self.assertIn("no se conoce su beneficio", e["limits"])
        self.assertFalse(fs.soft_deny_eval("t13", 8, 5.0, [{"team": "t09", "price": 9}], cfg)["deny"])      # puede comprar a otro
        self.assertFalse(fs.soft_deny_eval("t13", 30, 5.0, [], cfg)["deny"])                                 # sacrificio alto
        self.assertTrue(fs.soft_deny_eval("t13", 8, 5.0, [], self.cfg(soft_cost=1))["deny"] is False)       # umbral configurable

    def test_soft_denied_team_sells_when_a_visible_ask_exists(self):
        venues = {"rastro": SimpleNamespace(fee=lambda p, n: 3)}
        alt = offer(61, {"assets": [{"id": 99, "kind": "card", "ref": "LAT-01"}]}, {"cash": 12}, maker="t09", venue="rastro")
        ev = fs.evidence(world(offers=[bid(50, 12, maker="t13"), alt]), "LAT-01", self.cfg(), venues, None, 3.25)
        self.assertEqual([b["team"] for b in ev["bids"]], ["t13"])
        self.assertEqual(ev["bids"][0]["soft_deny"]["rival_visible_alternatives"], 1)


class Treasury(unittest.TestCase):
    CLOCK = {"closes": "2026-10-03T23:00:00+02:00"}

    def test_slow_sales_extend_the_transition_gradually_without_touching_limits(self):
        cfg = ph.PhaseConfig(enabled=True, accelerate_max=60)
        at = datetime.fromisoformat("2026-10-03T21:00:00+02:00")
        fast = ph.state(self.CLOCK, cfg, ph.scenarios(40, 0, 20), at, fill_min=5)
        slow = ph.state(self.CLOCK, cfg, ph.scenarios(40, 0, 20), at, fill_min=80)
        self.assertGreater(slow["b_start"], fast["b_start"])
        self.assertLessEqual(slow["b_start"], cfg.transition_min + cfg.accelerate_max)
        self.assertIn("ventas lentas", slow["reason"])
        self.assertEqual(slow["scenarios"], fast["scenarios"])                      # nunca gasta contra ingresos hipotéticos
        reach = ph.state(self.CLOCK, cfg, ph.scenarios(150, 0, 0), at, fill_min=80)
        self.assertEqual(reach["accelerated_min"], 0)                                # meta alcanzable: sin adelanto

    def test_three_cash_scenarios_stay_without_probabilities(self):
        sc = ph.scenarios(40, 15, 20)
        self.assertEqual((sc["confirmed"], sc["with_published_sales"], sc["with_executable_sales"]), (40, 55, 75))


class Team5AndV15(unittest.TestCase):
    def test_promised_commission_is_never_cash(self):
        state = {"approvals": {"a": {"status": "settled", "settlement_id": 1}, "b": {"status": "settled", "settlement_id": 2},
                               "c": {"status": "approved", "expires_at": "2026-10-03T18:30:00+02:00"}}}
        s = v10c.summary(state)
        self.assertEqual((s["commission_due_p"], s["commission_paid_confirmed_p"], s["commission_pending_p"]), (2, 0, 2))
        self.assertFalse(s["commission_counts_as_cash"])
        self.assertEqual((s["reciprocal_sales_contributed"], s["expired_unused"]), (2, 1))

    def test_v15_never_trades_for_us(self):
        import third_party as tp
        self.assertIn("NUNCA compra ni vende nosotros", tp.__doc__)


class Shadow(unittest.TestCase):
    def test_table_explains_every_disagreement(self):
        rows = shadow.close_table()
        diffs = [r for r in rows if not r["agree"]]
        self.assertTrue(diffs)
        self.assertTrue(all(r["why"] for r in diffs))
        thin = next(r for r in rows if "2 cierres" in r["scenario"])
        self.assertEqual((thin["legacy"], thin["proposed"]), ("wait", "accept"))                  # poca evidencia ya no frena una puja

    def test_selector_diff_runs_on_a_saved_cycle_without_sending(self):
        d = shadow.selector_diff([{"type": "list", "ref": "X", "du": 3.0, "score": 5, "blockers": []},
                                  {"type": "cancel", "module": "seguridad", "offer": 1, "ref": "Y", "score": 1, "blockers": []}])
        self.assertEqual(d["economic"][0], "cancel:Y")


if __name__ == "__main__":
    unittest.main()
