"""Auditoría: contabilidad canónica, presupuesto, capital asignable, venues rivales, v15 y aprobaciones t05. Sin red."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from datetime import datetime
from pathlib import Path

import accounting as acct
import coordinator as co
import market_agent as ma
import market_intel as mi
import negotiation as neg
import page_campaign as pc
import page_guard as pg
import performance as perf
import third_party as tp
import trading as tr
import v10_commission as v10c
from test_market_intel import TEAM, A, ask, bid, offer
from test_page_campaign import cargs, inventory, snapm, val_of
from test_team_sale import latina, snap10


def deal_thread(tid, price, dealer="chato", card="MAL-10"):
    return {"id": tid, "kind": "persona", "with": dealer, "status": "deal", "created_tick": 100, "standing_offers": [],
            "topic": {"buy": {"card": card}}, "messages": [
                {"tick": 101, "sender": dealer, "offer": {"id": tid * 10, "maker": dealer, "status": "settled",
                                                          "give": {"types": [f"card:{card}"]}, "want": {"cash": price}}}]}


def new_led(**kw):
    return {"actions": [], "spent_confirmed": 0, "cash_received": 0, "threads": [], "blocked": {}, "class_tick": {},
            "expiry_obs": [], **kw}


def reconcile(led, s):
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
        return co.reconcile(led, s, neg.Journal(d))


class Accounting(unittest.TestCase):
    def test_01_one_dealer_deal_seen_by_counter_and_accept_counts_once(self):
        s = snap10(latina(), cash=100)
        s["clock"]["tick"] = 110
        s["threads"] = {"open": [], "deal": [deal_thread(500, 96)]}
        led = new_led(actions=[
            {"type": "dealer_counter", "status": "submitted", "thread": 500, "tick": 105, "dealer": "chato", "item": "card:MAL-10", "price": 96},
            {"type": "dealer_accept", "status": "submitted", "thread": 500, "tick": 106, "dealer": "chato", "item": "card:MAL-10", "price": 96}])
        reconcile(led, s)
        self.assertEqual(led["spent_confirmed"], 96, "una compra, un pago")
        self.assertEqual([a.get("duplicate_of") for a in led["actions"]], [None, "dealer-buy:500"])
        reconcile(led, s)  # reinicio / mismo estado otra vez
        self.assertEqual(led["spent_confirmed"], 96)

    def test_02_inventory_fallback_never_double_counts(self):
        s = snap10(latina() + [A(900, "MAL-10")], cash=100)
        s["clock"]["tick"] = 120
        t = deal_thread(501, 96)
        t["messages"][0]["offer"]["status"] = "cancelled"   # deal sin oferta settled: evidencia = inventario
        s["threads"] = {"open": [], "deal": [t]}
        led = new_led(actions=[
            {"type": "dealer_counter", "status": "submitted", "thread": 501, "tick": 100, "dealer": "chato", "item": "card:MAL-10", "price": 90},
            {"type": "dealer_accept", "status": "submitted", "thread": 501, "tick": 101, "dealer": "chato", "item": "card:MAL-10", "price": 96}])
        reconcile(led, s)
        self.assertEqual(led["spent_confirmed"], 96)

    def test_03_market_settlement_seen_by_two_actions_counts_once(self):
        ev = {"type": "settlement", "tick": 105, "payload": {"settlement": 77, "tick": 105, "kind": "trade", "venue": "v02",
              "fee": 0, "price": 12, "parties": [TEAM, "t09"], "items": [{"id": 5, "kind": "card", "ref": "MAL-10", "frm": "t09", "to": TEAM}]}}
        s = snap10(latina(), cash=100)
        s["clock"]["tick"] = 106
        s["feed"] = {"events": [ev]}
        led = new_led(actions=[
            {"type": "bid", "status": "submitted", "tick": 100, "ref": "MAL-10", "price": 12, "offer_id": 1},
            {"type": "accept", "status": "submitted", "tick": 100, "ref": "MAL-10", "price": 12, "receive_assets": [5], "assets": []}])
        reconcile(led, s)
        self.assertEqual(led["spent_confirmed"], 12)
        self.assertEqual(sum(1 for a in led["actions"] if a.get("duplicate_of")), 1)

    def test_04_audit_and_explicit_repair_of_legacy_ledger(self):
        led = new_led(spent_confirmed=673, cash_received=250, actions=[
            {"type": "dealer_counter", "status": "settled", "thread": 1, "paid": 96, "tick": 1},
            {"type": "dealer_accept", "status": "settled", "thread": 1, "paid": 96, "tick": 2},
            {"type": "dealer_counter", "status": "settled", "thread": 2, "paid": 10, "tick": 3}])
        a = acct.audit(led)
        self.assertEqual((a["double_counted_spend"], a["spent_canonical"]), (96, 577))
        self.assertEqual(led["spent_confirmed"], 673, "auditar no modifica")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "led.json"
            path.write_text(json.dumps(led))
            r = acct.repair(led, path)
            self.assertTrue(r["repaired"] and list(Path(d).glob("led.json.bak-*")), "copia de seguridad")
        self.assertEqual(led["spent_confirmed"], 577)
        self.assertEqual(led["accounting_log"][0]["removed_spend"], 96)
        self.assertFalse(acct.repair(led)["repaired"], "idempotente")


class Budget(unittest.TestCase):
    def test_05_explicit_limit_respected_and_modes(self):
        led = new_led(spent_confirmed=557, cash_received=250)
        g = acct.budget_state(led, 650, "gross")
        n = acct.budget_state(led, 650, "net")
        self.assertEqual((g["remaining"], n["remaining"], g["limit"]), (93, 343, 650))
        self.assertEqual(acct.budget_state(new_led(spent_confirmed=673), 650)["remaining"], -23, "límite del operador intacto")

    def test_06_pending_income_and_commission_are_not_cash(self):
        st = {"approvals": {"k": {"status": "settled", "settlement_id": 1}}}
        self.assertEqual(v10c.summary(st)["commission_due_p"], 1)
        led = new_led(spent_confirmed=100, cash_received=0)
        self.assertEqual(acct.budget_state(led, 200, "net")["remaining"], 100, "la comisión por cobrar no es ingreso")

    def test_07_budget_not_reset_on_restart_and_over_limit_blocks_purchases(self):
        with tempfile.TemporaryDirectory() as d:
            saved = co.LEDGER
            co.LEDGER = Path(d) / "l.json"
            try:
                co.save(new_led(team=TEAM, spent_confirmed=300))
                self.assertEqual(co.load_ledger(TEAM)["spent_confirmed"], 300)
            finally:
                co.LEDGER = saved
        s = snapm(inventory(["MAL-08", "MAL-09"]), [ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=500)])
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                co.INTEL_STATE["intel"] = None
                cands, pl, _ = co.candidates(s, new_led(spent_confirmed=500), cargs(max_spend=450), neg.Journal(d))
            finally:
                co.DATA = saved
        self.assertLess(pl["budget"]["remaining"], 0)
        acc = [c for c in cands if c.get("page_campaign") == "MAL-10" and c["type"] == "accept"]
        self.assertTrue(acc and acc[0]["blockers"], "gasto por encima del límite explícito: no se compra")


class Capital(unittest.TestCase):
    def cands(self, s, led=None, **kw):
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                co.INTEL_STATE["intel"] = None
                return co.candidates(s, led or new_led(), cargs(**kw), neg.Journal(d))
            finally:
                co.DATA = saved

    def test_08_idle_dealer_liquidity_not_reserved_on_top_of_campaign(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]), cash=60)
        s["dealers"] = {"chato": {"open_to_all": True, "menu": {"sells": [{"rarity": "rare", "list_price": 40}]}}}
        _, pl, _ = self.cands(s, reserve=5, dealer_liquidity=40)
        self.assertEqual(pl["capital"]["dealer_liquidity_target"], 0, "el mismo dinero no se reserva dos veces")
        self.assertEqual(pl["capital"]["free_tactical_cash"], 55)
        _, pl2, _ = self.cands(s, reserve=5, dealer_liquidity=40, page_campaign="none")
        self.assertGreater(pl2["capital"]["dealer_liquidity_target"], 0, "sin campaña, sí se reserva")

    def test_09_unaffordable_route_reports_exact_deficit_and_never_opens_what_we_cannot_pay(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]), cash=38)
        s["dealers"] = {"picaros": {"open_to_all": True, "menu": {"sells": [{"rarity": "rare", "list_price": 63}]}}}
        s["me"]["unlocked"] = ["picaros"]
        cands, pl, _ = self.cands(s, reserve=5, max_spend=650)
        camp = pl["page_campaign"]
        f = camp["funding"]
        self.assertEqual((f["ref"], f["need"], f["free"], f["deficit"]), ("MAL-10", 63, 33, 30))
        self.assertIn("efectivo", f["binding"])
        self.assertIn("NO aplicado" if False else "operador", f["config"])
        self.assertIn("SIN FINANCIACIÓN", camp["next_action"])
        self.assertFalse([c for c in cands if c["type"] == "bid" and c.get("ref") == "MAL-10" and not c["blockers"]])
        for c in cands:
            if c["type"] == "bid" and c.get("ref") == "MAL-10":
                self.assertLessEqual(-c["cash"], 33, "ninguna puja por encima de lo que podemos pagar")

    def test_10_sale_proceeds_are_not_counted_as_available_cash(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]), cash=38)
        s["dealers"] = {"picaros": {"open_to_all": True, "menu": {"sells": [{"rarity": "rare", "list_price": 63}]}}}
        s["me"]["unlocked"] = ["picaros"]
        _, pl, _ = self.cands(s, reserve=5, max_spend=650)
        self.assertEqual(pl["funding"]["free"], 33, "una venta futura no suma a lo libre")


class RivalVenues(unittest.TestCase):
    def sale(self, du=8.0, receive=None):
        return {"type": "accept", "venue": "v02", "cash": 9, "du": du, "receive": receive or {}, "deliver": {"MAL-07": 1},
                "blockers": []}

    def test_11_prohibition_kept_by_default_and_exception_is_narrow(self):
        s = snap10(latina())
        self.assertIn("v02", co.rival_venue_blocker(self.sale(), s))
        a = Namespace(margin=1.0)
        self.assertTrue(co.funds_priority_purchase(self.sale(), a))
        self.assertFalse(co.funds_priority_purchase(self.sale(du=0.5), a), "sin excedente no se justifica")
        self.assertFalse(co.funds_priority_purchase(self.sale(receive={"X": 1}), a), "un trueque no es una venta")

    def test_12_exception_requires_flag_deficit_and_covering_funding(self):
        s = snap10(latina())
        a_off = Namespace(margin=1.0, rival_venue_allow_funding=False)
        a_on = Namespace(margin=1.0, rival_venue_allow_funding=True)
        cover = {"funding": {"deficit": 30, "covers_potential": True, "ref": "MAL-10"}}
        short = {"funding": {"deficit": 30, "covers_potential": False, "ref": "MAL-10"}}
        none = {}
        for args, pl, allowed in ((a_off, cover, False), (a_on, cover, True), (a_on, short, False), (a_on, none, False)):
            c = co.apply_rival_policy([self.sale()], s, args, pl)[0]
            self.assertEqual(not c["blockers"], allowed, (args, pl))
        ok = co.apply_rival_policy([self.sale()], s, a_on, cover)[0]
        self.assertIn("v02", ok["rival_venue_override"])
        self.assertTrue(any("NO cuantificado" in n for n in ok["notes"]))
        poor = co.apply_rival_policy([self.sale(du=0.2)], s, a_on, cover)[0]
        self.assertTrue(poor["blockers"], "sin excedente no hay excepción")


class SettlementSafety(unittest.TestCase):
    def test_13_mal10_bonus_counted_once(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]))
        val = val_of(s)
        counts = tr.counts_of(s["me"]["assets"])
        gain = val.next_copy(counts, "MAL-10")
        nobonus = tr.Valuation({**s["catalog"], "values": {**s["catalog"]["values"], "page_bonus": 0}}, s["me"]["affinity"])
        bonus = gain - nobonus.next_copy(counts, "MAL-10")
        self.assertAlmostEqual(bonus, 0.25 * val.page_sum("MAL"), places=2, msg="bono = 0,25 × suma de la página, UNA vez")
        st = pc.campaign_state(s, val, "MAL")
        self.assertEqual(st["gains"]["MAL-10"]["gain"], round(gain, 2))
        s2 = snapm(inventory(["MAL-08", "MAL-09"]), [ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=500)])
        p = pc.plan_target("MAL-10", st["gains"]["MAL-10"]["gain"], True, "CRITICAL", s2, val, pc.CampaignConfig(),
                           mi.venues_from(s2), None, [])
        self.assertAlmostEqual(p["best"]["du"], gain - 28, places=2, msg="ΔU = ganancia (con bono) − coste, sin repetir el bono")

    def test_14_completed_page_single_copies_never_offered_for_funding(self):
        s = snapm(inventory(["MAL-08", "MAL-09", "MAL-10"]))
        counts = tr.counts_of(s["me"]["assets"])
        prot = pg.protected_page_cards(counts, s["catalog"])
        assets = pc.our_trade_assets(s, val_of(s), set())
        self.assertFalse({a["ref"] for a in assets} & {r for r in prot if counts[r] < 2})

    def test_15_restart_does_not_duplicate_charges_or_publications(self):
        ledger = new_led(actions=[{"key": "k1", "type": "list", "status": "submitted", "tick": 100, "ref": "MAL-07"}])
        s = snap10(latina(1))
        s["clock"]["tick"] = 101
        reconcile(ledger, s)
        reconcile(ledger, s)
        self.assertEqual(ledger["spent_confirmed"], 0)
        self.assertEqual(len(ledger["actions"]), 1)


class T05Approvals(unittest.TestCase):
    NOW = datetime.fromisoformat("2026-10-03T17:50:00+02:00")
    OK = "APPROVE card=MAL-07 buyer=t02 price=14 venue=v10 date=2026-10-03 valid_until=18:30 commission=1 commission_payer=t05"

    def test_16_valid_strict_approval(self):
        t, why = v10c.parse_structured_approval(self.OK, self.NOW)
        self.assertEqual((why, t["card"], t["buyer"], t["price"], t["venue"], t["commission_payer"]),
                         ("", "MAL-07", "t02", 14, "v10", "t05"))

    def test_17_expired_ambiguous_or_incomplete_blocked(self):
        late = datetime.fromisoformat("2026-10-03T18:31:00+02:00")
        self.assertIn("vencida", v10c.parse_structured_approval(self.OK, late)[1])
        no_payer = self.OK.replace(" commission_payer=t05", "")
        self.assertIn("ausentes", v10c.parse_structured_approval(no_payer, self.NOW)[1])
        yesterday = self.OK.replace("2026-10-03", "2026-10-02")
        self.assertIn("vencida", v10c.parse_structured_approval(yesterday, self.NOW)[1])
        tomorrow = self.OK.replace("2026-10-03", "2026-10-04")
        self.assertIn("hoy", v10c.parse_structured_approval(tomorrow, self.NOW)[1])
        self.assertIsNone(v10c.parse_structured_approval("vale, vendednos esa MAL-07 a t02", self.NOW)[0])
        self.assertIsNone(v10c.parse_structured_approval(self.OK + " extra=1", self.NOW)[0])
        self.assertIsNone(v10c.parse_structured_approval(self.OK.replace("buyer=t02", "buyer=Team2"), self.NOW)[0])

    def test_18_only_t05_in_official_team_thread_and_tamper_detected(self):
        thread = {"id": 2000, "kind": "team", "team": "t15", "with": "t05",
                  "messages": [{"id": 1, "sender": "t05", "text": self.OK}]}
        st = {}
        self.assertEqual(len(v10c.sync_structured(st, thread, "t15", self.NOW)), 1)
        thread["messages"][0]["text"] = self.OK.replace("price=14", "price=1")
        v10c.sync_structured(st, thread, "t15", self.NOW)
        self.assertEqual(list(st["approvals"].values())[0]["status"], "tampered")
        other = {"id": 2001, "kind": "team", "team": "t15", "with": "t09",
                 "messages": [{"id": 1, "sender": "t09", "text": self.OK}]}
        self.assertEqual(v10c.sync_structured({}, other, "t15", self.NOW), [], "otro equipo no aprueba")
        spoof = {"id": 2000, "kind": "team", "team": "t15", "with": "t05",
                 "messages": [{"id": 5, "sender": "t09", "text": self.OK}]}
        self.assertEqual(v10c.sync_structured({}, spoof, "t15", self.NOW), [])
        with_offer = {"id": 2000, "kind": "team", "team": "t15", "with": "t05",
                      "messages": [{"id": 6, "sender": "t05", "text": self.OK, "offer": {"id": 1}}]}
        st2 = {}
        v10c.sync_structured(st2, with_offer, "t15", self.NOW)
        self.assertIn("sin oferta estructurada", list(st2["rejected_messages"].values())[0]["reason"])

    def test_19_approved_sale_checked_against_valuation_and_single_copy_not_sold(self):
        st = {"approvals": {"k": {"status": "approved", "card": "MAL-01", "buyer": "t02", "price": 3, "venue": "v10",
                                  "expires_at": "2999-01-01T00:00:00+02:00", "strict": True}}}
        me_single = {"assets": [A(1, "MAL-01")]}
        out = v10c.listing_candidates(st, me_single, 30.0, set())
        self.assertEqual(out, [], "una sola copia: no hay duplicado que vender")
        me_two = {"assets": [A(1, "MAL-01"), A(2, "MAL-01")]}
        c = v10c.listing_candidates(st, me_two, 30.0, set())[0]
        self.assertEqual((c["venue"], c["to"], c["price"]), ("v10", "t02", 3))


class V15(unittest.TestCase):
    def board(self, offers):
        s = snapm(inventory())
        for o in offers:
            s["boards"].setdefault(o["venue"], {"offers": []})["offers"].append(o)
        return s

    def test_20_detects_real_pair_ignores_aliases_and_tracks_states(self):
        sell = ask(1, "rastro", 11, "MAL-04", 10, maker="t06", exp=900)
        buy = bid(2, "v03", "MAL-04", 12, maker="t09", exp=900)
        alias = ask(3, "rastro", 12, "MAL-04", 9, maker="m3950d43b", exp=900)
        s = self.board([sell, buy, alias])
        s["clock"]["tick"] = 500
        found = tp.detect(s, "v15", TEAM, {"rastro": 500})
        self.assertEqual([(o["seller"], o["buyer"], o["ref"]) for o in found], [("t06", "t09", "MAL-04")])
        st = {}
        self.assertEqual(tp.update(st, s, "v15", TEAM, 500)[0][2], "OPPORTUNITY")
        key = found[0]["key"]
        tp.mark(st, key, "INTEREST_CONFIRMED", "t06 respondió")
        with self.assertRaises(ValueError):
            tp.mark(st, key, "OPPORTUNITY")
        s2 = self.board([ask(4, "v15", 11, "MAL-04", 10, maker="t06", exp=900), buy])
        s2["clock"]["tick"] = 501
        self.assertEqual(st["opps"][key]["state"], "INTEREST_CONFIRMED")
        tp.update(st, s2, "v15", TEAM, 501)
        self.assertEqual(st["opps"][key]["state"], "POSTED")
        s3 = self.board([ask(4, "v15", 11, "MAL-04", 10, maker="t06", exp=900), bid(5, "v15", "MAL-04", 12, maker="t09", exp=900)])
        s3["clock"]["tick"] = 502
        tp.update(st, s3, "v15", TEAM, 502)
        self.assertEqual(st["opps"][key]["state"], "PENDING")
        s4 = self.board([])
        s4["clock"]["tick"] = 503
        s4["feed"] = {"events": [{"type": "settlement", "payload": {"venue": "v15", "parties": ["t06", "t09"],
                                                                      "items": [{"ref": "MAL-04"}]}}]}
        tp.update(st, s4, "v15", TEAM, 503)
        self.assertEqual(st["opps"][key]["state"], "SETTLED")

    def test_21_draft_is_short_and_reveals_no_private_value(self):
        d = tp.draft({"ref": "MAL-04", "ask": 10, "bid": 12}, "v15")
        for text in d.values():
            self.assertLess(len(text), 220)
            for forbidden in ("valor", "bono", "techo", "reserva", "efectivo total"):
                self.assertNotIn(forbidden, text.lower())


class Performance(unittest.TestCase):
    def test_22_conversation_cohorts_and_no_double_counted_results(self):
        acts = [
            {"type": "dealer_open", "status": "settled", "thread": 1}, {"type": "dealer_open", "status": "settled", "thread": 2},
            {"type": "dealer_open", "status": "settled", "thread": 3}, {"type": "dealer_open", "status": "settled", "thread": 4},
            {"type": "dealer_counter", "status": "settled", "thread": 1, "paid": 10, "dv": 20, "du": 10, "tick": 1, "settled_tick": 2},
            {"type": "dealer_accept", "status": "settled", "thread": 1, "paid": 10, "dv": 20, "du": 10, "tick": 1,
             "settled_tick": 2, "duplicate_of": "dealer-buy:1"},
            {"type": "dealer_close", "status": "settled", "thread": 2},
            {"type": "dealer_counter", "status": "released", "thread": 3}]
        p = perf.realized(acts)
        c = p["conversations"]
        self.assertEqual((c["DEAL"], c["ABANDONED"], c["CLOSED_OTHER"], c["OPEN"], c["close_rate"]), (1, 1, 1, 1, 0.33))
        self.assertEqual(p["settlement_count"], 1)
        self.assertEqual(p["realized_surplus"], 10.0)


class Runtime(unittest.TestCase):
    def test_23_fingerprint_changes_with_code_and_status_reports_old_process(self):
        a = co.code_fingerprint()
        self.assertEqual(a, co.code_fingerprint())
        with tempfile.TemporaryDirectory() as d:
            saved = co.RUNTIME
            co.RUNTIME = Path(d) / "r.json"
            try:
                co.RUNTIME.write_text(json.dumps({"pid": 1, "started": "x", "fingerprint": "old", "argv": ["--execute"],
                                                  "execute": True}))
                self.assertIn("CÓDIGO ANTIGUO", co.runtime_status())
                co.RUNTIME.write_text(json.dumps({"pid": 1, "started": "x", "fingerprint": a, "argv": [], "execute": True}))
                self.assertIn("MISMO código", co.runtime_status())
            finally:
                co.RUNTIME = saved


if __name__ == "__main__":
    unittest.main()
