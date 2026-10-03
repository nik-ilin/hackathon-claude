"""Radio Rastro INTEGRADA en decisiones y negociaciones: cada prueba demuestra cómo una noticia cambia (o NO cambia) lo
que decide el ejecutor. Fixtures y snapshots; sin red, sin operaciones, sin tocar data/."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import coordinator as co
import negotiation as neg
import news_watch as nw
import phases as ph
import radio
from test_coordinator import args
from test_dealer_ladder import bid, led0, mine, thread
from test_news_watch import chato_snap

ABUELA = {"id": "abuela", "open_to_all": True, "menu": {"buys": [{"rarity": "common", "sets": "released"}], "sells": []}}
CHATO_LAT = {"id": 7, "tick": 440, "source": "radio", "headline": "El Chato is looking for common La Latina cards",
             "body": "One hour, no more."}


def snap(**kw):
    s = chato_snap(**kw)
    s["dealers"]["abuela"] = ABUELA
    return s


def plan_cands(s, led=None, **kw):
    a = args(dealer_sell_dups=True, **kw)
    if kw.get("news_sell"):
        co.radio_ingest(s, False)
    cands, pl, _ = co.candidates(s, led or led0(), a, neg.Journal(tempfile.mkdtemp()))
    return cands, co.select(cands, led or led0(), s["clock"]["tick"], 3)


def sells(cands):
    return [c for c in cands if c["type"] == "dealer_sell_open"]


class Before_after(unittest.TestCase):
    def test_confirmed_demand_changes_priority_counterparty(self):
        cands, chosen = plan_cands(snap())                                     # ANTES: sin radio
        self.assertEqual([(c["module"], c["dealer"]) for c in sells(chosen)], [("vendedores", "abuela")])
        cands, chosen = plan_cands(snap(), news_sell=True, news_margin=2.0)    # DESPUÉS: menú del Chato confirma
        pick = sells(chosen)[0]
        self.assertEqual((pick["module"], pick["dealer"], pick["asset"]), ("noticias", "chato", 2))
        plan = pick["radio_plan"]
        self.assertEqual(plan["A_value"], 3.25)                                # A: la noticia no cambia el valor privado
        self.assertGreaterEqual(plan["B_reserve"], 6)                          # B incluye la alternativa (abuela ≈ 6 P)
        self.assertGreaterEqual(plan["C_target"], 10)                          # C: no más tímido que la apertura habitual
        self.assertIsNone(plan["D_wtp"])                                       # D: desconocida; la noticia no la garantiza
        self.assertEqual(plan["mode"], "sonda")
        self.assertIn("sin noticia: vender a abuela", plan["delta"])
        self.assertTrue(any(b for c in sells(cands) if c["module"] == "vendedores" for b in [c["blockers"] or True]))

    def test_unconfirmed_hypothesis_does_not_displace_the_proven_route(self):
        s = snap(menu={"buys": [], "sells": []})                               # el menú NO confirma
        cands, chosen = plan_cands(s, news_sell=True, news_margin=2.0)
        # fuente sin observaciones verificadas ("delgada"): ni siquiera genera candidata sin menú
        self.assertEqual([c for c in cands if c.get("module") == "noticias" and c["type"] == "dealer_sell_open"], [])
        self.assertEqual([c["dealer"] for c in sells(chosen)], ["abuela"])

    def test_observed_worse_price_blocks_the_news_route(self):
        s = snap()
        s["feed"] = {"events": [{"type": "settlement", "tick": 400 + i, "payload": {
            "settlement": 900 + i, "persona": "chato", "tick": 400 + i, "price": 4, "fee": 0, "venue": "persona",
            "parties": ["chato", "t99"], "items": [{"kind": "card", "ref": "LAT-02", "frm": "t99", "to": "chato"}]}}
            for i in range(3)]}
        cands, chosen = plan_cands(s, news_sell=True, news_margin=2.0)
        news = [c for c in sells(cands) if c["module"] == "noticias"][0]
        self.assertEqual(news["radio_plan"]["D_wtp"]["value"], 4)
        self.assertTrue(any("no mejora la ruta habitual" in b for b in news["blockers"]))
        self.assertEqual([c["dealer"] for c in sells(chosen)], ["abuela"])


class Safety(unittest.TestCase):
    def test_rumour_never_buys_speculative_inventory(self):
        s = snap(news=[{"id": 4, "tick": 440, "source": "tablon", "headline": "El Chato sells rare Malasaña cards cheap",
                        "body": "my cousin says. One hour."}])
        cands, chosen = plan_cands(s, news_sell=True, radio_spec_budget=0)
        buy = [c for c in cands if c["type"] == "radio_buy"]
        self.assertTrue(buy and all(c["blockers"] for c in buy))
        self.assertTrue(any("presupuesto especulativo 0 P" in b for b in buy[0]["blockers"]))
        self.assertFalse([c for c in chosen if c["type"] == "radio_buy"])
        gate = radio.resale_gate(radio.resale_case(10, 14, True, signal_confirmed=True), 0)
        self.assertTrue(any("presupuesto especulativo" in x for x in gate))           # incluso con salida respaldada
        self.assertEqual(radio.resale_gate(radio.resale_case(10, 14, True, signal_confirmed=True), 30), [])
        self.assertTrue(radio.resale_gate(radio.resale_case(10, 14, False, signal_confirmed=True), 30))  # salida no asegurada

    def test_negation_blocks_habitual_sale_when_menu_agrees_and_never_creates_demand(self):
        stops = {"id": 7, "tick": 440, "source": "tablon", "headline": "Abuela stops buying common cards from today", "body": ""}
        gone = dict(ABUELA, menu={"buys": [], "sells": []})
        s = snap(news=[stops]); s["dealers"]["abuela"] = gone
        cands, chosen = plan_cands(s, news_sell=True)
        self.assertFalse([c for c in cands if c.get("module") == "noticias" and c["type"] == "dealer_sell_open"])
        hab = [c for c in sells(cands) if c["module"] == "vendedores"]
        self.assertTrue(hab and all(any("#7" in b for b in c["blockers"]) for c in hab))
        s = snap(news=[stops])                                                 # el menú aún compra: solo anotación
        cands, _ = plan_cands(s, news_sell=True)
        hab = [c for c in sells(cands) if c["module"] == "vendedores"]
        self.assertTrue(hab and not any(c["blockers"] for c in hab))
        self.assertTrue(any("sin cambio" in n for c in hab for n in c["notes"]))

    def test_unknown_window_is_not_invented(self):
        teatime = {"id": 9, "tick": 440, "source": "radio", "headline": "El Chato pays more for common La Latina cards until teatime", "body": ""}
        s = snap(news=[teatime])
        cands, _ = plan_cands(s, news_sell=True)
        e = s["radio"]["reg"]["items"]["9"]
        self.assertEqual(e["window"]["end"]["certainty"], "desconocido")
        self.assertIsNone(e["valid_until_tick"])
        self.assertEqual(e["window"]["start"]["certainty"], "estimado")
        self.assertIn("until teatime", e["window"]["end"]["note"])
        news = [c for c in sells(cands) if c["module"] == "noticias"]            # el hecho es el menú, no el horario
        self.assertTrue(news and "hecho observado" in " ".join(news[0]["notes"]))

    def test_unique_protected_card_is_never_offered(self):
        s = snap()
        s["me"]["assets"] = [a for a in s["me"]["assets"] if a["id"] != 2]      # LAT-01 única: página completa
        cands, chosen = plan_cands(s, news_sell=True)
        self.assertFalse([c for c in cands if c.get("module") == "noticias" and c["type"] == "dealer_sell_open"])
        self.assertFalse(sells(chosen))

    def test_committed_asset_is_not_reused(self):
        s = snap()
        s["offers"]["offers"] = [{"id": 50, "maker": s["me"]["id"], "status": "open", "give": {"assets": [2]},
                                  "want": {"cash": 9}, "venue": "rastro"}]
        cands, _ = plan_cands(s, news_sell=True)
        self.assertFalse([c for c in cands if c.get("module") == "noticias" and c.get("asset") == 2])

    def test_treasury_phase_blocks_a_news_motivated_purchase_but_not_the_sale(self):
        cfg = ph.PhaseConfig(enabled=True)
        clock = {"closes": "2026-10-03T23:00:00+02:00", "tick_seconds": 30.0}
        at = datetime.fromisoformat("2026-10-03T22:50:00+02:00")
        st = ph.state(clock, cfg, ph.scenarios(100, 0, 0), at)
        buy = {"type": "radio_buy", "price": 12, "ref": "MAL/rare", "blockers": [], "score": 1}
        sale = {"type": "dealer_sell_open", "price": 9, "ref": "card:LAT-01", "blockers": [], "score": 1}
        ph.apply([buy, sale], st, cfg, free_cash=100, states={}, tick_seconds=30.0, expiry_ratio=1.0, margin=2.0,
                 open_cash_offers=[])
        self.assertEqual(st["phase"], "C")
        self.assertTrue(any("TESORERÍA" in b for b in buy["blockers"]))
        self.assertFalse(any("TESORERÍA" in b for b in sale["blockers"]))


def open_sell_thread(tick_now, msgs, plan_state="sin_probar", created=450):
    s = snap()
    s["clock"]["tick"] = tick_now
    t = thread(8, "chato", msgs, {"sell": {"assets": [2]}}, created=created)
    s["threads"]["open"] = [t]
    plan = {"news_id": 7, "dealer": "chato", "asset": 2, "ref": "LAT-01", "A_value": 3.25, "B_reserve": 6, "C_target": 10,
            "D_wtp": None, "mode": "sonda", "probe_counters": 2, "deadline_tick": 456, "state": plan_state,
            "opened_tick": 450, "why": "A=3.2 · B=6 · C=10 · D=desconocida", "baseline": None, "news_id_": 7}
    led = dict(led0(), threads=[8], news_floor={"2": 6}, radio_plans={"2": plan})
    return s, led, plan


class Negotiation(unittest.TestCase):
    def thread_cands(self, s, led):
        a = args(news_sell=True, dealer_sell_dups=True)
        co.radio_ingest(s, False)
        co.radio_learn_threads(s, led, a)
        cands, _, _ = co.candidates(s, led, a, neg.Journal(tempfile.mkdtemp()))
        return [c for c in cands if c.get("thread") == 8 and c["module"] == "vendedores"]

    def test_expired_news_does_not_abandon_a_profitable_offer(self):
        s, led, plan = open_sell_thread(600, [bid("chato", 1, 597, 9, 2)], created=596)  # la noticia caducó en t560
        out = self.thread_cands(s, led)
        self.assertEqual(plan["state"], "confirmada_por_oferta")                # su puja estructurada confirma la mejora
        self.assertTrue(out and out[0]["type"] in ("dealer_sell_counter", "dealer_sell_accept"))
        self.assertNotEqual(out[0]["type"], "dealer_close")
        self.assertTrue(any("confirmada_por_oferta" in n for n in out[0]["notes"]))

    def test_accept_gets_closing_priority_when_the_offer_confirms(self):
        msgs = [bid("chato", 1, 452, 5, 2, "cancelled"), mine(2, 452, 10), bid("chato", 3, 453, 9, 2, final=True)]
        s, led, plan = open_sell_thread(454, msgs)
        out = self.thread_cands(s, led)
        self.assertEqual((out[0]["type"], out[0]["price"], out[0]["score"]), ("dealer_sell_accept", 9, 3 * 10 ** 5))

    def test_unconfirmed_hypothesis_stops_after_probe_and_is_not_repeated(self):
        msgs = [bid("chato", 1, 451, 3, 2, "cancelled"), mine(2, 451, 10), bid("chato", 3, 452, 3, 2, "cancelled"),
                mine(4, 452, 9), bid("chato", 5, 453, 3, 2)]
        s, led, plan = open_sell_thread(454, msgs)
        out = self.thread_cands(s, led)
        self.assertEqual(plan["state"], "no_confirmada")
        self.assertEqual(out[0]["type"], "dealer_close")                       # no insiste: pocas contraofertas de sonda
        reg = s["radio"]["reg"]
        self.assertTrue(radio.hypothesis_after_bid(plan, 3, 2)[0] == "no_confirmada")
        # sin nueva evidencia a favor la misma noticia no reabre otra conversación con ese vendedor
        s2 = snap()
        led2 = dict(led0(), radio_plans={"2": plan})
        cands, _ = plan_cands(s2, led2, news_sell=True)
        news = [c for c in sells(cands) if c["module"] == "noticias"]
        self.assertTrue(news and all(any("no se repite" in b for b in c["blockers"]) for c in news))

    def test_settlement_is_linked_once_even_if_seen_by_two_sources(self):
        s, led, plan = open_sell_thread(460, [])
        s["threads"]["open"] = []
        ev = {"type": "settlement", "tick": 458, "payload": {"settlement": 77, "persona": "chato", "tick": 458, "price": 9,
              "fee": 0, "parties": [s["me"]["id"], "chato"],
              "items": [{"kind": "card", "ref": "LAT-01", "id": 2, "frm": s["me"]["id"], "to": "chato"}]}}
        s["feed"] = {"events": [ev, dict(ev)]}                                 # misma liquidación, dos fuentes
        co.radio_ingest(s, False)
        co.radio_learn_threads(s, led, args(news_sell=True))
        co.radio_learn_threads(s, led, args(news_sell=True))
        e = s["radio"]["reg"]["items"]["7"]
        self.assertEqual(plan["state"], "settled")
        self.assertEqual(len(e["related"]["settlements"]), 1)
        self.assertEqual(len([x for x in e["evidence"] if x["key"] == "settlement:77"]), 1)
        self.assertEqual(len(radio.dealer_buy_prices([ev, ev], "chato", None)), 1)   # una liquidación, dos fuentes

    def test_equivalent_trades_dedup_and_association_is_not_causal(self):
        cat = chato_snap()["catalog"]
        mk = lambda i, tick, price, ref="LAT-02": {"type": "settlement", "tick": tick, "payload": {
            "settlement": i, "persona": "chato", "tick": tick, "price": price, "fee": 0,
            "items": [{"kind": "card", "ref": ref, "frm": "t99", "to": "chato"}]}}
        ev = [mk(1, 400, 4), mk(2, 410, 4), mk(3, 420, 5), mk(4, 450, 8), mk(5, 455, 9), mk(6, 460, 9), mk(4, 450, 8)]
        entry = {"tick": 440, "interpretation": {"kind": "demanda", "dealer": "chato", "set": "LAT", "rarity": "common"}}
        a = radio.association(entry, ev, cat)
        self.assertTrue(a["comparable"])
        self.assertEqual((a["n_before"], a["n_after"]), (3, 3))              # el duplicado de la liquidación 4 no cuenta
        self.assertIn("NO atribuible", a["causal"])
        mixed = ev + [mk(7, 461, 30, "ZZZ-01")]                                # carta de otro barrio: no se mezcla
        self.assertEqual(radio.association(entry, mixed, cat)["n_after"], 3)
        thin = radio.association(entry, ev[:4], cat)
        self.assertFalse(thin["comparable"])


class Persistence(unittest.TestCase):
    def test_restart_does_not_duplicate_messages_or_operations(self):
        with tempfile.TemporaryDirectory() as d:
            old = radio.DATA
            radio.DATA = Path(d)
            try:
                s = snap()
                co.radio_ingest(s, True)
                reg = s["radio"]["reg"]
                self.assertEqual(s["radio"]["new"], [7])
                self.assertTrue(radio.record_action(reg, 7, {"type": "dealer_sell_open", "dealer": "chato", "asset": 2, "tick": 450}))
                radio.link(reg, 7, "offers", {"thread": 8}, "thread:8")
                radio.write("registry", reg)
                s2 = snap()                                                    # «reinicio»: proceso nuevo, registro del disco
                co.radio_ingest(s2, True)
                self.assertEqual(s2["radio"]["new"], [])
                reg2 = s2["radio"]["reg"]
                self.assertTrue(radio.acted(reg2, 7, "chato"))
                self.assertFalse(radio.record_action(reg2, 7, {"type": "dealer_sell_open", "dealer": "chato", "asset": 2, "tick": 460}))
                self.assertEqual(len(reg2["items"]["7"]["related"]["offers"]), 1)
                cands, _ = plan_cands(s2, news_sell=True)
                news = [c for c in sells(cands) if c["module"] == "noticias"]
                self.assertTrue(news and all(c["blockers"] for c in news))     # ningún mensaje/operación duplicada
                # nueva evidencia a favor posterior a la acción sí reabre la puerta
                radio.add_evidence(reg2, 7, "for", "oferta estructurada 12 P", 470, "bid:new")
                self.assertFalse(radio.acted(reg2, 7, "chato"))
            finally:
                radio.DATA = old


class Reliability(unittest.TestCase):
    def test_sample_size_and_uncertainty_are_explicit(self):
        lo, hi = nw.interval(1, 1)
        self.assertLess(lo, 0.6)
        self.assertEqual(nw.interval(0, 0), (0.0, 1.0))
        rows = [{"source": "radio", "verdict": "sin confirmar"}] * 5
        self.assertEqual(nw.summarize(rows)["radio"]["n"], 0)                  # ausencia de operaciones no prueba falsedad
        self.assertEqual(nw.summarize(rows)["radio"]["unconfirmed"], 5)
        self.assertEqual(nw.summarize(rows)["radio"]["label"], "sin datos suficientes")


if __name__ == "__main__":
    unittest.main()
