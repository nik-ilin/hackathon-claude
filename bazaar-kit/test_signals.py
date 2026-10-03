"""Pruebas de `signals.py` con los esquemas reales del feed.

Los payloads están copiados de eventos observados en `data/feed_history.jsonl`
(ticks 192-254), no inventados: si el servidor cambia una forma, estas pruebas
son el sitio donde se nota.

Los cuatro escenarios que importan, porque son los que distinguen "rechazo" de
"no se sabe": oferta publicada y cancelada, publicada y liquidada, publicada y
desaparecida sin evento, y una comisión anunciada que luego se aplica.
"""

import unittest

import signals
from signals import CANCELLED, OPEN, SETTLED, UNKNOWN, Signals, from_events

_ID = [10000]


def _ev(type_, tick, payload, scope="public", actor=""):
    _ID[0] += 1
    return {"id": _ID[0], "tick": tick, "t": tick / 100.0, "type": type_,
            "scope": scope, "actor": actor, "payload": payload}


def card(asset, ref="MAL-01", rarity="common", frm=None, to=None):
    c = {"id": asset, "kind": "card", "ref": ref, "serial": 22,
         "rarity": rarity, "set": ref.split("-")[0],
         "print_run": 300 if rarity == "common" else 90}
    if frm is not None:
        c["frm"], c["to"] = frm, to
    return c


def listed(oid, tick, maker, price, asset, *, ref="MAL-01", rarity="common",
           venue="rastro", expires=None, to=None):
    """`offer.listed` de una carta por efectivo: el caso limpio del tablón."""
    return _ev("offer.listed", tick, {"venue": venue, "offer": {
        "id": oid, "maker": maker, "to": to, "venue": venue, "thread": None,
        "status": "open",
        "give": {"cash": 0, "assets": [card(asset, ref, rarity)], "types": []},
        "want": {"cash": price, "assets": [], "types": []},
        "expires_tick": tick + 20 if expires is None else expires,
        "created_tick": tick, "final": False}}, actor=maker)


def bid(oid, tick, maker, cash, ref, *, venue="rastro", expires=None):
    """Puja de compra: efectivo por una referencia."""
    return _ev("offer.listed", tick, {"venue": venue, "offer": {
        "id": oid, "maker": maker, "to": None, "venue": venue, "thread": None,
        "status": "open",
        "give": {"cash": cash, "assets": [], "types": []},
        "want": {"cash": 0, "assets": [], "types": [f"card:{ref}"]},
        "expires_tick": tick + 10 if expires is None else expires,
        "created_tick": tick, "final": False}}, actor=maker)


def cancelled(oid, tick, venue="rastro"):
    return _ev("offer.cancelled", tick, {"offer": oid, "venue": venue})


def settled(sid, tick, price, asset, frm, to, *, ref="MAL-01",
            rarity="common", venue="rastro", fee=0, persona=None):
    return _ev("settlement", tick, {
        "settlement": sid, "tick": tick, "kind": "trade", "parties": [frm, to],
        "venue": venue, "persona": persona, "fee": fee,
        "items": [dict(card(asset, ref, rarity, frm=frm, to=to),
                       name="Vinilo de la Movida")],
        "price": price})


class TestCicloDeOferta(unittest.TestCase):
    """Publicada y cancelada, publicada y liquidada, publicada y desaparecida."""

    def test_cancelada_es_rechazo_observado(self):
        s = from_events([listed(3100, 192, "t04", 7, 500),
                         cancelled(3100, 195),
                         _ev("schedule.fired", 200, {"action": "bench"})])
        o = s.offers[3100]
        self.assertEqual(o.outcome, CANCELLED)
        self.assertEqual(o.outcome_basis, signals.OBSERVED)
        self.assertTrue(o.rejected)
        self.assertFalse(o.accepted)

    def test_liquidada_al_precio_pedido_es_aceptacion_inferida(self):
        s = from_events([listed(3101, 193, "t04", 6, 378),
                         settled(342, 195, 6, 378, "t04", "t02", fee=2)])
        o = s.offers[3101]
        self.assertEqual(o.outcome, SETTLED)
        # El feed no dice qué oferta cerró la liquidación: el enlace es inferido.
        self.assertEqual(o.outcome_basis, signals.INFERRED)
        self.assertTrue(o.accepted)
        self.assertEqual(o.settled_price, 6)

    def test_liquidada_por_debajo_no_valida_el_precio_pedido(self):
        s = from_events([listed(3102, 193, "t04", 12, 379),
                         settled(343, 196, 7, 379, "t04", "t02")])
        o = s.offers[3102]
        self.assertEqual(o.outcome, SETTLED)
        self.assertFalse(o.accepted)
        self.assertTrue(o.rejected)   # 12 P no se aceptó: se cerró a 7

    def test_desaparecida_sin_evento_no_es_rechazo(self):
        # Caduca en 200 y lo último observado es 230: ya no está, pero nadie
        # dijo por qué. No puede contar como precio rechazado.
        s = from_events([listed(3103, 192, "t04", 9, 501, expires=200),
                         _ev("schedule.fired", 230, {"action": "bench"})])
        o = s.offers[3103]
        self.assertEqual(o.outcome, UNKNOWN)
        self.assertFalse(o.rejected)
        self.assertFalse(o.resolved)
        self.assertEqual(s.elasticity()[0].n, 0)
        self.assertIsNone(s.elasticity()[0].sell_rate)

    def test_sigue_viva_si_caduca_despues_de_lo_observado(self):
        s = from_events([listed(3104, 192, "t04", 9, 502, expires=300)])
        self.assertEqual(s.offers[3104].outcome, OPEN)

    def test_la_liquidacion_manda_sobre_la_cancelacion(self):
        # Si el activo se movió, el precio se probó: eso pesa más que el aviso.
        s = from_events([listed(3105, 192, "t04", 8, 503),
                         settled(344, 194, 8, 503, "t04", "t02"),
                         cancelled(3105, 195)])
        self.assertEqual(s.offers[3105].outcome, SETTLED)

    def test_no_se_enlaza_una_liquidacion_de_otro_vendedor(self):
        s = from_events([listed(3106, 192, "t04", 8, 504),
                         settled(345, 194, 8, 504, "t13", "t02")])
        self.assertEqual(s.offers[3106].outcome, OPEN)

    def test_no_se_enlaza_fuera_de_la_ventana(self):
        s = from_events([listed(3107, 192, "t04", 8, 505, expires=195),
                         settled(346, 220, 8, 505, "t04", "t02")])
        self.assertNotEqual(s.offers[3107].outcome, SETTLED)

    def test_los_lotes_y_trueques_se_omiten(self):
        swap = _ev("offer.listed", 192, {"venue": "rastro", "offer": {
            "id": 3108, "maker": "t04", "to": None, "venue": "rastro",
            "thread": None, "status": "open",
            "give": {"cash": 0, "assets": [card(1), card(2)], "types": []},
            "want": {"cash": 0, "assets": [], "types": ["card:RET-09"]},
            "expires_tick": 212, "created_tick": 192, "final": False}})
        s = from_events([swap])
        self.assertNotIn(3108, s.offers)

    def test_idempotente(self):
        evs = [listed(3109, 192, "t04", 7, 506), cancelled(3109, 195)]
        s = Signals()
        self.assertEqual(s.ingest(evs), 2)
        self.assertEqual(s.ingest(evs), 0)
        self.assertEqual(len(s.offers), 1)


class TestElasticidad(unittest.TestCase):
    def setUp(self):
        # A 12 P: una vendida, dos canceladas. A 8 P: dos vendidas, una
        # cancelada. Más una desaparecida, que no debe tocar ninguna tasa.
        evs = [
            listed(3200, 192, "t04", 12, 601), settled(400, 193, 12, 601, "t04", "t02"),
            listed(3201, 192, "t06", 12, 602), cancelled(3201, 194),
            listed(3202, 192, "t13", 12, 603), cancelled(3202, 194),
            listed(3203, 192, "t04", 8, 604), settled(401, 193, 8, 604, "t04", "t02"),
            listed(3204, 192, "t06", 8, 605), settled(402, 193, 8, 605, "t06", "t02"),
            listed(3205, 192, "t13", 8, 606), cancelled(3205, 194),
            listed(3206, 192, "t13", 8, 607, expires=200),
            _ev("schedule.fired", 230, {"action": "bench"}),
        ]
        self.s = from_events(evs)

    def test_niveles_con_su_n(self):
        lv = {l.price: l for l in self.s.elasticity(rarity="common")}
        self.assertEqual((lv[12].accepted, lv[12].n), (1, 3))
        self.assertEqual((lv[8].accepted, lv[8].n), (2, 3))
        self.assertEqual(lv[8].unknown, 1)       # la desaparecida, aparte
        self.assertAlmostEqual(lv[8].sell_rate, 2 / 3)

    def test_cartas_distintas_detras_del_nivel(self):
        # Un equipo que republica la misma carta infla `n`; esto lo delata.
        s = from_events([listed(3210, 192, "t03", 6, 700), cancelled(3210, 193),
                         listed(3211, 194, "t03", 6, 700), cancelled(3211, 195)])
        lv = s.elasticity()[0]
        self.assertEqual((lv.n, lv.distinct_assets), (2, 1))

    def test_odds_acumula_hacia_abajo(self):
        rate, n = self.s.sell_odds(12, rarity="common")
        self.assertEqual(n, 6)
        self.assertAlmostEqual(rate, 3 / 6)
        rate8, n8 = self.s.sell_odds(8, rarity="common")
        self.assertEqual(n8, 3)
        self.assertAlmostEqual(rate8, 2 / 3)

    def test_sin_muestra_no_hay_cifra(self):
        self.assertIsNone(self.s.sell_odds(3, rarity="common"))
        self.assertIsNone(self.s.sell_odds(12, rarity="legendary"))
        self.assertEqual(self.s.elasticity(ref="NO-99"), [])

    def test_price_for_odds_exige_muestra(self):
        self.assertEqual(self.s.price_for_odds(0.6, rarity="common", min_n=3), 8)
        # Con n=3 por nivel, pedir 10 desenlaces no puede devolver precio.
        self.assertIsNone(self.s.price_for_odds(0.6, rarity="common", min_n=10))

    def test_banda_de_rechazo(self):
        b = self.s.rejection_band(rarity="common")
        self.assertEqual(b["max_accepted"], 12)
        self.assertEqual(b["min_rejected"], 8)
        self.assertIsNone(self.s.rejection_band(rarity="epic"))

    def test_elasticidad_de_compra_separada_y_mas_debil(self):
        s = from_events([bid(3300, 192, "t16", 30, "SAL-10"),
                         settled(410, 195, 25, 800, "t02", "t16", ref="SAL-10"),
                         bid(3301, 192, "t15", 5, "RET-05"),
                         cancelled(3301, 196)])
        self.assertEqual(s.offers[3300].outcome, SETTLED)
        self.assertTrue(s.offers[3300].accepted)   # compró dentro del límite
        self.assertEqual(s.offers[3301].outcome, CANCELLED)
        self.assertEqual(s.outcome_counts("bid"), {SETTLED: 1, CANCELLED: 1})
        self.assertEqual(s.elasticity(side="bid")[0].price, 5)


class TestFracasos(unittest.TestCase):
    def test_motivos_por_dealer(self):
        evs = [
            _ev("thread.opened", 192, {"thread": 311, "kind": "persona",
                                       "team": "t07", "with": "chato",
                                       "topic": {"buy": {"card": "RET-09"}}}),
            _ev("thread.closed", 200, {"thread": 311, "kind": "persona",
                                       "team": "t07", "with": "chato",
                                       "reason": "idle"}),
            _ev("thread.opened", 201, {"thread": 312, "kind": "persona",
                                       "team": "t05", "with": "chato",
                                       "topic": {"buy": {"card": "RET-10"}}}),
            _ev("thread.closed", 205, {"thread": 312, "kind": "persona",
                                       "team": "t05", "with": "chato",
                                       "reason": "persona_quota"}),
            _ev("thread.opened", 202, {"thread": 313, "kind": "persona",
                                       "team": "t15", "with": "abuela",
                                       "topic": {"buy": {"card": "RET-06"}}}),
        ]
        s = from_events(evs)
        f = s.failures()
        self.assertEqual(f["chato"].reasons, {"idle": 1, "persona_quota": 1})
        self.assertEqual(f["chato"].top_reason[1], 1)
        self.assertEqual(f["abuela"].open_without_close, 1)
        self.assertEqual(f["abuela"].reasons, {})
        self.assertEqual(s.teams_hitting("persona_quota"), ["t05"])
        self.assertEqual(s.failure_reasons()["idle"], 1)

    def test_cierre_sin_apertura_vista(self):
        # El feed guarda 500 eventos: el cierre puede llegar huérfano.
        s = from_events([_ev("thread.closed", 207, {
            "thread": 324, "kind": "persona", "team": "t01",
            "with": "abuela", "reason": "cooloff"})])
        self.assertEqual(s.failures()["abuela"].reasons, {"cooloff": 1})
        self.assertEqual(s.failures()["abuela"].open_without_close, 0)

    def test_liquidacion_con_el_dealer_se_marca_como_inferida(self):
        evs = [_ev("thread.opened", 192, {"thread": 400, "kind": "persona",
                                          "team": "t13", "with": "abuela",
                                          "topic": {"sell": {"assets": [604]}}}),
               settled(338, 194, 5, 602, "t13", "abuela", persona="abuela",
                       venue=None)]
        s = from_events(evs)
        self.assertEqual(s.failures()["abuela"].settled_after_open, 1)


class TestSobres(unittest.TestCase):
    def test_muestra_insuficiente_se_declara(self):
        pack_sale = _ev("settlement", 223, {
            "settlement": 370, "tick": 223, "kind": "trade",
            "parties": ["t08", "abuela"], "venue": None, "persona": "abuela",
            "fee": 0, "price": 21,
            "items": [{"id": 900, "kind": "pack", "ref": "sobre_barrio",
                       "frm": "abuela", "to": "t08"}]})
        evs = [pack_sale,
               _ev("pack.opened", 223, {"team": "t08", "name": "Team 8",
                                        "pack": "sobre_barrio", "best": None}),
               _ev("pack.opened", 248, {"team": "t15", "name": "Team 15",
                                        "pack": "sobre_bienvenida",
                                        "best": {"id": 648, "kind": "card",
                                                 "ref": "LAV-10", "serial": 6,
                                                 "rarity": "rare",
                                                 "set": "LAV",
                                                 "print_run": 30}})]
        s = from_events(evs)
        st = s.pack_economics()
        self.assertEqual(st["sobre_barrio"].prices_paid, [21])
        self.assertFalse(st["sobre_barrio"].enough_sample)
        self.assertEqual(st["sobre_bienvenida"].best_rarities, {"rare": 1})
        v = s.pack_verdict("sobre_barrio")
        self.assertIsNone(v["verdict"])          # sin muestra, sin veredicto
        self.assertIn("aperturas", v["why"])

    def test_llamar_dos_veces_no_duplica_precios(self):
        s = from_events([_ev("settlement", 223, {
            "settlement": 371, "tick": 223, "kind": "trade",
            "parties": ["t08", "abuela"], "venue": None, "persona": "abuela",
            "fee": 0, "price": 21,
            "items": [{"id": 901, "kind": "pack", "ref": "sobre_barrio"}]})])
        s.pack_economics()
        self.assertEqual(s.pack_economics()["sobre_barrio"].prices_paid, [21])

    def test_sobre_desconocido(self):
        s = Signals()
        self.assertEqual(s.pack_verdict("sobre_barrio")["n"], 0)


class TestComisiones(unittest.TestCase):
    def _abierto(self, tick=201, venue="v10", bps=300):
        return _ev("venue.opened", tick, {
            "venue": venue, "name": "Puesto de Team 5", "owner": "t05",
            "fee_bps": bps, "fee_per_card": 0, "rules": {"mechanism": "auto"},
            "bond": 0, "starter": True})

    def test_anuncio_y_luego_aplicacion(self):
        evs = [self._abierto(),
               _ev("venue.fee_announced", 226, {"venue": "v10", "fee_bps": 0,
                                                "fee_per_card": 0,
                                                "effective_tick": 230})]
        s = from_events(evs)
        v = s.fee_state()["v10"]
        self.assertEqual(v.fee_bps, 300)              # todavía la vieja
        self.assertEqual(len(v.pending), 1)
        # En el tick de efecto, decidir con la comisión vieja está bloqueado.
        self.assertEqual([b["venue"] for b in s.fee_blocked(230)], ["v10"])
        self.assertEqual(s.fee_blocked(228), [])

        s.ingest([_ev("venue.fee_changed", 230, {"venue": "v10", "fee_bps": 0,
                                                 "fee_per_card": 0})])
        v = s.fee_state()["v10"]
        self.assertEqual((v.fee_bps, v.as_of_tick), (0, 230))
        self.assertEqual(v.pending, [])               # el anuncio ya se aplicó
        self.assertEqual(s.fee_blocked(240), [])

    def test_anuncios_repetidos_marcan_el_venue_como_dudoso(self):
        evs = [self._abierto(venue="v03", bps=100)]
        for a, e in ((233, 237), (237, 241), (239, 243), (247, 251)):
            evs.append(_ev("venue.fee_announced", a, {
                "venue": "v03", "fee_bps": 0, "fee_per_card": 0,
                "effective_tick": e}))
            evs.append(_ev("venue.fee_changed", e, {
                "venue": "v03", "fee_bps": 0, "fee_per_card": 0}))
        s = from_events(evs)
        b = s.fee_blocked()
        self.assertEqual([x["venue"] for x in b], ["v03"])
        self.assertTrue(b[0]["announcement_churn"])
        self.assertFalse(
            next(r for r in s.cheapest_venues() if r["venue"] == "v03")["trust"])

    def test_comision_cobrada_frente_a_declarada(self):
        # El Rastro cobra 5 % + 1 P por carta: sobre 6 P el efectivo es ~33 %.
        s = from_events([settled(342, 195, 6, 378, "t04", "t02", fee=2),
                         settled(352, 205, 12, 598, "t06", "t02", fee=2)])
        v = s.fee_state()["rastro"]
        self.assertEqual(len(v.charged), 2)
        self.assertAlmostEqual(v.effective_bps, 10000 * 4 / 18, places=3)
        row = next(r for r in s.cheapest_venues() if r["venue"] == "rastro")
        self.assertEqual(row["fee_basis"], "cobrada")
        self.assertFalse(row["trust"])       # nunca se vio su `fee_bps`

    def test_un_venue_cerrado_sale_de_la_lista(self):
        s = from_events([self._abierto(venue="v08", bps=300),
                         _ev("venue.closed", 262, {"venue": "v08",
                                                   "refund": 0,
                                                   "replaced": True})])
        self.assertEqual(s.fee_state()["v08"].closed_tick, 262)
        self.assertEqual([r["venue"] for r in s.cheapest_venues()], [])
        self.assertFalse(s.cheapest_venues(include_closed=True)[0]["trust"])
        self.assertIn("CERRADO", s.report())

    def test_el_orden_de_llegada_no_pisa_la_comision_vigente(self):
        # Ingerir desordenado no debe dejar la comisión vieja como vigente.
        evs = [_ev("venue.fee_changed", 230, {"venue": "v10", "fee_bps": 0,
                                              "fee_per_card": 0}),
               self._abierto()]
        s = from_events(list(reversed(evs)))
        self.assertEqual(s.fee_state()["v10"].fee_bps, 0)


class TestDuelos(unittest.TestCase):
    def test_tasa_de_no_deal_con_su_n(self):
        evs = [_ev("duel.closed", 192, {"duel": 305, "session": 1,
                                        "status": "no_deal",
                                        "item": "Mercado de Vallehermoso"}),
               _ev("duel.closed", 192, {"duel": 306, "session": 1,
                                        "status": "no_deal",
                                        "item": "Mercado de Vallehermoso"}),
               _ev("duel.closed", 193, {"duel": 307, "session": 1,
                                        "status": "deal",
                                        "item": "Mercado de Vallehermoso"}),
               _ev("duels.finished", 193, {"session": 1,
                                           "name": "Practice duels"})]
        s = from_events(evs)
        d = s.duel_stats()[1]
        self.assertTrue(d.finished)
        self.assertEqual((d.closed, d.no_deal), (3, 2))
        self.assertAlmostEqual(d.no_deal_rate, 2 / 3)
        self.assertEqual(d.as_dict()["name"], "Practice duels")
        rate, n = s.no_deal_rate()
        self.assertEqual(n, 3)
        self.assertAlmostEqual(rate, 2 / 3)

    def test_sin_duelos_no_hay_tasa(self):
        self.assertIsNone(Signals().no_deal_rate())


class TestBanco(unittest.TestCase):
    def test_ventana_y_venues_callados(self):
        evs = [_ev("bench.started", 201, {
            "session": 1, "name": "The Market Test",
            "venues": ["v01", "v02", "v03"], "ticks": 16, "start_tick": 201}),
            settled(351, 203, 7, 449, "t13", "t15", venue="v02"),
            settled(352, 250, 7, 450, "t13", "t15", venue="v03")]
        s = from_events(evs)
        b = s.bench_runs()[1]
        self.assertEqual((b.start_tick, b.end_tick), (201, 217))
        self.assertEqual(b.active_venues, ["v02"])        # v03 cerró fuera
        self.assertEqual(len(b.silent_venues), 2)
        self.assertEqual(b.as_dict()["basis_activity"], signals.INFERRED)

    def test_una_liquidacion_fuera_de_los_venues_del_banco_no_cuenta(self):
        s = from_events([_ev("bench.started", 201, {
            "session": 1, "name": "x", "venues": ["v01"], "ticks": 4,
            "start_tick": 201}),
            settled(353, 203, 7, 451, "t13", "t15", venue="rastro")])
        self.assertEqual(s.bench_runs()[1].active_venues, [])


class TestCoberturaEInforme(unittest.TestCase):
    def test_cobertura_cuenta_lo_que_el_oraculo_ignora(self):
        evs = [listed(3900, 192, "t04", 7, 950), cancelled(3900, 195),
               _ev("thread.message", 193, {"thread": 1}),
               _ev("venue.opened", 201, {"venue": "v08", "fee_bps": 300,
                                         "fee_per_card": 0})]
        c = from_events(evs).coverage
        self.assertEqual(c["events"], 4)
        self.assertEqual(c["ignored_by_oracle"], 2)   # cancelled + venue.opened
        self.assertEqual((c["tick_min"], c["tick_max"]), (192, 201))
        self.assertEqual(c["cancels"], 1)

    def test_el_informe_dice_cuando_no_hay_muestra(self):
        txt = Signals().report()
        self.assertIn("no hay curva", txt)
        self.assertIn("motivos desconocidos", txt)
        self.assertIn("ningún `duel.closed`", txt)

    def test_el_informe_sale_con_datos_reales(self):
        s = from_events([listed(3901, 192, "t04", 7, 960), cancelled(3901, 195),
                         listed(3902, 192, "t04", 7, 961),
                         settled(420, 194, 7, 961, "t04", "t02", fee=2)])
        txt = s.report()
        self.assertIn("1/2 vendidas", txt)


if __name__ == "__main__":
    unittest.main()
