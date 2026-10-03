"""Pruebas de `feed_oracle`. Escenarios locales, sin red.

Los esquemas de evento están copiados del feed real (ticks 189-220).
"""

import unittest

from feed_oracle import CardMarket, Oracle, is_team, read_quotes, read_settlements


def dealer_msg(eid, tick, dealer, team, ref, cash, *, final=False, thread=1, sells=True):
    """Un dealer cotiza en una conversación. `sells`: nos la vende."""
    if sells:
        give = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
        want = {"cash": cash, "assets": [], "types": []}
    else:
        give = {"cash": cash, "assets": [], "types": []}
        want = {"cash": 0, "assets": [{"id": 1, "kind": "card", "ref": ref,
                                       "rarity": "common", "set": ref[:3]}], "types": []}
    return {"id": eid, "tick": tick, "type": "thread.message",
            "payload": {"thread": thread, "kind": "persona", "message": eid,
                        "sender": dealer, "text": "...", "team": team, "with": dealer,
                        "offer": {"id": eid, "maker": dealer, "to": team, "venue": None,
                                  "thread": thread, "status": "open", "give": give,
                                  "want": want, "final": final}}}


def listed(eid, tick, team, ref, cash, *, rarity="common", venue="v02", bid=False):
    """Un equipo publica. `bid`: ofrece efectivo por la carta."""
    asset = {"id": 500 + eid, "kind": "card", "ref": ref, "rarity": rarity,
             "set": ref[:3], "print_run": 300}
    if bid:
        give = {"cash": cash, "assets": [], "types": []}
        want = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
    else:
        give = {"cash": 0, "assets": [asset], "types": []}
        want = {"cash": cash, "assets": [], "types": []}
    return {"id": eid, "tick": tick, "type": "offer.listed",
            "payload": {"venue": venue,
                        "offer": {"id": eid, "maker": team, "to": None, "venue": venue,
                                  "thread": None, "status": "open", "give": give,
                                  "want": want, "final": False}}}


def settled(eid, tick, frm, to, ref, price, *, rarity="common", venue="rastro", fee=2):
    item = {"id": 600 + eid, "kind": "card", "ref": ref, "serial": 2, "rarity": rarity,
            "set": ref[:3], "print_run": 300, "frm": frm, "to": to, "name": "X"}
    return {"id": eid, "tick": tick, "type": "settlement",
            "payload": {"settlement": eid, "tick": tick, "kind": "trade",
                        "parties": [frm, to], "venue": venue, "persona": None,
                        "fee": fee, "items": [item], "price": price}}


class TestIsTeam(unittest.TestCase):
    def test_distingue_equipos_de_dealers(self):
        for ok in ("t04", "t15", "t18"):
            self.assertTrue(is_team(ok), ok)
        for no in ("abuela", "chato", "t4", "t004", None, 15, ""):
            self.assertFalse(is_team(no), repr(no))


class TestLectura(unittest.TestCase):
    def test_liquidacion_de_una_carta(self):
        (s,) = read_settlements([settled(1, 205, "t06", "t02", "RET-02", 12)])
        self.assertEqual((s.ref, s.price, s.seller, s.buyer), ("RET-02", 12, "t06", "t02"))

    def test_lote_se_omite_porque_el_precio_no_es_atribuible(self):
        ev = settled(1, 205, "t06", "t02", "RET-02", 30)
        ev["payload"]["items"].append(dict(ev["payload"]["items"][0], id=999, ref="RET-03"))
        self.assertEqual(list(read_settlements([ev])), [])

    def test_trueque_sin_efectivo_no_es_cotizacion(self):
        ev = listed(1, 200, "t13", "LAT-04", 0)
        ev["payload"]["offer"]["want"] = {"cash": 0, "assets": [],
                                          "types": ["card:LAT-09"]}
        self.assertEqual(list(read_quotes([ev])), [])

    def test_dealer_vendiendo_y_comprando(self):
        ask, bid = read_quotes([dealer_msg(1, 200, "abuela", "t18", "RET-02", 12),
                                dealer_msg(2, 201, "abuela", "t18", "LAV-04", 5,
                                           sells=False)])
        self.assertEqual((ask.side, ask.price, ask.is_dealer), ("ask", 12, True))
        self.assertEqual((bid.side, bid.price), ("bid", 5))

    def test_equipo_no_es_dealer(self):
        (q,) = read_quotes([listed(1, 200, "t13", "LAT-04", 8)])
        self.assertFalse(q.is_dealer)
        self.assertEqual(q.venue, "v02")


class TestOracle(unittest.TestCase):
    def ladder(self):
        """Abuela baja 12 → 11 → 10 → 9 y liquida a 9."""
        o = Oracle()
        o.ingest([dealer_msg(i, 200 + i, "abuela", "t18", "RET-02", p)
                  for i, p in enumerate([12, 11, 10, 9], start=1)])
        return o

    def test_suelo_y_apertura(self):
        dl = self.ladder().dealer_floor("abuela", "RET-02")
        self.assertEqual((dl.opening, dl.floor, dl.steps), (12, 9, [1, 1, 1]))
        self.assertEqual(dl.confidence, "HIGH")

    def test_la_liquidacion_manda_sobre_la_cotizacion(self):
        o = self.ladder()
        o.ingest([settled(50, 210, "abuela", "t18", "RET-02", 8)])
        dl = o.dealer_floor("abuela", "RET-02")
        self.assertEqual(dl.floor, 8)
        self.assertEqual(dl.confidence, "SETTLED")

    def test_final_pesa_mas_que_una_cotizacion_suelta(self):
        o = Oracle()
        o.ingest([dealer_msg(1, 200, "abuela", "t18", "LAV-04", 7),
                  dealer_msg(2, 201, "abuela", "t18", "LAV-04", 5, final=True)])
        dl = o.dealer_floor("abuela", "LAV-04")
        self.assertEqual((dl.floor, dl.confidence), (5, "FINAL"))

    def test_bid_de_dealer_su_suelo_es_el_maximo(self):
        o = Oracle()
        o.ingest([dealer_msg(i, 200 + i, "abuela", "t18", "LAT-04", p, sells=False)
                  for i, p in enumerate([5, 6], start=1)])
        self.assertEqual(o.dealer_floor("abuela", "LAT-04", "bid").floor, 6)

    def test_ingesta_idempotente(self):
        o, ev = Oracle(), [dealer_msg(1, 200, "abuela", "t18", "RET-02", 12)]
        self.assertEqual(o.ingest(ev), 1)
        self.assertEqual(o.ingest(ev), 0)
        self.assertEqual(o.dealer_floor("abuela", "RET-02").n, 1)

    def test_no_aceptar_la_apertura(self):
        self.assertEqual(self.ladder().opening_to_avoid("abuela", "RET-02"), 12)

    def test_contraoferta_queda_bajo_el_suelo_conocido(self):
        o = self.ladder()
        c = o.suggest_counter("abuela", "RET-02", their_price=12)
        self.assertLess(c, 9)
        self.assertGreater(c, 0)

    def test_contraoferta_sin_datos_no_opina(self):
        self.assertIsNone(Oracle().suggest_counter("abuela", "ZZZ-01", their_price=10))

    def test_contraoferta_cae_a_la_rareza_si_no_hay_la_carta(self):
        o = self.ladder()
        # la rareza se aprende de lo publicado; RET-07 no tiene línea de dealer
        o.ingest([listed(80, 205, "t06", "RET-02", 11),
                  listed(81, 206, "t13", "RET-07", 9)])
        self.assertEqual(o.rarity_floor("common"), 9)
        self.assertIsNotNone(o.suggest_counter("abuela", "RET-07", their_price=12))

    def test_mercado_entre_equipos(self):
        o = Oracle()
        o.ingest([listed(1, 200, "t13", "LAT-04", 8),
                  listed(2, 201, "t14", "LAT-04", 10),
                  listed(3, 202, "t05", "LAT-04", 6, bid=True),
                  settled(4, 203, "t13", "t05", "LAT-04", 9)])
        cm = o.cards["LAT-04"]
        self.assertEqual(cm.fair, 9)
        self.assertEqual(cm.confidence, "MEDIUM")
        self.assertEqual(cm.undercut(), 7)
        self.assertEqual(o.who_wants("LAT-04"), ["t05"])
        self.assertIn("t14", o.who_has("LAT-04"))

    def test_la_liquidacion_mueve_al_poseedor(self):
        o = Oracle()
        o.ingest([listed(1, 200, "t13", "LAT-04", 8),
                  settled(2, 201, "t13", "t05", "LAT-04", 9)])
        self.assertEqual(o.who_has("LAT-04"), ["t05"])

    def test_arbitraje_detecta_equipo_mas_barato_que_dealer(self):
        o = self.ladder()                                   # abuela RET-02 suelo 9
        o.ingest([listed(20, 205, "t06", "RET-02", 7)])
        (a,) = o.arbitrage()
        self.assertEqual((a["ref"], a["team_ask"], a["dealer_floor"], a["saving"]),
                         ("RET-02", 7, 9, 2))
        self.assertEqual(a["holders"], ["t06"])

    def test_rareza_desconocida_no_se_inventa(self):
        self.assertIsNone(self.ladder().rarity_floor("legendary"))

    def test_sin_arbitraje_si_el_equipo_no_es_mas_barato(self):
        o = self.ladder()
        o.ingest([listed(20, 205, "t06", "RET-02", 11)])
        self.assertEqual(o.arbitrage(), [])

    def test_cobertura(self):
        c = self.ladder().coverage
        self.assertEqual((c["events"], c["tick_min"], c["tick_max"]), (4, 201, 204))


class TestCardMarket(unittest.TestCase):
    def test_sin_datos_no_hay_precio(self):
        cm = CardMarket(ref="X")
        self.assertIsNone(cm.fair)
        self.assertEqual(cm.confidence, "UNKNOWN")

    def test_con_asks_y_bids_promedia(self):
        cm = CardMarket(ref="X", asks=[10], bids=[6])
        self.assertEqual(cm.fair, 8)


if __name__ == "__main__":
    unittest.main()
