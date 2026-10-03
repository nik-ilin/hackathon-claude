"""Pruebas locales (sin red ni operaciones reales):  python3 -m unittest test_negotiation -v"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

import negotiation as neg
import sim
from sim import DEALER, ITEM, TEAM, SimSeller

CFG = neg.Config()


def play(seller_kw, ceiling, cfg=CFG, history=()):
    s = SimSeller(**seller_kw)
    return sim.run(s, sim.adaptive(ceiling, cfg, history)), s


def thread(messages, status="open"):
    return {"id": 12, "team": TEAM, "with": DEALER, "topic": {"buy": {"card": "LAV-03"}}, "status": status,
            "messages": messages}


def her(mid, tick, price, status="open", final=False):
    return {"id": mid, "tick": tick, "sender": DEALER, "offer": {
        "id": mid, "maker": DEALER, "to": TEAM, "thread": 12, "status": status, "final": final,
        "give": {"cash": 0, "assets": [], "types": [ITEM]}, "want": {"cash": price, "assets": [], "types": []},
        "expires_tick": tick + 2}}


def ours(mid, tick, price):
    return {"id": mid, "tick": tick, "sender": TEAM, "price": price}


class Scenarios(unittest.TestCase):
    def assert_sane(self, r, ceiling):
        self.assertEqual(r["repeats"], 0, "nunca repite ni baja su oferta")
        self.assertTrue(all(b <= ceiling for b in r["bids"]), "nunca ofrece por encima del máximo")
        if r["status"] == "deal":
            self.assertLessEqual(r["price"], ceiling)

    def test_progressive_seller(self):
        r, s = play(dict(opening=17, floor=10, patience=12, reciprocity=0.8), 16)
        self.assert_sane(r, 16)
        self.assertEqual(r["status"], "deal")
        self.assertLess(r["price"], 17)
        self.assertEqual(r["bids"], sorted(r["bids"]), "concesiones ascendentes")

    def test_seller_stops_conceding(self):
        r, _ = play(dict(opening=17, floor=8, patience=20, reciprocity=0.8, stop_after=3), 16)
        self.assert_sane(r, 16)
        self.assertEqual(r["status"], "deal")
        self.assertLessEqual(r["turns"], CFG.turn_budget)

    def test_early_final_within_ceiling_is_accepted(self):
        r, _ = play(dict(opening=17, floor=10, patience=12, reciprocity=0.8, final_at=1), 16)
        self.assertEqual(r["status"], "deal")
        self.assertEqual(r["turns"], 1, "no regatea tras una final")

    def test_early_final_above_ceiling_is_abandoned(self):
        r, _ = play(dict(opening=20, floor=17, patience=12, reciprocity=0.5, final_at=1), 14)
        self.assertEqual(r["status"], "abandoned")
        self.assert_sane(r, 14)

    def test_secret_limit_above_ceiling(self):
        r, _ = play(dict(opening=20, floor=16, patience=30, reciprocity=0.7), 13)
        self.assertEqual(r["status"], "abandoned")
        self.assert_sane(r, 13)

    def test_insufficient_cash(self):
        cfg = neg.Config(budget=60, reserve=100)
        ceiling, parts = neg.price_ceiling(cfg, cash=105, value=40)
        self.assertEqual(ceiling, 5)
        self.assertEqual(parts["efectivo - reserva"], 5)
        r, _ = play(dict(opening=17, floor=10, patience=12, reciprocity=0.8), ceiling)
        self.assertNotEqual(r["status"], "deal")
        self.assert_sane(r, ceiling)
        self.assertLess(neg.price_ceiling(cfg, cash=90, value=40)[0], 1)
        st = neg.state_from_thread(thread([her(1, 1, 17)]), DEALER, 2, cfg)
        d = neg.decide(st, cfg, -10, neg.estimate([], st, neg.metrics(st, cfg), cfg))
        self.assertEqual(d.action, "abandon")

    def test_opening_price_is_not_accepted_automatically(self):
        st = neg.state_from_thread(thread([her(1, 1, 17)]), DEALER, 2, CFG)
        d = neg.decide(st, CFG, 40, neg.estimate([], st, neg.metrics(st, CFG), CFG))
        self.assertEqual(d.action, "counter")
        self.assertLess(d.price, 17)


class Estimation(unittest.TestCase):
    def test_sparse_history_is_declared_weak(self):
        st = neg.state_from_thread(thread([her(1, 1, 20)]), DEALER, 2, CFG)
        e = neg.estimate([{"status": "deal", "opening": 20, "close_price": 14}], st, neg.metrics(st, CFG), CFG)
        self.assertEqual(e.strength, "supuesto")
        self.assertTrue(any("SUPUESTO INICIAL, no aprendido" in x for x in e.evidence))
        self.assertEqual((e.low, e.high), (round(CFG.prior_low * 20, 1), round(CFG.prior_high * 20, 1)))

    def test_enough_history_uses_comparables_and_reports_failures(self):
        st = neg.state_from_thread(thread([her(1, 1, 20)]), DEALER, 2, CFG)
        hist = [{"status": "deal", "opening": 20, "close_price": p} for p in (12, 13, 15)] + [{"status": "walked"}]
        e = neg.estimate(hist, st, neg.metrics(st, CFG), CFG)
        self.assertEqual((e.low, e.high, e.n, e.strength), (12.0, 15.0, 3, "limitada"))
        self.assertTrue(any("sin compra" in x for x in e.evidence))

    def test_journal_excludes_quota_closures_and_other_items(self):
        with tempfile.TemporaryDirectory() as d:
            j = neg.Journal(d)
            j.append("outcome", {"dealer": DEALER, "item": ITEM, "status": "deal", "context": "normal", "settled": True})
            j.append("outcome", {"dealer": DEALER, "item": ITEM, "status": "closed", "context": "quota"})
            j.append("outcome", {"dealer": DEALER, "item": "pack:sobre_barrio", "status": "deal", "context": "normal"})
            self.assertEqual(len(j.outcomes(DEALER, ITEM)), 1)
            self.assertTrue(j.purchased(DEALER, ITEM))
            self.assertFalse(j.purchased(DEALER, "pack:sobre_barrio"))


class Restart(unittest.TestCase):
    MSGS = [her(1, 1, 17, "expired"), ours(2, 2, 6), her(3, 2, 16, "expired"), ours(4, 3, 8), her(5, 3, 14)]

    def test_resumes_open_conversation_without_repeating(self):
        st = neg.state_from_thread(thread(self.MSGS), DEALER, 4, CFG)
        self.assertEqual((st.opening, st.last_ours, st.turns, st.live.price), (17, 8, 2, 14))
        m = neg.metrics(st, CFG)
        self.assertEqual((m.gap, m.her_drop, m.our_raise), (6, 2, 2))
        d = neg.decide(st, CFG, 16, neg.estimate([], st, m, CFG), m)
        self.assertEqual(d.action, "counter")
        self.assertGreater(d.price, 8)
        self.assertLess(d.price, 14)

    def test_waits_for_reply_then_times_out(self):
        msgs = self.MSGS + [ours(6, 4, 10)]
        st = neg.state_from_thread(thread(msgs), DEALER, 5, CFG)
        self.assertEqual(neg.decide(st, CFG, 16, neg.estimate([], st, neg.metrics(st, CFG), CFG)).action, "wait")
        st = neg.state_from_thread(thread(msgs), DEALER, 4 + CFG.reply_timeout_ticks, CFG)
        self.assertFalse(st.awaiting_reply)
        self.assertEqual(neg.metrics(st, CFG).stalled, 1)

    def test_stale_thread_with_expired_opening(self):  # el hilo #12 real: solo su apertura, ya caducada
        st = neg.state_from_thread(thread([her(10, 1, 17, "expired")]), DEALER, 40, CFG)
        d = neg.decide(st, CFG, 16, neg.estimate([], st, neg.metrics(st, CFG), CFG))
        self.assertEqual(d.action, "counter")
        self.assertEqual(d.price, 12)

    def test_final_is_never_countered(self):
        st = neg.state_from_thread(thread(self.MSGS[:-1] + [her(5, 3, 15, final=True)]), DEALER, 4, CFG)
        self.assertEqual(neg.decide(st, CFG, 16, neg.estimate([], st, neg.metrics(st, CFG), CFG)).action, "accept")
        self.assertEqual(neg.decide(st, CFG, 14, neg.estimate([], st, neg.metrics(st, CFG), CFG)).action, "abandon")


class Settlement(unittest.TestCase):
    PENDING = {"thread": 12, "offer_id": 5, "item": ITEM, "price": 13, "cash_before": 400, "holdings_before": 0}

    def test_pending_until_delivered(self):
        t = thread([her(5, 3, 13, "accepted")])
        self.assertEqual(neg.settlement_status(self.PENDING, t, 0, 400)[0], "waiting")
        t["status"] = "deal"
        self.assertEqual(neg.settlement_status(self.PENDING, t, 0, 400)[0], "waiting")
        self.assertEqual(neg.settlement_status(self.PENDING, t, 1, 387), ("settled", ""))

    def test_settled_price_comes_from_the_server_not_our_last_offer(self):  # hilo #123 real: 12, 13 y liquidó a 17
        msgs = [her(1, 70, 17, "cancelled"), ours(2, 71, 12), her(3, 72, 17, "cancelled"),
                {"id": 4, "tick": 73, "sender": TEAM, "offer": {"maker": TEAM, "status": "cancelled", "give": {"cash": 13}}},
                her(5, 74, 17, "settled")]
        self.assertEqual(neg.settled_price(thread(msgs, "deal"), DEALER), 17)
        ours_settled = {"id": 6, "tick": 75, "sender": TEAM,
                        "offer": {"maker": TEAM, "status": "settled", "give": {"cash": 15}, "want": {"cash": 0}}}
        self.assertEqual(neg.settled_price(thread([her(1, 1, 30, "expired"), ours_settled], "deal"), DEALER), 15)

    def test_failed_when_offer_gone(self):
        t = thread([her(5, 3, 13, "expired")], status="walked")
        self.assertEqual(neg.settlement_status(self.PENDING, t, 0, 400)[0], "failed")

    def test_pending_file_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            neg.Journal(d).set_pending(self.PENDING)
            self.assertEqual(neg.Journal(d).pending(), self.PENDING)
            neg.Journal(d).clear_pending()
            self.assertIsNone(neg.Journal(d).pending())


class Validation(unittest.TestCase):
    def check(self, offer, **kw):
        args = dict(dealer=DEALER, team=TEAM, thread_id=12, item=ITEM, ceiling=16, cash=400, now_tick=3)
        args.update(kw)
        return neg.validate_offer(offer, **args)

    def test_good_offer(self):
        self.assertEqual(self.check(her(5, 3, 13)["offer"]), [])

    def test_bad_structures(self):
        o = her(5, 3, 13)["offer"]
        self.assertTrue(self.check({**o, "maker": "t07"}))
        self.assertTrue(self.check({**o, "want": {"cash": 13, "assets": [99], "types": []}}))
        self.assertTrue(self.check({**o, "give": {"cash": 0, "assets": [], "types": ["card:LAV-04"]}}))
        self.assertTrue(self.check({**o, "status": "expired"}))
        self.assertTrue(self.check(o, now_tick=9))
        self.assertTrue(self.check(her(5, 3, 20)["offer"]))
        self.assertTrue(self.check(o, cash=10))

    def test_specific_asset_resolved_by_its_card(self):
        o = {**her(5, 3, 13)["offer"], "give": {"cash": 0, "assets": [777], "types": []}}
        self.assertEqual(self.check(o, resolve_asset=lambda a: ITEM), [])
        self.assertTrue(self.check(o, resolve_asset=lambda a: "card:LAV-09"))
        self.assertTrue(self.check(o))



# ------------------------------------------------------------------ modo primera compra: política

FAST = neg.FastConfig()


def play_fast(seller_kw, ceiling, total=10 ** 6):
    return sim.run(SimSeller(**seller_kw), sim.fast(ceiling, FAST, total))


def fast_decide(msgs, now, ceiling, conv_left=6, total_left=12):
    st = neg.state_from_thread(thread(msgs), DEALER, now, CFG)
    return neg.decide_fast(st, FAST, ceiling, conv_left, total_left)


class FastPolicy(unittest.TestCase):
    HOLD = dict(opening=17, floor=17, patience=8, reciprocity=0.0, stop_after=0)

    def assert_short(self, r, opening, ceiling):
        self.assertLessEqual(r["turns"], FAST.max_counteroffers, "como mucho dos contraofertas")
        self.assertEqual(r["repeats"], 0)
        self.assertTrue(all(b <= ceiling for b in r["bids"]))
        if r["bids"]:
            self.assertGreaterEqual(r["bids"][0], min(round(0.85 * opening), ceiling), "sin apertura muy baja")

    def test_holds_opening_but_profitable_accepts_within_budget(self):
        r = play_fast(self.HOLD, 18)
        self.assertEqual((r["status"], r["price"], r["bids"]), ("deal", 17, [14, 16]))
        self.assert_short(r, 17, 18)
        self.assertLessEqual(r["ticks"], FAST.max_negotiation_ticks)

    def test_holds_price_above_ceiling_abandons_without_overpaying(self):
        r = play_fast(self.HOLD, 15)
        self.assertEqual(r["status"], "abandoned")
        self.assert_short(r, 17, 15)

    def test_progressive_seller_accepts_first_concession(self):
        r = play_fast(dict(opening=17, floor=10, patience=12, reciprocity=0.8), 16)
        self.assertEqual((r["status"], r["bids"]), ("deal", [14]))
        self.assertLess(r["price"], 17)

    def test_example_sequence(self):  # pide 17 · ofrecemos 14 · si baja a 16, aceptamos
        msgs = [her(1, 1, 17, "expired"), ours(2, 1, 14), her(3, 2, 16)]
        d = fast_decide(msgs, 2, 20)
        self.assertEqual((d.action, d.price), ("accept", 16))
        msgs = [her(1, 1, 17, "expired"), ours(2, 1, 14), her(3, 2, 17)]  # si mantiene 17, ofrecemos 16
        self.assertEqual((fast_decide(msgs, 2, 20).action, fast_decide(msgs, 2, 20).price), ("counter", 16))
        msgs += [ours(4, 2, 16), her(5, 3, 17)]  # tras su respuesta, cerramos (17 cabe) o abandonamos
        d = fast_decide(msgs, 3, 20)
        self.assertEqual((d.action, d.price), ("accept", 17))
        self.assertIn("precio inicial", d.reason)
        self.assertEqual(fast_decide(msgs, 3, 16).action, "abandon")

    def test_final_has_priority(self):
        msgs = [her(1, 1, 17, final=True)]
        self.assertEqual(fast_decide(msgs, 1, 17).action, "accept")
        self.assertEqual(fast_decide(msgs, 1, 16).action, "abandon")

    def test_pending_reply_is_not_a_rejection_and_expired_offer(self):
        msgs = [her(1, 1, 17, "expired"), ours(2, 5, 14)]
        self.assertEqual(fast_decide(msgs, 6, 18).action, "wait")
        d = fast_decide(msgs, 5 + CFG.reply_timeout_ticks, 18)  # no contestó y su oferta caducó
        self.assertEqual((d.action, d.price), ("counter", 16))
        d = fast_decide(msgs + [ours(3, 9, 16)], 9 + CFG.reply_timeout_ticks, 18)
        self.assertEqual(d.action, "abandon", "sin oferta vigente y contraofertas agotadas")

    def test_resumed_thread_counts_previous_counteroffers(self):  # el hilo #102: 6, 9, 10, 11 frente a 17
        msgs = [her(1, 58, 17, "expired"), ours(2, 59, 6), her(3, 60, 17, "expired"), ours(4, 61, 9),
                her(5, 62, 17, "expired"), ours(6, 63, 10), her(7, 64, 17)]
        self.assertEqual(fast_decide(msgs, 65, 18).action, "accept")
        self.assertEqual(fast_decide(msgs, 65, 16).action, "abandon")
        self.assertEqual(fast_decide(msgs + [ours(8, 65, 11)], 66, 18).action, "wait")

    def test_time_budget(self):
        msgs = [her(1, 1, 17, "expired"), ours(2, 1, 14), her(3, 2, 17)]
        self.assertEqual(fast_decide(msgs, 2, 18, conv_left=0).action, "accept")
        self.assertEqual(fast_decide(msgs, 2, 16, total_left=0).action, "abandon")

    def test_ceiling_far_below_ask_prefers_other_item(self):
        d = fast_decide([her(1, 1, 20)], 1, 15)
        self.assertEqual(d.action, "abandon")
        self.assertIn("otro artículo", d.reason)

    def test_old_lowball_trajectory_no_longer_happens(self):
        for name, (seller, ceiling) in sim.SCENARIOS.items():
            with self.subTest(name):
                self.assert_short(sim.run(SimSeller(**seller), sim.fast(ceiling)), seller["opening"], ceiling)
        for seller, ceiling in sim.population(300, seed=3):
            self.assert_short(sim.run(SimSeller(**seller), sim.fast(ceiling)), seller["opening"], ceiling)


class Candidates(unittest.TestCase):
    def test_viable_card_beyond_first_six_is_chosen(self):
        rows = [{"item": f"card:LAT-0{i}", "name": str(i), "value": 3, "ceiling": 1, "list_price": 10, "owned": 0}
                for i in range(1, 9)]
        rows.append({"item": "card:MAL-04", "name": "x", "value": 14, "ceiling": 12, "list_price": 10, "owned": 0})
        ranked = neg.rank_cards(rows, FAST)
        self.assertEqual((ranked[0].item, ranked[0].tier), ("card:MAL-04", 0))

    def test_unowned_and_viable_at_published_price_first(self):
        rows = [{"item": "card:A", "name": "a", "value": 30, "ceiling": 24, "list_price": 25, "owned": 0},
                {"item": "card:B", "name": "b", "value": 14, "ceiling": 12, "list_price": 10, "owned": 1},
                {"item": "card:C", "name": "c", "value": 13, "ceiling": 11, "list_price": 10, "owned": 0}]
        self.assertEqual([c.item for c in neg.rank_cards(rows, FAST)], ["card:C", "card:B", "card:A"])


class Lock(unittest.TestCase):
    def test_two_instances(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.lock")
            first = neg.InstanceLock(path, {"version": "a"})
            self.assertIsNone(first.acquire())
            self.assertEqual(neg.InstanceLock(path, {"version": "b"}).acquire()["pid"], os.getpid())
            first.release()
            self.assertIsNone(neg.InstanceLock(path, {}).acquire())

    def test_stale_lock_from_dead_process_is_recovered(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.lock")
            with open(path, "w") as f:
                f.write('{"pid": 999999}')
            self.assertIsNone(neg.InstanceLock(path, {}).acquire())


# ------------------------------------------------------------------ modo primera compra: agente completo sin red

import starter_agent as sa  # noqa: E402


class FakeBazaar:
    """Servidor falso: cartas LAT-01..LAT-10 (comunes, publicadas a 10 P); cada conversación es un sim.SimSeller."""

    def __init__(self, sellers, values, tick=100):
        self.sellers_kw, self.values, self.tick = sellers, values, tick
        self.cash, self.assets, self.threads, self.calls, self.delivered = 400, [], {}, [], set()
        self.deliver = True

    def clock(self):
        return {"tick": self.tick, "paused": False, "doors": "open", "tick_seconds": 30, "t_hours": self.tick / 120}

    def catalog(self):
        return {"sets": [{"id": "LAT", "released": True, "cards": [
            {"id": f"LAT-{i:02d}", "name": f"Carta {i}", "rarity": "common"} for i in range(1, 11)]}], "packs": []}

    def dealer(self, _):
        return {"menu": {"sells": [{"rarity": "common", "list_price": 10}, {"pack": "sobre_barrio", "list_price": 26}]}}

    def me(self):
        return {"id": TEAM, "name": "Team test", "cash": self.cash, "assets": list(self.assets), "level": 1}

    def value(self, c):
        return {"your_value": self.values.get(c, 3)}

    def my_threads(self, status=None):
        return {"threads": [s.thread for s in self.threads.values() if s.thread["status"] == status]}

    def open_thread(self, _, topic):
        kind, ref = next(iter(topic["buy"].items()))
        tid = 500 + len(self.threads)
        self.calls.append(("open", f"{kind}:{ref}"))
        self.threads[tid] = SimSeller(**self.sellers_kw[f"{kind}:{ref}"], tick=self.tick + 1, thread_id=tid,
                                      item=f"{kind}:{ref}")
        self.threads[tid].thread["created_tick"] = self.tick
        return self.threads[tid].thread

    def thread(self, tid):
        return self.threads[tid].thread

    def say(self, tid, text, price=None):
        self.calls.append(("say", tid, price))
        self.threads[tid].bid(price)

    def accept(self, oid):
        self.calls.append(("accept", oid))
        for s in self.threads.values():
            if neg.find_offer(s.thread, oid):
                s.accept(oid)

    def close_thread(self, tid):
        self.calls.append(("close", tid))
        self.threads[tid].walk()

    def card(self, _):
        return {}

    def wait_tick(self):
        self.tick += 1
        for tid, s in self.threads.items():
            s.tick = self.tick
            s.respond()
            s.expire()
            if s.status == "deal" and tid not in self.delivered and self.deliver and s.deal_tick < self.tick:
                self.delivered.add(tid)
                self.cash -= s.price
                self.assets.append({"id": 9000 + tid, "kind": "card", "ref": s.item.split(":")[1]})
        return self.clock()


class FirstPurchaseAgent(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patches = [(sa, "HERE", self.dir.name), (sa.time, "sleep", lambda s: None)]
        self.saved = [(obj, name, getattr(obj, name)) for obj, name, _ in self.patches]
        for obj, name, value in self.patches:
            setattr(obj, name, value)
        os.environ.setdefault("BAZAAR_KEY", "test")

    def tearDown(self):
        for obj, name, value in self.saved:
            setattr(obj, name, value)
        self.dir.cleanup()

    def agent(self, fake, *argv):
        sa.Bazaar = lambda *a, **k: fake
        old = sys.argv
        sys.argv = ["starter_agent.py", "--first-purchase", *argv]
        try:
            return sa.Agent(sa.parse_args())
        finally:
            sys.argv = old

    def run_quiet(self, agent):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                agent.first_purchase()
            except sa.Stop as e:
                print(e)
        return out.getvalue()

    HOLD_HIGH = dict(opening=13, floor=13, patience=8, reciprocity=0.0, stop_after=0)  # por encima de nuestro máximo
    CONCEDES = dict(opening=11, floor=8, patience=8, reciprocity=0.8)

    def test_switches_card_without_resetting_global_budget_and_stops_after_first_purchase(self):
        fake = FakeBazaar({"card:LAT-09": self.HOLD_HIGH, "card:LAT-10": self.CONCEDES},
                          {"LAT-09": 14, "LAT-10": 14})  # máximo 12 P en ambas; LAT-01..08 valen 3 (fuera del top 6)
        out = self.run_quiet(self.agent(fake))
        att = neg.Journal(os.path.join(self.dir.name, "data")).load(sa.ATTEMPT)
        self.assertEqual([c for c in fake.calls if c[0] == "open"], [("open", "card:LAT-09"), ("open", "card:LAT-10")])
        self.assertEqual(att["start_tick"], 100, "el plazo global no se reinicia al cambiar de carta")
        self.assertEqual((att["status"], att["tried"], att["purchases"][0]["item"]), ("done", ["card:LAT-09"], "card:LAT-10"))
        self.assertEqual(len(fake.assets), 1)
        self.assertLessEqual(len([c for c in fake.calls if c[0] == "say" and c[1] == 500]), 2)
        self.assertIn("Primera compra completada", out)
        out = self.run_quiet(self.agent(fake))  # relanzar no compra otra
        self.assertIn("ya está confirmada", out)
        self.assertEqual(len([c for c in fake.calls if c[0] == "open"]), 2)

    def test_global_budget_persists_across_restarts(self):
        fake = FakeBazaar({"card:LAT-09": self.CONCEDES}, {"LAT-09": 14})
        j = neg.Journal(os.path.join(self.dir.name, "data"))
        j.save(sa.ATTEMPT, {"start_tick": 80, "max_total_ticks": 12, "spent": 0, "purchases": [], "tried": [],
                            "active_thread": None, "status": "running"})
        out = self.run_quiet(self.agent(fake))
        self.assertIn("Plazo global agotado", out)
        self.assertFalse([c for c in fake.calls if c[0] == "open"], "no inicia otra negociación")
        self.assertEqual(j.load(sa.ATTEMPT)["status"], "expired")
        self.run_quiet(self.agent(fake))
        self.assertFalse([c for c in fake.calls if c[0] == "open"], "tampoco al relanzar")

    def test_restart_with_pending_acceptance_never_accepts_again(self):
        fake = FakeBazaar({"card:LAT-09": self.CONCEDES}, {"LAT-09": 14})
        t = fake.open_thread("abuela", {"buy": {"card": "LAT-09"}})
        offer = t["messages"][0]["offer"]
        fake.threads[t["id"]].accept(offer["id"])  # la aceptación llegó al servidor, la respuesta se perdió
        fake.deliver = False
        j = neg.Journal(os.path.join(self.dir.name, "data"))
        j.set_pending({"thread": t["id"], "offer_id": offer["id"], "item": "card:LAT-09", "price": 11,
                       "cash_before": 400, "holdings_before": 0})
        out = self.run_quiet(self.agent(fake))
        self.assertIn("pendiente", out)
        self.assertIsNotNone(j.pending(), "sin entrega no se da por comprada")
        fake.deliver = True
        out = self.run_quiet(self.agent(fake))
        self.assertIn("Primera compra completada", out)
        self.assertIsNone(j.pending())
        self.assertFalse([c for c in fake.calls if c[0] in ("accept", "say")], "nunca vuelve a aceptar ni a ofertar")
        self.assertEqual(len([c for c in fake.calls if c[0] == "open"]), 1)

    def test_open_pack_thread_is_closed_and_a_card_is_bought(self):
        fake = FakeBazaar({"pack:sobre_barrio": self.HOLD_HIGH, "card:LAT-09": self.CONCEDES}, {"LAT-09": 14})
        fake.open_thread("abuela", {"buy": {"pack": "sobre_barrio"}})
        self.run_quiet(self.agent(fake))
        self.assertIn(("close", 500), fake.calls)
        self.assertEqual(fake.assets[0]["ref"], "LAT-09")


class DealerSellTest(unittest.TestCase):
    """Lado VENDEDOR: el canal que llena la escalera y que antes no existía (item_of devolvía None)."""

    @staticmethod
    def state(opening, current=None, ours=None, live=True, final=False, reply=None):
        st = neg.NegState(item="sell:7")
        st.opening = opening
        price = current if current is not None else opening
        st.current = neg.Ask(price=price, offer_id=99, final=final, live=live, tick=1)
        if ours is not None:
            st.rounds.append(neg.Round(ours=ours, before=opening, tick=1,
                                       reply=price if reply is None else reply))
        return st

    def test_item_of_reconoce_topic_de_venta(self):
        self.assertEqual(neg.item_of({"sell": {"assets": [7, 8]}}), "sell:7,8")
        self.assertTrue(neg.is_sell("sell:7,8"))
        self.assertEqual(neg.sell_assets("sell:7,8"), [7, 8])
        self.assertFalse(neg.is_sell("card:SAL-08"))
        self.assertEqual(neg.item_of({"buy": {"card": "SAL-08"}}), "card:SAL-08")

    def test_ningun_vendedor_abre_al_90_por_ciento(self):
        # el fallback anterior metía a chato, pilar y picaros en open_frac=0.90 con UNA contraoferta
        for d in ("chato", "pilar", "picaros", "desconocido"):
            with self.subTest(dealer=d):
                pol = neg.dealer_policy(d)
                self.assertLessEqual(pol.open_frac, 0.60)
                self.assertGreaterEqual(pol.max_counteroffers, 2)

    def test_las_rondas_siguen_a_la_paciencia_publicada(self):
        self.assertEqual(neg.dealer_policy("abuela").max_ticks, 8)   # patience 0.85: aguanta
        self.assertEqual(neg.dealer_policy("chato").max_ticks, 5)    # patience 0.35: rompe el hilo

    def test_modo_score_no_acepta_la_primera_rebaja_ni_la_apertura(self):
        for d in ("abuela", "chato", "pilar", "picaros"):
            with self.subTest(dealer=d):
                pol = neg.dealer_policy(d, "score")
                self.assertFalse(pol.accept_on_concession)
                self.assertFalse(pol.allow_opening_price)
        self.assertTrue(neg.dealer_policy("abuela", "acquire").accept_on_concession)

    def test_price_floor_incluye_el_bono_de_pagina(self):
        self.assertEqual(neg.price_floor(-12.5, 2.0), 15)   # SAL-08: 12.5 P privados + margen
        self.assertEqual(neg.price_floor(-110.0), 110)      # común de página completa: suelo inalcanzable

    def test_primera_peticion_es_multiplo_de_su_puja(self):
        d = neg.decide_dealer_sell(self.state(opening=10), neg.dealer_policy("pilar"), 13, 6)
        self.assertEqual((d.action, d.price), ("counter", 22))   # 2.2x de 10

    def test_nunca_cierra_a_su_puja_de_apertura(self):
        st = self.state(opening=20, current=20, ours=44, reply=20)
        d = neg.decide_dealer_sell(st, neg.dealer_policy("pilar"), 13, 6)
        self.assertNotEqual(d.action, "accept")   # 20 == apertura -> rango capturado 0

    def test_acepta_por_encima_de_la_apertura_y_del_suelo(self):
        st = self.state(opening=20, current=25, ours=26)
        d = neg.decide_dealer_sell(st, neg.dealer_policy("pilar"), 15, 6)
        self.assertEqual((d.action, d.price), ("accept", 25))   # brecha 1 <= accept_gap

    def test_abandona_si_su_puja_no_llega_al_suelo(self):
        st = self.state(opening=5, current=5, ours=6)
        st.rounds.append(neg.Round(ours=6, before=5, tick=2, reply=5))
        st.rounds.append(neg.Round(ours=6, before=5, tick=3, reply=5))
        self.assertEqual(neg.decide_dealer_sell(st, neg.dealer_policy("chato"), 110, 5).action, "abandon")

    def test_concede_bajando_y_nunca_sube_su_peticion(self):
        st = self.state(opening=10, current=12, ours=24)
        d = neg.decide_dealer_sell(st, neg.dealer_policy("abuela"), 13, 8)
        self.assertEqual(d.action, "counter")
        self.assertTrue(13 <= d.price < 24, d.price)

    def test_puja_final_por_debajo_del_suelo_se_abandona(self):
        st = self.state(opening=4, current=4, final=True)
        self.assertEqual(neg.decide_dealer_sell(st, neg.dealer_policy("picaros"), 13, 5).action, "abandon")

    def test_ancla_la_primera_peticion_al_techo_observado_si_existe(self):
        """playbook.py deriva, de data/feed_history.jsonl: pilar LAT-08 techo 19 -> abrir 21, paso 2."""
        d = neg.decide_dealer_sell(self.state(opening=10), neg.dealer_policy("pilar"), 13, 6, ceiling_seen=19)
        self.assertEqual((d.action, d.price), ("counter", 21))
        self.assertIn("techo observado", d.reason)

    def test_el_multiplo_es_solo_respaldo_sin_observacion(self):
        # anclar al múltiplo de su puja de apertura es un ancla equivocada: con puja 50 y techo real 71,
        # 2.2x son 110 y se gastan las rondas por encima de lo que paga
        sin_techo = neg.decide_dealer_sell(self.state(opening=50), neg.dealer_policy("pilar"), 13, 6)
        con_techo = neg.decide_dealer_sell(self.state(opening=50), neg.dealer_policy("pilar"), 13, 6,
                                           ceiling_seen=71)
        self.assertEqual(sin_techo.price, 110)
        self.assertEqual(con_techo.price, 73)
        self.assertIn("sin techo observado", sin_techo.reason)

    def test_el_paso_de_venta_sigue_lo_observado_por_vendedor(self):
        self.assertEqual(neg.dealer_policy("abuela").sell_step, 1)   # playbook: paso 1, 6 rondas
        self.assertEqual(neg.dealer_policy("pilar").sell_step, 2)    # playbook: paso 2, 5 rondas
        self.assertEqual(neg.dealer_policy("chato").sell_step, 4)    # playbook: paso 4, 5 rondas

    def test_un_techo_bajo_el_suelo_no_manda_sobre_el_suelo(self):
        d = neg.decide_dealer_sell(self.state(opening=4), neg.dealer_policy("pilar"), 110, 6, ceiling_seen=9)
        self.assertEqual(d.action, "counter")
        self.assertGreaterEqual(d.price, 110)

    def test_espera_si_aun_no_ha_pujado(self):
        st = neg.NegState(item="sell:7")
        self.assertEqual(neg.decide_dealer_sell(st, neg.dealer_policy("pilar"), 13, 6).action, "wait")


if __name__ == "__main__":
    unittest.main()
