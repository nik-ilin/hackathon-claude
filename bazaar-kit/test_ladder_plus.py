"""Pruebas offline de las mejoras OPT-IN (ladder_plus.py + coordinador): --deny-teams, --pilar-sell y --ladder-fill,
apoyadas en la escalera y las ventas de #8. Sin red ni claves: instantáneas sintéticas y una API falsa."""
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path

import coordinator as co
import ladder_plus as lp
import market_agent as ma
import negotiation as neg
import trading as tr
from test_coordinator import FakeAPI, FakeReader, args, dealer_thread, full, her, ours
from test_trading import TEAM, asset, catalog, offer, snap

PILAR_T = 8


def led0(**kw):
    base = {"actions": [], "spent_confirmed": 0, "cash_received": 0, "threads": [], "blocked": {}, "class_tick": {},
            "expiry_obs": []}
    base.update(kw)
    return base


def sal_catalog():
    cat = catalog()

    def card(i, rarity, book):
        return {"id": f"SAL-{i:02d}", "name": f"Salamanca {i}", "rarity": rarity, "book": book, "page": True}
    cat["sets"].append({"id": "SAL", "name": "Salamanca", "released": True,
                        "cards": [card(1, "common", 10), card(2, "common", 10), card(6, "uncommon", 20),
                                  card(9, "rare", 70)]})
    return cat


def sal_snap(assets, cash=200, tick=50, pilar=True, board=(), offers=(), sal=1.0):
    s = snap(assets, cash=cash, tick=tick, board=board, offers=offers)
    s["catalog"] = sal_catalog()
    s["me"]["affinity"]["SAL"] = sal
    val = tr.Valuation(s["catalog"], s["me"]["affinity"])
    s["me"]["collection_value"] = round(val.total(tr.counts_of(assets)), 2)
    s["dealers"] = {"abuela": {"menu": {"sells": [{"rarity": "common", "list_price": 10}]}}}
    s["me"]["unlocked"] = ["abuela"]
    if pilar:
        s["dealers"]["pilar"] = {"menu": {"buys": [{"rarity": "uncommon"}, {"rarity": "rare"}]}}
        s["me"]["unlocked"].append("pilar")
    return s


def pilar_thread(msgs, status="open", aid=11, tid=PILAR_T):
    return {"id": tid, "kind": "persona", "with": "pilar", "team": TEAM, "status": status, "created_tick": 45,
            "topic": {"sell": {"assets": [aid]}}, "messages": msgs, "standing_offers": []}


def pil(oid, tick, price, status="open", final=False, aid=11):
    return {"id": oid, "tick": tick, "sender": "pilar", "offer": {
        "id": oid, "maker": "pilar", "to": TEAM, "thread": PILAR_T, "status": status, "final": final,
        "give": {"cash": price, "assets": [], "types": []}, "want": {"cash": 0, "assets": [aid], "types": []},
        "expires_tick": tick + 2}}


def ask(oid, tick, price, status="cancelled", aid=11):
    return {"id": oid, "tick": tick, "sender": TEAM, "price": price, "offer": {
        "id": oid, "maker": TEAM, "status": status, "give": {"assets": [aid]}, "want": {"cash": price}}}


def sell_st(msgs, now):
    return neg.state_from_thread(pilar_thread(msgs), "pilar", now, neg.Config(), side="sell")


def cands(s, led=None, **kw):
    out, pl, _ = co.candidates(s, led or led0(), args(**kw), neg.Journal(tempfile.mkdtemp()))
    return out, pl


def opens(out):
    return [c for c in out if c["type"] == "dealer_sell_open"]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = (co.DATA, co.LEDGER, ma.LEDGER)
        co.DATA = Path(self.dir.name)
        co.LEDGER = co.DATA / "coordinator_ledger.json"
        ma.LEDGER = co.DATA / "market_ledger.json"
        self.journal = neg.Journal(self.dir.name)

    def tearDown(self):
        co.DATA, co.LEDGER, ma.LEDGER = self.saved
        self.dir.cleanup()

    def run_cycle(self, s, **kw):
        led = co.load_ledger(TEAM)
        reader = PilarReader(s)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            co.cycle(reader, args(**kw), led, self.journal, True)
        return out.getvalue(), reader.api.calls, led


class PilarAPI(FakeAPI):
    def open_thread(self, with_, topic=None, venue=None):
        self.calls.append(("open_thread", with_, topic))
        return {"id": 77}

    def say(self, thread_id, text="", price=None, offer=None, topic=None):
        self.calls.append(("say", thread_id, price))
        return {"id": 500}

    def close_thread(self, thread_id):
        self.calls.append(("close", thread_id))
        return {"status": "closed"}


class PilarReader(FakeReader):
    def __init__(self, s):
        super().__init__(s)
        self.api = PilarAPI()


# ------------------------------------------------------------------ sin flags, nada cambia

class OptIn(unittest.TestCase):
    def test_neutral_flags_equal_no_flags(self):
        s = sal_snap([asset(1, "LAT-01"), asset(2, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")],
                     board=[offer(9, {"assets": [asset(70, "LAT-03")]}, {"cash": 12}, maker="t12")])
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 16)]), dealer_thread(7, [her(1, 41, 12)])]
        led = led0(threads=[7, PILAR_T])
        strip = lambda cs: [{k: v for k, v in c.items() if k != "opp"} for c in cs]
        base, pl0 = cands(copy.deepcopy(s), copy.deepcopy(led))
        neutral, pl1 = cands(copy.deepcopy(s), copy.deepcopy(led), deny_teams="", deny_margin=None, pilar_sell=None,
                             pilar_open=None, pilar_step=None, pilar_last_copy=False, pilar_from_tick=None,
                             pilar_until_tick=None, ladder_fill=False)
        self.assertEqual(strip(base), strip(neutral))
        self.assertEqual(pl0["capital"], pl1["capital"])
        self.assertNotIn("pilar", pl0)
        self.assertFalse([c for c in base if c["type"] in ("dealer_sell_open", "dealer_sell_counter")])


# ------------------------------------------------------------------ --deny-teams

class DenyTeams(unittest.TestCase):
    def test_parse_and_normalise(self):
        self.assertEqual(lp.parse_deny_teams("t12, T013,t014,"), frozenset({"t12", "t13", "t14"}))
        self.assertEqual(lp.parse_deny_teams(""), frozenset())
        self.assertEqual(lp.parse_deny_teams(None), frozenset())

    def test_blocks_accept_directed_and_campaign_but_never_cancel_or_public(self):
        deny = lp.parse_deny_teams("t12")
        self.assertTrue(lp.deny_blockers({"type": "accept", "maker": "t12", "du": 9}, deny))
        self.assertTrue(lp.deny_blockers({"type": "bid", "to": "t012", "du": 9}, deny))
        self.assertTrue(lp.deny_blockers({"type": "team_open", "team": "t12", "du": 9}, deny))
        self.assertTrue(lp.deny_blockers({"type": "team_propose", "team": "t12"}, deny))
        self.assertTrue(lp.deny_blockers({"type": "team_accept", "team": "t12", "du": 1}, deny))
        self.assertFalse(lp.deny_blockers({"type": "team_close", "team": "t12"}, deny), "cerrar siempre se puede")
        self.assertFalse(lp.deny_blockers({"type": "cancel", "maker": "t12"}, deny))
        self.assertFalse(lp.deny_blockers({"type": "list", "to": None, "du": 3}, deny), "pública: no aplica")
        self.assertFalse(lp.deny_blockers({"type": "accept", "maker": "t05", "du": 3}, deny))
        self.assertFalse(lp.deny_blockers({"type": "accept", "maker": "t12", "du": 3}, frozenset()))

    def test_margin_allows_only_large_surplus(self):
        deny = lp.parse_deny_teams("t12,t13")
        c = {"type": "accept", "maker": "t13", "du": 12.0}
        self.assertFalse(lp.deny_blockers(c, deny, 10))
        self.assertTrue(any("permitido" in n for n in c["notes"]))
        self.assertIn("< --deny-margin 10", lp.deny_blockers({"type": "accept", "maker": "t13", "du": 9.9}, deny, 10)[0])
        self.assertTrue(lp.deny_blockers({"type": "accept", "maker": "t13"}, deny, 10), "sin ΔU conocido: bloquea")
        self.assertFalse(lp.deny_blockers({"type": "team_open", "opp": {"team": "t13", "du_est": 15}}, deny, 10))


class DenyCycle(Base):
    def board(self):
        # t12 vende LAT-03, que completa nuestra página: una compra muy rentable
        board = [offer(9, {"assets": [asset(70, "LAT-03")]}, {"cash": 12}, maker="t12")]
        return full(snap([asset(1, "LAT-01"), asset(2, "LAT-02")], cash=150, board=board), {})

    def test_without_flag_the_accept_is_sent(self):
        _, calls, _ = self.run_cycle(self.board())
        self.assertIn(("accept", 9), calls)

    def test_deny_blocks_accepting_from_the_team(self):
        out, calls, _ = self.run_cycle(self.board(), deny_teams="t12")
        self.assertNotIn(("accept", 9), calls)
        self.assertIn("DENY_TEAMS", out)

    def test_deny_margin_lets_a_large_surplus_through(self):
        _, calls, _ = self.run_cycle(self.board(), deny_teams="t12", deny_margin=5.0)
        self.assertIn(("accept", 9), calls, "el excedente (bono de página) supera 5 P")
        _, calls, _ = self.run_cycle(self.board(), deny_teams="t12", deny_margin=10 ** 4)
        self.assertNotIn(("accept", 9), calls)


# ------------------------------------------------------------------ --pilar-sell: lógica pura

class PilarParse(unittest.TestCase):
    def test_parse(self):
        c = lp.parse_pilar_sell("SAL:1.25")
        self.assertEqual((c.sets, c.mult, c.open_ask, c.step), (frozenset({"SAL"}), 1.25, 33, 2))
        c = lp.parse_pilar_sell("sal,mal")
        self.assertEqual((c.sets, c.mult), (frozenset({"SAL", "MAL"}), 1.0))
        self.assertEqual(lp.parse_pilar_sell("*").sets, frozenset())
        self.assertIsNone(lp.parse_pilar_sell(None))
        with self.assertRaises(ValueError):
            lp.parse_pilar_sell("SAL:x")
        with self.assertRaises(ValueError):
            lp.parse_pilar_sell("SAL:9")

    def test_window(self):
        c = lp.PilarConfig(frozenset(), from_tick=100, until_tick=200)
        self.assertEqual([c.in_window(t) for t in (99, 100, 200, 201)], [False, True, True, False])

    def test_expected_price_is_the_observed_median_with_the_fever_only_on_its_sets(self):
        c = lp.parse_pilar_sell("SAL:1.25")
        self.assertEqual(lp.expected_price({"set": "SAL"}, c), 22, "17,5 × 1,25 (hasta 25 observado)")
        self.assertEqual(lp.expected_price({"set": "LAT"}, c), 18, "mediana 17-18 sin fiebre")
        self.assertEqual(lp.expected_price({"set": "SAL"}, c, {"list_price": 20}), 25, "el menú manda si existe")
        self.assertEqual(lp.first_ask(5, c), 33)
        self.assertEqual(lp.first_ask(40, c), 40, "nunca pedir por debajo del valor privado")

    def test_last_copy_permission(self):
        c = lp.parse_pilar_sell("SAL")
        self.assertFalse(lp.last_copy_allowed("SAL-06", "SAL", set(), c))
        self.assertTrue(lp.last_copy_allowed("SAL-06", "SAL", {"SAL-06"}, c))
        self.assertTrue(lp.last_copy_allowed("SAL-06", "SAL", {"SAL"}, c), "--allow-last-copy por barrio")
        c.last_copy = True
        self.assertTrue(lp.last_copy_allowed("SAL-06", "SAL", set(), c))
        self.assertFalse(lp.last_copy_allowed("MAL-06", "MAL", set(), c), "solo los barrios del modo")
        self.assertFalse(lp.last_copy_allowed("X-1", "X", set(), lp.PilarConfig(frozenset(), last_copy=True)),
                         "con * no hay permiso implícito para todo")


class PilarDecide(unittest.TestCase):
    cfg = lp.parse_pilar_sell("SAL:1.25")

    def d(self, msgs, now, floor=5, left=10, cfg=None):
        cfg = cfg or self.cfg
        return lp.decide_sell(sell_st(msgs, now), floor, lp.first_ask(floor, cfg), cfg, left)

    def test_opens_at_33_and_steps_down_by_two(self):
        self.assertEqual(self.d([pil(1, 46, 16)], 46).price, 33)
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 33), pil(3, 47, 17)]
        d = self.d(msgs, 47)
        self.assertEqual((d.action, d.price), ("counter", 31))
        self.assertEqual(self.d(msgs[:2], 46).action, "wait", "esperando su respuesta")

    def test_t04_variant_open_40_step_1(self):
        cfg = lp.parse_pilar_sell("SAL:1.25")
        cfg.open_ask, cfg.step = 40, 1
        self.assertEqual(self.d([pil(1, 46, 16)], 46, cfg=cfg).price, 40)
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 40), pil(3, 47, 17)]
        self.assertEqual(self.d(msgs, 47, cfg=cfg).price, 39)

    def test_reaches_25_like_t10(self):
        msgs = [pil(1, 46, 16, "cancelled")]
        for i, (mine, hers) in enumerate([(33, 18), (31, 20), (29, 22), (27, 24)]):
            msgs += [ask(10 + i, 47 + i, mine), pil(20 + i, 48 + i, hers, "cancelled")]
        msgs[-1] = pil(23, 51, 24)  # su puja vigente: 24 frente a nuestro 27
        d = self.d(msgs, 51)
        self.assertEqual((d.action, d.price), ("counter", 25))
        msgs[-1] = pil(23, 51, 24, "cancelled")
        msgs += [ask(30, 51, 25), pil(31, 52, 25)]
        self.assertEqual(self.d(msgs, 52).action, "accept")

    def test_accepts_final_at_or_above_private_value_only(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 33), pil(3, 47, 18, final=True)]
        d = self.d(msgs, 47)
        self.assertEqual((d.action, d.price, d.offer_id), ("accept", 18, 3))
        self.assertEqual(self.d(msgs, 47, floor=19).action, "abandon")

    def test_closes_when_gap_is_one_step_and_the_deal_is_negotiated(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 21), pil(3, 47, 19)]
        self.assertEqual(self.d(msgs, 47).action, "accept")

    def test_opening_bid_is_not_taken_while_there_is_room(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 18), pil(3, 47, 16)]
        d = self.d(msgs, 47)
        self.assertEqual((d.action, d.price), ("counter", 17), "aceptar 16 no puntuaría")

    def test_never_asks_below_floor_nor_repeats(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 21), pil(3, 47, 17)]
        self.assertEqual(self.d(msgs, 47, floor=20).price, 20)
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 20), pil(3, 47, 17)]
        self.assertEqual(self.d(msgs, 47, floor=20).action, "abandon", "no queda petición nueva ≥ mínimo")

    def test_out_of_rounds_takes_a_bid_above_floor(self):
        msgs = [pil(1, 46, 16, "cancelled"), ask(2, 46, 30), pil(3, 47, 17)]
        self.assertEqual(self.d(msgs, 47, left=0).action, "accept")

    def test_qualifying_sell_deals(self):
        won = pilar_thread([pil(1, 46, 16, "cancelled"), ask(2, 46, 20), pil(3, 47, 19, "settled")], status="deal")
        flat = pilar_thread([pil(1, 46, 16, "settled")], status="deal", tid=9)
        q = lp.qualifying_sell_deals("pilar", [won, flat])
        self.assertEqual([(x["thread"], x["opening"], x["close"]) for x in q], [(PILAR_T, 16, 19)])
        jr = [{"dealer": "pilar", "side": "sell", "status": "deal", "settled": True, "opening": 16, "close_price": 18,
               "thread": 30}, {"dealer": "pilar", "side": "sell", "status": "deal", "settled": True, "opening": 16,
                               "close_price": 16, "thread": 31}]
        self.assertEqual([x["thread"] for x in lp.qualifying_sell_deals("pilar", [], jr)], [30])


# ------------------------------------------------------------------ --pilar-sell en el coordinador

class PilarCoordinator(unittest.TestCase):
    def mine(self, n=2):
        return [asset(1, "LAT-01")] + [asset(10 + i, "SAL-06") for i in range(1, n + 1)]

    def test_opens_one_thread_for_a_surplus_copy(self):
        out, pl = cands(sal_snap(self.mine()), pilar_sell="SAL:1.25")
        c = opens(out)[0]
        self.assertFalse(c["blockers"])
        self.assertEqual((c["dealer"], c["ref"], c["price"], c["floor"]), ("pilar", "card:SAL-06", 22, 5))
        self.assertIn(c["asset"], (11, 12))
        self.assertEqual(pl["pilar"]["next"]["first_ask"], 33)
        self.assertEqual(co.select(out, led0(), 50)[0]["type"], "dealer_sell_open")

    def test_last_copy_needs_explicit_permission(self):
        one = sal_snap(self.mine(1), sal=0.5)  # nuestra afinidad SAL es 0,5: la valoramos poco
        self.assertFalse(opens(cands(one, pilar_sell="SAL:1.25")[0]))
        for kw in ({"pilar_last_copy": True}, {"allow_last_copy": "SAL"}, {"allow_last_copy": "SAL-06"}):
            c = opens(cands(one, pilar_sell="SAL:1.25", **kw)[0])
            self.assertTrue(c and not c[0]["blockers"], kw)
            self.assertEqual(c[0]["floor"], 10, "valor privado de la única copia: 20 × 0,5")
        self.assertFalse(opens(cands(sal_snap(self.mine()), pilar_sell="MAL:1.25")[0]))

    def test_never_breaks_a_completed_page_even_with_last_copy(self):
        page = [asset(1, "SAL-01"), asset(2, "SAL-02"), asset(11, "SAL-06"), asset(5, "SAL-09")]
        out, _ = cands(sal_snap(page, sal=0.5), pilar_sell="SAL", pilar_last_copy=True)
        self.assertFalse([c for c in opens(out) if not c["blockers"]],
                         "las únicas copias mantienen la página SAL completa")
        out, _ = cands(sal_snap(page + [asset(12, "SAL-06")]), pilar_sell="SAL")
        self.assertEqual(opens(out)[0]["asset"], 12, "se vende la copia excedente, nunca la protegida")

    def test_window_unlock_and_target(self):
        out, _ = cands(sal_snap(self.mine(), tick=50), pilar_sell="SAL:1.25", pilar_from_tick=60)
        self.assertTrue(opens(out)[0]["blockers"])
        out, _ = cands(sal_snap(self.mine(), tick=50), pilar_sell="SAL:1.25", pilar_until_tick=49)
        self.assertTrue(opens(out)[0]["blockers"])
        s = sal_snap(self.mine())
        s["me"]["unlocked"] = ["abuela"]
        self.assertTrue(any("no está desbloqueado" in b for b in opens(cands(s, pilar_sell="SAL:1.25")[0])[0]["blockers"]))
        s = sal_snap(self.mine())
        s["threads"]["deal"] = [pilar_thread([pil(1, 40 + i, 16, "cancelled"), ask(2, 40 + i, 20),
                                              pil(3, 41 + i, 18, "settled")], status="deal", tid=100 + i)
                                for i in range(3)]
        out, pl = cands(s, pilar_sell="SAL:1.25")
        self.assertEqual(pl["ladder"]["pilar"], 3)
        self.assertTrue(any("objetivo cumplido" in b for b in opens(out)[0]["blockers"]))

    def test_continues_our_thread_and_blocks_the_copy_elsewhere(self):
        s = sal_snap(self.mine(3))
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 16, "cancelled"), ask(2, 46, 33), pil(3, 47, 17)])]
        s["clock"]["tick"] = 47
        out, pl = cands(s, led0(threads=[PILAR_T]), pilar_sell="SAL:1.25")
        c = next(c for c in out if c["type"] == "dealer_sell_counter")
        self.assertEqual((c["price"], c["thread"], c["asset"], c["blockers"]), (31, PILAR_T, 11, []))
        self.assertFalse([c for c in opens(out) if not c["blockers"]], "un hilo por carta y uno con Pilar a la vez")
        for c in out:
            if not str(c["type"]).startswith("dealer_sell") and c["type"] not in ("cancel", "info") \
                    and not c.get("blockers"):
                self.assertNotIn(11, co.pg.delivery_of(c, s)[1], f"la copia 11 sale por otra vía: {c}")
        self.assertIn("PILAR VENTA", co.pilar_line(pl["pilar"]))

    def test_without_flag_a_pilar_thread_needs_dealer_sell_dups(self):
        s = sal_snap(self.mine())
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 16)])]
        out, _ = cands(s, led0(threads=[PILAR_T]))
        self.assertFalse([c for c in out if c["type"] == "dealer_sell_counter"], "comportamiento de #8 intacto")

    def test_final_offer_is_validated_and_accepted(self):
        s = sal_snap(self.mine())
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 16, "cancelled"), ask(2, 46, 25), pil(3, 47, 18, final=True)])]
        s["clock"]["tick"] = 47
        s["offers"]["offers"] = [offer(2, {"assets": [asset(11, "SAL-06")]}, {"cash": 25}, maker=TEAM, thread=PILAR_T)]
        out, _ = cands(s, led0(threads=[PILAR_T]), pilar_sell="SAL:1.25")
        c = next(c for c in out if c["type"] == "dealer_sell_accept")
        self.assertEqual((c["price"], c["offer"], c["blockers"]), (18, 3, []))
        self.assertEqual(c["score"], 3 * 10 ** 5)
        self.assertFalse(co.double_commit(c, s, {11}), "nuestra petición vigente en el hilo no es otra obligación")

    def test_foreign_sell_threads_are_untouched(self):
        s = sal_snap(self.mine())
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 16)])]
        out, _ = cands(s, pilar_sell="SAL:1.25")
        self.assertFalse([c for c in out if c["type"] in ("dealer_sell_counter", "dealer_sell_accept")])


class PilarCycle(Base):
    def test_sends_the_sell_topic_and_records_the_copy(self):
        s = sal_snap([asset(1, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")], cash=100)
        _, calls, led = self.run_cycle(s, pilar_sell="SAL:1.25")
        self.assertIn(("open_thread", "pilar", {"sell": {"assets": [12]}}), calls)
        self.assertIn(77, led["threads"])
        self.assertEqual(led["pilar_sell"], {"12": 50})

    def test_counter_is_sent_as_a_price(self):
        s = sal_snap([asset(1, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")], cash=100)
        s["threads"]["open"] = [pilar_thread([pil(1, 46, 16)])]
        s["clock"]["tick"] = 47
        co.save({"team": TEAM, "threads": [PILAR_T], "actions": [], "spent_confirmed": 0})
        _, calls, _ = self.run_cycle(s, pilar_sell="SAL:1.25")
        self.assertIn(("say", PILAR_T, 33), calls)

    def test_reconcile_counts_cash_received_and_the_ladder(self):
        t = pilar_thread([pil(1, 46, 16, "cancelled"), ask(2, 46, 19), pil(3, 47, 18, "settled")], status="deal")
        s = sal_snap([asset(1, "LAT-01"), asset(12, "SAL-06")])
        s["threads"]["deal"] = [t]
        led = led0(threads=[PILAR_T], actions=[{"key": "k", "type": "dealer_sell_accept", "status": "submitted",
                                                "tick": 47, "thread": PILAR_T, "dealer": "pilar", "item": "card:SAL-06",
                                                "price": 18, "opening": 16, "assets": [11]}])
        fresh, _ = co.reconcile(led, s, self.journal)
        self.assertEqual((fresh[0]["received"], led["cash_received"], led["spent_confirmed"]), (18, 18, 0))
        self.assertEqual(len(lp.qualifying_sell_deals("pilar", [], self.journal.records("outcome"))), 1)


# ------------------------------------------------------------------ --ladder-fill sobre la escalera de #8

class LadderFill(unittest.TestCase):
    def test_top3_overrides_only_what_differs_from_8(self):
        lc = lp.top3_ladder_config(neg.LadderConfig())
        abuela, chato = neg.ladder_profile("abuela", lc), neg.ladder_profile("chato", lc)
        self.assertEqual((abuela.opening("common"), abuela.step("common", 6)), (0.5, 1))
        self.assertEqual(abuela.opening("uncommon"), 0.60, "lo demás, como en #8")
        self.assertEqual((chato.opening("rare"), chato.step("rare", 25)), (0.74, 3))
        self.assertEqual(chato.step("uncommon", 10), 3)
        self.assertLess(lp.fill_priority("chato", "uncommon"), 0)
        self.assertGreater(lp.fill_priority("abuela", "common"), 0)
        self.assertGreater(lp.fill_priority("chato", "rare"), 0)

    def test_chato_rare_opens_at_74_percent(self):
        lc = lp.top3_ladder_config(neg.LadderConfig())
        t = dealer_thread(7, [dict(her(1, 41, 97), sender="chato", offer=dict(her(1, 41, 97)["offer"], maker="chato"))])
        st = neg.state_from_thread(t, "chato", 41, neg.Config())
        d = neg.decide_ladder(st, neg.ladder_profile("chato", lc), 95, 10, "rare", 41)
        self.assertEqual((d.action, d.price), ("counter", 72), "t17: 72 frente a 97")

    def test_coordinator_opening_by_flag(self):
        s = full(snap([asset(1, "LAT-01"), asset(2, "LAT-02")], cash=200))
        s["threads"]["open"] = [dealer_thread(7, [her(1, 41, 12)])]
        s["clock"]["tick"] = 41
        price = lambda **kw: next(c for c in cands(s, led0(threads=[7]), **kw)[0]
                                  if c["type"] == "dealer_counter")["price"]
        self.assertEqual(price(), 5, "por defecto (política corregida de dealer_policy, PR #24): 45 % de 12")
        self.assertEqual(price(dealer_ladder=True), 7, "#8: 60 % de 12")
        self.assertEqual(price(ladder_fill=True), 6, "ESTRATEGIA_TOP3: abrir 5-7")

    def test_ladder_capital_goes_before_passive_bids(self):
        s = full(snap([asset(1, "LAT-01")], cash=200))
        self.assertEqual(cands(s, dealer_buffer_mode="off")[1]["capital"]["dealer_liquidity_target"], 0)
        self.assertEqual(cands(s, dealer_buffer_mode="off", ladder_fill=True)[1]["capital"]["dealer_liquidity_target"],
                         10, "el siguiente trato de la Abuela")
        s["threads"]["deal"] = [dealer_thread(100 + i, [her(1, 41, 12, "cancelled"), ours(2, 41, 8),
                                                        her(3, 42, 9, "settled")], status="deal") for i in range(3)]
        self.assertEqual(cands(s, dealer_buffer_mode="off", ladder_fill=True)[1]["capital"]["dealer_liquidity_target"],
                         0, "escalera llena: nada que apartar")

    def test_open_priority_abuela_commons_over_chato_uncommons(self):
        s = sal_snap([asset(1, "LAT-01")], cash=300, pilar=False)
        s["dealers"]["chato"] = {"open_to_all": True, "menu": {"sells": [{"rarity": "uncommon", "list_price": 18}]}}
        out, _ = cands(s, ladder_fill=True)
        best = lambda o, d: max(c["score"] for c in o if c["type"] == "dealer_open" and c["dealer"] == d)
        self.assertGreater(best(out, "abuela"), best(out, "chato"))
        self.assertLess(best(out, "chato"), best(cands(s)[0], "chato"), "Chato poco comunes: baja prioridad")

    def test_ladder_fill_enables_pilar_for_all_sets(self):
        s = sal_snap([asset(1, "LAT-01"), asset(11, "SAL-06"), asset(12, "SAL-06")])
        out, pl = cands(s, ladder_fill=True)
        self.assertEqual((opens(out)[0]["price"], pl["pilar"]["sets"]), (18, ["*"]), "mediana, sin fiebre")


if __name__ == "__main__":
    unittest.main()
