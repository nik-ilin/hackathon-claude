"""Servicio compartido de oportunidades: señal de punta a punta, pistas históricas caducadas, revalidación y dashboard."""
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

import coordinator as co
import dashboard as D
import intelligence as it
import negotiation as neg
import opportunities as opps
import phases as ph
from feed_oracle import Oracle
from test_audit_fixes import new_led
from test_market_intel import TEAM, A, ask, bid
from test_page_campaign import cargs, inventory, snapm

TICK = 400


def listed(eid, tick, offer):
    return {"id": eid, "tick": tick, "type": "offer.listed", "actor": offer["maker"], "payload": {"venue": offer["venue"], "offer": offer}}


def world(boards=(), mine=(), events=(), cash=120):
    s = snapm(inventory(["MAL-08", "MAL-09"]), list(boards), mine=list(mine), cash=cash, tick=TICK)
    s["feed"] = {"events": list(events)}
    return s


def run(s, execute=False, **kw):
    kw.setdefault("reserve", 5)
    with tempfile.TemporaryDirectory() as d:
        saved = co.DATA
        co.DATA = Path(d)
        try:
            co.INTEL_STATE["intel"] = None
            with contextlib.redirect_stdout(io.StringIO()):
                cands, pl, _ = co.candidates(s, new_led(), cargs(**kw), neg.Journal(d))
                chosen = co.select(cands, {"actions": [], "class_tick": {}}, TICK, 3)
                payload = co.export_shared(s, cands, chosen, pl, new_led(), cargs(**kw), execute, None)
            written = sorted(p.name for p in Path(d).glob("opportunities*.json"))
            return cands, pl, chosen, payload, written
        finally:
            co.DATA = saved


class SignalToDecision(unittest.TestCase):
    def test_01_signal_followed_from_ingestion_to_decision(self):
        offer = ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=900)
        ev = listed(17000, 399, offer)
        s = world(boards=[offer], events=[ev])
        # 1) ingestión y evidencia persistida
        with tempfile.TemporaryDirectory() as d:
            db = it.Intelligence(str(Path(d) / "m.db"), TEAM)
            db.ingest(s)
            row = db.db.execute("select offer_id, maker, price, status from offers where offer_id=970").fetchone()
            self.assertEqual((row["maker"], row["price"], row["status"]), ("t07", 28, "open"))
            self.assertGreater(db.p_owns("t07", "MAL-10")["p"], 0, "evidencia de posesión histórica")
            db.close()
        # 2) la pista histórica se contrasta con la oferta viva
        live = opps.live_index(s)
        lead = opps.historical_leads([ev], TICK, live)[0]
        self.assertEqual((lead["offer_id"], lead["validity"], lead["role"], lead["executable"]),
                         (970, "VIGENTE", "vendedor confirmado", True))
        # 3) candidata del agente, decisión y registro compartido (mismo offer_id)
        cands, pl, chosen, payload, written = run(s)
        acc = [c for c in cands if c.get("offer") == 970 and c["type"] == "accept" and not c["blockers"]]
        self.assertEqual(len(acc), 1, "una sola candidata ejecutable para esa oferta")
        self.assertIn(970, [c.get("offer") for c in chosen], "decisión: seleccionada para ejecutar")
        o = next(r for r in payload["opportunities"] if r["offer_id"] == 970)
        self.assertEqual((o["status"], o["role"], o["counterparty"], o["venue"], o["valid_until"], o["verified_live"]),
                         ("SELECCIONADA", "vendedor confirmado", "t07", "v02", 900, True))
        for k in ("source", "tick", "offer_id", "fee", "marginal_value", "capital_needed", "reason", "net_surplus"):
            self.assertIn(k, o)
        self.assertEqual(o["capital_needed"], 28 + o["fee"])
        self.assertAlmostEqual(o["marginal_value"], pl["page_campaign"]["state"]["gains"]["MAL-10"]["gain"], places=1,
                               msg="valor marginal con el bono de página una vez")
        self.assertEqual(written, ["opportunities_analysis.json"], "un análisis nunca pisa el estado del agente real")
        self.assertEqual([r["offer_id"] for r in payload["leads"] if r["executable"]], [970], "una sola cuenta")

    def test_02_execute_writes_its_own_file(self):
        _, _, _, _, written = run(world(), execute=True)
        self.assertEqual(written, ["opportunities.json"])


class StaleHistory(unittest.TestCase):
    def stale_events(self):
        old = ask(4483, "rastro", 66, "MAL-10", 76, maker="t01", exp=327, created=307)
        return [listed(16638, 307, old)]

    def test_03_expired_historical_offer_is_never_operable(self):
        s = world(events=self.stale_events())
        leads = opps.historical_leads(s["feed"]["events"], TICK, opps.live_index(s))
        self.assertEqual((leads[0]["validity"], leads[0]["executable"], leads[0]["role"]), ("CADUCADA", False, "poseedor histórico"))
        self.assertIn("caducó en el tick 327", leads[0]["reason"])
        cands, _, chosen, payload, _ = run(s)
        self.assertFalse([c for c in cands if c.get("offer") == 4483], "una oferta histórica no genera candidata")
        self.assertFalse([r for r in payload["opportunities"] if r["offer_id"] == 4483])
        self.assertFalse(chosen and any(c.get("offer") == 4483 for c in chosen))
        self.assertEqual(opps.historical_leads(s["feed"]["events"], TICK, None)[0]["validity"], "SIN VERIFICAR")

    def test_04_cancelled_and_not_live_without_expiry(self):
        o = ask(5, "v02", 70, "MAL-10", 30, maker="t02", exp=9999)
        ev = [listed(1, 390, o), {"id": 2, "tick": 395, "type": "offer.cancelled", "payload": {"offer": 5}}]
        self.assertEqual(opps.historical_leads(ev, TICK, {})[0]["validity"], "RETIRADA")
        ev2 = [listed(1, 390, o)]
        self.assertEqual(opps.historical_leads(ev2, TICK, {})[0]["validity"], "NO VIGENTE")

    def test_05_price_comparison_is_reference_not_arbitrage_and_inherits_staleness(self):
        s = world(events=self.stale_events())
        leads = opps.historical_leads(s["feed"]["events"], TICK, opps.live_index(s))
        row = {"ref": "MAL-10", "team_ask": 76, "dealer": "picaros", "dealer_floor": 167, "saving": 91, "holders": ["t01"]}
        ref = opps.price_reference([row], leads)[0]
        self.assertEqual((ref["kind"], ref["validity"], ref["executable"], ref["offer_id"]), ("REFERENCIA_DE_PRECIOS", "CADUCADA", False, 4483))
        self.assertIn("no es arbitraje", ref["note"])
        self.assertEqual(ref["holders_historical"], ["t01"])

    def test_06_arbitrage_needs_two_live_legs(self):
        fee = lambda v, p: 0
        sell_only = world(boards=[ask(1, "v02", 60, "MAL-10", 28, maker="t07", exp=900),
                                  ask(2, "v02", 61, "MAL-10", 80, maker="t09", exp=900)])
        self.assertEqual(opps.executable_arbitrage(sell_only, fee), [], "dos precios de venta no son arbitraje")
        both = world(boards=[ask(1, "v02", 60, "MAL-10", 28, maker="t07", exp=900), bid(3, "v02", "MAL-10", 40, maker="t09", exp=900)])
        a = opps.executable_arbitrage(both, fee)
        self.assertEqual((a[0]["buy_offer"], a[0]["sell_offer"], a[0]["gross"]), (1, 3, 12))
        self.assertEqual(opps.executable_arbitrage(world(boards=[ask(1, "v02", 60, "MAL-10", 28, maker="t07", exp=900),
                                                            bid(3, "v02", "MAL-10", 40, maker="t07", exp=900)]), fee), [],
                         "mismo equipo en las dos patas no cuenta")

    def test_07_alias_makers_are_never_attributed(self):
        o = ask(9, "rastro", 70, "MAL-10", 30, maker="m3950d43b", exp=900)
        lead = opps.historical_leads([listed(1, 399, o)], TICK, opps.live_index(world(boards=[o])))[0]
        self.assertEqual((lead["role"], lead["counterparty"]), ("anónimo (alias)", None))


class Revalidation(unittest.TestCase):
    def fresh(self, offer):
        return dict(fresh_board=[offer], my_offers=[], me={"cash": 120, "assets": [A(1, "MAL-01")]}, tick=TICK, team=TEAM)

    def cand(self, **kw):
        return {"type": "accept", "offer": 970, "maker": "t07", "price": 28, "cash": -28, "assets": [], "venue": "v02", **kw}

    def test_08_revalidate_catches_every_drift(self):
        ok = ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=900)
        self.assertEqual(opps.revalidate_accept(self.cand(), **self.fresh(ok)), [])
        gone = opps.revalidate_accept(self.cand(), **{**self.fresh(ok), "fresh_board": []})
        self.assertIn("ya no figura abierta", gone[0])
        exp = opps.revalidate_accept(self.cand(), **self.fresh(ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=TICK)))
        self.assertTrue(any("caducó" in p for p in exp))
        price = opps.revalidate_accept(self.cand(), **self.fresh(ask(970, "v02", 89, "MAL-10", 35, maker="t07", exp=900)))
        self.assertTrue(any("el precio cambió" in p for p in price))
        other = opps.revalidate_accept(self.cand(), **self.fresh(ask(970, "v02", 89, "MAL-10", 28, maker="t09", exp=900)))
        self.assertTrue(any("maker" in p for p in other))
        poor = opps.revalidate_accept(self.cand(), **{**self.fresh(ok), "me": {"cash": 10, "assets": []}})
        self.assertTrue(any("efectivo insuficiente" in p for p in poor))
        sold = opps.revalidate_accept(self.cand(assets=[77]), **self.fresh(ok))
        self.assertTrue(any("ya no tenemos" in p for p in sold))
        locked = {"id": 5, "maker": TEAM, "status": "open", "give": {"assets": [{"id": 1}]}, "want": {"cash": 5}}
        lk = opps.revalidate_accept(self.cand(assets=[1]), **{**self.fresh(ok), "my_offers": [locked]})
        self.assertTrue(any("comprometido" in p for p in lk))

    def test_09_send_revalidates_against_server_and_does_not_send_stale_accept(self):
        calls = []

        class API:
            def accept(self, oid, assets=None):
                calls.append(("accept", oid))
                return {"queued": True}

        class Reader:
            api = API()
            last = None

            def __init__(self, board):
                self.board = board

            def call(self, method, *a):
                if method == "board":
                    return {"offers": self.board}
                if method == "me":
                    return {"cash": 120, "assets": [A(1, "MAL-01")]}
                if method == "my_offers":
                    return {"offers": []}
                if method == "clock":
                    return {"tick": TICK}
                raise AssertionError(method)

        s = world()
        s["threads"] = {"open": [], "deal": []}
        c = {"type": "accept", "module": "mercado", "kind": "comprar", "offer": 970, "maker": "t07", "price": 28, "cash": -28,
             "assets": [], "receive": {"MAL-10": 1}, "deliver": {}, "venue": "v02", "blockers": []}
        args = cargs()
        for board, expect_sent in (([], False), ([ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=900)], True)):
            calls.clear()
            with tempfile.TemporaryDirectory() as d:
                saved = co.LEDGER
                co.LEDGER = Path(d) / "l.json"
                try:
                    led = co.load_ledger(TEAM)
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        co.send(Reader(board), led, s, dict(c), args, neg.Journal(d))
                finally:
                    co.LEDGER = saved
            self.assertEqual(bool(calls), expect_sent, out.getvalue())
            if not expect_sent:
                self.assertIn("REVALIDACIÓN", out.getvalue())
                self.assertEqual(led["actions"], [], "ni siquiera queda intención registrada")


class DashboardView(unittest.TestCase):
    def snap(self, agent=None, events=()):
        mods = D.load_optional(("opportunities",))
        return D.Snapshot(oracle=Oracle(), clock={"tick": 410}, mods=mods, events=list(events), agent=agent or {})

    def payload(self, **kw):
        base = {"generated": time.time(), "tick": 408, "mode": "execute", "pid": 123, "fingerprint": "abc123def456",
                "argv": ["--execute", "--phases"], "phase": {"phase": "A", "minutes_to_close": 200.0, "w": 0, "reason": ""},
                "memory": {"integrated": True, "ok": True, "stored_events": 900, "market_db_last_tick": "408",
                           "market_db_lag_ticks": 0},
                "blockers": [{"reason": "capital: faltan N P", "count": 4}],
                "opportunities": [{"status": "BLOQUEADA", "type": "bid", "ref": "MAL-10", "offer_id": None, "thread": None,
                                   "counterparty": "t07", "role": "destinatario de nuestra propuesta (sin confirmar)",
                                   "valid_until": None, "verified_live": False, "price": 50, "fee": 0, "marginal_value": 77.0,
                                   "capital_needed": 50, "reason": "50 P > efectivo táctico libre 33 P"}],
                "leads": [], "live_offer_ids": [], "_age_s": 3.0, "_file": "opportunities.json"}
        base.update(kw)
        return base

    def test_10_agent_panel_shows_tick_mode_memory_and_block_reasons(self):
        html = D._panel_agent(self.snap({"execute": self.payload()}))
        for needle in ("EXECUTE", "408", "retraso 2 ticks", "memoria", "OK", "eventos", "market.db hasta el tick 408",
                       "capital: faltan N P", "efectivo táctico libre 33 P", "destinatario de nuestra propuesta"):
            self.assertIn(needle, html)

    def test_11_no_agent_and_obsolete_agent_are_visible(self):
        self.assertIn("no hay estado de un agente en modo EJECUCIÓN", D._panel_agent(self.snap()))
        dry_only = D._panel_agent(self.snap({"analysis": self.payload(mode="analysis")}))
        self.assertIn("análisis del tick 408", dry_only)
        self.assertIn("OBSOLETO", D._panel_agent(self.snap({"execute": self.payload(_age_s=500.0)})))
        degraded = D._panel_agent(self.snap({"execute": self.payload(memory={"integrated": True, "ok": False, "error": "OSError"})}))
        self.assertIn("DEGRADADA", degraded)
        self.assertIn("no integrada", D._panel_agent(self.snap({"execute": self.payload(memory={"integrated": False})})))

    def test_12_reference_panel_uses_shared_validity_and_roles(self):
        old = ask(4483, "rastro", 66, "MAL-10", 76, maker="t01", exp=327, created=307)
        s = self.snap({"execute": self.payload(live_offer_ids=[1, 2])}, events=[listed(16638, 307, old)])
        s.oracle.arbitrage = lambda **k: [{"ref": "MAL-10", "rarity": "rare", "team_ask": 76, "dealer": "picaros",
                                           "dealer_floor": 167, "saving": 91, "holders": ["t01"]}]
        html = D._panel_now(s)
        self.assertIn("NO es arbitraje", html)
        self.assertNotIn(">arbitraje<", html)
        self.assertIn("CADUCADA", html)
        self.assertIn("poseedor histórico: t01", html)
        self.assertIn("oferta #4483", html)
        self.assertNotIn("vendedor confirmado", html)
        self.assertNotIn("a quién", html)

    def test_13_builder_reads_agent_files_and_separates_modes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            opps.write_json(root / "data" / "opportunities.json", {"generated": time.time(), "tick": 7, "mode": "execute"})
            opps.write_json(root / "data" / "opportunities_analysis.json", {"generated": time.time(), "tick": 9, "mode": "analysis"})
            b = D.Builder(None, root=root, stores=("data/feed_history.jsonl",), mods={})
            ag = b.build().agent
            self.assertEqual((ag["execute"]["tick"], ag["analysis"]["tick"]), (7, 9))


if __name__ == "__main__":
    unittest.main()
