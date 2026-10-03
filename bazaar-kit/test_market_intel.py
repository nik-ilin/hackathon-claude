"""Pruebas offline de la inteligencia de mercado multi-venue (Day 2). Sin red ni operaciones reales."""
import contextlib
import copy
import io
import os
import tempfile
import unittest
from argparse import Namespace
from collections import Counter

import coordinator as co
import market_intel as mi
import negotiation as neg
import trading as tr

TEAM = "t15"
U = 13.0  # LAT: book 10 x 1.3
VENUES = [{"venue": "rastro", "name": "El Rastro", "owner": "world", "status": "open", "fee_bps": 500, "fee_per_card": 1,
           "rules": {}},
          {"venue": "v02", "name": "El Duende", "owner": "t12", "status": "open", "fee_bps": 0, "fee_per_card": 0,
           "rules": {"mechanism": "board"}},
          {"venue": "v03", "name": "Mercado Trece", "owner": "t13", "status": "open", "fee_bps": 100, "fee_per_card": 0,
           "rules": {"mechanism": "board"}}]


def catalog():
    def cards(sid, n=3):
        return [{"id": f"{sid}-{i:02d}", "name": f"{sid}{i}", "rarity": "common", "book": 10, "page": True}
                for i in range(1, n + 1)]
    return {"values": {"copy_marginals": [1.0, 0.25, 0.1], "page_bonus": 0.25, "master_bonus": 0.1},
            "sets": [{"id": "LAT", "name": "La Latina", "released": True, "cards": cards("LAT")},
                     {"id": "MAL", "name": "Malasaña", "released": True, "cards": cards("MAL")}], "packs": []}


def A(aid, ref):
    return {"id": aid, "kind": "card", "ref": ref}


def offer(oid, venue, give, want, maker="m1", to=None, exp=150, created=99, status="open"):
    g = {"cash": 0, "assets": [], "types": []}
    g.update(give)
    w = {"cash": 0, "assets": [], "types": []}
    w.update(want)
    return {"id": oid, "maker": maker, "to": to, "venue": venue, "thread": None, "status": status, "give": g, "want": w,
            "expires_tick": exp, "created_tick": created}


def ask(oid, venue, aid, ref, price, **kw):
    return offer(oid, venue, {"assets": [A(aid, ref)]}, {"cash": price}, **kw)


def bid(oid, venue, ref, price, **kw):
    return offer(oid, venue, {"cash": price}, {"types": [f"card:{ref}"]}, **kw)


def swap(oid, venue, aid, ref, want_ref, **kw):
    return offer(oid, venue, {"assets": [A(aid, ref)]}, {"types": [f"card:{want_ref}"]}, **kw)


def snap(assets, boards=None, mine=(), feed=(), cash=300, tick=100, venues=None):
    cat = catalog()
    aff = {"LAT": 1.3, "MAL": 1.0}
    val = tr.Valuation(cat, aff)
    me = {"id": TEAM, "name": "Team 15", "cash": cash, "level": 2, "affinity": aff, "assets": list(assets),
          "collection_value": round(val.total(tr.counts_of(assets)), 2), "score": {}}
    b = {"rastro": {"offers": []}, "v02": {"offers": []}, "v03": {"offers": []}}
    for o in (boards or []):
        b[o["venue"]]["offers"].append(o)
    return {"me": me, "catalog": cat, "clock": {"tick": tick, "tick_seconds": 30.0, "limits": {
            "offers_per_team_per_tick": 12, "accepts_per_team_per_tick": 1, "max_open_offers_per_team": 30}},
            "venues": {"venues": copy.deepcopy(venues or VENUES)}, "boards": b, "board": b["rastro"],
            "offers": {"offers": list(mine)}, "feed": {"events": list(feed)}}


BASE = [A(1, "LAT-01"), A(2, "LAT-02"), A(3, "MAL-01"), A(4, "MAL-01")]  # LAT 2/3 (falta LAT-03), MAL-01 duplicada


def plan(s, **kw):
    hist = kw.pop("history", None)
    actions = kw.pop("actions", ())
    return mi.plan(s, mi.IntelConfig(**kw), history=hist, actions=actions, expiry_ratio=2.0)


def ops(pl, **match):
    return [o for o in pl["opportunities"] if all(o.get(k) == v for k, v in match.items())]


def state(s, ref):
    return plan(s)["states"][ref]


class Venues(unittest.TestCase):
    def test_01_v02_zero_fee_is_read_from_the_api(self):
        v = mi.venues_from(snap(BASE))
        self.assertEqual((v["v02"].fee(100, 1), v["rastro"].fee(20, 1), v["v03"].fee(20, 1)), (0, 2, 1))
        vs = copy.deepcopy(VENUES)
        vs[1]["pending_fee"] = {"fee_bps": 300, "fee_per_card": 0}
        self.assertEqual(mi.venues_from(snap(BASE, venues=vs))["v02"].fee(100, 1), 3, "comisión anunciada: conservador")

    def test_02_venue_selection_by_effective_cost_not_sticker_price(self):
        s = snap(BASE, [ask(10, "rastro", 70, "LAT-03", 12), ask(11, "v02", 71, "LAT-03", 13)])
        buys = sorted(ops(plan(s), type="accept", kind="comprar"), key=lambda o: -o["score"])
        self.assertEqual(buys[0]["venue"], "v02")  # 13 + 0 < 12 + 2
        self.assertEqual([o["fee"] for o in buys], [0, 2])


class Estimation(unittest.TestCase):
    def test_03_executions_dominate(self):
        e = mi.estimate_market_value([], [], [12, 14, 13, 15, 13], 10)
        self.assertEqual((e.value, e.confidence, e.source), (13, "HIGH", "ejecuciones"))

    def test_04_a_high_ask_is_not_a_value(self):
        q = [mi.Quote("rastro", 1, "m1", "ask", "LAT-03", 200)]
        e = mi.estimate_market_value(q, [], [], 10)
        self.assertEqual((e.value, e.confidence), (15.0, "LOW"))
        self.assertTrue(any("no hay pujas" in x for x in e.evidence))

    def test_microprice_with_bids_and_asks(self):
        e = mi.estimate_market_value([mi.Quote("v02", 1, "a", "ask", "X", 20)], [mi.Quote("v02", 2, "b", "bid", "X", 16)],
                                     [], 10)
        self.assertEqual((e.value, e.confidence), (18.0, "MEDIUM"))


class Pricing(unittest.TestCase):
    def test_05_profitable_undercut_uses_the_fee_advantage(self):
        s = snap(BASE, [ask(10, "rastro", 70, "MAL-01", 30)])
        pl = plan(s)
        lst = ops(pl, type="list", ref="MAL-01")[0]
        v = mi.venues_from(s)
        self.assertEqual(lst["venue"], "v02")
        self.assertLess(lst["price"] + v["v02"].fee(lst["price"], 1), 30 + v["rastro"].fee(30, 1))  # más barato para él
        self.assertGreaterEqual(lst["price"], lst["floor"])
        self.assertGreaterEqual(lst["price"], 30, "sin comisión podemos cobrar ≥ su precio y seguir siendo más baratos")

    def test_06_unprofitable_undercut_is_refused(self):
        s = snap(BASE, [ask(10, "v02", 70, "MAL-01", 3)])  # rival a 3 P; nuestro suelo = 2.5 + 2 -> 5
        st = state(s, "MAL-01")
        r = mi.competitive_sell_price(st, mi.venues_from(s)["v02"], mi.IntelConfig())
        self.assertTrue(r["stop"])
        self.assertIsNone(r["price"])
        self.assertFalse(ops(plan(s), type="list", ref="MAL-01", venue="v02"))

    def test_07_adaptive_undercut(self):
        c = mi.IntelConfig()
        self.assertEqual(mi.undercut_step(5, None, 0.5, c), 1)
        self.assertGreaterEqual(mi.undercut_step(500, None, 0.5, c), 15)
        self.assertLessEqual(mi.undercut_step(500, 6, 0.5, c), 3, "el spread acota el paso")

    def test_08_price_war_stops(self):
        s = snap(BASE, [ask(10, "v02", 70, "MAL-01", 9)])
        st, v, c = state(s, "MAL-01"), mi.venues_from(s)["v02"], mi.IntelConfig()
        self.assertTrue(mi.competitive_sell_price(st, v, c, reprices=2)["stop"])
        r = mi.competitive_sell_price(st, v, c, reprices=1, last_price=9)
        self.assertTrue(r["stop"])

    def test_bid_is_below_reservation_and_ladders_up(self):
        s = snap(BASE, [ask(10, "rastro", 70, "LAT-03", 40)])
        st, v, c = state(s, "LAT-03"), mi.venues_from(s)["v02"], mi.IntelConfig()
        b0 = mi.calculate_bid_price(st, v, c, cap=200)
        self.assertLess(b0["price"], b0["reservation"])
        b1 = mi.calculate_bid_price(st, v, c, cap=200, last_bid=b0["price"])
        self.assertGreater(b1["price"], b0["price"])
        self.assertLess(b1["price"], b0["reservation"])

    def test_no_lowball_bid_far_below_market(self):
        s = snap(BASE, [ask(10, "rastro", 70, "LAT-03", 400)])
        st = state(s, "LAT-03")
        self.assertIsNone(mi.calculate_bid_price(st, mi.venues_from(s)["v02"], mi.IntelConfig(), cap=5)["price"])


class Bids(unittest.TestCase):
    def test_09_directed_bid_is_filled_immediately(self):
        s = snap(BASE, mine=[bid(20, "v02", "MAL-01", 12, maker="t05", to=TEAM)])
        sell = ops(plan(s), type="accept", kind="vender")[0]
        self.assertEqual((sell["venue"], sell["fee"], sell["source"]), ("v02", 0, "dirigida"))

    def test_10_11_public_bid_for_a_missing_card_goes_to_the_duende(self):
        s = snap(BASE, [ask(10, "rastro", 70, "LAT-03", 25)])
        b = ops(plan(s), type="bid", ref="LAT-03")[0]
        self.assertEqual((b["venue"], b["to"]), ("v02", None))
        self.assertLess(b["price"], b["reservation"])

    def test_targeted_bid_needs_published_evidence(self):
        feed = [{"type": "offer.listed", "tick": 99, "actor": "t05", "payload": {"offer": ask(30, "rastro", 72, "LAT-03", 30,
                                                                                             maker="t05")}}]
        s = snap(BASE, feed=feed)
        s["leaderboard"] = {"teams": [{"team": "t05"}, {"team": TEAM}]}
        b = ops(plan(s), type="bid", ref="LAT-03")
        self.assertTrue(b)
        self.assertIn(b[0]["to"], (None, "t05"))
        self.assertFalse(ops(plan(snap(BASE)), type="bid", to="t05"), "sin evidencia no hay puja dirigida")

    def test_12_listing_on_v02(self):
        s = snap(BASE, [ask(10, "rastro", 70, "MAL-01", 20)])
        self.assertEqual(ops(plan(s), type="list", ref="MAL-01")[0]["venue"], "v02")

    def test_13_14_expiry_defaults_and_configuration(self):
        s = snap(BASE, [ask(10, "rastro", 70, "MAL-01", 20)])
        self.assertEqual(ops(plan(s), type="list")[0]["expires_in"], 120)
        self.assertEqual(ops(plan(s, duende_expiry_ticks=60), type="list")[0]["expires_in"], 60)
        no_duende = snap(BASE, [ask(10, "rastro", 70, "MAL-01", 20)], venues=[VENUES[0], VENUES[2]])
        lst = ops(plan(no_duende), type="list")[0]
        self.assertEqual(lst["expires_in"], tr.listing_request(10, 2.0))  # resto: vigencia efectiva calibrada


class Swaps(unittest.TestCase):
    def test_15_profitable_swap_is_accepted(self):
        s = snap(BASE, [swap(40, "v02", 70, "LAT-03", "MAL-01")])
        o = ops(plan(s), type="accept", kind="intercambio")[0]
        self.assertEqual(o["fee"], 0)
        self.assertAlmostEqual(o["du"], U + 0.25 * 3 * U - 2.5, places=2)  # completa LAT; perdemos la 2.ª MAL-01

    def test_16_unprofitable_swap_is_ignored(self):
        s = snap(BASE, [swap(41, "v02", 70, "LAT-01", "MAL-01")])  # nos da una copia que ya tenemos
        o = ops(plan(s), type="accept", kind="intercambio")
        self.assertTrue(not o or o[0]["du"] >= 2)
        s = snap(BASE, [swap(42, "v02", 70, "MAL-01", "LAT-01")])  # nos pide la única LAT-01
        self.assertFalse([x for x in ops(plan(s), type="accept") if not x["blockers"]])

    def test_17_18_swap_fees_by_venue(self):
        for venue, fee in (("v03", 0), ("v02", 0), ("rastro", 2)):
            s = snap(BASE, [swap(40, venue, 70, "LAT-03", "MAL-01")])
            self.assertEqual(ops(plan(s), type="accept", kind="intercambio")[0]["fee"], fee, venue)

    def test_publish_swap_with_opportunity_cost(self):
        s = snap(BASE, [ask(10, "rastro", 70, "LAT-03", 40), bid(11, "v02", "MAL-01", 9)])
        sw = ops(plan(s), type="swap_list")
        self.assertTrue(sw)
        self.assertEqual(sw[0]["opportunity_cost"], ops(plan(s), type="accept", kind="vender")[0]["du"])  # 21


class Liquidation(unittest.TestCase):
    def test_19_20_existing_bid_beats_a_speculative_listing(self):
        s = snap(BASE, [bid(10, "v02", "MAL-01", 18), ask(11, "rastro", 71, "MAL-01", 30)])
        pl = plan(s)
        sell = ops(pl, type="accept", kind="vender")[0]
        self.assertEqual(sell["du"], 15.5)  # 18 - 0 comisión - 2.5 perdidos
        lst = ops(pl, type="list", ref="MAL-01")
        self.assertTrue(not lst or lst[0]["score"] < sell["score"])
        led = {"actions": [], "class_tick": {}}
        chosen = co.select([dict(o, module="mercado") for o in pl["opportunities"]], led, 100, max_posts=4)
        self.assertEqual([c["type"] for c in chosen if c.get("ref") == "MAL-01" or c.get("deliver")][:1], ["accept"])

    def test_21_opportunity_cost_on_effective_loss(self):
        s = snap(BASE, [bid(10, "v02", "MAL-01", 20), ask(11, "rastro", 70, "LAT-03", 40)])
        sw = ops(plan(s), type="swap_list")[0]
        self.assertEqual(sw["opportunity_cost"], 17.5)
        self.assertLess(sw["score"], sw["expected_du"])

    def test_22_page_completion_bonus(self):
        st = state(snap(BASE), "LAT-03")
        self.assertTrue(st.completes_page)
        self.assertEqual(st.gain_if_bought, U + 0.25 * 3 * U)

    def test_23_asset_locking(self):
        mine = [ask(50, "v02", 4, "MAL-01", 15, maker=TEAM)]
        self.assertFalse(ops(plan(snap(BASE, mine=mine)), type="list", ref="MAL-01"))

    def test_24_cash_reservation(self):
        mine = [bid(51, "v02", "SAL-01", 150, maker=TEAM)]
        s = snap(BASE, [ask(10, "v02", 70, "LAT-03", 20)], mine=mine, cash=270)
        pl = plan(s)
        self.assertEqual(pl["free_cash"], 270 - 100 - 150)
        buy = ops(pl, type="accept", kind="comprar")[0]
        self.assertFalse(buy["blockers"])  # 20 cabe en 20
        pl = plan(snap(BASE, [ask(10, "v02", 70, "LAT-03", 20)], mine=mine, cash=269))  # libre 19 < 20
        self.assertTrue(any("capacidad" in b for b in ops(pl, type="accept", kind="comprar")[0]["blockers"]))


class MarketWide(unittest.TestCase):
    def test_25_arbitrage_is_reported_not_executed(self):
        s = snap(BASE, [ask(10, "v02", 70, "MAL-02", 20), bid(11, "v03", "MAL-02", 30)])
        pl = plan(s)
        self.assertEqual(pl["arbitrage"][0]["ref"], "MAL-02")
        self.assertFalse(ops(pl, type="arbitrage"))

    def test_26_repricing_cancels_a_stale_ask(self):
        mine = [ask(50, "v02", 4, "MAL-01", 40, maker=TEAM, created=90)]
        s = snap(BASE, [ask(10, "v02", 70, "MAL-01", 30)], mine=mine)
        c = ops(plan(s), type="cancel", offer=50)
        self.assertTrue(c and c[0]["reason"].startswith("reprecio"))
        young = snap(BASE, [ask(10, "v02", 70, "MAL-01", 30)], mine=[ask(50, "v02", 4, "MAL-01", 40, maker=TEAM, created=99)])
        self.assertFalse(ops(plan(young), type="cancel"), "edad mínima antes de repreciar")
        many = [{"type": "cancel", "ref": "MAL-01", "reason": "reprecio: x"}] * 2
        self.assertFalse(ops(plan(s, actions=many), type="cancel", kind="repreciar venta"), "límite de reprecios")

    def test_27_market_history(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.jsonl")
            s = snap(BASE, [ask(10, "v02", 70, "MAL-01", 30), bid(11, "v02", "MAL-01", 20)])
            books, _ = mi.build_books(s, mi.venues_from(s))
            h = mi.History(path, window=50, max_lines=10).load(100)
            setl = [{"settlement": 7, "tick": 99, "venue": "v02", "price": 21, "fee": 0,
                     "items": [{"kind": "card", "ref": "MAL-01"}], "parties": ["t1", "t2"]}]
            self.assertEqual(h.record(100, books, setl, 0.0), 2)
            self.assertEqual(h.record(101, {}, setl, 0.0), 0, "liquidación deduplicada")
            h2 = mi.History(path, window=50).load(101)
            self.assertEqual(h2.executions("MAL-01"), [21])
            self.assertEqual(mi.History(path, window=0).load(200).rows, [], "ventana temporal")

    def test_28_29_trends(self):
        up = [(t, 10 + t) for t in range(10)]
        self.assertEqual(mi.trend(up), "UP")
        self.assertEqual(mi.trend([(t, 20 - t) for t in range(10)]), "DOWN")
        self.assertEqual(mi.trend([(t, 15) for t in range(10)]), "STABLE")
        self.assertEqual(mi.trend(up[:3]), "UNKNOWN")

    def test_30_directed_offers_from_me_offers(self):
        mine = [ask(60, "v02", 80, "LAT-03", 15, maker="t05", to=TEAM), ask(61, "v02", 81, "LAT-03", 5, maker="t06",
                                                                             to="t09")]
        pl = plan(snap(BASE, mine=mine))
        buys = ops(pl, type="accept", kind="comprar")
        self.assertEqual([b["offer"] for b in buys], [60], "la dirigida a otro equipo no es nuestra")
        self.assertEqual(buys[0]["source"], "dirigida")


class Integration(unittest.TestCase):
    def test_select_respects_max_posts_and_one_acceptance(self):
        led = {"actions": [], "class_tick": {}}
        cands = [{"type": "list", "ref": f"MAL-0{i}", "score": 5 - i, "blockers": [], "module": "m"} for i in range(1, 4)]
        cands += [{"type": "accept", "receive": {"LAT-03": 1}, "score": 50, "blockers": [], "module": "m"},
                  {"type": "accept", "receive": {"SAL-01": 1}, "score": 40, "blockers": [], "module": "m"}]
        chosen = co.select(cands, led, 100, max_posts=2)
        self.assertEqual(sorted(c["type"] for c in chosen), ["accept", "list", "list"])

    def test_send_routes_venue_recipient_and_expiry(self):
        calls = []

        class API:
            def list_offer(self, give, want, venue=None, to=None, expires_in_ticks=None):
                calls.append((give, want, venue, to, expires_in_ticks))
                return {"id": 9, "created_tick": 100, "expires_tick": 160}

        class R:
            api = API()

        with tempfile.TemporaryDirectory() as d:
            saved = co.LEDGER
            co.LEDGER = co.Path(d) / "l.json"
            try:
                led = co.load_ledger(TEAM)
                s = snap(BASE)
                s["threads"] = {"open": [], "deal": []}
                c = {"type": "bid", "module": "mercado", "kind": "puja dirigida", "ref": "LAT-03", "price": 12,
                     "cash": -12, "venue": "v02", "to": "t05", "expires_in": 120}
                with contextlib.redirect_stdout(io.StringIO()):
                    co.send(R(), led, s, c, Namespace(listing_ticks=10, duende_venue="v02", mode="score"),
                            neg.Journal(d))
                c2 = {"type": "swap_list", "module": "mercado", "kind": "publicar trueque", "ref": "LAT-03",
                      "asset": 4, "price": 0, "venue": "v02", "expires_in": 120}
                with contextlib.redirect_stdout(io.StringIO()):
                    co.send(R(), led, s, c2, Namespace(listing_ticks=10, duende_venue="v02", mode="score"),
                            neg.Journal(d))
            finally:
                co.LEDGER = saved
        self.assertEqual(calls[0], ({"cash": 12}, {"cards": ["LAT-03"]}, "v02", "t05", 120))
        self.assertEqual(calls[1][:2], ({"assets": [4]}, {"cards": ["LAT-03"]}))
        self.assertEqual(led["actions"][0]["effective_ticks"], 60)

class AliasOwnOffers(unittest.TestCase):
    def test_own_offer_shown_under_alias_on_board_is_still_ours(self):
        mine = swap(8926, "rastro", 214, "MAL-01", "LAT-03", maker=TEAM)
        aliased = dict(mine, maker="m3950d43b")  # El Rastro anonimiza: también a nosotros
        s = snap(BASE, [aliased], mine=[mine])
        books, ours = mi.build_books(s, mi.venues_from(s))
        self.assertEqual([o["id"] for o in ours], [8926])
        self.assertFalse(books["MAL-01"]["swaps_give"], "nuestra oferta no es un trueque rival")
        pl = mi.plan(s, mi.IntelConfig(), expiry_ratio=2.0)
        self.assertFalse([o for o in pl["opportunities"] if o.get("offer") == 8926], "nunca aceptar la propia")
        self.assertFalse([o for o in pl["opportunities"] if o["type"] in ("bid", "swap_list") and o.get("ref") == "LAT-03"],
                         "ya perseguimos LAT-03 con ese trueque")


if __name__ == "__main__":
    unittest.main()
