"""Ejecución, asignación de capital y cierre de tratos (sin red ni operaciones reales)."""
import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path

import capital as ca
import coordinator as co
import market_intel as mi
import negotiation as neg
import page_guard as pg
import performance as perf
import trading as tr
from bazaar_sdk import BazaarError
from test_market_intel import TEAM, A, bid, catalog, offer, snap

T = 7  # id del hilo con el vendedor


def her(oid, tick, price, status="open", final=False, exp=None):
    return {"id": oid, "tick": tick, "sender": "abuela", "offer": {
        "id": oid, "maker": "abuela", "to": TEAM, "thread": T, "status": status, "final": final,
        "give": {"cash": 0, "assets": [], "types": ["card:LAT-03"]}, "want": {"cash": price, "assets": [], "types": []},
        "expires_tick": tick + 5 if exp is None else exp}}


def ours(oid, tick, price):
    return {"id": oid, "tick": tick, "sender": TEAM, "offer": {"id": oid, "maker": TEAM, "status": "cancelled",
            "give": {"cash": price, "assets": [], "types": []}, "want": {"cash": 0, "types": ["card:LAT-03"]}}}


def thread(msgs, status="open", tid=T, dealer="abuela"):
    for m in msgs:
        if m["sender"] != TEAM:
            m["sender"] = dealer
            m["offer"]["maker"] = dealer
            m["offer"]["thread"] = tid
    return {"id": tid, "kind": "persona", "with": dealer, "team": TEAM, "status": status, "created_tick": 95,
            "topic": {"buy": {"card": "LAT-03"}}, "messages": msgs, "standing_offers": []}


def state(msgs, now=100):
    return neg.state_from_thread(thread(msgs), "abuela", now, neg.Config())


CONCEDED = [her(1, 96, 17, "cancelled"), ours(2, 96, 11), her(3, 97, 14, exp=101)]  # abre 17, ofrecemos 11, rebaja a 14


def outcome(thread_id, opening, close, dealer="abuela"):
    return {"dealer": dealer, "item": "card:X", "thread": thread_id, "status": "deal", "opening": opening,
            "close_price": close, "settled": True, "context": "normal"}


class DealerSecureMode(unittest.TestCase):
    def test_01_zero_deals_concession_inside_ceiling_secure_accept(self):
        pol = neg.policy_for("abuela", "score", 0)
        d = neg.decide_dealer(state(CONCEDED), pol, ceiling=20, conv_ticks_left=4)
        self.assertEqual((d.action, d.price), ("accept", 14))
        self.assertIn("SECURE", d.reason)
        base = neg.decide_dealer(state(CONCEDED), neg.dealer_policy("abuela", "score"), 20, 4)
        self.assertEqual(base.action, "counter", "antes: la abuela en modo score seguía regateando")

    def test_02_two_deals_concession_third_close_gets_top_priority(self):
        self.assertEqual(neg.ladder_mode(2), "SECURE")
        d = neg.decide_dealer(state(CONCEDED), neg.policy_for("abuela", "score", 2), 20, 4)
        self.assertEqual(d.action, "accept")
        third = neg.dealer_accept_priority("SECURE", 2, None)
        self.assertGreater(third, neg.dealer_accept_priority("SECURE", 0, None))
        self.assertGreater(third, 10 ** 5 + 1000, "por encima de una aceptación de campaña")
        self.assertGreater(third, 10 ** 4 + 500, "por encima de una aceptación de mercado")
        self.assertLess(third + 5 * 10 ** 4, 10 ** 6, "nunca por encima de la seguridad")

    def test_03_three_deals_optimize_may_keep_negotiating(self):
        self.assertEqual(neg.ladder_mode(3), "OPTIMIZE")
        pol = neg.policy_for("abuela", "score", 3)
        self.assertFalse(pol.secure)
        self.assertEqual(neg.decide_dealer(state(CONCEDED), pol, 20, 4).action, "counter")

    def test_04_opening_price_never_accepted_in_score_mode(self):
        st = state([her(1, 97, 17)])  # solo su apertura, vigente
        for n in (0, 2):
            d = neg.decide_dealer(st, neg.policy_for("abuela", "score", n), ceiling=30, conv_ticks_left=4)
            self.assertNotEqual(d.action, "accept")
        final_open = state([her(1, 97, 17, final=True)])
        self.assertEqual(neg.decide_dealer(final_open, neg.policy_for("abuela", "score", 0), 30, 4).action, "abandon")

    def test_05_final_offer_below_opening_inside_ceiling_accept_above_ceiling_reject(self):
        msgs = [her(1, 96, 17, "cancelled"), ours(2, 96, 11), her(3, 97, 15, final=True)]
        pol = neg.policy_for("abuela", "score", 1)
        self.assertEqual(neg.decide_dealer(state(msgs), pol, 16, 4).action, "accept")
        self.assertEqual(neg.decide_dealer(state(msgs), pol, 14, 4).action, "abandon", "15 > máximo 14")
        self.assertEqual(neg.decide_dealer(state(CONCEDED), pol, 13, 4).action != "accept", True,
                         "SECURE nunca acepta por encima del máximo económico")

    def test_06_qualifying_deals_from_server_threads_and_journal(self):
        settled = thread([her(1, 96, 17, "cancelled"), ours(2, 96, 11), her(3, 97, 14, "settled")], status="deal")
        at_open = thread([her(4, 96, 20, "settled")], status="deal", tid=8)  # aceptó la apertura: no cuenta
        deals = neg.qualifying_deals("abuela", [settled, at_open],
                                     [outcome(T, 17, 14), outcome(9, 12, 10), outcome(10, 12, 12), outcome(11, 9, 5, "chato")])
        self.assertEqual(sorted(d["thread"] for d in deals), [7, 9])
        self.assertEqual([d["source"] for d in deals if d["thread"] == 7], ["servidor"])


class Urgency(unittest.TestCase):
    def test_07_expiring_valid_dealer_close_outranks_market_accepts(self):
        expiring = neg.dealer_accept_priority("OPTIMIZE", 3, 1)
        self.assertGreater(expiring, neg.dealer_accept_priority("OPTIMIZE", 3, None))
        dealer = {"type": "dealer_accept", "thread": T, "ref": "card:LAT-03", "score": expiring, "blockers": []}
        market = {"type": "accept", "receive": {"MAL-02": 1}, "score": 10 ** 4 + 80, "blockers": []}
        team = {"type": "team_accept", "receive": {"MAL-03": 1}, "score": 10 ** 5 + 1000, "blockers": [], "thread": 9}
        chosen = co.select([market, team, dealer], {"actions": [], "class_tick": {}}, 100)
        self.assertEqual([c["type"] for c in chosen], ["dealer_accept"], "una sola aceptación por tick: la del vendedor")


def cargs(**kw):
    base = dict(mode="score", max_spend=200, reserve=100, per_card=60, margin=2.0, listing_ticks=10, fill_prior=0.3,
                allow_last_copy="", cancel_unsafe=False, allow_concurrent=False, show=0, campaign="none",
                campaign_ticks=30, campaign_budget=60, max_proposals=3, negotiation_ticks=6, max_conversations=2,
                engine="intel", duende_venue="v02", duende_expiry=120, history_window=240, dealer_liquidity=40,
                min_cancel_gain=2.0, rebalance_threshold=20, stale_age=30)
    base.update(kw)
    return Namespace(**base)


BASE = [A(1, "LAT-01"), A(2, "LAT-02"), A(3, "MAL-01"), A(4, "MAL-01")]  # LAT 2/3: LAT-03 completa la página
MY_BIDS = [bid(50, "v02", "MAL-02", 5, maker=TEAM, created=80), bid(51, "v02", "MAL-03", 6, maker=TEAM, created=80)]


def world(cash, msgs=None, bids=MY_BIDS):
    s = snap(BASE, mine=list(bids), cash=cash)
    s["threads"] = {"open": [thread(msgs)] if msgs else [], "deal": []}
    s["dealers"] = {"abuela": {"menu": {"sells": [{"rarity": "common", "list_price": 10}]}}}
    s["me"]["unlocked"] = ["abuela"]
    s["levels"] = {"levels": []}
    return s


class Capital(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = co.DATA
        co.DATA = Path(self.dir.name)
        self.journal = neg.Journal(self.dir.name)

    def tearDown(self):
        co.DATA = self.saved
        self.dir.cleanup()

    def cands(self, s, **kw):
        led = {"actions": [], "spent_confirmed": 0, "threads": [T], "blocked": {}, "class_tick": {}, "expiry_obs": []}
        return co.candidates(s, led, cargs(**kw), self.journal)

    def test_08_dealer_buffer_and_market_cannot_consume_it(self):
        cands, pl, _ = self.cands(world(200, CONCEDED, bids=[]))
        cap = pl["capital"]
        for k in ("cash", "hard_reserve", "dealer_liquidity_target", "market_reserved_cash", "dealer_exposure",
                  "free_market_cash", "free_dealer_cash"):
            self.assertIn(k, cap)
        self.assertEqual(cap["dealer_liquidity_target"], 20)  # máximo económico de la negociación activa (LAT-03)
        self.assertEqual(cap["free_dealer_cash"], 100)
        # pasivo = min(100 − 20 vendedores − 30 táctico, 50 % de 100) = 50; las compras inmediatas ven 80
        self.assertEqual((cap["tactical_buffer"], cap["passive_limit"], cap["free_market_cash"]), (30, 50, 50))
        self.assertEqual(cap["free_tactical_cash"], 80)
        self.assertEqual(pl["free_cash"], 80, "aceptaciones inmediatas: 100 − 20 de liquidez de vendedores")
        for c in cands:
            if c["type"] == "bid" and not c["blockers"]:
                self.assertLessEqual(c["price"], 50, "una puja pasiva nunca usa el colchón táctico")
        acc = next(c for c in cands if c["type"] == "dealer_accept")
        self.assertEqual((acc["price"], acc["blockers"], acc["ladder_mode"]), (14, [], "SECURE"))

    def test_09_bids_cannot_starve_dealer_close_rebalancer_cancels_weakest(self):
        cands, pl, _ = self.cands(world(121, CONCEDED))  # 121 − 100 − 11 en pujas = 10 libres; el vendedor pide 14
        acc = next(c for c in cands if c["type"] == "dealer_accept")
        self.assertTrue(any(b.startswith("capital") for b in acc["blockers"]), "espera a que se confirme la cancelación")
        cancels = [c for c in cands if c.get("capital_cancel") and "REBALANCEO" in c["reason"]]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["blockers"], [])
        weakest = pl["open_bids"][0]["offer"]
        self.assertEqual(cancels[0]["offer"], weakest)
        self.assertIn("REBALANCEO", cancels[0]["reason"])
        chosen = co.select(cands, {"actions": [], "class_tick": {}}, 100, max_posts=4)
        self.assertIn(weakest, [c.get("offer") for c in chosen])
        self.assertNotIn("dealer_accept", [c["type"] for c in chosen])
        diag = pl["dealer_diag"]["abuela"]
        self.assertTrue(diag["concession"])
        self.assertIn("ACCEPT 14", co.dealer_lines(pl["dealer_diag"])[0])

    def test_10_rebalance_unit_and_dominance(self):
        bids = [{"offer": 1, "ref": "A", "venue": "v02", "price": 10, "age": 9, "expected_du": 0.5, "efficiency": 0.05,
                 "p_fill": 0.05, "confidence": "HEURISTIC", "sample_count": 0},
                {"offer": 2, "ref": "B", "venue": "v02", "price": 30, "age": 9, "expected_du": 6.0, "efficiency": 0.2,
                 "p_fill": 0.3, "confidence": "HEURISTIC", "sample_count": 0}]
        cfg = ca.CapitalConfig()
        cancels, released, _ = ca.rebalance(bids, 8, 20.0, cfg, "x")
        self.assertEqual(([c["offer"] for c in cancels], released), ([1], 10))
        self.assertEqual(ca.rebalance(bids, 8, 1.0, cfg, "x")[0], [], "no compensa cancelar por algo peor")
        self.assertEqual(ca.rebalance(bids, 100, 99.0, cfg, "x")[0], [], "no alcanza: no se cancela nada")
        young = [dict(bids[0], age=1)]
        self.assertEqual(ca.rebalance(young, 5, 50.0, cfg, "x")[0], [], "sin churn: puja demasiado joven")

    def test_11_stale_bid_cancellation(self):
        cfg = ca.CapitalConfig()
        old = {"offer": 1, "ref": "A", "venue": "v02", "price": 10, "age": 40, "expected_du": 0.1, "efficiency": 0.01,
               "p_fill": 0.02, "confidence": "HEURISTIC", "sample_count": 0, "acquired": False, "supply": 0.0,
               "surplus": 5.0, "gain": 15.0}
        scarce = ca.capital_view(120, 100, 10, 0, 0, 0, 200)
        rich = ca.capital_view(400, 100, 10, 0, 0, 0, 400)
        self.assertEqual(len(ca.stale_bid_cancels([old], scarce, cfg, 2.0)), 1)
        self.assertEqual(ca.stale_bid_cancels([old], rich, cfg, 2.0), [], "con capital de sobra no se hace churn")
        self.assertEqual(len(ca.stale_bid_cancels([dict(old, acquired=True, age=5)], rich, cfg, 2.0)), 1)
        self.assertEqual(len(ca.stale_bid_cancels([dict(old, surplus=1.0, age=5)], rich, cfg, 2.0)), 1)
        self.assertEqual(ca.stale_bid_cancels([dict(old, acquired=True, age=1)], rich, cfg, 2.0), [])
        twin = [dict(old, offer=4194, ref="MAL-10", price=35, age=2), dict(old, offer=4225, ref="MAL-10", price=38, age=2)]
        dup = ca.stale_bid_cancels(twin, rich, cfg, 2.0)
        self.assertEqual([c["offer"] for c in dup], [4194], "dos pujas por la misma carta: se conserva la más alta")


class SwapSafety(unittest.TestCase):
    def setUp(self):
        self.val = tr.Valuation(catalog(), {"LAT": 1.3, "MAL": 1.0})
        self.counts = Counter({"LAT-01": 1, "LAT-02": 1, "MAL-01": 2})

    def own_swap(self, oid, give_id, give_ref, want_ref):
        return offer(oid, "v02", {"assets": [A(give_id, give_ref)]}, {"types": [f"card:{want_ref}"]}, maker=TEAM)

    def test_12_profitable_swap_not_unsafe_and_incoming_card_valued(self):
        o = self.own_swap(60, 4, "MAL-01", "LAT-03")  # duplicado → completa la página
        ev = tr.evaluate_own_open_offer(o, self.val, self.counts)
        dv, _ = self.val.delta(self.counts, Counter({"LAT-03": 1}), Counter({"MAL-01": 1}))
        self.assertEqual((ev["receive"], ev["deliver"], ev["du"]), (Counter({"LAT-03": 1}), Counter({"MAL-01": 1}), dv))
        self.assertGreater(ev["du"], 0)
        self.assertEqual(tr.unsafe_own_offers([o], TEAM, self.val, self.counts), [])

    def test_13_genuinely_negative_swap_is_unsafe(self):
        o = self.own_swap(61, 1, "LAT-01", "MAL-01")  # rompe progreso de página por un duplicado
        bad = tr.unsafe_own_offers([o], TEAM, self.val, self.counts)
        self.assertEqual(len(bad), 1)
        self.assertTrue(any("ΔU" in w and "trueque" in w for w in bad[0]["why"]))

    def test_14_planner_and_safety_agree_on_the_same_swap(self):
        s = snap(BASE, [offer(70, "v02", {"assets": [A(80, "LAT-03")]}, {"cash": 30}, maker="m1")])
        swap = next(o for o in mi.plan(s, mi.IntelConfig(), expiry_ratio=2.0)["opportunities"]
                    if o["type"] == "swap_list")
        published = self.own_swap(62, swap["asset"], swap["give_ref"], swap["ref"])
        ev = tr.evaluate_own_open_offer(published, self.val, self.counts)
        self.assertEqual(ev["du"], swap["du"], "publicación y seguridad dan el MISMO ΔU")
        self.assertEqual(tr.unsafe_own_offers([published], TEAM, self.val, self.counts), [])
        s2 = snap(BASE, mine=[dict(published, created_tick=90)], tick=100)
        retire = [o for o in mi.plan(s2, mi.IntelConfig(), expiry_ratio=2.0)["opportunities"]
                  if o["type"] == "cancel" and o.get("offer") == 62]
        self.assertEqual(retire, [], "el reprecio tampoco lo retira")


class Exposure(unittest.TestCase):
    def test_15_duplicate_physical_asset_exposure(self):
        a = offer(30, "v02", {"assets": [A(3, "MAL-01")]}, {"cash": 9}, maker=TEAM)
        b = offer(31, "v03", {"assets": [A(3, "MAL-01")]}, {"cash": 8}, maker=TEAM)
        ok = offer(32, "v02", {"assets": [A(4, "MAL-01")]}, {"cash": 8}, maker=TEAM)
        exp = pg.asset_exposure([a, b, ok], TEAM)
        self.assertEqual(len(exp[3]), 2)
        out = pg.exposure_conflicts([a, b, ok], TEAM, my_assets=BASE)
        self.assertEqual([(c["offer"], c["blockers"], c["score"] > 10 ** 6) for c in out], [(31, [], True)])
        pend = [{"key": "acc", "assets": [4], "type": "accept"}]
        self.assertEqual([c["offer"] for c in pg.exposure_conflicts([ok], TEAM, pend, BASE)], [32],
                         "una aceptación pendiente gana a la oferta")
        s = snap(BASE, mine=[a])
        self.assertTrue(co.double_commit({"type": "list", "ref": "MAL-01", "asset": 3}, s, {3}))
        self.assertIsNone(co.double_commit({"type": "list", "ref": "MAL-01", "asset": 4}, s, {3}))

    def test_16_completed_page_protection_still_holds(self):
        full_page = BASE + [A(5, "LAT-03")]
        s = snap(full_page)
        self.assertTrue(pg.guard_candidate({"type": "list", "ref": "LAT-03", "asset": 5}, s))
        self.assertEqual(pg.guard_candidate({"type": "list", "ref": "MAL-01", "asset": 4}, s), [])


class LiveLoop(unittest.TestCase):
    class API:
        def __init__(self):
            self.waits = 0

        def wait_tick(self):
            self.waits += 1
            return {}

    def test_17_rate_limited_does_not_terminate_and_idle_ticks_continue(self):
        calls = []

        def cycle_fn(reader, args, led, journal, execute, cache):
            calls.append(len(calls))
            if len(calls) == 2:
                raise BazaarError("rate_limited", "at most 5 requests per second", 429)
            return 0  # "Sin oportunidades válidas"

        api = self.API()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            saved = co.LEDGER
            co.LEDGER = Path(d) / "l.json"
            try:
                done = co.run_loop(api, None, Namespace(ticks=5, execute=True), {"actions": []}, None, cycle_fn)
            finally:
                co.LEDGER = saved
        self.assertEqual((done, len(calls), api.waits), (5, 5, 4))
        self.assertIn("rate_limited", out.getvalue())

    def test_18_non_transient_error_still_stops(self):
        def boom(*a):
            raise BazaarError("forbidden", "no", 403)
        with self.assertRaises(BazaarError):
            co.run_loop(self.API(), None, Namespace(ticks=3, execute=True), {"actions": []}, None, boom)

    def test_19_slow_resources_cached_and_invalidated_by_events(self):
        n = Counter()

        class R:
            def call(self, method, *a):
                n[method] += 1
                return {"v": n[method]}

        cache = co.SlowCache()
        cache.invalidate_from([], 10)
        for tick in (10, 11, 12):
            cache.get(R(), tick, "catalog")
            cache.get(R(), tick, "me")
        self.assertEqual((n["catalog"], n["me"]), (1, 3))
        cache.invalidate_from([{"type": "set.released", "tick": 12}], 13)
        cache.get(R(), 13, "catalog")
        self.assertEqual(n["catalog"], 2)


class Confidence(unittest.TestCase):
    def test_20_fill_confidence_labels_and_tiny_samples(self):
        self.assertEqual([mi.fill_confidence(n) for n in (0, 1, 4, 5, 14, 15)],
                         ["HEURISTIC", "EARLY DATA / LOW CONFIDENCE", "EARLY DATA / LOW CONFIDENCE", "LEARNING",
                          "LEARNING", "LEARNED"])
        st = mi.plan(snap(BASE), mi.IntelConfig(), expiry_ratio=2.0)["states"]["MAL-01"]
        v = mi.venues_from(snap(BASE))["v02"]
        p0, b0 = mi.fill_probability("ask", 9, st, v, mi.IntelConfig(), [])
        one = [{"type": "list", "venue": "v02", "status": "settled"}]
        p1, b1 = mi.fill_probability("ask", 9, st, v, mi.IntelConfig(), one)
        self.assertEqual(b0, "HEURISTIC")
        self.assertTrue(b1.startswith("EARLY DATA"))
        self.assertLess(p1 - p0, 0.1, "una sola publicación llenada no domina")
        self.assertEqual(mi.fill_stats(one, "v02", "ask"), (1, "EARLY DATA / LOW CONFIDENCE"))


class Performance(unittest.TestCase):
    def test_21_realized_vs_open_vs_estimated(self):
        actions = [
            {"type": "list", "venue": "v02", "status": "settled", "price": 10, "du": 6.0, "tick": 100, "settled_tick": 104},
            {"type": "dealer_accept", "status": "settled", "price": 14, "paid": 14, "dv": 22.75, "du": 8.75,
             "tick": 101, "settled_tick": 102},
            {"type": "list", "venue": "v02", "status": "released", "price": 9, "du": 4.0, "tick": 90},
            {"type": "bid", "venue": "v02", "status": "submitted", "price": 12, "du": 9.0, "expected_du": 1.7,
             "tick": 103},
            {"type": "dealer_open", "status": "settled"}, {"type": "dealer_open", "status": "settled"},
            {"type": "dealer_close", "status": "settled"}]
        p = perf.realized(actions, {"abuela": [1, 2]}, open_offers=1)
        self.assertEqual(p["realized_surplus"], round(6.0 + (-14 + 22.75), 2))
        self.assertEqual(p["settlement_count"], 2)
        self.assertEqual(p["median_ticks_to_fill"], 2.5)
        self.assertEqual(p["fill_rate"], {"v02/list": "1/2"})
        self.assertEqual((p["dealer_deals"], p["dealer_conversations"], p["dealer_abandoned"]), (1, 2, 1))
        self.assertEqual(p["dealer_qualifying"], {"abuela": 2})
        self.assertEqual(p["open_expected_du"], 1.7, "lo abierto es ESTIMADO, no beneficio")
        self.assertIn("no es beneficio", perf.line(p))


if __name__ == "__main__":
    unittest.main()
