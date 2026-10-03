"""Pruebas offline de la escalera de vendedores (opt-in): políticas observadas de Chato, Abuela y vendedores nuevos
(AUDITORIA_LIDERES.md), enrutado, venta de duplicados comunes y pujas por cartas que ya negociamos con un vendedor
(sin red ni claves). La caja para vendedores y las pujas duplicadas entre sí las cubre capital.py en main."""
import contextlib
import copy
import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import coordinator as co
import market_agent as ma
import negotiation as neg
import page_guard as pg
import trading as tr
from test_coordinator import FakeAPI, FakeReader, args, full
from test_trading import TEAM, asset, offer, snap


def led0():
    return {"actions": [], "spent_confirmed": 0, "cash_received": 0, "threads": [], "blocked": {}, "class_tick": {},
            "expiry_obs": []}


def thread(tid, dealer, msgs, topic, status="open", created=40):
    return {"id": tid, "kind": "persona", "with": dealer, "team": TEAM, "status": status, "created_tick": created,
            "topic": topic, "messages": msgs, "standing_offers": []}


def ask(dealer, oid, tick, price, status="open", final=False, ref="LAT-03"):
    """Oferta de VENTA del vendedor (pide efectivo)."""
    return {"id": oid, "tick": tick, "sender": dealer, "offer": {
        "id": oid, "maker": dealer, "to": TEAM, "thread": 7, "status": status, "final": final,
        "give": {"cash": 0, "assets": [], "types": [f"card:{ref}"]}, "want": {"cash": price, "assets": [], "types": []}}}


def bid(dealer, oid, tick, price, asset_id, status="open", final=False):
    """Oferta de COMPRA del vendedor (paga efectivo por nuestra copia)."""
    return {"id": oid, "tick": tick, "sender": dealer, "offer": {
        "id": oid, "maker": dealer, "to": TEAM, "thread": 8, "status": status, "final": final,
        "give": {"cash": price, "assets": [], "types": []}, "want": {"cash": 0, "assets": [asset_id], "types": []}}}


def mine(oid, tick, price):
    return {"id": oid, "tick": tick, "sender": TEAM, "price": price}


def bid_offer(oid, ref, price, venue="rastro"):
    return offer(oid, {"cash": price}, {"cards": [ref]}, maker=TEAM, venue=venue)


class Bids(unittest.TestCase):
    def test_own_bids_ignores_other_makers_listings_and_thread_offers(self):
        offers = [bid_offer(10, "LAT-03", 9), bid_offer(11, "LAT-03", 12, venue="v02"),
                  offer(13, {"cash": 9}, {"cards": ["LAT-03"]}, maker="t04"),
                  offer(14, {"assets": [asset(1, "LAT-01")]}, {"cash": 20}, maker=TEAM),
                  offer(15, {"cash": 9}, {"types": ["card:LAT-02"]}, maker=TEAM, thread=7)]
        self.assertEqual(sorted(b["offer"] for b in tr.own_bids(offers, TEAM)), [10, 11])

    def test_dedupe_cancels_and_blocks_bids_for_a_card_we_negotiate_with_a_dealer(self):
        s = full(snap([asset(1, "LAT-01")], cash=300, offers=[bid_offer(10, "LAT-03", 9)]))
        s["threads"]["open"] = [thread(7, "abuela", [ask("abuela", 1, 41, 12)], {"buy": {"card": "LAT-03"}})]
        led = dict(led0(), threads=[7])

        def kinds(**kw):
            cands, _, _ = co.candidates(s, led, args(**kw), neg.Journal(tempfile.mkdtemp()))
            return [c for c in cands if c["type"] == "cancel" and c["offer"] == 10 and "vendedor" in c["kind"]]
        self.assertFalse(kinds(), "sin flag no cambia nada")
        self.assertTrue(kinds(dedupe_bids=True))
        new = [{"type": "bid", "ref": "LAT-03", "blockers": []}, {"type": "bid", "ref": "LAT-02", "blockers": []}]
        co.dealer_bid_cancels(s, new, s["threads"]["open"], {7})
        self.assertTrue(new[0]["blockers"])
        self.assertEqual(new[1]["blockers"], [])

    def test_default_flags_leave_candidates_unchanged(self):
        s = full(snap([asset(1, "LAT-01"), asset(2, "LAT-01")], cash=262, offers=[bid_offer(20, "LAT-03", 61)]))
        s["threads"]["open"] = [thread(7, "abuela", [ask("abuela", 1, 41, 12)], {"buy": {"card": "LAT-03"}})]
        led = dict(led0(), threads=[7])
        a, pa, _ = co.candidates(s, led, args(), neg.Journal(tempfile.mkdtemp()))
        b, pb, _ = co.candidates(s, led, args(dealer_ladder=False, dedupe_bids=False, dealer_sell_dups=False),
                                 neg.Journal(tempfile.mkdtemp()))
        self.assertEqual(a, b)
        self.assertEqual(pa, pb)


class Chato(unittest.TestCase):
    def st(self, msgs, now, ref="LAT-03"):
        return neg.state_from_thread(thread(7, "chato", msgs, {"buy": {"card": ref}}), "chato", now, neg.Config())

    def prof(self, mode="score"):
        return neg.ladder_profile("chato", mode=mode)

    def test_rare_opens_at_70_and_steps_by_4_then_closes_within_1(self):
        p = self.prof()
        d = neg.decide_ladder(self.st([ask("chato", 1, 41, 40)], 41), p, 45, 10, "rare")
        self.assertEqual((d.action, d.price), ("counter", 28))
        msgs = [ask("chato", 1, 41, 40, "cancelled"), mine(2, 41, 28), ask("chato", 3, 42, 37)]
        d = neg.decide_ladder(self.st(msgs, 42), p, 45, 9, "rare")
        self.assertEqual((d.action, d.price), ("counter", 32))
        msgs += [mine(4, 42, 32), ask("chato", 5, 43, 33)]  # copia nuestro paso: -4
        d = neg.decide_ladder(self.st(msgs, 43), p, 45, 8, "rare")
        self.assertEqual((d.action, d.price), ("accept", 33))

    def test_uncommon_steps_by_3_and_lands_one_below_his_price(self):
        p = self.prof()
        self.assertEqual(neg.decide_ladder(self.st([ask("chato", 1, 41, 30)], 41), p, 40, 10, "uncommon").price, 21)
        msgs = [ask("chato", 1, 41, 30, "cancelled"), mine(2, 41, 21), ask("chato", 3, 42, 27, "cancelled"),
                mine(4, 42, 24), ask("chato", 5, 43, 27)]
        d = neg.decide_ladder(self.st(msgs, 43), p, 40, 8, "uncommon")
        self.assertEqual((d.action, d.price), ("counter", 26), "+3 acotado a su precio - 1: a 1 P acepta él")

    def test_final_counter_once_then_accept_final(self):
        p = self.prof()
        base = [ask("chato", 1, 41, 40, "cancelled"), mine(2, 41, 28), ask("chato", 3, 42, 34, final=True)]
        d = neg.decide_ladder(self.st(base, 42), p, 45, 9, "rare")
        self.assertEqual((d.action, d.price), ("counter", 33))
        countered = base + [mine(4, 42, 33)]
        self.assertEqual(neg.decide_ladder(self.st(countered, 42), p, 45, 8, "rare", 42).action, "wait",
                         "nunca se acepta su final en el mismo tick de nuestra contraoferta")
        again = countered + [ask("chato", 5, 43, 34, final=True)]
        d = neg.decide_ladder(self.st(again, 43), p, 45, 8, "rare", 43)
        self.assertEqual((d.action, d.price), ("accept", 34), "una sola contraoferta tras la final")
        silent = neg.decide_ladder(self.st(countered, 43), p, 45, 8, "rare", 43)
        self.assertEqual((silent.action, silent.price), ("accept", 34), "sin respuesta: su final en el tick siguiente")

    def test_final_above_ceiling_tries_final_minus_one_or_walks(self):
        p = self.prof()
        base = [ask("chato", 1, 41, 40, "cancelled"), mine(2, 41, 28), ask("chato", 3, 42, 34, final=True)]
        self.assertEqual(neg.decide_ladder(self.st(base, 42), p, 33, 9, "rare").price, 33)
        self.assertEqual(neg.decide_ladder(self.st(base, 42), p, 32, 9, "rare").action, "abandon")

    def test_never_pays_the_opening_price_in_score_mode(self):
        msgs = [ask("chato", 1, 41, 30, final=True)]
        self.assertEqual(neg.decide_ladder(self.st(msgs, 41), self.prof(), 40, 9, "uncommon").action, "counter")
        msgs = [ask("chato", 1, 41, 30, "cancelled"), mine(2, 41, 29), ask("chato", 3, 42, 30, final=True)]
        self.assertEqual(neg.decide_ladder(self.st(msgs, 42), self.prof(), 40, 8, "uncommon").action, "abandon")
        self.assertEqual(neg.decide_ladder(self.st(msgs, 42), self.prof("acquire"), 40, 8, "uncommon").action, "accept")


class Abuela(unittest.TestCase):
    def st(self, msgs, now):
        return neg.state_from_thread(thread(7, "abuela", msgs, {"buy": {"card": "LAT-03"}}), "abuela", now,
                                     neg.Config())

    def test_steps_of_one_and_patience(self):
        p = neg.ladder_profile("abuela")
        self.assertEqual(neg.decide_ladder(self.st([ask("abuela", 1, 41, 22)], 41), p, 30, 20, "uncommon").price, 13)
        msgs = [ask("abuela", 1, 41, 22, "cancelled"), mine(2, 41, 13), ask("abuela", 3, 42, 21)]
        d = neg.decide_ladder(self.st(msgs, 42), p, 30, 20, "uncommon")
        self.assertEqual((d.action, d.price), ("counter", 14))
        self.assertGreaterEqual(p.max_counteroffers, 10)

    def test_final_of_pack_is_accepted_within_ceiling(self):
        p = neg.ladder_profile("abuela")
        msgs = [ask("abuela", 1, 41, 26, "cancelled"), mine(2, 41, 18), ask("abuela", 3, 42, 20, final=True)]
        self.assertEqual(neg.decide_ladder(self.st(msgs, 42), p, 25, 20).action, "accept")
        self.assertEqual(neg.decide_ladder(self.st(msgs, 42), p, 19, 20).action, "abandon")


class NewDealer(unittest.TestCase):
    def st(self, msgs, now):
        return neg.state_from_thread(thread(7, "pilar", msgs, {"buy": {"card": "LAT-03"}}), "pilar", now, neg.Config())

    def test_prudent_and_parametrizable(self):
        p = neg.ladder_profile("pilar")
        self.assertEqual(neg.decide_ladder(self.st([ask("pilar", 1, 41, 50)], 41), p, 60, 10).price, 40)
        msgs = [ask("pilar", 1, 41, 50, "cancelled"), mine(2, 41, 40), ask("pilar", 3, 42, 49, "cancelled"),
                mine(4, 42, 44), ask("pilar", 5, 43, 48, final=True)]
        d = neg.decide_ladder(self.st(msgs, 43), p, 60, 8)
        self.assertEqual(d.action, "abandon", "48 > 95 % de su apertura (47): no se paga")
        lc = co.ladder_cfg(args(ladder_new_open=0.6, ladder_new_counters=5, ladder_new_max_frac=1.0))
        p = neg.ladder_profile("pilar", lc)
        self.assertEqual((p.max_counteroffers, p.max_open_frac), (5, 1.0))
        self.assertEqual(neg.decide_ladder(self.st([ask("pilar", 1, 41, 50)], 41), p, 60, 10).price, 30)

    def test_messages_do_not_address_a_new_dealer_as_abuela(self):
        self.assertNotIn("Abuela", neg.ladder_message("pilar", 0, 30, "LAT-03"))
        self.assertEqual(neg.ladder_message("abuela", 0, 30, "x"), neg.dealer_message("abuela", 0, 30, "x"))


class Routing(unittest.TestCase):
    def test_uncommon_purchases_are_routed_to_abuela(self):
        s = full(snap([asset(1, "LAT-01")], cash=300), {
            "abuela": {"menu": {"sells": [{"rarity": "uncommon", "list_price": 22}]}},
            "chato": {"open_to_all": True, "menu": {"sells": [{"rarity": "uncommon", "list_price": 30}]}}})
        s["catalog"]["sets"][0]["cards"][2]["rarity"] = "uncommon"  # LAT-03
        s["catalog"]["sets"][0]["cards"][2]["book"] = 40
        s["me"]["collection_value"] = round(tr.Valuation(s["catalog"], s["me"]["affinity"]).total(
            tr.counts_of(s["me"]["assets"])), 2)

        def opens(**kw):
            cands, _, _ = co.candidates(s, led0(), args(**kw), neg.Journal(tempfile.mkdtemp()))
            return {c["dealer"]: c["blockers"] for c in cands if c["type"] == "dealer_open" and c["ref"] == "card:LAT-03"}
        self.assertEqual(opens(), {"abuela": [], "chato": []})
        o = opens(dealer_ladder=True)
        self.assertEqual(o["abuela"], [])
        self.assertTrue(any("enrutado a abuela" in b for b in o["chato"]))


class Sell(unittest.TestCase):
    def st(self, msgs, now, aid=2):
        return neg.state_from_thread(thread(8, "abuela", msgs, {"sell": {"assets": [aid]}}), "abuela", now,
                                     neg.Config(), side="sell")

    def test_sell_ladder_steps_down_by_one_and_takes_final_six(self):
        lc = neg.LadderConfig()
        d = neg.decide_ladder_sell(self.st([bid("abuela", 1, 41, 5, 2)], 41), lc, 5, 10)
        self.assertEqual((d.action, d.price), ("counter", 10))
        msgs = [bid("abuela", 1, 41, 5, 2, "cancelled"), mine(2, 41, 10), bid("abuela", 3, 42, 6, 2, final=True)]
        d = neg.decide_ladder_sell(self.st(msgs, 42), lc, 5, 9)
        self.assertEqual((d.action, d.price), ("accept", 6))
        self.assertEqual(neg.decide_ladder_sell(self.st(msgs, 42), lc, 7, 9).action, "abandon", "6 bajo el suelo 7")
        msgs = [bid("abuela", 1, 41, 5, 2, "cancelled"), mine(2, 41, 10), bid("abuela", 3, 42, 5, 2)]
        self.assertEqual(neg.decide_ladder_sell(self.st(msgs, 42), lc, 4, 9).price, 9)

    def test_sell_never_accepts_her_opening_bid_in_score_mode(self):
        lc = neg.LadderConfig()
        msgs = [bid("abuela", 1, 41, 5, 2, "cancelled"), mine(2, 41, 6), bid("abuela", 3, 42, 5, 2)]
        d = neg.decide_ladder_sell(self.st(msgs, 42), lc, 3, 9)
        self.assertNotEqual(d.action, "accept")

    def test_sell_offer_structure_is_validated(self):
        o = bid("abuela", 3, 42, 6, 2, final=True)["offer"]
        self.assertEqual(neg.sell_offer_problems(o, dealer="abuela", asset_id=2, floor=5), [])
        self.assertTrue(neg.sell_offer_problems(o, dealer="abuela", asset_id=9, floor=5))
        self.assertTrue(neg.sell_offer_problems(o, dealer="abuela", asset_id=2, floor=7))

    def dealers(self):
        return {"abuela": {"menu": {"sells": [{"rarity": "common", "list_price": 10}]}}}

    def test_only_surplus_copies_are_offered_and_complete_pages_are_never_broken(self):
        page = [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")]
        s = full(snap(page, cash=300), self.dealers())
        cands, _, _ = co.candidates(s, led0(), args(), neg.Journal(tempfile.mkdtemp()))
        self.assertFalse([c for c in cands if c["type"] == "dealer_sell_open"], "sin flag no se vende")
        cands, _, _ = co.candidates(s, led0(), args(dealer_sell_dups=True), neg.Journal(tempfile.mkdtemp()))
        sells = [c for c in cands if c["type"] == "dealer_sell_open"]
        self.assertEqual([(c["ref"], c["asset"], c["blockers"]) for c in sells], [("card:LAT-01", 2, [])])
        self.assertEqual(sells[0]["floor"], 5)  # pierde 3,25 P (2.ª copia) + margen 1
        self.assertEqual(pg.guard_candidate(sells[0], s), [])
        last = dict(sells[0], asset=3, ref="card:LAT-02")  # la única copia de LAT-02: rompería la página
        self.assertTrue(pg.guard_candidate(last, s))
        s1 = full(snap(page[:1] + page[2:], cash=300), self.dealers())
        cands, _, _ = co.candidates(s1, led0(), args(dealer_sell_dups=True), neg.Journal(tempfile.mkdtemp()))
        self.assertFalse([c for c in cands if c["type"] == "dealer_sell_open"])

    def test_sell_thread_accepts_valid_final_and_closes_when_copy_is_gone(self):
        page = [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")]
        s = full(snap(page, cash=300), self.dealers())
        t = thread(8, "abuela", [bid("abuela", 1, 41, 5, 2, "cancelled"), mine(2, 41, 8),
                                 bid("abuela", 3, 42, 6, 2, final=True)], {"sell": {"assets": [2]}})
        s["threads"]["open"] = [t]
        led = dict(led0(), threads=[8])
        cands, _, _ = co.candidates(s, led, args(), neg.Journal(tempfile.mkdtemp()))
        self.assertEqual([c["type"] for c in cands if c.get("thread") == 8], ["info"], "sin flag no se toca")
        cands, _, _ = co.candidates(s, led, args(dealer_sell_dups=True), neg.Journal(tempfile.mkdtemp()))
        acc = next(c for c in cands if c.get("thread") == 8)
        self.assertEqual((acc["type"], acc["price"], acc["asset"], acc["blockers"]), ("dealer_sell_accept", 6, 2, []))
        self.assertEqual(pg.guard_candidate(acc, s), [])
        s["me"]["assets"] = [a for a in page if a["id"] != 2]
        cands, _, _ = co.candidates(s, led, args(dealer_sell_dups=True), neg.Journal(tempfile.mkdtemp()))
        self.assertEqual(next(c for c in cands if c.get("thread") == 8)["type"], "dealer_close")


class SellExposure(unittest.TestCase):
    def test_our_previous_ask_in_the_same_thread_is_not_a_second_commitment(self):
        page = [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")]
        same = offer(30, {"assets": [asset(2, "LAT-01")]}, {"cash": 10}, maker=TEAM, thread=8)
        s = full(snap(page, cash=300, offers=[same]))
        c = {"type": "dealer_sell_accept", "thread": 8, "asset": 2, "offer": 3}
        self.assertIsNone(co.double_commit(c, s, co.committed_ids(s, led0())))
        s = full(snap(page, cash=300, offers=[same, offer(31, {"assets": [asset(2, "LAT-01")]}, {"cash": 9},
                                                          maker=TEAM)]))
        self.assertTrue(co.double_commit(c, s, co.committed_ids(s, led0())), "la copia ya está en otra oferta")
        team = {"type": "team_propose", "thread": 8, "asset": 2}
        self.assertTrue(co.double_commit(team, s, co.committed_ids(s, led0())), "otros tipos: sin cambios")


class LadderAPI(FakeAPI):
    def say(self, tid, text="", price=None, offer=None):
        self.calls.append(("say", tid, text, price))
        return {"id": 500}

    def open_thread(self, with_, topic=None, venue=None):
        self.calls.append(("open", with_, topic))
        return {"id": 77}

    def close_thread(self, tid):
        self.calls.append(("close", tid))
        return {"status": "closed"}


class Cycle(unittest.TestCase):
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
        reader = FakeReader(s)
        reader.api = LadderAPI()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            co.cycle(reader, args(**kw), led, self.journal, True)
        return out.getvalue(), reader.api.calls, led

    def test_sell_open_sends_sell_topic_and_settles_cash(self):
        page = [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")]
        s = full(snap(page, cash=100), {"abuela": {"menu": {"sells": []}}})
        out, calls, led = self.run_cycle(s, dealer_sell_dups=True)
        self.assertIn(("open", "abuela", {"sell": {"assets": [2]}}), calls, out)
        self.assertIn(77, led["threads"])
        # el trato se liquida: el vendedor pagó 6 P
        t = thread(77, "abuela", [bid("abuela", 1, 41, 5, 2, "cancelled"), mine(2, 41, 8),
                                  bid("abuela", 3, 42, 6, 2, "settled", final=True)], {"sell": {"assets": [2]}},
                   status="deal")
        led["actions"].append({"key": "x", "type": "dealer_sell_accept", "status": "submitted", "tick": 49,
                               "thread": 77, "dealer": "abuela", "item": "card:LAT-01", "price": 6})
        s2 = copy.deepcopy(s)
        s2["threads"]["deal"] = [t]
        co.reconcile(led, s2, self.journal)
        a = led["actions"][-1]
        self.assertEqual((a["status"], a["received"], led["cash_received"]), ("settled", 6, 6))

    def test_ladder_counter_uses_fixed_step_and_dealer_text(self):
        s = full(snap([asset(1, "LAT-01")], cash=300), {"chato": {"open_to_all": True, "menu": {"sells": []}}})
        s["catalog"]["sets"][0]["cards"][2]["rarity"] = "rare"
        s["catalog"]["sets"][0]["cards"][2]["book"] = 40
        s["me"]["collection_value"] = round(tr.Valuation(s["catalog"], s["me"]["affinity"]).total(
            tr.counts_of(s["me"]["assets"])), 2)
        s["threads"]["open"] = [thread(7, "chato", [ask("chato", 1, 41, 40, "cancelled"), mine(2, 41, 28),
                                                    ask("chato", 3, 42, 37)], {"buy": {"card": "LAT-03"}})]
        co.save({"team": TEAM, "threads": [7], "actions": [], "spent_confirmed": 0})
        out, calls, _ = self.run_cycle(s, dealer_ladder=True)
        says = [c for c in calls if c[0] == "say"]
        self.assertEqual(says[0][3], 32, out)
        self.assertEqual(says[0][2], neg.dealer_message("chato", 1, 32, "card:LAT-03"))



if __name__ == "__main__":
    unittest.main()
