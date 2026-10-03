"""Campaña de ventas rápidas: inventario revalidado, tres precios, rutas, presupuesto de ticks, compromisos, reinicio y
liquidación única. Fixtures; sin red, sin operaciones y sin tocar data/."""
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import accounting as acct
import coordinator as co
import fast_sales as fs
import negotiation as neg
import v10_commission as v10c
from test_coordinator import args as base_args, full
from test_dealer_ladder import led0
from test_trading import TEAM, asset, offer, snap

CFG = fs.Config(refs=("LAT-01", "LAT-02"), ticks=6, counters=2, margin=2.0)


def world(assets=None, offers=(), cash=100, tick=500, feed=()):
    assets = assets if assets is not None else [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")]
    s = full(snap(assets, cash=cash, offers=list(offers), tick=tick, feed=list(feed)))
    s["clock"]["tick_seconds"] = 30.0
    s["venues"] = {"venues": [dict(venue="rastro", status="open", fee_bps=500, fee_per_card=1, owner="world"),
                              dict(venue="v05", status="open", fee_bps=0, fee_per_card=0, owner="t05"),
                              dict(venue="vrival", status="open", fee_bps=0, fee_per_card=0, owner="t10")]}
    return s


def bid(oid, price, maker="t07", venue="rastro", ref="LAT-01", exp=520, to=None):
    return offer(oid, {"cash": price}, {"types": [f"card:{ref}"]}, maker=maker, to=to, exp=exp, venue=venue)


def run(s, led=None, **kw):
    a = base_args(dealer_sell_dups=False, fast_sales="LAT-01,LAT-02", margin=2.0, duende_venue="rastro", **kw)
    with tempfile.TemporaryDirectory() as d:
        saved = co.DATA
        co.DATA = Path(d)
        try:
            co.INTEL_STATE["intel"] = None
            cands, pl, _ = co.candidates(s, led or led0(), a, neg.Journal(d))
        finally:
            co.DATA = saved
    return [c for c in cands if c.get("module") == fs.MODULE], pl, cands


class Inventory(unittest.TestCase):
    def test_last_copy_of_completed_page_is_protected(self):
        s = world([asset(1, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")])
        mine, pl, cands = run(s)
        self.assertEqual(mine, [])
        row = next(r for r in pl["fast_sales_report"]["refs"] if r["ref"] == "LAT-01")
        self.assertFalse(row["authorized"])
        self.assertIn("NO SE VENDE", row["reasons"][0])

    def test_only_listed_cards_and_only_surplus(self):
        mine, pl, _ = run(world())
        self.assertEqual({c["ref"] for c in mine if c["type"] == "list"}, {"LAT-01"})        # LAT-02 única: protegida
        self.assertNotIn("LAT-03", [r["ref"] for r in pl["fast_sales_report"]["refs"]])

    def test_duplicate_committed_elsewhere_is_cancelled_then_reused_only_after_release(self):
        own = offer(77, {"assets": [{"id": 2, "kind": "card", "ref": "LAT-01"}]}, {"cash": 30}, maker=TEAM, venue="rastro")
        mine, pl, _ = run(world(offers=[own]))
        self.assertEqual([(c["type"], c["offer"]) for c in mine], [("cancel", 77)])            # no se reutiliza todavía
        self.assertTrue(mine[0]["reason"].startswith("COMPROMISO"))
        mine, _, _ = run(world())                                                              # servidor confirma liberación
        self.assertEqual([(c["type"], c["asset"]) for c in mine], [("list", 2)])

    def test_thread_with_a_dealer_is_not_duplicated(self):
        s = world()
        s["threads"]["open"] = [{"id": 9, "kind": "persona", "with": "abuela", "team": TEAM, "status": "open", "created_tick": 495,
                                 "topic": {"sell": {"assets": [2]}}, "messages": [], "standing_offers": []}]
        mine, pl, _ = run(s, dict(led0(), threads=[9]))
        self.assertEqual(mine, [])
        row = next(r for r in pl["fast_sales_report"]["refs"] if r["ref"] == "LAT-01")
        self.assertIn("negociación en curso con abuela", row["assets"][0]["reason"])


class LastCopy(unittest.TestCase):
    def incomplete_world(self):
        return world([asset(1, "LAT-01"), asset(3, "LAT-02")])          # LAT-03 falta: la página NO está completa

    def run_with(self, allow):
        a = base_args(dealer_sell_dups=False, fast_sales="LAT-01", margin=2.0, duende_venue="rastro", allow_last_copy=allow)
        cands, pl, _ = co.candidates(self.incomplete_world(), led0(), a, neg.Journal(tempfile.mkdtemp()))
        return [c for c in cands if c.get("module") == fs.MODULE], pl

    def test_last_copy_of_an_incomplete_page_needs_explicit_operator_authorization(self):
        mine, pl = self.run_with("")
        self.assertEqual(mine, [])
        self.assertIn("--allow-last-copy", pl["fast_sales_report"]["refs"][0]["reasons"][0])
        mine, pl = self.run_with("LAT-01")
        self.assertEqual([c["type"] for c in mine], ["list"])


    def test_security_cancel_respects_the_operator_last_copy_authorization(self):
        import trading as tr
        from collections import Counter
        s = self.incomplete_world()
        own = offer(70, {"assets": [{"id": 1, "kind": "card", "ref": "LAT-01"}]}, {"cash": 20}, maker=TEAM, venue="rastro")
        val = tr.Valuation(s["catalog"], s["me"]["affinity"])
        counts = tr.counts_of(s["me"]["assets"])
        self.assertTrue(tr.unsafe_own_offers([own], TEAM, val, counts))                           # sin autorización: se cancela
        self.assertFalse(tr.unsafe_own_offers([own], TEAM, val, counts, frozenset({"LAT-01"})))


class Prices(unittest.TestCase):
    def test_minimum_is_full_loss_plus_margin_and_operator_margin_wins(self):
        s = world()
        _, pl, _ = run(s)
        row = next(r for r in pl["fast_sales_report"]["refs"] if r["ref"] == "LAT-01")
        self.assertEqual(row["prices"]["minimum"], fs.min_net(row["loss"], 2.0))
        self.assertGreaterEqual(row["prices"]["minimum"], row["loss"] + 2)
        _, pl5, _ = run(s)
        a = base_args(dealer_sell_dups=False, fast_sales="LAT-01", margin=5.0, duende_venue="rastro")
        cands, pl5, _ = co.candidates(s, led0(), a, neg.Journal(tempfile.mkdtemp()))
        self.assertEqual(pl5["fast_sales_report"]["refs"][0]["prices"]["minimum"], fs.min_net(row["loss"], 5.0))

    def test_objective_comes_from_evidence_and_never_beats_the_live_competitor(self):
        closes = [{"type": "settlement", "tick": 480 + i, "payload": {"settlement": 900 + i, "tick": 480 + i, "price": 14,
                   "items": [{"kind": "card", "ref": "LAT-01"}]}} for i in range(3)]
        cheap = offer(60, {"assets": [{"id": 99, "kind": "card", "ref": "LAT-01"}]}, {"cash": 11}, maker="t09", venue="rastro")
        _, pl, _ = run(world(feed=closes, offers=[cheap]))
        p = pl["fast_sales_report"]["refs"][0]["prices"]
        self.assertEqual(p["objective"], max(p["minimum"], 10))                                # 11 − 1 < mediana 14
        self.assertTrue(any("competidor vivo" in b for b in p["basis"]))

    def test_catalog_book_is_labelled_not_market_value(self):
        _, pl, _ = run(world())
        self.assertTrue(any("no es un valor de mercado" in b for b in pl["fast_sales_report"]["refs"][0]["prices"]["basis"]))


class Routes(unittest.TestCase):
    def test_executable_bid_at_or_above_objective_closes_without_waiting(self):
        mine, pl, _ = run(world(offers=[bid(50, 40)]))
        acc = [c for c in mine if c["type"] == "accept"]
        self.assertEqual((len(acc), acc[0]["offer"], acc[0]["price"], acc[0]["cash"], acc[0]["asset"]), (1, 50, 40, 37, 2))
        self.assertFalse([c for c in mine if c["type"] == "list"])                             # ni publica ni espera

    def test_bid_below_minimum_is_never_accepted(self):
        mine, pl, _ = run(world(offers=[bid(50, 3)]))
        self.assertFalse([c for c in mine if c["type"] == "accept"])
        p = pl["fast_sales_report"]["refs"][0]["prices"]
        self.assertLess(p["quick_close"], p["minimum"])                                         # neta de comisión: < mínimo
        self.assertTrue([c for c in mine if c["type"] == "list"])

    def test_operator_restrictions_rival_venue_and_denied_team(self):
        mine, pl, _ = run(world(offers=[bid(50, 40, venue="vrival"), bid(51, 40, maker="t13")]), deny_teams="t13,t12",
                          no_rival_venues=True)
        self.assertFalse([c for c in mine if c["type"] == "accept"])
        rej = pl["fast_sales_report"]["refs"][0]["evidence"]["rejected_bids"]
        self.assertEqual(len(rej), 2)

    def test_concurrent_bids_do_not_reuse_one_offer_for_two_assets(self):
        s = world([asset(1, "LAT-01"), asset(2, "LAT-01"), asset(5, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")])
        mine, _, _ = run(s)                                                                    # 2 excedentes, sin pujas
        self.assertEqual(sorted(c["asset"] for c in mine if c["type"] == "list"), [2, 5])
        mine, _, _ = run(dict(s, offers={"offers": [bid(50, 40), bid(51, 38, maker="t08")]}))
        acc = sorted((c["asset"], c["offer"]) for c in mine if c["type"] == "accept")
        self.assertEqual(acc, [(2, 50), (5, 51)])
        mine, _, _ = run(dict(s, offers={"offers": [bid(50, 40)]}))
        self.assertEqual([c["offer"] for c in mine if c["type"] == "accept"], [50])            # un solo accept por puja
        self.assertEqual([c["asset"] for c in mine if c["type"] == "list"], [5])

    def test_between_minimum_and_objective_waits_when_better_is_evidenced_else_closes(self):
        closes = [{"type": "settlement", "tick": 490 + i, "payload": {"settlement": 700 + i, "tick": 490 + i, "price": 30,
                   "items": [{"kind": "card", "ref": "LAT-01"}]}} for i in range(3)]
        s = world(offers=[bid(50, 12)], feed=closes)
        mine, _, _ = run(s)
        self.assertFalse([c for c in mine if c["type"] == "accept"])                           # hay evidencia de 30 P
        mine, _, _ = run(world(offers=[bid(50, 12)]))
        self.assertTrue([c for c in mine if c["type"] == "accept"])                            # sin evidencia: no se prolonga


class Budget(unittest.TestCase):
    def listed(self, tick_first, price=30):
        a = {"key": "k1", "type": "list", "module": fs.MODULE, "asset": 2, "tick": tick_first, "status": "submitted",
             "price": price, "offer": None, "offer_id": 88}            # como lo guarda send(): id en offer_id
        return dict(led0(), actions=[a])

    def test_restart_does_not_duplicate_the_proposal(self):
        own = offer(88, {"assets": [{"id": 2, "kind": "card", "ref": "LAT-01"}]}, {"cash": 30}, maker=TEAM, venue="rastro")
        mine, pl, _ = run(world(offers=[own], tick=502), self.listed(500))
        self.assertEqual(mine, [])
        row = pl["fast_sales_report"]["refs"][0]["assets"][0]
        self.assertEqual(row["action"], "wait")
        self.assertEqual(row["stage"]["age"], 2)

    def test_budget_exhausted_releases_then_cools_down(self):
        own = offer(88, {"assets": [{"id": 2, "kind": "card", "ref": "LAT-01"}]}, {"cash": 30}, maker=TEAM, venue="rastro")
        mine, _, _ = run(world(offers=[own], tick=507), self.listed(500))
        self.assertEqual([(c["type"], c["offer"]) for c in mine], [("cancel", 88)])
        self.assertTrue(mine[0]["reason"].startswith("LIBERA"))
        led = self.listed(500)
        led["actions"].append({"key": "k2", "type": "cancel", "module": fs.MODULE, "asset": 2, "tick": 507, "status": "settled",
                               "reason": mine[0]["reason"]})
        mine, pl, _ = run(world(tick=508), led)
        self.assertEqual(mine, [])                                                             # enfriamiento: no repite
        self.assertIn("enfriamiento", pl["fast_sales_report"]["refs"][0]["assets"][0]["reason"])
        mine, _, _ = run(world(tick=514), led)
        self.assertEqual([c["type"] for c in mine], ["list"])

    def test_natural_expiry_opens_a_new_window_without_cooldown_and_dry_windows_revise_the_price(self):
        mine, pl, _ = run(world(tick=507), self.listed(500, price=30))                          # caducó sola: sin enfriamiento
        self.assertEqual([c["type"] for c in mine], ["list"])
        self.assertNotIn("ventana(s) sin comprador", mine[0]["reason"])                        # 1 ventana seca: sin rebaja por silencio
        led = self.listed(500, price=30)
        led["actions"].append({"key": "k2", "type": "list", "module": fs.MODULE, "asset": 2, "tick": 507, "status": "released",
                               "price": 30, "offer": 89})
        mine, pl, _ = run(world(tick=514), led)                                                  # 2 ventanas secas a 30 P
        lst = [c for c in mine if c["type"] == "list"]
        self.assertTrue(lst and 6 <= lst[0]["price"] < 30)
        self.assertIn("ventana(s) sin comprador", lst[0]["reason"])
        self.assertEqual(pl["fast_sales_report"]["refs"][0]["assets"][0]["stage"]["dry_windows"], 2)

    def test_dry_windows_never_go_below_the_minimum_and_a_fill_resets_the_count(self):
        cfg = fs.Config(refs=("LAT-01",))
        st = {"last_window_price": 7, "dry_windows": 5}
        self.assertEqual(fs.revised_price(7, 7, st, cfg, False)[0], 7)                           # ya en el mínimo
        self.assertEqual(fs.revised_price(20, 7, {"last_window_price": 20, "dry_windows": 1}, cfg, False)[0], 20)
        self.assertLess(fs.revised_price(20, 7, {"last_window_price": 20, "dry_windows": 1}, cfg, True)[0], 20)  # urgencia
        led = {"actions": [{"type": "list", "module": fs.MODULE, "asset": 2, "tick": t, "status": s, "price": 20}
                           for t, s in ((500, "released"), (506, "settled"), (512, "released"))]}
        self.assertEqual(fs.stage(led, 2, 520, cfg)["dry_windows"], 1)                           # la venta corta la racha
        led = {"actions": [{"type": "list", "module": fs.MODULE, "asset": 2, "tick": t, "status": "released", "price": p}
                           for t, p in ((500, 20), (506, 20), (512, 17))]}
        st = fs.stage(led, 2, 520, cfg)
        self.assertEqual((st["dry_windows"], st["last_window_price"]), (1, 17))                 # tras rebajar, racha nueva
        self.assertIsNone(fs.revised_price(17, 7, st, cfg, False)[1])                            # aún no toca otra rebaja

    def test_valid_bid_at_deadline_closes_instead_of_releasing(self):
        own = offer(88, {"assets": [{"id": 2, "kind": "card", "ref": "LAT-01"}]}, {"cash": 30}, maker=TEAM, venue="rastro")
        mine, _, _ = run(world(offers=[own, bid(50, 12)], tick=507), self.listed(500))
        self.assertEqual(mine[0]["type"], "cancel")
        self.assertTrue(mine[0]["reason"].startswith("ACEPTAR"))                               # libera y acepta al confirmarse

    def test_reprice_needs_stale_ticks_and_respects_the_counter_limit(self):
        d = fs.Config(refs=("LAT-01",), ticks=6, counters=2, margin=2.0)
        rep = {"ref": "LAT-01", "loss": 3.25}
        pr = {"minimum": 6, "objective": 10, "quick_close": None, "basis": []}
        ev = {"bids": [], "closes": [], "asks": [], "recent_buyers": []}
        own = {"id": 9}
        st = {"first_tick": 500, "age": 3, "asks": [20], "last_ask_tick": 500, "cooling_until": None}
        self.assertEqual(fs.decide(rep, 2, ev, pr, st, own, d, 503, False, "rastro")["action"], "cancel")
        self.assertEqual(fs.decide(rep, 2, ev, pr, dict(st, age=1), own, d, 501, False, "rastro")["action"], "wait")
        self.assertEqual(fs.decide(rep, 2, ev, pr, dict(st, asks=[30, 20, 15]), own, d, 503, False, "rastro")["action"], "wait")


class Safety(unittest.TestCase):
    def test_expired_approval_from_team5_is_never_reused(self):
        state = {"approvals": {"k": {"status": "approved", "card": "LAT-01", "buyer": "t09", "price": 9, "venue": "v10",
                                     "expires_at": "2026-10-03T18:30:00+02:00"}}}
        self.assertEqual(v10c.listing_candidates(state, {"assets": [asset(1, "LAT-01"), asset(2, "LAT-01")]}, 30.0, set()), [])

    def test_v10_route_cannot_go_below_the_minimum_and_incentive_is_not_income(self):
        c = {"type": "list", "module": "comision v10", "v10_approval": True, "ref": "card:LAT-01", "price": 4, "blockers": []}
        pl = {"fast_sales_report": {"refs": [{"ref": "LAT-01", "prices": {"minimum": 6, "quick_close": None}}]}}
        co.fast_sales_supersede([c], SimpleNamespace(fast_sales="LAT-01"), pl)
        self.assertTrue(any("bajo el mínimo" in b and "no cuenta hasta cobrarse" in b for b in c["blockers"]))
        c2 = {"type": "list", "module": "comision v10", "v10_approval": True, "ref": "card:LAT-01", "price": 9, "blockers": []}
        co.fast_sales_supersede([c2], SimpleNamespace(fast_sales="LAT-01"), pl)
        self.assertEqual(c2["blockers"], [])
        self.assertTrue(any("PENDIENTE" in n for n in c2["notes"]))
        c3 = dict(c2, blockers=[])
        pl["fast_sales_report"]["refs"][0]["prices"]["quick_close"] = 15
        co.fast_sales_supersede([c3], SimpleNamespace(fast_sales="LAT-01"), pl)
        self.assertTrue(any("otra ruta ejecutable" in b for b in c3["blockers"]))

    def test_generic_exits_for_campaign_cards_are_superseded(self):
        gen = {"type": "list", "module": "mercado", "ref": "LAT-01", "blockers": []}
        other = {"type": "list", "module": "mercado", "ref": "LAT-03", "blockers": []}
        co.fast_sales_supersede([gen, other], SimpleNamespace(fast_sales="LAT-01"), {})
        self.assertTrue(gen["blockers"])
        self.assertFalse(other["blockers"])

    def test_offer_that_expires_during_acceptance_is_not_sent(self):
        import opportunities as opps
        c = {"type": "accept", "offer": 50, "venue": "v05", "price": 40, "maker": "t07", "assets": [2], "deliver": {"LAT-01": 1},
             "receive": {}, "cash": 40, "blockers": []}
        fresh_board = []                                                                       # ya no figura abierta
        me = {"cash": 100, "assets": [asset(1, "LAT-01"), asset(2, "LAT-01")]}
        problems = opps.revalidate_accept(c, fresh_board=fresh_board, my_offers=[], me=me, tick=505, team=TEAM)
        self.assertTrue(problems)

    def test_settlement_is_counted_once_and_buyer_log_is_derived(self):
        led = led0()
        self.assertTrue(acct.count(led, acct.key_settlement(77), "income", 12, 505, "accept"))
        self.assertFalse(acct.count(led, acct.key_settlement(77), "income", 12, 505, "accept"))   # visto por otra vía
        self.assertEqual(led["cash_received"], 12)
        led["actions"] = [{"module": fs.MODULE, "type": "accept", "maker": "t07", "status": "settled", "price": 12, "tick": 500,
                           "settled_tick": 502},
                          {"module": fs.MODULE, "type": "list", "to": "t09", "status": "released", "price": 20, "tick": 500}]
        log = fs.buyer_log(led)
        self.assertEqual((log["t07"]["settled"], log["t07"]["ticks_to_close"], log["t09"]["released"]), (1, [2], 1))


if __name__ == "__main__":
    unittest.main()
