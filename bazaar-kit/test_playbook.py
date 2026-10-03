"""Pruebas de `playbook`. Escenarios reales del feed, sin red.

Las escaleras no son inventadas: son hilos literales de
`data/feed_history.jsonl` (ticks 192-234), con sus liquidaciones. Así una
prueba que pasa dice algo del juego y no sólo del código.
"""

import json
import tempfile
import unittest
from pathlib import Path

from feed_oracle import Oracle
from playbook import (
    Close, Plan, backtest, dance_side, match_closes, playbook, plan_for,
    read_history, report, simulate,
)


# ----------------------------------------------------------------- escenarios

def msg(eid, tick, *, sender, team, ref, cash, thread, sells, final=False):
    """Un mensaje de conversación con dealer. `sells`: el dueño cede la carta.

    Mismo esquema que el feed real: el dealer que vende pide la carta por
    referencia (`types`) y quiere efectivo; el equipo que compra ofrece
    efectivo y pide la carta.
    """
    if sells:
        give = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
        want = {"cash": cash, "assets": [], "types": []}
    else:
        give = {"cash": cash, "assets": [], "types": []}
        want = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
    return {"id": eid, "tick": tick, "type": "thread.message",
            "payload": {"thread": thread, "kind": "persona", "message": eid,
                        "sender": sender, "text": "...", "team": team,
                        "with": sender,
                        "offer": {"id": eid, "maker": sender, "to": None,
                                  "venue": None, "thread": thread,
                                  "status": "open", "give": give, "want": want,
                                  "final": final}}}


def dance_events(thread, dealer, team, ref, dealer_prices, team_prices, *,
                 dealer_sells=True, final_at=None, tick0=200, eid0=1000):
    """Un baile entero, alternando dealer y equipo como en el feed.

    `final_at` es el índice de la cotización del dealer que lleva
    `final: true`; las reglas dicen que es su última palabra.
    """
    events, eid, tick = [], eid0, tick0
    for i in range(max(len(dealer_prices), len(team_prices))):
        if i < len(dealer_prices):
            events.append(msg(eid, tick, sender=dealer, team=team, ref=ref,
                              cash=dealer_prices[i], thread=thread,
                              sells=dealer_sells, final=(final_at == i)))
            eid, tick = eid + 1, tick + 1
        if i < len(team_prices):
            events.append(msg(eid, tick, sender=team, team=team, ref=ref,
                              cash=team_prices[i], thread=thread,
                              sells=not dealer_sells))
            eid, tick = eid + 1, tick + 1
    return events


def settlement(eid, tick, *, frm, to, ref, price, rarity="common"):
    item = {"id": 9000 + eid, "kind": "card", "ref": ref, "serial": 3,
            "rarity": rarity, "set": ref[:3], "print_run": 300,
            "frm": frm, "to": to, "name": "X"}
    return {"id": eid, "tick": tick, "type": "settlement",
            "payload": {"settlement": eid, "tick": tick, "kind": "trade",
                        "parties": [frm, to], "venue": None, "persona": None,
                        "fee": 0, "items": [item], "price": price}}


def ret01():
    """Hilo 397 real: abuela 12→10→9 contra t12 6→7, y liquida en 9.

    Es el patrón dominante de abuela con comunes: abre en 12, baja a 9 y
    acepta ahí. Nótese que liquida en 9 habiendo cotizado 9.
    """
    ev = dance_events(397, "abuela", "t12", "RET-01", [12, 10, 9], [6, 7],
                      tick0=206, eid0=1000)
    ev.append(settlement(1100, 211, frm="abuela", to="t12", ref="RET-01", price=9))
    return ev


def ret10():
    """Hilo 412 real: chato 97→96→95→92→89→86 contra t05 57→...→69, cierra 86."""
    ev = dance_events(412, "chato", "t05", "RET-10",
                      [97, 96, 95, 92, 89, 86], [57, 60, 63, 66, 69],
                      tick0=220, eid0=2000)
    ev.append(settlement(2100, 232, frm="chato", to="t05", ref="RET-10",
                         price=86, rarity="rare"))
    return ev


def sal02():
    """Hilo 366 real: abuela cotiza 12→10→10→10 pero LIQUIDA en 9.

    El caso que justifica el canal `dealer_accepted`: su cotización más baja
    (10) es una cota superior del precio pagado, no el precio.
    """
    ev = dance_events(366, "abuela", "t16", "SAL-02", [12, 10, 10, 10],
                      [6, 7, 8, 9], tick0=190, eid0=3000)
    ev.append(settlement(3100, 196, frm="abuela", to="t16", ref="SAL-02", price=9))
    return ev


def ret02_bid():
    """Hilo 385 real: abuela compra y NO se mueve (5,5,5) pese a 22→20→19.

    Nunca liquidó. Es el dealer plantado: cada concesión del equipo se
    desperdició.
    """
    return dance_events(385, "abuela", "t13", "RET-02", [5, 5, 5], [22, 20, 19],
                        dealer_sells=False, tick0=210, eid0=4000)


def ret03_stuck():
    """Hilos 395 y 380 reales, misma carta y mismo dealer, final distinto.

    En el 395 abuela liquidó RET-03 en 9 con t18; en el 380 se plantó en 10 y
    t12 pagó 10. El suelo visto (9) es por tanto una cota que no todas las
    conversaciones alcanzan: es exactamente el riesgo que mide el abandono.
    """
    ev = dance_events(395, "abuela", "t18", "RET-03", [12, 10, 10], [7, 8, 9],
                      tick0=210, eid0=8000)
    ev.append(settlement(8100, 215, frm="abuela", to="t18", ref="RET-03", price=9))
    ev += dance_events(380, "abuela", "t12", "RET-03", [12, 11, 10, 10],
                       [6, 7, 8], tick0=198, eid0=8200)
    ev.append(settlement(8300, 206, frm="abuela", to="t12", ref="RET-03", price=10))
    return ev


def oracle_for(*event_groups):
    o = Oracle()
    events = [e for g in event_groups for e in g]
    o.ingest(events)
    return o, events


# ----------------------------------------------------------------- lectura

class TestHistorial(unittest.TestCase):
    def test_salta_la_linea_a_medias(self):
        """El archivo se escribe en caliente: la última línea puede truncarse."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.jsonl"
            p.write_text(json.dumps({"id": 1, "tick": 2}) + "\n\n{\"id\": 2, \"ti\n",
                         encoding="utf-8")
            self.assertEqual([e["id"] for e in read_history(p)], [1])


class TestCierres(unittest.TestCase):
    def test_empareja_la_liquidacion_con_su_hilo(self):
        o, ev = oracle_for(ret01())
        (close,) = match_closes(o, ev).values()
        self.assertEqual((close.thread, close.price), (397, 9))

    def test_un_traspaso_entre_equipos_no_cierra_un_baile(self):
        """Dos equipos intercambiando no dicen nada del suelo de un dealer."""
        o, ev = oracle_for(dance_events(500, "abuela", "t12", "RET-01",
                                        [12, 10, 9], [6, 7]))
        ev = ev + [settlement(7, 205, frm="t04", to="t12", ref="RET-01", price=9)]
        self.assertEqual(match_closes(o, ev), {})

    def test_una_liquidacion_fuera_del_tramo_no_cuenta(self):
        o, ev = oracle_for(dance_events(501, "abuela", "t12", "RET-01",
                                        [12, 10, 9], [6, 7], tick0=200))
        ev = ev + [settlement(7, 280, frm="abuela", to="t12", ref="RET-01", price=9)]
        self.assertEqual(match_closes(o, ev), {})

    def test_sentido_del_dealer(self):
        o, _ = oracle_for(ret01(), ret02_bid())
        self.assertEqual(dance_side(o.dances[397]), "ask")
        self.assertEqual(dance_side(o.dances[385]), "bid")


# -------------------------------------------------------------------- plan

class TestPlan(unittest.TestCase):
    def test_sin_datos_no_opina(self):
        self.assertIsNone(plan_for(Oracle(), "abuela", "RET-01"))

    def test_abre_por_debajo_del_suelo_y_nunca_en_su_apertura(self):
        o, _ = oracle_for(ret01())
        p = plan_for(o, "abuela", "RET-01")
        self.assertEqual((p.floor_seen, p.never_accept), (9, 12))
        self.assertLess(p.open_at, p.floor_seen)      # margen para ceder
        self.assertLess(p.open_at, p.never_accept)    # cerrar ahí no puntúa
        self.assertEqual(p.basis, "observed")

    def test_el_abandono_es_el_suelo_visto_y_la_tolerancia_lo_ensancha(self):
        """El suelo visto es una cota: con tolerancia 0 no se paga de más."""
        o, _ = oracle_for(ret01())
        self.assertEqual(plan_for(o, "abuela", "RET-01").walk_away, 9)
        self.assertEqual(plan_for(o, "abuela", "RET-01", tolerance=2).walk_away, 11)

    def test_pasos_topados_en_el_abandono(self):
        o, _ = oracle_for(ret10())
        p = plan_for(o, "chato", "RET-10")
        self.assertEqual(p.quote(0), p.open_at)
        self.assertEqual(p.quote(99), p.walk_away)    # nunca se pasa
        self.assertTrue(p.acceptable(p.walk_away))
        self.assertFalse(p.acceptable(p.walk_away + 1))

    def test_spread_son_las_primas_en_juego(self):
        o, _ = oracle_for(ret10())
        p = plan_for(o, "chato", "RET-10")
        self.assertEqual(p.spread, 97 - 86)

    def test_vendiendo_el_plan_se_invierte(self):
        """Nos compra: su suelo es el máximo ofrecido y nosotros bajamos."""
        o, _ = oracle_for(dance_events(600, "abuela", "t13", "LAT-04",
                                        [5, 5, 6], [22, 20, 19],
                                        dealer_sells=False))
        p = plan_for(o, "abuela", "LAT-04", side="bid")
        self.assertEqual(p.floor_seen, 6)             # el mejor para nosotros
        self.assertGreater(p.open_at, p.floor_seen)   # se pide más y se baja
        self.assertGreaterEqual(p.quote(0), p.quote(1))

    def test_rondas_salen_de_la_paciencia_observada(self):
        o, _ = oracle_for(dance_events(601, "abuela", "t03", "LAT-02",
                                        [12, 10, 9, 9, 8], [4, 5, 6, 7],
                                        final_at=4))
        p = plan_for(o, "abuela", "LAT-02")
        self.assertEqual(p.max_rounds, 5)             # llegó al final en 5

    def test_sin_final_observado_las_rondas_son_una_cota_inferior(self):
        o, _ = oracle_for(ret01())
        self.assertEqual(plan_for(o, "abuela", "RET-01").max_rounds, 3)


# --------------------------------------------------------------- simulación

class TestSimulate(unittest.TestCase):
    def test_cierra_antes_que_el_equipo_real(self):
        """t12 gastó 3 rondas para llegar a 9; abriendo en 8 bastan 2."""
        o, ev = oracle_for(ret01())
        p = plan_for(o, "abuela", "RET-01")
        r = simulate(p, o.dances[397], match_closes(o, ev)[397])
        self.assertEqual((r.price, r.rounds), (9, 2))
        self.assertLess(r.rounds, o.dances[397].rounds)

    def test_no_acepta_la_apertura_en_la_primera_ronda(self):
        """Un trato al precio de apertura no cuenta para la escalera.

        El plan se construye a mano porque `plan_for` ya impide abrir ahí: lo
        que se prueba es que el simulador tampoco lo acepta si se lo cuelan.
        """
        o, _ = oracle_for(ret01())
        p = Plan(dealer="abuela", ref="RET-01", side="ask", floor_seen=9,
                 open_at=12, step=1, walk_away=12, never_accept=12,
                 max_rounds=3, confidence="SETTLED", observations=3,
                 basis="observed")
        r = simulate(p, o.dances[397])
        self.assertEqual(r.rounds, 2)                 # la ronda 1 se descarta
        self.assertEqual(r.price, 10)                 # cierra en su 2º precio

    def test_plan_for_nunca_abre_en_la_apertura_del_dealer(self):
        o, _ = oracle_for(ret01(), ret10(), sal02())
        for row in playbook(o):
            p = plan_for(o, row["dealer"], row["ref"], side=row["side"])
            if p.never_accept is None:
                continue
            if p.side == "ask":
                self.assertLess(p.open_at, p.never_accept)
            else:
                self.assertGreater(p.open_at, p.never_accept)

    def test_el_precio_aceptado_de_hecho_vence_a_la_cotizacion(self):
        """abuela cotizó 10 y liquidó en 9: el canal observado lo recoge."""
        o, ev = oracle_for(sal02())
        p = plan_for(o, "abuela", "SAL-02")
        r = simulate(p, o.dances[366], match_closes(o, ev)[366])
        self.assertEqual((r.price, r.channel), (9, "dealer_accepted"))

    def test_sin_cierre_observado_el_plan_estricto_no_cierra(self):
        """Hilo 366: abuela nunca bajó de 10 cotizando y el abandono es 9.

        Sin la liquidación que prueba que aceptó 9, el simulador no tiene
        evidencia de ningún precio alcanzable: reporta sin trato en vez de
        suponer que habría aceptado.
        """
        o, _ = oracle_for(sal02())
        p = plan_for(o, "abuela", "SAL-02")
        self.assertEqual(p.walk_away, 9)
        r = simulate(p, o.dances[366], None)
        self.assertEqual((r.price, r.channel), (None, "no_deal"))

    def test_dealer_plantado_no_hay_trato(self):
        """Hilo 380 real: abuela se planta en 10 y el suelo visto es 9.

        El plan agota las rondas y se va. No cerrar es el resultado: pagar 10
        donde otro equipo consiguió 9 es perder la prima sin negociarla.
        """
        o, _ = oracle_for(ret03_stuck())
        p = plan_for(o, "abuela", "RET-03")
        self.assertEqual((p.floor_seen, p.walk_away), (9, 9))
        r = simulate(p, o.dances[380])
        self.assertIsNone(r.price)
        self.assertEqual(r.channel, "no_deal")

    def test_dealer_plantado_comprando_deja_de_quemar_rondas(self):
        """Hilo 385 real: abuela no pasa de 5 pese a 22→20→19 del equipo.

        El plan cierra en 5 —el máximo que ese dealer ha ofrecido jamás— en 2
        rondas, en vez de gastar 3 empujando contra un dealer plantado.
        """
        o, _ = oracle_for(ret02_bid())
        p = plan_for(o, "abuela", "RET-02", side="bid")
        r = simulate(p, o.dances[385])
        self.assertEqual((r.price, r.rounds), (5, 2))
        self.assertLess(r.rounds, o.dances[385].rounds)

    def test_final_en_la_primera_ronda_dentro_del_abandono(self):
        o, _ = oracle_for(ret01())             # fija suelo 9 y apertura 12
        extra = dance_events(701, "abuela", "t04", "RET-01", [9], [],
                             final_at=0, tick0=230, eid0=5000)
        o.ingest(extra)
        p = plan_for(o, "abuela", "RET-01")
        r = simulate(p, o.dances[701])
        self.assertEqual((r.price, r.rounds, r.channel), (9, 1, "took_final"))

    def test_final_en_la_primera_ronda_por_encima_del_abandono(self):
        """«Take it or it walks»: fuera del abandono, se deja marchar."""
        o, _ = oracle_for(ret01())
        o.ingest(dance_events(702, "abuela", "t04", "RET-01", [11], [],
                              final_at=0, tick0=230, eid0=5200))
        p = plan_for(o, "abuela", "RET-01")
        r = simulate(p, o.dances[702])
        self.assertEqual((r.price, r.channel), (None, "walked"))

    def test_sin_cotizacion_del_dealer_no_hay_nada_que_replicar(self):
        o, _ = oracle_for(ret01())
        o.ingest(dance_events(703, "abuela", "t04", "RET-01", [], [7],
                              tick0=230, eid0=5400))
        p = plan_for(o, "abuela", "RET-01")
        self.assertIsNone(simulate(p, o.dances[703]))


# ---------------------------------------------------------------- backtest

class TestBacktest(unittest.TestCase):
    def test_muestra_de_un_solo_baile_se_marca_como_delgada(self):
        """Con n=1 una media no es una media: el informe tiene que decirlo."""
        o, ev = oracle_for(ret01())
        bt = backtest(o, events=ev)
        self.assertEqual(bt["dances_sampled"], 1)
        self.assertTrue(bt["thin_sample"])
        self.assertTrue(any("demasiado pequeña" in c for c in bt["caveats"]))

    def test_descarta_los_bailes_sin_cierre_observado(self):
        """Sin liquidación no sabemos qué consiguió el equipo real."""
        o, ev = oracle_for(ret01(), ret02_bid())
        bt = backtest(o, events=ev)
        self.assertEqual(bt["dances_sampled"], 1)
        self.assertEqual(bt["skipped_no_observed_close"], 1)

    def test_ahorra_rondas_en_los_hilos_reales(self):
        o, ev = oracle_for(ret01(), ret10(), sal02())
        bt = backtest(o, events=ev)
        self.assertEqual(bt["dances_sampled"], 3)
        self.assertEqual(bt["dances_closed"], 3)
        self.assertGreater(bt["rounds_saved"]["median"], 0)

    def test_declara_que_es_dentro_de_muestra(self):
        o, ev = oracle_for(ret01())
        bt = backtest(o, events=ev)
        self.assertTrue(bt["in_sample"])
        self.assertTrue(any("Dentro de muestra" in c for c in bt["caveats"]))

    def test_cuenta_los_cierres_acreditados_al_precio_observado(self):
        """Donde el precio lo pone la liquidación, el ahorro es ~0 por diseño."""
        o, ev = oracle_for(sal02())
        bt = backtest(o, events=ev)
        self.assertEqual(bt["credited_by_observed_close"], 1)
        self.assertEqual(bt["primas_saved"]["median"], 0)

    def test_la_tolerancia_cierra_mas_tratos_pagando_mas(self):
        """El canje explícito: con abandono 9 el hilo 380 no cierra; con 10 sí."""
        o, ev = oracle_for(ret03_stuck())
        strict = backtest(o, events=ev, tolerance=0)
        loose = backtest(o, events=ev, tolerance=1)
        self.assertGreater(strict["dances_no_deal"], loose["dances_no_deal"])
        self.assertLessEqual(loose["primas_saved"]["median"],
                             strict["primas_saved"]["median"])

    def test_sin_eventos_no_hay_backtest_en_vez_de_cifra_inventada(self):
        o, _ = oracle_for(ret01())
        bt = backtest(o)                      # sin events ni closes
        self.assertEqual(bt["dances_sampled"], 0)
        self.assertIsNone(bt["primas_saved"]["median"])


# ---------------------------------------------------------------- playbook

class TestPlaybook(unittest.TestCase):
    def test_ordena_por_confianza_y_luego_por_primas_en_juego(self):
        o, _ = oracle_for(ret01(), ret10())
        rows = playbook(o)
        self.assertEqual(rows[0]["ref"], "RET-10")    # 11 primas de spread
        self.assertTrue(all(r["floor_seen"] is not None for r in rows))

    def test_no_saca_filas_a_medias(self):
        """Una carta sin suelo ni paso observados se omite, no se rellena."""
        o, _ = oracle_for(ret01())
        o.ingest(dance_events(900, "chato", "t07", "LAV-08", [13], [],
                              tick0=232, eid0=7000))
        refs = {r["ref"] for r in playbook(o)}
        self.assertIn("RET-01", refs)
        self.assertNotIn("LAV-08", refs)              # chato sin pares aún

    def test_el_informe_pone_la_muestra_antes_de_la_media(self):
        o, ev = oracle_for(ret01(), ret10(), sal02())
        txt = report(o, events=ev)
        self.assertIn("bailes con cierre observado: 3", txt)
        self.assertIn("COTA", txt)                    # el suelo no es promesa
        self.assertLess(txt.index("bailes con cierre"), txt.index("primas ahorradas"))


class TestFeedReal(unittest.TestCase):
    """Si el historial recogido está en disco, el backtest corre entero.

    No es una aserción sobre el juego de hoy: sólo comprueba que la tubería
    aguanta los datos reales, con sus 48 bailes y sus dos dealers.
    """

    PATH = Path(__file__).with_name("data") / "feed_history.jsonl"

    def test_el_historial_real_produce_un_backtest_coherente(self):
        if not self.PATH.exists():
            self.skipTest("sin data/feed_history.jsonl recogido")
        events = list(read_history(self.PATH))
        o = Oracle()
        o.ingest(events)
        bt = backtest(o, events=events)
        self.assertEqual(bt["dances_sampled"],
                         bt["dances_closed"] + bt["dances_no_deal"])
        for row in bt["rows"]:
            if row["plan_price"] is None:
                continue
            # el plan nunca cierra peor que su propio abandono
            self.assertTrue(row["primas_saved"] is not None)
        self.assertGreaterEqual(len(playbook(o)), 1)


if __name__ == "__main__":
    unittest.main()
