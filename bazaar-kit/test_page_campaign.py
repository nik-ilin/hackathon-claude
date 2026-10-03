"""Campaña de completar Malasaña — sin red ni operaciones reales."""
import copy
import os
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path

import coordinator as co
import intelligence as it
import market_intel as mi
import negotiation as neg
import page_campaign as pc
import page_guard as pg
import trading as tr
from test_market_intel import TEAM, VENUES, A, ask, bid, offer

MAL = [f"MAL-{i:02d}" for i in range(1, 11)]
TARGETS = ["MAL-08", "MAL-09", "MAL-10"]


def catm():
    mal = [{"id": r, "name": r, "rarity": "rare" if r in ("MAL-09", "MAL-10") else "common",
            "book": 30 if r in ("MAL-09", "MAL-10") else 10, "page": True} for r in MAL]
    lat = [{"id": f"LAT-0{i}", "name": "l", "rarity": "common", "book": 10, "page": True} for i in (1, 2, 3)]
    return {"values": {"copy_marginals": [1.0, 0.25, 0.1], "page_bonus": 0.25},
            "sets": [{"id": "MAL", "name": "Malasaña", "released": True, "cards": mal},
                     {"id": "LAT", "name": "La Latina", "released": True, "cards": lat}], "packs": []}


def inventory(owned_targets=()):
    assets = [A(100 + i, r) for i, r in enumerate(MAL[:7])] + [A(200 + i, r) for i, r in enumerate(owned_targets)]
    return assets + [A(300, "LAT-01"), A(301, "LAT-02"), A(302, "LAT-03"),   # La Latina completa (una copia cada una)
                     A(310, "MAL-02"), A(311, "LAT-01")]                       # duplicados: MAL-02 y LAT-01 (excedente)


def snapm(assets, boards=(), mine=(), cash=300, tick=400):
    cat, aff = catm(), {"MAL": 1.0, "LAT": 1.0}
    val = tr.Valuation(cat, aff)
    me = {"id": TEAM, "cash": cash, "level": 2, "affinity": aff, "assets": list(assets),
          "collection_value": round(val.total(tr.counts_of(assets)), 2), "score": {}}
    b = {"rastro": {"offers": []}, "v02": {"offers": []}, "v03": {"offers": []}}
    for o in boards:
        b[o["venue"]]["offers"].append(o)
    return {"me": me, "catalog": cat, "clock": {"tick": tick, "tick_seconds": 30.0, "limits": {
            "offers_per_team_per_tick": 12, "accepts_per_team_per_tick": 1, "max_open_offers_per_team": 30}},
            "venues": {"venues": copy.deepcopy(VENUES)}, "boards": b, "board": b["rastro"],
            "offers": {"offers": list(mine)}, "feed": {"events": []}, "threads": {"open": [], "deal": []},
            "dealers": {}, "levels": {"levels": []}}


def val_of(s):
    return tr.Valuation(s["catalog"], s["me"]["affinity"])


def cargs(**kw):
    base = dict(mode="score", max_spend=500, reserve=100, per_card=100, margin=2.0, listing_ticks=10, fill_prior=0.3,
                allow_last_copy="", cancel_unsafe=False, allow_concurrent=False, show=0, campaign="none",
                campaign_ticks=30, campaign_budget=60, max_proposals=3, negotiation_ticks=6, max_conversations=2,
                engine="intel", duende_venue="v02", duende_expiry=120, history_window=240, dealer_liquidity=0,
                min_cancel_gain=2.0, rebalance_threshold=20, stale_age=30, tactical_buffer=30, max_passive_frac=0.5,
                dealer_buffer_mode="active_max", sale_target=[], page_campaign="MAL")
    base.update(kw)
    return Namespace(**base)


class State(unittest.TestCase):
    def test_01_all_three_missing(self):
        s = snapm(inventory())
        st = pc.campaign_state(s, val_of(s), "MAL")
        self.assertEqual((st["missing"], st["state"], st["level"], st["progress"]), (TARGETS, "3_MISSING", "HIGH", "7/10"))
        self.assertFalse(any(g["completes_page"] for g in st["gains"].values()))

    def test_02_one_acquired_recomputes(self):
        s0, s1 = snapm(inventory()), snapm(inventory(["MAL-08"]))
        st0, st1 = pc.campaign_state(s0, val_of(s0), "MAL"), pc.campaign_state(s1, val_of(s1), "MAL")
        self.assertEqual((st1["missing"], st1["level"]), (["MAL-09", "MAL-10"], "VERY HIGH"))
        self.assertEqual(st1["gains"]["MAL-09"]["gain"], st0["gains"]["MAL-09"]["gain"], "sin bono aún: igual")

    def test_03_two_acquired_final_card_gets_page_bonus(self):
        s0 = snapm(inventory())
        s2 = snapm(inventory(["MAL-08", "MAL-09"]))
        st0, st2 = pc.campaign_state(s0, val_of(s0), "MAL"), pc.campaign_state(s2, val_of(s2), "MAL")
        self.assertEqual((st2["missing"], st2["level"]), (["MAL-10"], "CRITICAL"))
        self.assertTrue(st2["gains"]["MAL-10"]["completes_page"])
        bonus = 0.25 * (7 * 10 + 30 + 30 + 10 * 0)  # bono = 0,25 × suma de la página (MAL-08 común 10)
        self.assertAlmostEqual(st2["gains"]["MAL-10"]["gain"] - st0["gains"]["MAL-10"]["gain"], 0.25 * 140, places=1)
        self.assertGreater(bonus, 0)


class Routes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.intel = it.Intelligence(os.path.join(self.dir.name, "m.db"), TEAM)

    def tearDown(self):
        self.intel.close()
        self.dir.cleanup()

    def feed_owner(self, team, ref, wants):
        # t04 publicó MAL-09 antes (posesión observada) y pide MAL-02 (nuestro duplicado)
        s = snapm([], [ask(900, "v03", 77, ref, 40, maker=team, created=390),
                       bid(901, "v03", wants, 8, maker=team, created=391)], tick=392)
        self.intel.ingest(s)
        self.intel.update_models(400)
        self.intel.tick = 400

    def plan(self, s, ref, assets=None):
        val = val_of(s)
        st = pc.campaign_state(s, val, "MAL")
        g = st["gains"][ref]
        return pc.plan_target(ref, g["gain"], g["completes_page"], st["level"], s, val, pc.CampaignConfig(),
                              mi.venues_from(s), self.intel,
                              pc.our_trade_assets(s, val, set()) if assets is None else assets)

    def test_04_owner_ranked_with_evidence_and_swap_with_duplicate(self):
        self.feed_owner("t04", "MAL-09", "MAL-02")
        s = snapm(inventory())
        owners = pc.owners_ranked("MAL-09", self.intel, s, pc.our_trade_assets(s, val_of(s), set()))
        self.assertEqual(owners[0]["team"], "t04")
        self.assertGreater(owners[0]["ownership_confidence"], 0.5)
        self.assertIn("MAL-02", owners[0]["wants_of_ours"])
        p = self.plan(s, "MAL-09")
        swap = next(r for r in p["routes"] if r["route"] == "C trueque dirigido")
        c = swap["candidate"]
        self.assertEqual((c["type"], c["to"], c["give_ref"], c["asset"]), ("swap_list", "t04", "MAL-02", 310))
        self.assertEqual(pg.guard_candidate(c, s), [])

    def test_05_protected_card_cannot_be_offered(self):
        self.feed_owner("t04", "MAL-09", "LAT-03")  # pide LAT-03: solo tenemos la copia que mantiene La Latina
        s = snapm(inventory())
        assets = pc.our_trade_assets(s, val_of(s), set())
        self.assertNotIn("LAT-03", [a["ref"] for a in assets])
        self.assertTrue(all(a["asset"] not in (300, 301, 302) for a in assets), "nunca la copia protegida")
        p = self.plan(s, "MAL-09")
        self.assertFalse([r for r in p["routes"] if r["route"] == "C trueque dirigido"])
        self.assertTrue(pg.guard_candidate({"type": "swap_list", "ref": "MAL-09", "asset": 302, "give_ref": "LAT-03"}, s))

    def test_06_identifiable_owner_ask_beats_weak_passive_bid(self):
        s = snapm(inventory(), [ask(950, "v02", 88, "MAL-08", 6, maker="t11", exp=500)],
                  mine=[bid(960, "v02", "MAL-08", 3, maker=TEAM, created=380, exp=500)])
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                co.INTEL_STATE["intel"] = None
                led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": []}
                cands, pl, _ = co.candidates(s, led, cargs(), neg.Journal(d))
            finally:
                co.DATA = saved
        acc = [c for c in cands if c.get("page_campaign") == "MAL-08" and c["type"] == "accept"]
        self.assertEqual((acc[0]["offer"], acc[0]["blockers"]), (950, []))
        self.assertTrue([c for c in cands if c["type"] == "cancel" and c.get("offer") == 960 and not c["blockers"]],
                        "la puja pasiva de MAL-08 sobra")

    def test_07_final_card_secure_close_and_top_priority(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]), [ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=500)])
        p = self.plan(s, "MAL-10")
        c = p["best"]["candidate"]
        self.assertEqual((c["type"], c["price"]), ("accept", 28), "la última carta se compra ya, sin regatear")
        self.assertGreater(c["score"], neg.dealer_accept_priority("SECURE", 0, None), "por encima de un cierre de vendedor")
        self.assertLess(c["score"], 10 ** 6, "nunca por encima de la seguridad")
        self.assertIn("*** FINAL MALASAÑA PIECE ***", pc.report_block(pc.campaign_state(s, val_of(s), "MAL"), [p], "x"))
        expensive = snapm(inventory(["MAL-08", "MAL-09"]), [ask(971, "v02", 89, "MAL-10", 500, maker="t07", exp=500)])
        self.assertFalse([r for r in self.plan(expensive, "MAL-10")["routes"] if r.get("candidate")],
                         "por encima del techo económico: nunca")

    def test_13_alias_offer_never_attributed_and_collector_owner_discounted(self):
        listed = ask(930, "rastro", 66, "MAL-08", 26, maker="t01", created=418)  # el feed expone el maker real
        feed = [{"id": 1, "tick": 418, "type": "offer.listed", "actor": "t01", "payload": {"venue": "rastro", "offer": listed}}]
        self.intel.ingest({**snapm([]), "feed": {"events": feed}})
        self.assertGreater(self.intel.p_owns("t01", "MAL-08")["p"], 0, "solo con el feed parecería suyo")
        aliased = dict(listed, maker="m8314b1c5")  # el tablón lo muestra con alias
        self.intel.ingest(snapm([], [aliased], tick=419))
        self.assertEqual(self.intel.p_owns("t01", "MAL-08")["p"], 0.0, "nunca se desanonimiza un alias")
        s = snapm(inventory())
        self.assertEqual(pc.owners_ranked("MAL-08", self.intel, s, []), [])
        self.assertLess(pc.p_response({"response_history": 0, "owner_wants_target": 0.9}),
                        pc.p_response({"response_history": 0, "owner_wants_target": 0.0}))


class Integration(unittest.TestCase):
    def run_cands(self, s, **kw):
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                co.INTEL_STATE["intel"] = None
                led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": []}
                cands, pl, _ = co.candidates(s, led, cargs(**kw), neg.Journal(d))
                pg.apply_guard(cands, s, co.committed_ids(s, led))
            finally:
                co.DATA = saved
        return cands, pl

    def test_08_acquired_target_cancels_redundant_offers_and_no_second_copy(self):
        mine = [bid(980, "v02", "MAL-08", 5, maker=TEAM, created=380, exp=500),
                offer(981, "v02", {"assets": [A(310, "MAL-02")]}, {"types": ["card:MAL-08"]}, maker=TEAM, created=380)]
        s = snapm(inventory(["MAL-08"]), [ask(982, "v02", 90, "MAL-08", 4, maker="t11", exp=500)], mine=mine)
        cands, pl = self.run_cands(s)
        gone = {c["offer"] for c in cands if c["type"] == "cancel" and not c["blockers"]}
        self.assertTrue({980, 981} <= gone)
        self.assertFalse([c for c in cands if c.get("page_campaign") == "MAL-08"], "MAL-08 ya está: no se persigue")
        self.assertNotIn("MAL-08", pl["page_campaign"]["state"]["missing"])

    def test_09_insufficient_cash_invokes_rebalance(self):
        mine = [bid(990, "v02", "LAT-01", 25, maker=TEAM, created=300, exp=500)]  # puja pasiva floja (ya tenemos LAT-01)
        s = snapm(inventory(), [ask(991, "v02", 91, "MAL-08", 7, maker="t11", exp=500)], mine=mine, cash=128)
        cands, pl = self.run_cands(s)
        acc = next(c for c in cands if c.get("page_campaign") == "MAL-08" and c["type"] == "accept")
        self.assertTrue(any(b.startswith("capital") for b in acc["blockers"]), "espera a liberar efectivo")
        self.assertTrue([c for c in cands if c.get("offer") == 990 and c["type"] == "cancel" and not c["blockers"]])

    def test_10_focus_blocks_unrelated_public_bids(self):
        s = snapm(inventory(), [ask(995, "v02", 92, "LAT-01", 99, maker="t11", exp=500)])  # mercado de LAT irrelevante
        cands, _ = self.run_cands(s)
        for c in cands:
            if c["type"] == "bid" and c.get("module") == "mercado" and not c.get("to"):
                self.assertTrue(c["blockers"], "con la campaña activa no se dispersa efectivo en pujas públicas")

    def test_11_campaign_terminates_and_page_is_protected(self):
        s = snapm(inventory(TARGETS))
        cands, pl = self.run_cands(s)
        camp = pl["page_campaign"]
        self.assertTrue(camp["state"]["complete"])
        self.assertIn("COMPLETA", camp["next_action"])
        self.assertFalse([c for c in cands if c.get("page_campaign")])
        counts = tr.counts_of(s["me"]["assets"])
        self.assertTrue(set(MAL) <= pg.protected_page_cards(counts, s["catalog"]))
        self.assertTrue(pg.guard_candidate({"type": "list", "ref": "MAL-09", "asset": 201, "price": 99}, s))
        self.assertEqual(pg.guard_candidate({"type": "list", "ref": "MAL-02", "asset": 310, "price": 9}, s), [],
                         "el duplicado sí se puede vender")

    def test_12_dealer_conversation_for_campaign_card_is_secure(self):
        t = {"id": 7, "kind": "persona", "with": "abuela", "status": "open", "created_tick": 395,
             "topic": {"buy": {"card": "MAL-08"}}, "standing_offers": [], "messages": [
                 {"tick": 396, "sender": "abuela", "offer": {"id": 1, "maker": "abuela", "to": TEAM, "thread": 7,
                  "status": "cancelled", "final": False, "give": {"types": ["card:MAL-08"]}, "want": {"cash": 9}}},
                 {"tick": 396, "sender": TEAM, "offer": {"id": 2, "maker": TEAM, "status": "cancelled",
                  "give": {"cash": 6}, "want": {"types": ["card:MAL-08"]}}},
                 {"tick": 397, "sender": "abuela", "offer": {"id": 3, "maker": "abuela", "to": TEAM, "thread": 7,
                  "status": "open", "final": False, "give": {"types": ["card:MAL-08"]}, "want": {"cash": 8},
                  "expires_tick": 405}}]}
        s = snapm(inventory())
        s["threads"]["open"] = [t]
        deals = [{"kind": "outcome", "dealer": "abuela", "item": "card:X", "thread": 100 + i, "status": "deal",
                  "opening": 20, "close_price": 15, "settled": True, "context": "normal"} for i in range(3)]
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                j = neg.Journal(d)
                for x in deals:
                    j.append("outcome", {k: v for k, v in x.items() if k != "kind"})
                led = {"actions": [], "spent_confirmed": 0, "threads": [7], "blocked": {}, "class_tick": {}, "expiry_obs": []}
                cands, _, _ = co.candidates(s, led, cargs(), j)
            finally:
                co.DATA = saved
        acc = [c for c in cands if c["type"] == "dealer_accept"]
        self.assertEqual((len(acc), acc[0]["price"], acc[0]["ladder_mode"]), (1, 8, "SECURE"),
                         "la abuela está en OPTIMIZE (3 tratos), pero una carta de la campaña se cierra en SECURE")

class Focus(Integration):
    def test_14_unrelated_small_purchase_blocked_while_campaign_active(self):
        s = snapm(inventory(), [ask(996, "v02", 93, "LAT-02", 1, maker="t11", exp=500)])  # duplicado LAT barato
        cands, _ = self.run_cands(s)
        buys = [c for c in cands if c["type"] == "accept" and c.get("offer") == 996]
        self.assertTrue(all(any("compra ajena" in b for b in c["blockers"]) for c in buys))

class DealerMemory(Integration):
    def test_15_no_reopen_dealer_when_ceiling_below_its_known_opening(self):
        s = snapm(inventory())
        s["dealers"] = {"chato": {"open_to_all": True, "menu": {"sells": [{"rarity": "rare", "list_price": 77}]}}}
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {},
                       "expiry_obs": [], "dealer_openings": {"chato|card:MAL-09": {"opening": 97, "tick": 607}}}
                cands, _, _ = co.candidates(s, led, cargs(), neg.Journal(d))
            finally:
                co.DATA = saved
        mal9 = [c for c in cands if c["type"] == "dealer_open" and c["ref"] == "card:MAL-09"]
        self.assertTrue(mal9 and any("abrió a 97" in b for b in mal9[0]["blockers"]))


if __name__ == "__main__":
    unittest.main()
