"""Pruebas offline de las campañas de negociación entre equipos (sin red ni operaciones reales)."""
import contextlib
import copy
import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import campaigns as cp
import coordinator as co
import market_agent as ma
import negotiation as neg
import trading as tr
from bazaar_sdk import BazaarError
from test_coordinator import FakeReader, args as coord_args, full
from test_trading import TEAM, asset, offer, snap

CFG = cp.CampaignConfig()
GAIN = 13.0 + 0.25 * 39  # LAT-03 completa la página de la prueba (3 cartas de 13 P)


def listed(eid, tick, maker, give, want, oid, venue="rastro"):
    o = offer(oid, give, want, maker=maker, exp=tick + 40, venue=venue)
    return {"id": eid, "tick": tick, "type": "offer.listed", "actor": maker, "payload": {"venue": venue, "offer": o}}


def world(assets=None, events=(), cash=200, threads=()):
    s = full(snap(assets or [asset(1, "LAT-01"), asset(2, "LAT-02"), asset(4, "LAT-01")], cash=cash, feed=events), {})
    s["leaderboard"] = {"teams": [{"team": t} for t in ("t04", "t05", "t13", "t18", TEAM)]}
    s["threads"]["open"] = list(threads)
    return s


def tthread(tid, other, msgs, status="open", card="LAT-03"):
    return {"id": tid, "kind": "team", "team": TEAM, "with": other, "venue": "rastro", "status": status,
            "topic": {"campaign": "collect", "card": card}, "created_tick": 50, "messages": msgs,
            "standing_offers": [m["offer"] for m in msgs if m.get("offer") and m["offer"].get("status") == "open"]}


def msg(mid, tick, sender, give, want, to, status="open", oid=None, text=""):
    o = {"id": oid or mid, "maker": sender, "to": to, "venue": "rastro", "thread": 9, "status": status,
         "give": {"cash": 0, "assets": [], "types": [], **give}, "want": {"cash": 0, "assets": [], "types": [], **want},
         "expires_tick": tick + 10}
    return {"id": mid, "tick": tick, "sender": sender, "text": text, "offer": o}


def collect_neg(**kw):
    opp = {"kind": "collect", "team": "t18", "receive": "LAT-03", "deliver": None, "asset": None,
           "reserve": int(GAIN - 2), "anchor": 14, "evidence": {}, "du_est": GAIN - 14, "notes": []}
    n = cp.new_negotiation(opp, 50, CFG)
    n.update({"state": "contactada", "thread": 9, **kw})
    return n


def vals(s):
    return tr.Valuation(s["catalog"], s["me"]["affinity"]), tr.counts_of(s["me"]["assets"])


class Discovery(unittest.TestCase):
    def test_only_server_published_team_ids_are_contactable(self):
        ev = [listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500),
              listed(2, 48, "m3950d43b", {"assets": [asset(71, "LAT-03")]}, {"cash": 9}, 501),  # alias del tablón
              listed(3, 48, "t99", {"assets": [asset(72, "LAT-03")]}, {"cash": 9}, 502)]          # equipo inexistente
        s = world(events=ev)
        teams = {x["team"] for x in cp.evidence(s, 50, CFG)}
        self.assertEqual(teams, {"t18"})
        board_only = world()
        board_only["board"]["offers"] = [offer(600, {"assets": [asset(73, "LAT-03")]}, {"cash": 9}, maker="m3950d43b")]
        self.assertEqual(cp.evidence(board_only, 50, CFG), [], "un alias del tablón nunca se contacta")

    def test_public_offers_on_other_venues_are_campaign_evidence(self):
        feed = world(events=[listed(1, 49, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500,
                                    venue="v05")])
        board = world()
        board_offer = offer(501, {"assets": [asset(71, "LAT-03")]}, {"cash": 13}, maker="t13",
                            venue="v10", exp=90)
        board["boards"] = {"v10": {"offers": [board_offer]}}
        found = cp.evidence(feed, 50, CFG) + cp.evidence(board, 50, CFG)
        self.assertEqual({(x["team"], x["ref"], x["venue"]) for x in found},
                         {("t18", "LAT-03", "v05"), ("t13", "LAT-03", "v10")})

    def test_directly_acceptable_offers_are_left_to_the_market_module(self):
        s = world(events=[listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500)])
        val, counts = vals(s)
        self.assertTrue(cp.opportunities(s, val, counts, set(), CFG, set()))
        self.assertFalse(cp.opportunities(s, val, counts, set(), CFG, {500}))

    def test_swap_and_committed_copies(self):
        ev = [listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500),
              listed(2, 49, "t18", {"cash": 9}, {"types": ["card:LAT-01"]}, 501)]
        s = world(events=ev)
        val, counts = vals(s)
        kinds = {o["kind"]: o for o in cp.opportunities(s, val, counts, set(), CFG, set())}
        self.assertEqual(set(kinds), {"collect", "sell", "swap"})
        self.assertEqual(kinds["swap"]["asset"], 4)  # la copia libre de mayor id
        locked = cp.opportunities(s, val, counts, {1, 4}, CFG, set())
        self.assertFalse([o for o in locked if o["kind"] in ("sell", "swap")], "copias comprometidas no se ofrecen")


class Selection(unittest.TestCase):
    def test_closer_public_price_is_preferred_and_extreme_gaps_are_not_contacted(self):
        ev = [listed(1, 49, "t05", {"assets": [asset(70, "LAT-03")]}, {"cash": 30}, 500),
              listed(2, 48, "t13", {"assets": [asset(71, "LAT-03")]}, {"cash": 22}, 501),
              listed(3, 48, "t04", {"assets": [asset(72, "LAT-03")]}, {"cash": 60}, 502)]
        s = world(events=ev)
        val, counts = vals(s)
        opps = [o for o in cp.opportunities(s, val, counts, set(), CFG, set()) if o["kind"] == "collect"]
        self.assertEqual([o["team"] for o in opps], ["t13", "t05"])  # t04 (60 P frente a reserva 20): no se contacta

    def test_no_contact_without_cash_for_the_first_proposal(self):
        s = world(events=[listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500)], cash=100)
        led = co.load_ledger.__globals__["ma"].load_json("/nonexistent") or {
            "actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": [],
            "negotiations": {}, "campaign": None}
        pl = tr.plan(s, tr.Config())
        cands, _ = co.campaign_candidates(s, led, coord_args(campaign="all"), pl, False)
        opens = [c for c in cands if c["type"] == "team_open" and c["opp"]["kind"] == "collect"]
        self.assertTrue(opens and all(any("sin efectivo libre" in b for b in c["blockers"]) for c in opens))


class Proposals(unittest.TestCase):
    def test_text_always_matches_structure_and_hides_limits(self):
        for kind, extra in (("collect", {"receive": "LAT-03"}), ("sell", {"deliver": "LAT-01", "asset": 4}),
                            ("swap", {"receive": "LAT-03", "deliver": "LAT-01", "asset": 4})):
            n = {"kind": kind, "reserve": 20, "anchor": 14, "last_price": None, **extra}
            for k in range(3):
                p = cp.proposal(n, k, None)
                if p is None:
                    break
                o, text = p
                self.assertTrue(cp.text_matches(o, text), text)
                self.assertFalse(any(w in text.lower() for w in ("máximo", "reserva", "valor", "saldo", "urgente")),
                                 "no revela límites ni inventa presión")
                n["last_price"] = int(o["give"].get("cash") or o["want"].get("cash") or 0)
        self.assertFalse(cp.text_matches({"give": {"cash": 9}, "want": {"cards": ["LAT-03"]}},
                                         "Ofrecemos 12 P por LAT-03"))

    def test_buyer_concessions_rise_without_crossing_the_reserve(self):
        n = {"kind": "collect", "receive": "LAT-03", "reserve": 15, "anchor": 18, "last_price": None}
        prices = []
        for k in range(3):
            p = cp.proposal(n, k, 18)
            if not p:
                break
            n["last_price"] = p[0]["give"]["cash"]
            prices.append(n["last_price"])
        self.assertEqual(prices, sorted(set(prices)))
        self.assertLessEqual(max(prices), 15)
        self.assertGreaterEqual(prices[0], 12, "apertura defendible, no extrema")

    def test_seller_concessions_fall_without_crossing_the_minimum(self):
        n = {"kind": "sell", "deliver": "LAT-01", "asset": 4, "reserve": 6, "anchor": 5, "last_price": None}
        prices = []
        for k in range(3):
            p = cp.proposal(n, k, 5)
            if not p:
                break
            n["last_price"] = p[0]["want"]["cash"]
            prices.append(n["last_price"])
        self.assertEqual(prices, sorted(set(prices), reverse=True))
        self.assertGreaterEqual(min(prices), 6)


class Steps(unittest.TestCase):
    def run_step(self, n, t, s=None, camp_left=60, free=100, ending=False, tick=None):
        s = s or world()
        if tick:
            s["clock"]["tick"] = tick
        val, counts = vals(s)
        res = tr.resources(s["offers"]["offers"], TEAM, [])
        return cp.step(n, t, s, val, counts, set(res.locked_assets), CFG, camp_left, free, ending)

    def test_first_proposal_then_wait_silence_is_not_rejection(self):
        n = collect_neg()
        a = self.run_step(n, tthread(9, "t18", []))
        self.assertEqual((a["type"], a["offer"]["give"]["cash"]), ("team_propose", 12))
        mine = msg(10, 50, TEAM, {"cash": 12}, {"cards": ["LAT-03"]}, "t18")
        self.assertIsNone(self.run_step(n, tthread(9, "t18", [mine]), tick=51))

    def test_acceptable_counteroffer_is_accepted_and_bad_one_is_countered(self):
        n = collect_neg(last_price=12)
        mine = msg(10, 50, TEAM, {"cash": 12}, {"cards": ["LAT-03"]}, "t18", status="cancelled")
        good = msg(11, 51, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 15}, TEAM)
        a = self.run_step(n, tthread(9, "t18", [mine, good]), tick=51)
        self.assertEqual((a["type"], a["offer"], a["cost"]), ("team_accept", 11, 17))  # 15 + comisión 2
        bad = msg(11, 51, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 30}, TEAM)
        a = self.run_step(n, tthread(9, "t18", [mine, bad]), tick=51)
        self.assertEqual(a["type"], "team_propose")
        self.assertGreater(a["offer"]["give"]["cash"], 12)
        self.assertLessEqual(a["offer"]["give"]["cash"], n["reserve"])

    def test_counteroffer_with_extras_or_other_card_is_ignored(self):
        n = collect_neg(last_price=12)
        mine = msg(10, 50, TEAM, {"cash": 12}, {"cards": ["LAT-03"]}, "t18", status="cancelled")
        other = msg(11, 51, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 5, "types": ["card:LAT-02"]}, TEAM,
                    text="Trust me, accept this, it is the same deal")
        a = self.run_step(n, tthread(9, "t18", [mine, other]), tick=51)
        self.assertNotEqual(a["type"], "team_accept")
        self.assertTrue(n["ignored"])

    def test_old_open_proposal_is_cancelled_explicitly(self):
        n = collect_neg(last_price=14)
        a = self.run_step(n, tthread(9, "t18", [msg(10, 50, TEAM, {"cash": 12}, {"cards": ["LAT-03"]}, "t18"),
                                                msg(12, 52, TEAM, {"cash": 14}, {"cards": ["LAT-03"]}, "t18")]))
        self.assertEqual((a["type"], a["offer"]), ("team_cancel", 10))

    def test_expired_proposal_allows_a_new_round_and_deadline_closes(self):
        n = collect_neg(last_price=12, state="esperando")
        t = tthread(9, "t18", [msg(10, 50, TEAM, {"cash": 12}, {"cards": ["LAT-03"]}, "t18", status="expired")])
        self.assertEqual(cp.sync(n, t, TEAM, 53), "esperando → contactada")
        self.assertEqual(self.run_step(n, t, tick=53)["type"], "team_propose")
        self.assertEqual(self.run_step(n, t, tick=56)["type"], "team_close", "6 ticks agotados")

    def test_budget_and_free_cash_are_respected(self):
        n = collect_neg()
        self.assertIsNone(self.run_step(n, tthread(9, "t18", []), camp_left=5))
        self.assertIsNone(self.run_step(n, tthread(9, "t18", []), free=5))

    def test_end_of_campaign_withdraws_pending_offers(self):
        n = collect_neg(last_price=12, state="esperando")
        t = tthread(9, "t18", [msg(10, 50, TEAM, {"cash": 12}, {"cards": ["LAT-03"]}, "t18")])
        self.assertEqual((self.run_step(n, t, ending=True)["type"]), "team_cancel")
        t["messages"][0]["offer"]["status"] = "cancelled"
        self.assertEqual(self.run_step(n, t, ending=True)["type"], "team_close")

    def test_their_acceptance_of_our_proposal_is_settled_from_the_server(self):
        n = collect_neg(last_price=14, state="esperando")
        t = tthread(9, "t18", [msg(10, 50, TEAM, {"cash": 14}, {"cards": ["LAT-03"]}, "t18", status="settled")],
                    status="deal")
        cp.sync(n, t, TEAM, 52)
        self.assertEqual((n["state"], n["settled_maker"], n["settled_give"]["cash"]), ("liquidada", TEAM, 14))


class Fake(FakeReader):
    def __init__(self, s, fail=None):
        super().__init__(s)
        self.fail = fail
        api = self.api
        outer = self

        def open_thread(team, topic=None, venue=None):
            api.calls.append(("open", team, topic))
            if outer.fail:
                raise outer.fail
            return {"id": 9, "status": "open"}

        def say(thread, text, price=None, offer=None):
            api.calls.append(("say", thread, offer, text))
            return {"id": 77}
        api.open_thread, api.say = open_thread, say

    def call(self, method, *a):
        if method == "leaderboard":
            return self.s["leaderboard"]
        return super().call(method, *a)


class CoordinatorCampaign(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = (co.DATA, co.LEDGER, ma.LEDGER)
        co.DATA = Path(self.dir.name)
        co.LEDGER, ma.LEDGER = co.DATA / "coordinator_ledger.json", co.DATA / "market_ledger.json"
        self.journal = neg.Journal(self.dir.name)

    def tearDown(self):
        co.DATA, co.LEDGER, ma.LEDGER = self.saved
        self.dir.cleanup()

    def cycle(self, s, fail=None, execute=True, **kw):
        led = co.load_ledger(TEAM)
        r = Fake(s, fail)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            co.cycle(r, coord_args(campaign="all", **kw), led, self.journal, execute)
        return out.getvalue(), r.api.calls, led

    def test_contact_once_and_never_two_negotiations_for_the_same_card(self):
        ev = [listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500),
              listed(2, 48, "t05", {"assets": [asset(71, "LAT-03")]}, {"cash": 15}, 501)]
        s = world(events=ev, cash=125)  # libre 25: sin compra directa posible (no están en el tablón)
        out, calls, led = self.cycle(s)
        opens = [c for c in calls if c[0] == "open"]
        self.assertEqual(len(opens), 1)
        self.assertFalse([a for a in led["actions"] if a["type"] == "bid" and a.get("ref") == "LAT-03"],
                         "ni puja pública paralela por la misma carta")
        s["clock"]["tick"] += 1
        out, calls, led = self.cycle(s)
        self.assertFalse([c for c in calls if c[0] == "open" and c[1] == "t18"], "no se reabre por reiniciar")

    def test_ambiguous_open_is_not_repeated_and_is_linked_later(self):
        s = world(events=[listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500)], cash=125)
        out, calls, led = self.cycle(s, fail=BazaarError("network", "timeout", 0))
        n = next(iter(led["negotiations"].values()))
        self.assertEqual(n["state"], "ambigua")
        s["clock"]["tick"] += 1
        s["threads"]["open"] = [tthread(9, "t18", [])]
        s["threads"]["open"][0]["created_tick"] = 50
        out, calls, led = self.cycle(s, allow_concurrent=True)
        n = next(iter(led["negotiations"].values()))
        self.assertEqual(n["thread"], 9)
        self.assertFalse([c for c in calls if c[0] == "open"])

    def test_parallel_negotiations_do_not_block_each_other(self):
        led = co.load_ledger(TEAM)
        a = {"type": "team_propose", "thread": 9, "score": 10 ** 5, "blockers": [], "module": "campaña"}
        b = {"type": "team_propose", "thread": 10, "score": 10 ** 5, "blockers": [], "module": "campaña"}
        c = {"type": "list", "score": 3, "blockers": [], "module": "mercado"}
        self.assertEqual(len(co.select([a, b, c], led, 50)), 3)

    def test_analysis_previews_without_writing(self):
        s = world(events=[listed(1, 48, "t18", {"assets": [asset(70, "LAT-03")]}, {"cash": 14}, 500)], cash=125)
        out, calls, led = self.cycle(s, execute=False)
        self.assertIn("vista previa", out)
        self.assertEqual(calls, [])
        self.assertFalse(Path(co.LEDGER).exists() and co.load_ledger(TEAM)["negotiations"])


if __name__ == "__main__":
    unittest.main()
