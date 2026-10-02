"""Pruebas offline del motor de comercio (trading.py) y de su integración en market_agent.py (motor v2)."""
import copy
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path

import market_agent as ma
import negotiation as neg
import trading as tr
from bazaar_sdk import BazaarError

TEAM = "t15"
RASTRO = {"venue": "rastro", "status": "open", "fee_bps": 500, "fee_per_card": 1, "owner": "world"}


def catalog():
    def card(set_, i, rarity="common", book=10):
        return {"id": f"{set_}-{i:02d}", "name": f"{set_}{i}", "rarity": rarity, "book": book, "page": True}
    return {"values": {"copy_marginals": [1.0, 0.25, 0.1], "page_bonus": 0.25, "master_bonus": 0.1},
            "sets": [{"id": "LAT", "name": "La Latina", "released": True,
                      "cards": [card("LAT", i) for i in range(1, 4)]},
                     {"id": "RET", "name": "El Retiro", "released": False, "cards": [card("RET", 1)]}],
            "packs": []}


def asset(aid, ref):
    return {"id": aid, "kind": "card", "ref": ref}


def snap(assets, cash=200, offers=(), board=(), feed=(), tick=50, collection_value=None):
    cat = catalog()
    me = {"id": TEAM, "name": "Team 15", "cash": cash, "affinity": {"LAT": 1.3, "RET": 1.6}, "assets": list(assets),
          "score": {"score": 4.35, "neg_points": -40.9}, "venue": None}
    val = tr.Valuation(cat, me["affinity"])
    me["collection_value"] = round(val.total(tr.counts_of(assets)), 2) if collection_value is None else collection_value
    return {"me": me, "catalog": cat, "clock": {"tick": tick, "limits": {"accepts_per_team_per_tick": 1,
            "offers_per_team_per_tick": 12, "max_open_offers_per_team": 30}},
            "venues": {"venues": [dict(RASTRO)]}, "offers": {"offers": list(offers)}, "board": {"offers": list(board)},
            "feed": {"events": list(feed)}, "threads": {"open": [], "deal": []}, "team_threads": [],
            "levels": {"levels": []}}


def offer(oid, give, want, maker="m1", to=None, exp=90, status="open", venue="rastro", thread=None):
    g = {"cash": 0, "assets": [], "types": []}
    g.update(give)
    w = {"cash": 0, "assets": [], "types": []}
    w.update(want)
    return {"id": oid, "maker": maker, "to": to, "venue": venue, "thread": thread, "status": status,
            "give": g, "want": w, "expires_tick": exp}


U = 13.0  # book 10 x 1.3


class Valuation(unittest.TestCase):
    def setUp(self):
        self.v = tr.Valuation(catalog(), {"LAT": 1.3})

    def test_second_and_third_copy_have_different_marginals(self):
        c = Counter({"LAT-01": 3})
        self.assertEqual(self.v.delta(c, Counter(), Counter({"LAT-01": 1}))[0], -round(U * 0.1, 2))
        self.assertEqual(self.v.delta(c, Counter(), Counter({"LAT-01": 2}))[0], -round(U * 0.35, 2))
        self.assertEqual(self.v.delta(Counter({"LAT-01": 2}), Counter(), Counter({"LAT-01": 1}))[0], -round(U * 0.25, 2))

    def test_selling_last_copy_that_breaks_a_page_loses_the_bonus_and_is_protected(self):
        c = Counter({"LAT-01": 1, "LAT-02": 1, "LAT-03": 1})
        dv, notes = self.v.delta(c, Counter(), Counter({"LAT-02": 1}))
        self.assertEqual(dv, -(U + 0.25 * 3 * U))  # la carta y el bono de la página
        self.assertTrue(any("ROMPERÍA la página LAT" in n for n in notes))
        p = tr.Proposal(1, "rastro", "m1", 50, 0, Counter(), Counter({"LAT-02": 1}), [2], 1, 90, "tablón", 50)
        self.assertIn("entregaría la última copia de LAT-02", tr.evaluate(p, self.v, c, RASTRO).blockers)
        e = tr.evaluate(p, self.v, c, RASTRO, allow_last_copy=frozenset({"LAT-02"}))
        self.assertEqual((e.blockers, e.du), ([], round(50 - 4 - U * 1.75, 2)))

    def test_purchase_completing_page_counts_the_bonus_once(self):
        c = Counter({"LAT-01": 1, "LAT-02": 1})
        dv, notes = self.v.delta(c, Counter({"LAT-03": 1}), Counter())
        self.assertEqual(dv, U + 0.25 * 3 * U)
        self.assertTrue(any("completa la página LAT" in n for n in notes))
        full = Counter({"LAT-01": 2, "LAT-02": 1, "LAT-03": 1})  # una copia extra no vuelve a sumar el bono
        self.assertEqual(self.v.total(full), round(3 * U + 0.25 * U + 0.25 * 3 * U, 2))
        self.assertEqual(self.v.delta(full, Counter({"LAT-03": 1}), Counter())[0], round(U * 0.25, 2))

    def test_model_reproduces_the_real_api_value_of_LAT_10(self):  # /api/me/value LAT-10 = 177.1 en el tick 129
        books = [10] * 5 + [25] * 3 + [70] * 2
        rar = ["common"] * 5 + ["uncommon"] * 3 + ["rare"] * 2
        cat = {"values": {"copy_marginals": [1.0, 0.25, 0.1], "page_bonus": 0.25}, "sets": [{"id": "LAT",
               "released": True, "cards": [{"id": f"LAT-{i + 1:02d}", "book": b, "rarity": r, "page": True}
                                          for i, (b, r) in enumerate(zip(books, rar))]}]}
        v = tr.Valuation(cat, {"LAT": 1.3})
        have = Counter({f"LAT-{i:02d}": 1 for i in range(1, 10)})
        self.assertEqual(round(v.next_copy(have, "LAT-10"), 1), 177.1)
        self.assertEqual(round(v.next_copy(have, "LAT-03"), 1), 3.2)

    def test_model_matches_collection_value_and_mismatch_blocks(self):
        assets = [asset(1, "LAT-01"), asset(2, "LAT-01")]
        self.assertTrue(tr.plan(snap(assets), tr.Config())["valuation_verified"])
        pl = tr.plan(snap(assets, collection_value=99), tr.Config())
        self.assertFalse(pl["valuation_verified"])
        self.assertTrue(all("valoración no verificada" in o["blockers"] for o in pl["opportunities"]))


class Offers(unittest.TestCase):
    MINE = [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-01"), asset(4, "LAT-02")]

    def parse(self, o, locked=(), tick=50):
        return tr.parse_offer(o, team=TEAM, tick=tick, own_venue=None, my_assets=self.MINE, locked=set(locked))

    def test_swap_with_repeated_cards(self):  # nos dan LAT-03 a cambio de dos LAT-01 cualesquiera
        p, why = self.parse(offer(9, {"assets": [asset(70, "LAT-03")]}, {"types": ["card:LAT-01", "card:LAT-01"]}))
        self.assertEqual((p.receive, p.deliver, p.deliver_assets, p.n_cards), (Counter({"LAT-03": 1}),
                                                                               Counter({"LAT-01": 2}), [3, 2], 3))
        e = tr.evaluate(p, tr.Valuation(catalog(), {"LAT": 1.3}), tr.counts_of(self.MINE), RASTRO)
        self.assertEqual(e.dv, round(U - U * 0.1 - U * 0.25 + 0.25 * 3 * U, 2))  # LAT-03 completa la página
        self.assertEqual(e.fee, 3)  # 0 P de efectivo: solo 1 P por cada una de las 3 cartas

    def test_lot_with_two_copies_of_the_same_card(self):
        p, _ = self.parse(offer(9, {"assets": [asset(70, "LAT-02"), asset(71, "LAT-02")]}, {"cash": 6}))
        e = tr.evaluate(p, tr.Valuation(catalog(), {"LAT": 1.3}), tr.counts_of(self.MINE), RASTRO)
        self.assertEqual(e.dv, round(U * 0.25 + U * 0.1, 2))
        self.assertEqual((e.fee, e.cash), (3, -9))  # ceil(0.3)=1 + 2 cartas

    def test_rejections(self):
        cases = {
            "es nuestra": offer(1, {"assets": [asset(70, "LAT-03")]}, {"cash": 5}, maker=TEAM),
            "caduca ya": offer(2, {"assets": [asset(70, "LAT-03")]}, {"cash": 5}, exp=51),
            "dirigida a otro equipo": offer(3, {"assets": [asset(70, "LAT-03")]}, {"cash": 5}, to="t04"),
            "estructura no reconocida o con extras": offer(4, {"assets": [asset(70, "LAT-03")]}, {"cash": 5, "note": 1}),
            "estado expired": offer(5, {"assets": [asset(70, "LAT-03")]}, {"cash": 5}, status="expired"),
            "efectivo en ambos lados": offer(6, {"cash": 3, "assets": [asset(70, "LAT-03")]}, {"cash": 5}),
        }
        for why, o in cases.items():
            self.assertEqual(self.parse(o), (None, why))
        self.assertIsNone(self.parse(offer(7, {"cash": 5}, {"types": ["pack:sobre_barrio"]}))[0])
        self.assertEqual(self.parse(offer(8, {"cash": 5}, {"types": ["card:LAT-02"]}), locked={4})[1],
                         "pide LAT-02, que no tenemos libre")
        p, _ = self.parse(offer(10, {"cash": 9}, {"types": ["card:LAT-02"]}, to=TEAM))
        self.assertEqual(p.source, "dirigida")

    def test_fee_by_role_matches_observed_settlements(self):
        for price, cards, f in ((18, 2, 3), (40, 2, 4), (22, 1, 3), (9, 1, 2)):  # liquidaciones reales 130/140/142/146
            self.assertEqual(tr.fee(price, cards, RASTRO), f)
        pl = tr.plan(snap(self.MINE, board=[offer(9, {"cash": 12}, {"types": ["card:LAT-01"]})]), tr.Config())
        sell = next(o for o in pl["opportunities"] if o["kind"] == "vender")
        self.assertEqual((sell["fee"], sell["cash"]), (2, 10))  # aceptamos: pagamos ceil(0.6)+1
        lst = next(o for o in tr.plan(snap(self.MINE), tr.Config())["opportunities"] if o["type"] == "list")
        self.assertEqual(lst["fee"], 0)  # publicamos: la comisión la paga quien acepte


class Resources(unittest.TestCase):
    def test_expired_offers_release_reservations_and_one_listing_per_asset(self):
        mine = [asset(1, "LAT-01"), asset(2, "LAT-01")]
        bid_open = offer(20, {"cash": 21}, {"types": ["card:LAT-03"]}, maker=TEAM)
        bid_gone = offer(21, {"cash": 30}, {"types": ["card:LAT-02"]}, maker=TEAM, status="expired")
        list_a = offer(22, {"assets": [asset(2, "LAT-01")]}, {"cash": 9}, maker=TEAM)
        list_b = offer(23, {"assets": [asset(2, "LAT-01")]}, {"cash": 8}, maker=TEAM)
        r = tr.resources([bid_open, bid_gone, list_a, list_b], TEAM, [{"cost": 10, "assets": []}])
        self.assertEqual((r.reserved_cash, r.pending_cash, r.locked_assets), (21, 10, {2}))
        pl = tr.plan(snap(mine, cash=200, offers=[bid_open, list_a]), tr.Config())
        self.assertFalse([o for o in pl["opportunities"] if o["type"] == "list"], "no se duplica la publicación")
        self.assertFalse([o for o in pl["opportunities"] if o.get("ref") == "LAT-03" and o["type"] == "bid"])
        self.assertEqual(pl["free_cash"], 200 - 100 - 21)

    def test_rival_bid_lowers_fill_probability(self):
        mine = [asset(1, "LAT-01"), asset(2, "LAT-02")]
        rival = offer(30, {"cash": 60}, {"types": ["card:LAT-03"]})
        pl = tr.plan(snap(mine, board=[rival]), tr.Config(per_card=40))
        bid = next(o for o in pl["opportunities"] if o.get("ref") == "LAT-03")
        self.assertLess(bid["p_fill"], tr.Config().fill_prior)
        self.assertTrue(any("puja rival mayor" in n for n in bid["notes"]))

    def test_first_copy_is_kept_by_default(self):
        mine = [asset(1, "LAT-01"), asset(2, "LAT-02")]
        pl = tr.plan(snap(mine, board=[offer(9, {"cash": 50}, {"types": ["card:LAT-02"]})]), tr.Config())
        self.assertFalse([o for o in pl["opportunities"] if o["kind"] == "vender" and not o["blockers"]])
        pl = tr.plan(snap(mine, board=[offer(9, {"cash": 50}, {"types": ["card:LAT-02"]})]),
                     tr.Config(allow_last_copy=frozenset({"LAT-02"})))
        self.assertTrue([o for o in pl["opportunities"] if o["kind"] == "vender" and not o["blockers"]])

    def test_unreleased_set_is_not_bid(self):
        pl = tr.plan(snap([asset(1, "LAT-01")]), tr.Config())
        self.assertFalse([o for o in pl["opportunities"] if (o.get("ref") or "").startswith("RET")])


def settlement(sid, tick, items_in=(), items_out=(), price=17, fee=0, other="abuela", persona="abuela"):
    items = [{"id": i, "kind": k, "ref": r, "frm": other, "to": TEAM} for i, k, r in items_in]
    items += [{"id": i, "kind": k, "ref": r, "frm": TEAM, "to": other} for i, k, r in items_out]
    return {"type": "settlement", "tick": tick, "payload": {"settlement": sid, "tick": tick, "parties": [other, TEAM],
            "persona": persona, "venue": None if persona else "rastro", "fee": fee, "items": items, "price": price}}


class Reconciliation(unittest.TestCase):
    def test_settled_price_differs_from_our_last_counteroffer(self):  # hilo #123 real
        t = {"id": 123, "with": "abuela", "status": "deal", "messages": [
            {"offer": {"id": 1029, "maker": TEAM, "status": "cancelled", "give": {"cash": 13}, "want": {"cash": 0}}},
            {"offer": {"id": 1045, "maker": "abuela", "status": "settled", "give": {"cash": 0}, "want": {"cash": 17}}}]}
        out = tr.journal_corrections([{"thread": 123, "item": "card:LAT-08", "status": "deal", "close_price": 13}],
                                     {123: t}, [])
        self.assertEqual((out[0]["old"], out[0]["new"]), (13, 17))
        self.assertIn("no consta", out[0]["accepted_by"])
        self.assertEqual(tr.journal_corrections([{"thread": 123, "status": "deal", "close_price": 13}], {123: t},
                                                out), [], "idempotente")

    def test_settled_pack_that_was_already_opened(self):
        t = {"id": 139, "status": "deal", "messages": [{"offer": {"id": 1140, "maker": "abuela", "status": "settled"}}]}
        pending = {"offer_id": 1140, "price": 30, "cash_before": 300, "holdings_before": 0}
        self.assertEqual(neg.settlement_status(pending, t, holdings_now=0, cash_now=270)[0], "settled")

    def test_external_concurrent_activity_is_flagged(self):
        sets = tr.settlements_for([settlement(131, 79, [(403, "pack", "sobre_barrio")], price=30)], TEAM)
        self.assertEqual(sets[0]["cash_direction"], "pagamos")
        self.assertTrue(tr.classify(sets[0], [], []).startswith("origen desconocido"))
        flags = tr.unexplained_activity(sets, [{"id": 143, "kind": "persona", "with": "abuela", "created_tick": 79}],
                                        [], [], set(), since_tick=70)
        self.assertEqual(len(flags), 2)
        self.assertEqual(tr.classify(sets[0], [], [{"tick": 78, "price": 30}]), "propia (agente de vendedores)")

    def test_dealer_accepting_our_counteroffer_is_ours(self):  # caso real: LAT-03, contraoferta 10 P en el tick 125
        sets = tr.settlements_for([settlement(210, 126, [(500, "card", "LAT-03")], price=10)], TEAM)
        counter = {"tick": 125, "price": 10, "action": "counter"}
        self.assertEqual(tr.classify(sets[0], [], [counter]), "propia (agente de vendedores)")
        self.assertEqual(tr.unexplained_activity(sets, [], [], [counter], set(), since_tick=109), [])
        self.assertTrue(tr.classify(sets[0], [], [{"tick": 125, "price": 9}]).startswith("origen desconocido"))


class FakeAPI:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def accept(self, oid, assets=None):
        self.calls.append(("accept", oid, assets))
        if self.fail:
            raise self.fail
        return {"queued": True, "offer": oid}

    def list_offer(self, give, want, venue=None, expires_in_ticks=None):
        self.calls.append(("list", give, want))
        if self.fail:
            raise self.fail
        return {"id": 999, "maker": TEAM, "status": "open", "give": give, "want": want}


class FakeReader:
    def __init__(self, s, api):
        self.s, self.api = s, api

    def snapshot(self):
        return copy.deepcopy({k: self.s[k] for k in ("catalog", "me", "venues", "board", "offers", "clock")})

    def call(self, method, *args):
        if method == "feed":
            return self.s["feed"]
        if method == "my_threads":
            return {"threads": self.s["threads"][args[0]]}
        return self.s["levels"]


def args(**kw):
    base = dict(reserve=100, margin=2, per_card=40, max_spend=100, listing_ticks=8, fill_prior=0.3,
                allow_last_copy="", execute=True, allow_concurrent=False, apply_corrections=False, action="best")
    base.update(kw)
    return Namespace(**base)


class Execution(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = (ma.DATA, ma.LEDGER, ma.REPORT2)
        ma.DATA = Path(self.dir.name)
        ma.LEDGER, ma.REPORT2 = ma.DATA / "market_ledger.json", ma.DATA / "trading_report.json"
        self.mine = [asset(1, "LAT-01"), asset(2, "LAT-01")]
        self.bid = offer(9, {"cash": 12}, {"types": ["card:LAT-01"]})

    def tearDown(self):
        ma.DATA, ma.LEDGER, ma.REPORT2 = self.saved
        self.dir.cleanup()

    def key(self, s):
        return next(o for o in tr.plan(s, ma.trading_config(args()))["opportunities"] if o["kind"] == "vender")["key"]

    def test_restart_never_repeats_the_same_action(self):
        s = snap(self.mine, board=[self.bid])
        api = FakeAPI()
        led = ma.load_ledger(TEAM)
        ma.execute_v2(FakeReader(s, api), led, args(), self.key(s))
        led2 = ma.load_ledger(TEAM)  # "reinicio": se relee del disco
        s["clock"]["tick"] += 1
        with self.assertRaisesRegex(ValueError, "idempotencia|cambiado"):
            ma.execute_v2(FakeReader(s, api), led2, args(), self.key(snap(self.mine, board=[self.bid])))
        self.assertEqual(len(api.calls), 1)

    def test_ambiguous_network_response_blocks_until_reconciled(self):
        s = snap(self.mine, board=[self.bid])
        api = FakeAPI(fail=BazaarError("network", "timeout", 0))
        led = ma.load_ledger(TEAM)
        with self.assertRaises(BazaarError):
            ma.execute_v2(FakeReader(s, api), led, args(), self.key(s))
        self.assertEqual(ma.load_ledger(TEAM)["actions"][0]["status"], "ambiguous")
        s["clock"]["tick"] += 1
        with self.assertRaisesRegex(ValueError, "ambigua"):
            ma.execute_v2(FakeReader(s, FakeAPI()), ma.load_ledger(TEAM), args(), self.key(s))
        # la evidencia del servidor (la copia salió) la resuelve y cuenta el cobro neto
        s["feed"]["events"] = [settlement(200, 51, items_out=[(2, "card", "LAT-01")], price=12, fee=2, other="t17",
                                          persona=None)]
        led = ma.load_ledger(TEAM)
        self.assertEqual(ma.reconcile_v2(led, s, tr.settlements_for(s["feed"]["events"], TEAM)), [])
        self.assertEqual((led["actions"][0]["status"], led["cash_received"]), ("settled", 10))

    def test_rejected_write_releases_and_can_be_retried_later(self):
        s = snap(self.mine, board=[self.bid])
        with self.assertRaises(BazaarError):
            ma.execute_v2(FakeReader(s, FakeAPI(fail=BazaarError("offer_gone", "x", 409))), ma.load_ledger(TEAM),
                          args(), self.key(s))
        self.assertEqual(ma.load_ledger(TEAM)["actions"][0]["status"], "rejected")

    def test_listing_lost_response_found_on_server_is_not_reposted(self):
        s = snap(self.mine)
        key = next(o for o in tr.plan(s, ma.trading_config(args()))["opportunities"] if o["type"] == "list")["key"]
        with self.assertRaises(BazaarError):
            ma.execute_v2(FakeReader(s, FakeAPI(fail=BazaarError("network", "", 0))), ma.load_ledger(TEAM), args(), key)
        posted = offer(555, {"assets": [asset(2, "LAT-01")]}, {"cash": 9}, maker=TEAM)
        posted["created_tick"] = 50
        s["offers"]["offers"] = [posted]
        led = ma.load_ledger(TEAM)
        self.assertEqual(ma.reconcile_v2(led, s, []), [])
        self.assertEqual((led["actions"][0]["status"], led["actions"][0]["offer_id"]), ("submitted", 555))
        self.assertFalse([o for o in tr.plan(s, ma.trading_config(args()))["opportunities"] if o["type"] == "list"])

    def test_open_dealer_thread_or_other_pending_blocks_writes(self):
        s = snap(self.mine, board=[self.bid])
        s["threads"]["open"] = [{"id": 1, "kind": "persona", "with": "abuela"}]
        with self.assertRaisesRegex(ValueError, "dealers"):
            ma.execute_v2(FakeReader(s, FakeAPI()), ma.load_ledger(TEAM), args(), self.key(s))


if __name__ == "__main__":
    unittest.main()
