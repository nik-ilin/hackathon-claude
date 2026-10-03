"""Pruebas offline del coordinador único y de las políticas por vendedor (sin red ni operaciones reales)."""
import contextlib
import copy
import io
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path

import coordinator as co
import market_agent as ma
import negotiation as neg
import trading as tr
from bazaar_sdk import BazaarError
from test_trading import TEAM, asset, offer, settlement, snap

U = 13.0


def args(**kw):
    base = dict(mode="score", max_spend=80, reserve=100, per_card=60, margin=2.0, listing_ticks=10, fill_prior=0.3,
                allow_last_copy="", cancel_unsafe=False, allow_concurrent=False, show=0, campaign="none",
                campaign_ticks=30, campaign_budget=60, max_proposals=3, negotiation_ticks=6, max_conversations=2)
    base.update(kw)
    return Namespace(**base)


def dealer_thread(tid, msgs, status="open", topic=None):
    return {"id": tid, "kind": "persona", "with": "abuela", "team": TEAM, "status": status, "created_tick": 40,
            "topic": topic or {"buy": {"card": "LAT-03"}}, "messages": msgs, "standing_offers": []}


def her(oid, tick, price, status="open", final=False):
    return {"id": oid, "tick": tick, "sender": "abuela", "offer": {"id": oid, "maker": "abuela", "to": TEAM,
            "thread": 7, "status": status, "final": final, "give": {"cash": 0, "assets": [], "types": ["card:LAT-03"]},
            "want": {"cash": price, "assets": [], "types": []}, "expires_tick": tick + 2}}


def ours(oid, tick, price, status="cancelled"):
    return {"id": oid, "tick": tick, "sender": TEAM, "offer": {"id": oid, "maker": TEAM, "status": status,
            "give": {"cash": price, "assets": [], "types": []}, "want": {"cash": 0, "types": ["card:LAT-03"]}}}


def full(s, dealers=None):
    s = copy.deepcopy(s)
    s["dealers"] = dealers if dealers is not None else {"abuela": {"menu": {"sells": [{"rarity": "common",
                                                                                     "list_price": 10}]}}}
    s["me"]["unlocked"] = ["abuela"]
    return s


class Policies(unittest.TestCase):
    def st(self, msgs, now):
        return neg.state_from_thread(dealer_thread(7, msgs), "abuela", now, neg.Config())

    def test_abuela_score_mode_uses_significant_steps_and_never_pays_the_opening_price(self):
        pol = neg.dealer_policy("abuela", "score")
        d = neg.decide_dealer(self.st([her(1, 41, 12)], 41), pol, 11, 8)
        self.assertEqual((d.action, d.price), ("counter", 8))  # 65 % de 12: sin aperturas extremas
        msgs = [her(1, 41, 12, "cancelled"), ours(2, 41, 8), her(3, 42, 12)]
        d = neg.decide_dealer(self.st(msgs, 42), pol, 11, 7)
        self.assertEqual((d.action, d.price), ("counter", 10))  # 8 + ceil(0.4 * 4): no pasos de 1 P
        stalled = msgs[:2] + [her(3, 42, 12, "cancelled"), ours(4, 42, 10), her(5, 43, 12, "cancelled"),
                              ours(6, 43, 11), her(7, 44, 12)]
        self.assertEqual(neg.decide_dealer(self.st(stalled, 44), pol, 20, 5).action, "abandon",
                         "estancada en su apertura: en modo score no se compra a precio inicial")
        self.assertEqual(neg.decide_dealer(self.st(stalled, 44), neg.dealer_policy("abuela", "acquire"), 20, 5).action,
                         "accept")

    def test_final_offer_accept_within_ceiling_or_walk(self):
        msgs = [her(1, 41, 12, "cancelled"), ours(2, 41, 8), her(3, 42, 10, final=True)]
        pol = neg.dealer_policy("abuela", "score")
        self.assertEqual(neg.decide_dealer(self.st(msgs, 42), pol, 11, 7).action, "accept")
        self.assertEqual(neg.decide_dealer(self.st(msgs, 42), pol, 9, 7).action, "abandon")

    def test_chato_is_short(self):
        pol = neg.dealer_policy("chato", "score")
        self.assertEqual((pol.open_frac, pol.max_counteroffers, pol.accept_on_concession), (0.9, 1, True))

    def test_restart_counts_previous_counteroffers_and_never_repeats(self):
        msgs = [her(1, 41, 12, "cancelled"), ours(2, 41, 8), her(3, 42, 11, "cancelled"), ours(4, 42, 10),
                her(5, 43, 11, "cancelled"), ours(6, 43, 11)]
        st = self.st(msgs + [her(7, 44, 12)], 44)
        d = neg.decide_dealer(st, neg.dealer_policy("abuela", "score"), 20, 4)
        self.assertNotEqual(d.action, "counter", "3 contraofertas ya hechas")


class Budget(unittest.TestCase):
    def test_shared_budget_counts_open_bids_pending_accepts_and_dealer_exposure(self):
        mine = [asset(1, "LAT-01"), asset(2, "LAT-02")]
        board = [offer(9, {"assets": [asset(70, "LAT-03")]}, {"cash": 12})]  # completa la página: excedente alto
        s = full(snap(mine, cash=150, board=board, offers=[offer(20, {"cash": 15}, {"types": ["card:SAL-01"]},
                                                                 maker=TEAM)]))
        led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": []}
        cands, pl, _ = co.candidates(s, led, args(), neg.Journal(tempfile.mkdtemp()))
        buy = next(c for c in cands if c.get("type") == "accept")
        self.assertEqual(pl["free_cash"], 150 - 100 - 15)  # 35 P libres: la compra (12 + 2) cabe
        self.assertFalse(buy["blockers"])
        # una puja abierta más grande y una conversación con un vendedor que podría aceptar nuestra contraoferta
        s["offers"]["offers"][0]["give"]["cash"] = 30
        t = dealer_thread(7, [her(1, 41, 12, "cancelled"), ours(2, 41, 8, status="open")])
        t["standing_offers"] = [{"maker": TEAM, "status": "open", "give": {"cash": 8}}]
        s["threads"]["open"] = [t]
        cands, pl, exposure = co.candidates(s, led, args(), neg.Journal(tempfile.mkdtemp()))
        self.assertEqual(exposure, 8)
        buy = next(c for c in cands if c.get("type") == "accept")
        self.assertTrue(any("capacidad" in b for b in buy["blockers"]), "30 + 8 comprometidos: quedan 12 P y la compra cuesta 14")

    def test_fees_remove_an_apparent_profit(self):
        mine = [asset(1, "LAT-01")]
        s = full(snap(mine, cash=300, board=[offer(9, {"assets": [asset(70, "LAV-01")]}, {"cash": 6})]))
        pl = tr.plan(s, tr.Config(margin=2))
        self.assertFalse([o for o in pl["opportunities"] if o["type"] == "accept"])  # sin valor conocido / no rentable
        s = full(snap([asset(1, "LAT-01"), asset(2, "LAT-01")], cash=300,
                      board=[offer(9, {"cash": 6}, {"types": ["card:LAT-01"]})]))
        # vender la 2.ª copia (pierde 3.25) a 6 P: 6 - comisión 2 = 4 -> ΔU 0.75 < margen 2
        self.assertFalse([o for o in tr.plan(s, tr.Config(margin=2))["opportunities"] if o["kind"] == "vender"])

    def test_last_copy_protection_and_unsafe_own_offers(self):
        mine = [asset(1, "LAT-01"), asset(2, "LAT-02"), asset(3, "LAT-02")]
        own = [offer(30, {"assets": [asset(1, "LAT-01")]}, {"cash": 12}, maker=TEAM),
               offer(31, {"assets": [asset(3, "LAT-02")]}, {"cash": 12}, maker=TEAM),
               offer(32, {"assets": [asset(3, "LAT-02")]}, {"cash": 10}, maker=TEAM)]
        val = tr.Valuation(snap(mine)["catalog"], {"LAT": 1.3})
        bad = {c["offer"]: c["why"] for c in tr.unsafe_own_offers(own, TEAM, val, tr.counts_of(mine))}
        self.assertIn("vende la última copia de LAT-01", bad[30])
        self.assertNotIn(31, bad)
        self.assertTrue(any("ya está en la oferta 31" in w for w in bad[32]))
        s = full(snap(mine, offers=own))
        led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": []}
        cands, _, _ = co.candidates(s, led, args(), neg.Journal(tempfile.mkdtemp()))
        self.assertTrue(all(c["blockers"] for c in cands if c["type"] == "cancel"), "sin --cancel-unsafe no se cancela")
        cands, _, _ = co.candidates(s, led, args(cancel_unsafe=True), neg.Journal(tempfile.mkdtemp()))
        self.assertEqual(co.select(cands, led, 50)[0]["type"], "cancel")

    def test_effective_expiry_is_calibrated(self):
        ratio, basis = tr.expiry_ratio(co.EXPIRY_EVIDENCE, 60.0)
        self.assertEqual((ratio, tr.listing_request(10, ratio)), (4.0, 40))
        self.assertIn("mediana de 3", basis)
        ratio, basis = tr.expiry_ratio(co.EXPIRY_EVIDENCE, 30.0)
        self.assertEqual(ratio, 2.0)
        self.assertIn("HIPÓTESIS", basis)

    def test_packs_are_never_bought_by_routine(self):
        s = full(snap([asset(1, "LAT-01")]), {"abuela": {"menu": {"sells": [{"pack": "p", "list_price": 26}]}}})
        s["catalog"]["packs"] = [{"id": "p", "slots": [{"common": 1.0}, {"common": 1.0}]}]
        led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": []}
        cands, _, _ = co.candidates(s, led, args(), neg.Journal(tempfile.mkdtemp()))
        pack = next(c for c in cands if c["ref"] == "pack:p")
        self.assertEqual(pack["type"], "info")
        self.assertFalse(co.select([pack], led, 50))

    def test_pack_value_counts_repeats_inside_the_pack(self):
        cat = snap([])["catalog"]
        val = tr.Valuation(cat, {"LAT": 1.3})
        ev, _ = tr.pack_value(val, Counter(), {"slots": [{"common": 1.0}, {"common": 1.0}]}, draws=6000)
        # 3 comunes de LAT: P(repetida) = 1/3 -> E = 13 + 13 * (2/3 * 1 + 1/3 * 0.25) + bono si se completa (no posible)
        self.assertAlmostEqual(ev, round(13 + 13 * (2 / 3 + 1 / 3 * 0.25), 1), delta=0.4)


class FakeAPI:
    def __init__(self):
        self.calls = []

    def accept(self, oid, assets=None):
        self.calls.append(("accept", oid))
        return {"queued": True, "offer": oid}

    def list_offer(self, give, want, venue=None, expires_in_ticks=None, to=None):
        self.calls.append(("list", expires_in_ticks))
        return {"id": 900, "status": "open", "created_tick": 50, "expires_tick": 50 + expires_in_ticks // 4}

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        return {"status": "cancelled"}


class FakeReader:
    def __init__(self, s):
        self.s, self.api = s, FakeAPI()

    def snapshot(self):
        return copy.deepcopy({k: self.s[k] for k in ("catalog", "me", "venues", "board", "offers", "clock")})

    def call(self, method, *a):
        if method == "feed":
            return self.s["feed"]
        if method == "my_threads":
            return {"threads": self.s["threads"][a[0]]}
        if method == "dealer":
            if a[0] not in self.s["dealers"]:
                raise BazaarError("not_found", "", 404)
            return self.s["dealers"][a[0]]
        return self.s["levels"]


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

    def run_cycle(self, s, execute=True, **kw):
        led = co.load_ledger(TEAM)
        reader = FakeReader(s)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            co.cycle(reader, args(**kw), led, self.journal, execute)
        return out.getvalue(), reader.api.calls, led

    def test_no_valid_opportunity_means_no_action(self):
        s = full(snap([asset(1, "LAT-01")], cash=100), {})  # sin capital libre, sin duplicados, sin vendedores
        out, calls, _ = self.run_cycle(s)
        self.assertIn("Sin oportunidades válidas", out)
        self.assertEqual(calls, [])

    def test_stages_are_explicit_and_expiry_uses_the_server_value(self):
        s = full(snap([asset(1, "LAT-01"), asset(2, "LAT-01")], cash=100), {})
        out, calls, led = self.run_cycle(s)
        self.assertIn("SELECCIONADA [mercado] publicar venta LAT-01", out)
        self.assertIn("ENVIADA    [mercado] publicar venta LAT-01", out)
        self.assertEqual(calls, [("list", 40)])  # 10 ticks efectivos pedidos como 40 a 60 s/tick
        self.assertEqual(led["actions"][0]["effective_ticks"], 10)

    def test_restart_with_ambiguous_acceptance_blocks_everything(self):
        s = full(snap([asset(1, "LAT-01"), asset(2, "LAT-01")], cash=105), {})
        co.save({"team": TEAM, "actions": [{"key": "k", "type": "accept", "status": "ambiguous", "tick": 49,
                                            "offer": 5, "assets": [2], "price": 9}], "spent_confirmed": 0})
        out, calls, _ = self.run_cycle(s)
        self.assertIn("ambigua", out)
        self.assertEqual(calls, [])

    def test_executed_price_differs_from_our_last_counteroffer(self):
        t = dealer_thread(7, [her(1, 41, 17, "cancelled"), ours(2, 41, 13), her(3, 42, 17, "settled")], status="deal")
        s = full(snap([asset(1, "LAT-01"), asset(3, "LAT-03")], cash=183), {})
        s["threads"]["deal"] = [t]
        co.save({"team": TEAM, "threads": [7], "spent_confirmed": 0, "actions": [
            {"key": "c", "type": "dealer_counter", "status": "submitted", "tick": 41, "thread": 7, "dealer": "abuela",
             "item": "card:LAT-03", "price": 13, "before": {"cash": 200, "round": 1}}]})
        out, _, led = self.run_cycle(s, execute=False)
        a = led["actions"][0]
        self.assertEqual((a["status"], a["paid"], led["spent_confirmed"]), ("settled", 17, 17))
        self.assertIn("LIQUIDADA", out)
        self.assertIn("RESULTADO OBSERVADO", out)

    def test_round_change_makes_attribution_uncertain(self):
        s = full(snap([asset(1, "LAT-01")]), {})
        s["clock"]["round"] = 2
        self.assertIn("cambió la ronda", co.observed({"before": {"round": 1}}, s, []))

    def test_reserve_released_when_cancel_confirmed(self):
        s = full(snap([asset(1, "LAT-01")]), {})
        led = {"actions": [{"type": "cancel", "status": "submitted", "offer": 31, "tick": 49}], "spent_confirmed": 0,
               "cash_received": 0, "threads": []}
        co.reconcile(led, s, self.journal)
        self.assertEqual(led["actions"][0]["status"], "settled")


if __name__ == "__main__":
    unittest.main()
