"""Pruebas de la PROTECCIÓN DE PÁGINAS COMPLETAS (restricción dura, prioridad 1). Sin red ni operaciones reales."""
import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from collections import Counter

import coordinator as co
import market_intel as mi
import negotiation as neg
import page_guard as pg
from test_market_intel import TEAM, A, ask, bid, catalog, offer, snap, swap

LAT = ["LAT-01", "LAT-02", "LAT-03"]
MAL = ["MAL-01", "MAL-02", "MAL-03"]
FULL = [A(1, "LAT-01"), A(2, "LAT-02"), A(3, "LAT-03"), A(4, "MAL-01"), A(5, "MAL-01")]  # LAT completa, MAL-01 x2
ALLOW_ALL = frozenset(LAT + MAL)  # desactiva la regla de "última copia" para comprobar que bloquea ESTA protección


def plan(s, **kw):
    kw.setdefault("allow_last_copy", ALLOW_ALL)
    return mi.plan(s, mi.IntelConfig(**kw), actions=kw.pop("actions", ()), expiry_ratio=2.0)


def protected(o):
    return any(b.startswith("BLOCKED: card belongs to completed page") for b in o.get("blockers") or [])


def delivers(o, ref):
    return (o.get("deliver") or {}).get(ref) or o.get("give_ref") == ref or \
        (o.get("type") == "list" and o.get("ref") == ref)


class Detection(unittest.TestCase):
    def test_01_completed_page_one_copy_each_nothing_can_be_sold(self):
        counts = Counter(a["ref"] for a in FULL)
        self.assertEqual(pg.protected_page_cards(counts, catalog()), set(LAT))
        self.assertEqual([pg.tradeable_surplus(r, counts, catalog()) for r in LAT], [0, 0, 0])
        s = snap(FULL, [bid(10 + i, "v02", r, 500, maker="m1") for i, r in enumerate(LAT)])
        pl = plan(s)
        sells = [o for o in pl["opportunities"] if any(delivers(o, r) for r in LAT)]
        self.assertTrue(sells, "las pujas generan candidatas (la protección no debe esconderlas: debe bloquearlas)")
        self.assertTrue(all(protected(o) for o in sells))
        self.assertFalse([c for c in co.select(sells, {"actions": [], "class_tick": {}}, 100) if c.get("ref") in LAT
                          or set(c.get("deliver") or {}) & set(LAT)])

    def test_02_duplicate_only_the_extra_copy_is_tradeable(self):
        for n, surplus in ((1, 0), (2, 1), (3, 2)):
            counts = Counter({"LAT-01": 1, "LAT-02": 1, "LAT-03": n})
            self.assertEqual(pg.tradeable_surplus("LAT-03", counts, catalog()), surplus)
            self.assertEqual(pg.protected_required_count("LAT-03", counts, catalog()), 1)
        assets = FULL + [A(6, "LAT-03")]
        pl = plan(snap(assets, [bid(10, "v02", "LAT-03", 40, maker="m1")]))
        sell = [o for o in pl["opportunities"] if o["type"] == "accept" and o["deliver"] == {"LAT-03": 1}]
        self.assertTrue(sell and not protected(sell[0]), "el duplicado SÍ se puede vender")
        counts = Counter(a["ref"] for a in assets)
        self.assertTrue(pg.validate_protected_assets(counts=counts, catalog=catalog(), deliver=Counter({"LAT-03": 2}),
                                                     action_type="sell"), "las dos copias no")


class Validation(unittest.TestCase):
    def test_03_swap_that_breaks_the_page_is_rejected(self):
        s = snap(FULL, [swap(10, "v02", 70, "MAL-02", "LAT-01", maker="m1")])
        acc = {"type": "accept", "offer": 10, "venue": "v02", "receive": {"MAL-02": 1}, "deliver": {"LAT-01": 1},
               "assets": [1]}
        self.assertIn("would break completed page LAT", pg.guard_candidate(acc, s)[0])
        swl = {"type": "swap_list", "ref": "MAL-02", "asset": 2, "give_ref": "LAT-02", "venue": "v02"}
        self.assertTrue(pg.guard_candidate(swl, s))
        swap_to_same = {"type": "accept", "offer": 11, "receive": {"LAT-01": 1}, "deliver": {"LAT-01": 1}, "assets": [1]}
        self.assertEqual(pg.guard_candidate(swap_to_same, s), [], "entregar y recibir la misma carta no rompe nada")

    def test_04_cash_sale_that_breaks_the_page_is_rejected(self):
        s = snap(FULL, [bid(10, "rastro", "LAT-02", 900, maker="m1")])
        sale = [o for o in plan(s)["opportunities"] if o["type"] == "accept" and o.get("deliver") == {"LAT-02": 1}]
        self.assertTrue(sale and protected(sale[0]) and sale[0]["du"] > 800, "ΔU enorme y aun así inviable")
        self.assertTrue(pg.guard_candidate({"type": "list", "ref": "LAT-02", "asset": 2, "price": 999}, s))

    def test_05_bundle_with_a_protected_card_is_rejected(self):
        bundle = offer(10, "v02", {"cash": 300}, {"types": ["card:LAT-01", "card:MAL-01"]}, maker="m1")
        s = snap(FULL, [bundle])
        lots = [o for o in plan(s)["opportunities"] if o.get("offer") == 10]
        self.assertTrue(lots and all(protected(o) for o in lots))
        self.assertTrue(pg.guard_candidate({"type": "accept", "offer": 10}, s), "sin ids explícitos: se lee la oferta")
        ok = offer(11, "v02", {"cash": 30}, {"types": ["card:MAL-01"]}, maker="m1")
        self.assertEqual(pg.guard_candidate({"type": "accept", "offer": 11}, snap(FULL, [ok])), [])

    def test_06_directed_offer_with_a_protected_card_is_rejected(self):
        directed = offer(10, "v02", {"cash": 200}, {"types": ["card:LAT-03"]}, maker="t05", to=TEAM)
        s = snap(FULL, mine=[directed])
        acc = [o for o in plan(s)["opportunities"] if o.get("offer") == 10]
        self.assertTrue(acc and protected(acc[0]))
        propose = {"type": "team_propose", "thread": 5, "offer": {"give": {"assets": [3]}, "want": {"cash": 90}}}
        self.assertTrue(pg.guard_candidate(propose, s), "propuesta en conversación con equipo")
        team_open = {"type": "team_open", "team": "t05", "opp": {"kind": "sell", "deliver": "LAT-03", "asset": 3}}
        self.assertTrue(pg.guard_candidate(team_open, s), "ni siquiera se abre la negociación")
        dir_list = {"type": "list", "ref": "LAT-01", "asset": 1, "price": 80, "to": "t05", "venue": "v02"}
        self.assertTrue(pg.guard_candidate(dir_list, s), "publicación dirigida")

    def test_07_optimizer_cannot_override_protection(self):
        s = snap(FULL)
        s["threads"] = {"open": [], "deal": []}
        rogue = {"type": "list", "module": "otro", "kind": "venta", "ref": "LAT-01", "asset": 1, "price": 9999,
                 "du": 9999, "score": 10 ** 9, "blockers": [], "venue": "v02"}
        safe = {"type": "list", "module": "m", "kind": "venta", "ref": "MAL-01", "asset": 5, "price": 20, "du": 9,
                "score": 1, "blockers": [], "venue": "v02"}
        cands = [dict(rogue), dict(safe)]
        self.assertEqual(pg.apply_guard(cands, s), 1)
        chosen = co.select(cands, {"actions": [], "class_tick": {}}, 100, max_posts=4)
        self.assertEqual([c["ref"] for c in chosen], ["MAL-01"])
        calls = []

        class API:
            def list_offer(self, *a, **k):
                calls.append(a)
                return {"id": 1}

        class R:
            api = API()

        with tempfile.TemporaryDirectory() as d:
            saved = co.LEDGER
            co.LEDGER = co.Path(d) / "l.json"
            try:
                led = co.load_ledger(TEAM)
                with contextlib.redirect_stdout(io.StringIO()) as out:  # aunque otro módulo lo meta directo en send
                    r = co.send(R(), led, s, dict(rogue), Namespace(listing_ticks=10, duende_venue="v02"),
                                neg.Journal(d))
            finally:
                co.LEDGER = saved
        self.assertIsNone(r)
        self.assertEqual(calls, [])
        self.assertEqual(led["actions"], [], "ni siquiera queda intención en el registro")
        self.assertIn("PROTECTED_PAGE_BLOCK", out.getvalue())


class OpenOffers(unittest.TestCase):
    def test_08_open_listing_becomes_unsafe_after_completion_is_cancelled(self):
        listed = ask(50, "v02", 3, "LAT-03", 25, maker=TEAM, created=90)  # se publicó con la página incompleta
        s = snap(FULL, mine=[listed])
        pl = plan(s)
        cancel = [o for o in pl["opportunities"] if o["type"] == "cancel" and o.get("offer") == 50]
        self.assertEqual(len(cancel), 1)
        self.assertEqual(cancel[0]["blockers"], [])
        self.assertTrue(cancel[0]["protected_page_cancel"])
        chosen = co.select([dict(c, module="seguridad") for c in pl["opportunities"]],
                           {"actions": [], "class_tick": {}}, 100, max_posts=1)
        self.assertEqual(chosen[0].get("offer"), 50, "la cancelación de seguridad va primero")
        # con un duplicado la oferta es segura; si las DOS copias están publicadas, se retira solo la segunda
        two = FULL + [A(6, "LAT-03")]
        self.assertEqual(pg.unsafe_open_offers([listed], TEAM, Counter(a["ref"] for a in two), catalog(), two), [])
        both = [listed, ask(51, "v03", 6, "LAT-03", 26, maker=TEAM, created=91)]
        out = pg.unsafe_open_offers(both, TEAM, Counter(a["ref"] for a in two), catalog(), two)
        self.assertEqual([c["offer"] for c in out], [51])
        # y una copia comprometida en una oferta abierta no puede venderse otra vez por otra vía
        s2 = snap(two, [bid(60, "v02", "LAT-03", 40, maker="m1")], mine=[listed])
        sell = [o for o in plan(s2)["opportunities"] if o["type"] == "accept" and o.get("offer") == 60]
        self.assertTrue(not sell or protected(sell[0]))

    def test_08b_safety_cancel_does_not_depend_on_valuation(self):
        s = snap(FULL, mine=[ask(50, "v02", 3, "LAT-03", 25, maker=TEAM, created=90)])
        s["me"]["collection_value"] = 1.0  # modelo no verificado: todo lo demás se bloquea
        pl = plan(s)
        cancel = [o for o in pl["opportunities"] if o.get("offer") == 50]
        self.assertFalse(pl["valuation_verified"])
        self.assertEqual(cancel[0]["blockers"], [])

    def test_08c_coordinator_cancels_without_cancel_unsafe_flag(self):
        s = snap(FULL, mine=[ask(50, "v02", 3, "LAT-03", 25, maker=TEAM, created=90)])
        counts = Counter(a["ref"] for a in FULL)
        out = pg.unsafe_open_offers(s["offers"]["offers"], TEAM, counts, s["catalog"], FULL)
        self.assertTrue(out and out[0]["score"] > 10 ** 6 and out[0]["blockers"] == [])


class Scope(unittest.TestCase):
    def test_09_two_completed_pages_are_both_protected(self):
        assets = [A(i + 1, r) for i, r in enumerate(LAT + MAL)]
        counts = Counter(a["ref"] for a in assets)
        self.assertEqual(pg.completed_pages(counts, catalog()), {"LAT", "MAL"})
        self.assertEqual(pg.protected_page_cards(counts, catalog()), set(LAT + MAL))
        s = snap(assets, [bid(10, "v02", "MAL-02", 300, maker="m1"), bid(11, "v02", "LAT-01", 300, maker="m1")])
        sells = [o for o in plan(s)["opportunities"] if o["type"] == "accept"]
        self.assertEqual(len(sells), 2)
        self.assertTrue(all(protected(o) for o in sells))

    def test_10_incomplete_page_normal_rules_apply(self):
        base = [A(1, "LAT-01"), A(2, "LAT-02"), A(4, "MAL-01"), A(5, "MAL-01")]
        counts = Counter(a["ref"] for a in base)
        self.assertEqual(pg.protected_page_cards(counts, catalog()), set())
        s = snap(base, [bid(10, "v02", "MAL-01", 30, maker="m1"), ask(11, "v02", 70, "LAT-03", 12, maker="m1")])
        pl = plan(s, allow_last_copy=frozenset())
        self.assertFalse([o for o in pl["opportunities"] if protected(o)])
        self.assertTrue([o for o in pl["opportunities"] if o.get("offer") == 11 and not o["blockers"]],
                        "comprar la carta que completa la página sigue permitido")
        self.assertTrue([o for o in pl["opportunities"] if o.get("offer") == 10 and not o["blockers"]])

    def test_11_block_is_logged_with_page_card_asset_action_reason(self):
        pg._logged.clear()
        with self.assertLogs("page_guard", level="WARNING") as logs:
            pg.guard_candidate({"type": "swap_list", "ref": "MAL-02", "asset": 2, "give_ref": "LAT-02"}, snap(FULL))
        line = logs.output[0]
        for part in ("PROTECTED_PAGE_BLOCK", "page=LAT", "card=LAT-02", "asset=2", "action=swap_list",
                     "reason=would break completed page"):
            self.assertIn(part, line)
        self.assertEqual(pg.EVENTS[-1]["card"], "LAT-02")

    def test_12_missing_catalog_fails_closed(self):
        s = snap(FULL)
        s["catalog"] = {}
        self.assertTrue(pg.guard_candidate({"type": "list", "ref": "MAL-01", "asset": 5}, s))
        self.assertEqual(pg.guard_candidate({"type": "bid", "ref": "LAT-03", "price": 5}, s), [])


if __name__ == "__main__":
    unittest.main()
