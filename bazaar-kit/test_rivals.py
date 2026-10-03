"""Pruebas de `rivals`. Escenarios locales, sin red.

Los esquemas de evento están copiados del feed real (ticks 192-240), incluidos
los campos que `feed_oracle` no usa: `expires_tick`, `thread.opened` con tema
y `offer.cancelled`, que sólo trae el ID de oferta.
"""

import unittest

from feed_oracle import Oracle
from rivals import EV_PUBLISHED, EV_SETTLED, EV_TOPIC, PAGE_BONUS, Rivals

# --------------------------------------------------------------- fixtures

#: Mismo reparto de rarezas que el juego: 5 comunes, 3 infrecuentes, 2 raras
#: (la página), más épica y legendaria fuera de ella.
_PLAN = [("common", 10, 300)] * 5 + [("uncommon", 22, 90)] * 3 + \
        [("rare", 86, 30)] * 2 + [("epic", 300, 9), ("legendary", 900, 3)]


def catalog(*sets: str) -> dict:
    out = []
    for sid in sets or ("LAV", "RET"):
        cards = []
        for i, (rar, book, run) in enumerate(_PLAN, start=1):
            cards.append({"id": f"{sid}-{i:02d}", "rarity": rar, "book": book,
                          "print_run": run, "minted": 20,
                          "page": rar in ("common", "uncommon", "rare")})
        out.append({"id": sid, "cards": cards})
    return {"sets": out}


PAGE_SUM = 5 * 10 + 3 * 22 + 2 * 86          # 288: la página de un set
BONUS = PAGE_SUM * PAGE_BONUS                # 72.0 en unidades de book


def asset(aid, ref, rarity="common"):
    return {"id": aid, "kind": "card", "ref": ref, "serial": 7, "rarity": rarity,
            "set": ref[:3], "print_run": 300}


def listed(eid, tick, team, ref, cash, *, aid=None, bid=False, venue="rastro",
           swap_for=None, expires=None):
    """Publicación en un venue. `bid`: ofrece efectivo. `swap_for`: trueque."""
    if bid:
        give = {"cash": cash, "assets": [], "types": []}
        want = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
    elif swap_for:
        give = {"cash": 0, "assets": [asset(aid or 500 + eid, ref)], "types": []}
        want = {"cash": 0, "assets": [], "types": [f"card:{swap_for}"]}
    else:
        give = {"cash": 0, "assets": [asset(aid or 500 + eid, ref)], "types": []}
        want = {"cash": cash, "assets": [], "types": []}
    return {"id": eid, "tick": tick, "type": "offer.listed",
            "payload": {"venue": venue,
                        "offer": {"id": 3000 + eid, "maker": team, "to": None,
                                  "venue": venue, "thread": None, "status": "open",
                                  "give": give, "want": want, "final": False,
                                  "created_tick": tick, "expires_tick": expires}}}


def cancelled(eid, tick, offer_id, venue="rastro"):
    return {"id": eid, "tick": tick, "type": "offer.cancelled",
            "payload": {"offer": offer_id, "venue": venue}}


def settled(eid, tick, frm, to, ref, price, *, aid=None, rarity="common",
            venue="rastro", kind="trade", extra=None):
    items = [dict(asset(aid or 600 + eid, ref, rarity), frm=frm, to=to, name="X")]
    if extra:
        items.append(dict(asset(extra, ref, rarity), frm=frm, to=to, name="X"))
    return {"id": eid, "tick": tick, "type": "settlement",
            "payload": {"settlement": eid, "tick": tick, "kind": kind,
                        "parties": [frm, to], "venue": venue, "persona": None,
                        "fee": 2, "items": items, "price": price}}


def opened(eid, tick, team, dealer="abuela", *, buy=None, sell=None, thread=1):
    topic = {"buy": {"card": buy}} if buy else {"sell": {"assets": sell or []}}
    return {"id": eid, "tick": tick, "type": "thread.opened",
            "payload": {"thread": thread, "kind": "persona", "team": team,
                        "with": dealer, "topic": topic}}


def team_msg(eid, tick, team, ref, cash, *, dealer="abuela", thread=1,
             selling=False, aid=None):
    """Un equipo cotiza a un dealer. `selling`: le ofrece su carta."""
    if selling:
        give = {"cash": 0, "assets": [asset(aid or 700 + eid, ref)], "types": []}
        want = {"cash": cash, "assets": [], "types": []}
    else:
        give = {"cash": cash, "assets": [], "types": []}
        want = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
    return {"id": eid, "tick": tick, "type": "thread.message",
            "payload": {"thread": thread, "kind": "persona", "message": eid,
                        "sender": team, "text": "...", "team": team, "with": dealer,
                        "offer": {"id": 4000 + eid, "maker": team, "to": dealer,
                                  "venue": None, "thread": thread, "status": "open",
                                  "give": give, "want": want, "final": False}}}


def dealer_msg(eid, tick, dealer, team, ref, cash, *, thread=9):
    """Un dealer cotiza vendiendo una referencia. Igual que en el feed real."""
    return {"id": eid, "tick": tick, "type": "thread.message",
            "payload": {"thread": thread, "kind": "persona", "message": eid,
                        "sender": dealer, "text": "...", "team": team,
                        "with": dealer,
                        "offer": {"id": 5000 + eid, "maker": dealer, "to": team,
                                  "venue": None, "thread": thread,
                                  "status": "open", "final": False,
                                  "give": {"cash": 0, "assets": [],
                                           "types": [f"card:{ref}"]},
                                  "want": {"cash": cash, "assets": [],
                                           "types": []}}}}


def fresh(*events, cat=True):
    r = Rivals()
    if cat:
        r.load_catalog(catalog())
    r.ingest(events)
    return r


# ------------------------------------------------------------------ lectura

class TestPosesion(unittest.TestCase):
    def test_liquidacion_entre_equipos_mueve_la_carta(self):
        r = fresh(settled(1, 205, "t06", "t02", "RET-02", 12, aid=901))
        self.assertEqual(r.view("t02").held_refs, ["RET-02"])
        self.assertEqual(r.view("t06").held_refs, [])
        self.assertEqual(r.view("t06").released_refs, ["RET-02"])
        self.assertEqual(r.view("t02").held["RET-02"].evidence, EV_SETTLED)

    def test_comprarle_a_un_dealer_tambien_es_posesion(self):
        """`feed_oracle` manda esto a la línea del dealer; aquí es una mano."""
        r = fresh(settled(1, 194, "abuela", "t12", "RET-05", 9))
        self.assertEqual(r.view("t12").held_refs, ["RET-05"])
        self.assertNotIn("abuela", r.teams)      # un dealer no es un equipo

    def test_venderle_a_un_dealer_prueba_que_la_tuvo(self):
        r = fresh(settled(1, 194, "t13", "abuela", "LAV-04", 5))
        v = r.view("t13")
        self.assertEqual(v.seen_ever_refs, ["LAV-04"])
        self.assertEqual(v.held_refs, [])

    def test_match_de_venue_cuenta_igual(self):
        r = fresh(settled(1, 234, "t13", "t15", "RET-02", 10, kind="match",
                          venue="v02"))
        self.assertEqual(r.view("t15").held_refs, ["RET-02"])

    def test_publicar_una_venta_es_posesion_y_duplicado_ofrecido(self):
        r = fresh(listed(1, 192, "t12", "LAV-02", 10))
        v = r.view("t12")
        self.assertEqual(v.held_refs, ["LAV-02"])
        self.assertEqual(v.held["LAV-02"].evidence, EV_PUBLISHED)
        self.assertEqual(v.duplicates_offered(), ["LAV-02"])

    def test_oferta_a_un_dealer_con_carta_propia_es_posesion(self):
        r = fresh(team_msg(1, 200, "t13", "LAV-07", 26, selling=True))
        self.assertEqual(r.view("t13").held_refs, ["LAV-07"])

    def test_dos_copias_distintas_son_duplicado_duro(self):
        r = fresh(listed(1, 192, "t03", "LAV-01", 10, aid=11),
                  listed(2, 193, "t03", "LAV-01", 9, aid=12))
        v = r.view("t03")
        self.assertEqual(v.duplicates_observed, ["LAV-01"])
        self.assertEqual(v.held["LAV-01"].copies_seen, 2)

    def test_una_sola_copia_vista_no_es_duplicado(self):
        r = fresh(listed(1, 192, "t03", "LAV-01", 10, aid=11),
                  listed(2, 193, "t03", "LAV-01", 9, aid=11))   # la misma copia
        self.assertEqual(r.view("t03").duplicates_observed, [])

    def test_vender_y_recomprar_la_misma_referencia(self):
        """Caso real (t02 con RET-07): soltarla no la borra para siempre."""
        r = fresh(settled(1, 234, "t02", "chato", "RET-07", 15, aid=55),
                  settled(2, 236, "chato", "t02", "RET-07", 24, aid=56))
        v = r.view("t02")
        self.assertEqual(v.held_refs, ["RET-07"])
        self.assertEqual(v.released_refs, [])
        self.assertEqual(v.held["RET-07"].first_tick, 234)
        self.assertTrue(v.held["RET-07"].acquired)

    def test_recomprar_la_misma_copia_la_devuelve(self):
        r = fresh(settled(1, 234, "t02", "chato", "RET-07", 15, aid=55),
                  settled(2, 236, "chato", "t02", "RET-07", 24, aid=55))
        self.assertTrue(r.view("t02").held["RET-07"].likely_still_holds)

    def test_vender_las_dos_copias_la_saca_de_la_mano(self):
        r = fresh(listed(1, 192, "t08", "LAV-03", 10, aid=21),
                  listed(2, 192, "t08", "LAV-03", 10, aid=22),
                  settled(3, 195, "t08", "abuela", "LAV-03", 10, aid=21),
                  settled(4, 196, "t08", "abuela", "LAV-03", 10, aid=22))
        self.assertEqual(r.view("t08").held_refs, [])


class TestBusqueda(unittest.TestCase):
    def test_puja_declara_lo_que_le_falta(self):
        r = fresh(listed(1, 200, "t15", "RET-06", 14, bid=True))
        s = r.view("t15").sought["RET-06"]
        self.assertEqual((s.evidence, s.best_bid, s.times), (EV_PUBLISHED, 14, 1))
        self.assertEqual(r.view("t15").held_refs, [])

    def test_tema_de_conversacion_es_intencion_no_oferta(self):
        r = fresh(opened(1, 192, "t05", buy="RET-02"))
        self.assertEqual(r.view("t05").sought["RET-02"].evidence, EV_TOPIC)

    def test_trueque_dice_las_dos_cosas(self):
        r = fresh(listed(1, 200, "t14", "LAV-01", 0, swap_for="RET-09"))
        v = r.view("t14")
        self.assertEqual((v.held_refs, v.sought_refs), (["LAV-01"], ["RET-09"]))

    def test_pedir_efectivo_a_un_dealer_por_una_referencia(self):
        r = fresh(team_msg(1, 200, "t18", "RET-06", 20))
        self.assertEqual(r.view("t18").sought["RET-06"].best_bid, 20)

    def test_el_tema_de_venta_sin_referencia_conocida_no_supone_nada(self):
        r = fresh(opened(1, 192, "t07", sell=[9999]))
        self.assertEqual(r.view("t07").held_refs, [])

    def test_el_tema_de_venta_con_copia_ya_vista_si_cuenta(self):
        r = fresh(listed(1, 192, "t07", "LAV-05", 10, aid=777),
                  opened(2, 194, "t07", sell=[777]))
        self.assertEqual(r.view("t07").held_refs, ["LAV-05"])


class TestOfertasVivas(unittest.TestCase):
    def test_cancelar_retira_la_oferta_no_la_posesion(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10), cancelled(2, 193, 3001))
        v = r.view("t03")
        self.assertEqual(v.duplicates_offered(), [])
        self.assertEqual(v.held_refs, ["LAV-02"])

    def test_caducar_tambien_retira(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10, expires=196),
                  settled(9, 200, "t06", "t02", "RET-02", 12))     # avanza el tick
        self.assertEqual(r.view("t03").duplicates_offered(r.tick_max), [])


class TestIngesta(unittest.TestCase):
    def test_idempotente(self):
        ev = [listed(1, 192, "t03", "LAV-02", 10),
              settled(2, 195, "t03", "t15", "LAV-02", 11)]
        r = Rivals()
        r.load_catalog(catalog())
        self.assertEqual(r.ingest(ev), 2)
        self.assertEqual(r.ingest(ev), 0)
        self.assertEqual(r.view("t15").held["LAV-02"].sightings, 1)

    def test_orden_por_tick_aunque_llegue_desordenado(self):
        later = settled(2, 210, "chato", "t02", "RET-07", 24, aid=56)
        earlier = settled(1, 205, "t02", "chato", "RET-07", 15, aid=55)
        r = fresh(later, earlier)
        self.assertEqual(r.view("t02").held_refs, ["RET-07"])


# ----------------------------------------------------------------- huecos

class TestHuecos(unittest.TestCase):
    def test_equipo_sin_actividad_observada_no_tiene_huecos_estimables(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10))
        g = r.gaps("t09", "RET")
        self.assertEqual((g.held_refs, g.sought_refs), ([], []))
        self.assertEqual(len(g.unseen_refs), 10)      # la página entera
        self.assertEqual(g.confidence, "NONE")
        pp = r.completion_pressure("t09", "RET")
        self.assertEqual(pp.confidence, "NONE")
        self.assertIsNone(pp.extra_if_this_closes_page)
        self.assertIsNone(pp.extra_amortized)

    def test_sin_catalogo_no_hay_pagina_que_comparar(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10), cat=False)
        self.assertEqual(r.page_refs("LAV"), [])
        g = r.gaps("t03", "LAV")
        self.assertEqual((g.page_refs, g.confidence), ([], "NONE"))
        self.assertIsNone(r.completion_pressure("t03", "LAV").bonus_book)

    def test_una_sola_observacion_es_muestra_debil(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10))
        g = r.gaps("t03", "LAV")
        self.assertEqual(g.confidence, "WEAK")
        self.assertEqual(g.held_refs, ["LAV-02"])
        self.assertIn("muestra corta", g.caveat)

    def test_las_tres_listas_son_disjuntas_y_suman_la_pagina(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10),
                  listed(2, 193, "t03", "LAV-07", 14, bid=True))
        g = r.gaps("t03", "LAV")
        todo = g.held_refs + g.sought_refs + g.unseen_refs
        self.assertEqual(sorted(todo), g.page_refs)
        self.assertEqual(len(set(todo)), len(todo))
        self.assertEqual((g.missing_min, g.missing_max), (1, 9))
        self.assertEqual(g.confidence, "DECLARED")

    def test_lo_que_pide_no_cuenta_como_tenido_aunque_se_le_viera_antes(self):
        r = fresh(settled(1, 190, "t03", "abuela", "LAV-02", 9),
                  listed(2, 195, "t03", "LAV-02", 10, bid=True))
        g = r.gaps("t03", "LAV")
        self.assertEqual(g.held_refs, [])
        self.assertEqual(g.sought_refs, ["LAV-02"])

    def test_media_pagina_vista_sube_a_observed(self):
        ev = [listed(i, 192, "t03", f"LAV-{i:02d}", 10, aid=100 + i)
              for i in range(1, 5)]
        self.assertEqual(fresh(*ev).gaps("t03", "LAV").confidence, "OBSERVED")


class TestPresion(unittest.TestCase):
    def aritmetica(self):
        """t03 con 9 de las 10 de la página y pidiendo la que falta."""
        ev = [listed(i, 192, "t03", f"LAV-{i:02d}", 10, aid=100 + i)
              for i in range(1, 10)]
        ev.append(listed(20, 193, "t03", "LAV-10", 90, bid=True))
        return fresh(*ev)

    def test_bonus_y_techo_con_un_solo_hueco(self):
        pp = self.aritmetica().completion_pressure("t03", "LAV")
        self.assertEqual(pp.page_book_sum, PAGE_SUM)
        self.assertEqual(pp.bonus_book, BONUS)
        self.assertEqual(pp.extra_if_this_closes_page, BONUS)
        self.assertEqual(pp.extra_amortized, BONUS)       # un hueco: no reparte
        self.assertEqual(pp.confidence, "DECLARED")
        self.assertEqual((pp.gaps.missing_min, pp.gaps.missing_max), (1, 1))

    def test_el_bonus_se_reparte_entre_los_huecos_posibles(self):
        r = fresh(listed(1, 192, "t03", "LAV-01", 10),
                  listed(2, 193, "t03", "LAV-02", 10, aid=102))
        pp = r.completion_pressure("t03", "LAV")
        self.assertEqual(pp.gaps.missing_max, 8)
        self.assertEqual(pp.extra_amortized, round(BONUS / 8, 2))
        self.assertEqual(pp.extra_if_this_closes_page, BONUS)
        self.assertTrue(any("entre 0 y 8" in n for n in pp.notes))

    def test_pagina_entera_vista_no_tiene_presion(self):
        ev = [listed(i, 192, "t03", f"LAV-{i:02d}", 10, aid=100 + i)
              for i in range(1, 11)]
        pp = fresh(*ev).completion_pressure("t03", "LAV")
        self.assertEqual(pp.gaps.missing_max, 0)
        self.assertIsNone(pp.extra_if_this_closes_page)


# ---------------------------------------------------------------- objetivos

def _oracle(*events, cat=True):
    o = Oracle()
    if cat:
        o.load_catalog(catalog())
    o.ingest(events)
    return o


class TestVenta(unittest.TestCase):
    def test_quien_la_pidio_va_antes_que_quien_solo_es_sospechoso(self):
        ev = [listed(1, 200, "t07", "LAV-10", 90, bid=True),    # la pide
              listed(2, 201, "t11", "LAV-01", 10, aid=301)]     # sólo activo en LAV
        r, o = fresh(*ev), _oracle(*ev)
        rows = r.sell_targets(o, ["LAV-10"], exclude={"t15"})
        self.assertEqual(rows[0]["team"], "t07")
        self.assertEqual(rows[0]["evidence"], EV_PUBLISHED)
        self.assertTrue(rows[0]["rank"] > rows[-1]["rank"])
        self.assertTrue(any("la pidió" in b for b in rows[0]["basis"]))

    def test_no_se_le_vende_a_quien_ya_la_tiene(self):
        ev = [listed(1, 200, "t11", "LAV-01", 10, aid=301)]
        rows = fresh(*ev).sell_targets(_oracle(*ev), ["LAV-01"])
        self.assertEqual([x["team"] for x in rows], [])

    def test_si_la_vendio_y_luego_la_pide_vuelve_a_ser_comprador(self):
        ev = [listed(1, 200, "t11", "LAV-01", 10, aid=301),
              listed(2, 205, "t11", "LAV-01", 12, bid=True)]
        rows = fresh(*ev).sell_targets(_oracle(*ev), ["LAV-01"])
        self.assertEqual(rows[0]["team"], "t11")

    def test_la_prima_se_ancla_en_lo_que_ya_pago(self):
        """t18 pagó 49 P por una común de book 10: eso es conducta, no modelo."""
        ev = [settled(1, 230, "t02", "t18", "RET-02", 49, aid=911),
              settled(2, 231, "t04", "t06", "RET-02", 10, aid=912),
              listed(3, 232, "t18", "RET-05", 60, bid=True)]
        r, o = fresh(*ev), _oracle(*ev)
        rows = r.sell_targets(o, ["RET-05"], exclude={"t15"})
        t18 = next(x for x in rows if x["team"] == "t18")
        self.assertEqual(t18["evidence"], EV_SETTLED)
        self.assertTrue(any("49 P" in b for b in t18["basis"]))
        self.assertGreaterEqual(t18["suggested_ask"], 49)
        self.assertEqual((t18["premium_over"], t18["base_price"]), ("book", 10.0))

    def test_el_techo_entero_solo_para_la_ultima_carta_de_la_pagina(self):
        """Pedirla con nueve huecos abiertos no justifica el bonus completo."""
        ev = [listed(1, 200, "t07", "LAV-10", 90, bid=True)]
        rows = fresh(*ev).sell_targets(_oracle(*ev), ["LAV-10"])
        row = rows[0]
        self.assertEqual(row["missing_max"], 10)
        self.assertEqual(row["premium"], round(BONUS / 10))   # repartido
        self.assertTrue(any("repartido entre 10" in b for b in row["basis"]))

    def test_a_una_carta_de_cerrar_la_pagina_sube_el_rango_y_el_techo(self):
        """t18 real: 9/10 de RET vistas, la que falta vale la página entera."""
        ev = [listed(i, 192, "t18", f"RET-{i:02d}", 10, aid=200 + i)
              for i in range(1, 10)]
        ev.append(listed(30, 200, "t11", "RET-10", 10, bid=True))   # otro la pide
        r, o = fresh(*ev), _oracle(*ev)
        rows = r.sell_targets(o, ["RET-10"], exclude={"t15"})
        t18 = next(x for x in rows if x["team"] == "t18")
        self.assertEqual((t18["missing_max"], t18["premium"]), (1, int(BONUS)))
        self.assertTrue(any("la única que no se le ha visto" in b
                            for b in t18["basis"]))
        # Quien la pidió va primero (su palabra es evidencia), pero la prima
        # de quien cierra página es mucho mayor: la decisión es del que negocia.
        t11 = next(x for x in rows if x["team"] == "t11")
        self.assertEqual(rows[0]["team"], "t11")
        self.assertGreater(t18["premium"], t11["premium"])
        self.assertEqual(t18["evidence"], "INFERRED")

    def test_sin_referencia_de_precio_no_se_inventa_una_cifra(self):
        ev = [listed(1, 200, "t07", "ZZZ-04", 30, bid=True)]
        r = Rivals()                       # sin catálogo: no hay book
        r.ingest(ev)
        rows = r.sell_targets(_oracle(cat=False), ["ZZZ-04"])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["base_price"])
        self.assertIsNone(rows[0]["suggested_ask"])
        self.assertIsNone(rows[0]["premium"])
        self.assertIsNone(rows[0]["premium_over"])

    def test_sin_ninguna_evidencia_no_hay_objetivos(self):
        r, o = Rivals(), _oracle()
        r.load_catalog(catalog())
        self.assertEqual(r.sell_targets(o, ["LAV-01"]), [])


class TestCompra(unittest.TestCase):
    def test_la_oferta_viva_mas_barata_primero(self):
        ev = [listed(1, 200, "t03", "LAV-01", 14, aid=401),
              listed(2, 201, "t06", "LAV-01", 8, aid=402),
              settled(3, 202, "t04", "t02", "LAV-01", 12, aid=403)]
        r, o = fresh(*ev), _oracle(*ev)
        rows = r.buy_targets(o, ["LAV-01"])
        self.assertEqual(rows[0]["team"], "t06")
        self.assertEqual(rows[0]["ask"], 8)
        self.assertEqual(rows[0]["fair"], 12)
        self.assertEqual((rows[0]["saving"], rows[0]["cheap_vs"]), (4, "settled"))
        self.assertTrue(rows[0]["listed"])

    def test_la_cancelada_no_cuenta_y_el_poseedor_sin_oferta_va_al_final(self):
        ev = [listed(1, 200, "t03", "LAV-01", 14, aid=401),
              cancelled(2, 201, 3001),
              listed(3, 202, "t06", "LAV-01", 9, aid=402)]
        rows = fresh(*ev).buy_targets(_oracle(*ev), ["LAV-01"])
        self.assertEqual([(x["team"], x["ask"]) for x in rows],
                         [("t06", 9), ("t03", None)])
        self.assertFalse(rows[-1]["listed"])

    def test_sin_mercado_se_compara_con_el_suelo_del_dealer(self):
        ev = [listed(1, 200, "t06", "LAV-04", 6, aid=402)]
        o = _oracle(*ev, dealer_msg(50, 199, "abuela", "t18", "LAV-04", 10))
        rows = fresh(*ev).buy_targets(o, ["LAV-04"])
        self.assertEqual((rows[0]["dealer_floor"], rows[0]["cheap_vs"]),
                         (10, "dealer"))
        self.assertEqual(rows[0]["saving"], 4)

    def test_lo_que_nadie_ha_mostrado_no_devuelve_filas(self):
        r, o = fresh(listed(1, 200, "t06", "LAV-04", 6)), _oracle()
        self.assertEqual(r.buy_targets(o, ["RET-11"]), [])


class TestResumen(unittest.TestCase):
    def test_coverage_y_volcado(self):
        r = fresh(listed(1, 192, "t03", "LAV-02", 10),
                  listed(2, 200, "t15", "RET-06", 14, bid=True))
        self.assertEqual(r.coverage["teams"], 2)
        self.assertEqual(r.coverage["tick_max"], 200)
        d = r.to_json()
        self.assertEqual({t["team"] for t in d["teams"]}, {"t03", "t15"})
        self.assertEqual(next(t for t in d["teams"]
                              if t["team"] == "t15")["sought"], ["RET-06"])


if __name__ == "__main__":
    unittest.main()
