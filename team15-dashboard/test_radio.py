import unittest

import radio

CATALOG = {"sets": [
    {"id": "MAL", "name": "Malasaña", "cards": [
        {"id": "MAL-01", "rarity": "common"}, {"id": "MAL-06", "rarity": "uncommon"},
        {"id": "MAL-09", "rarity": "rare"}, {"id": "MAL-10", "rarity": "rare"}]},
    {"id": "SAL", "name": "Salamanca", "cards": [
        {"id": "SAL-01", "rarity": "common"}, {"id": "SAL-08", "rarity": "uncommon"}]},
]}
HOLDINGS = {"MAL-01": 1, "MAL-06": 1, "SAL-01": 1}          # MAL-09 y MAL-10 no las tenemos
GUIDE = [{"ref": "MAL-01"}]                                  # unica con copia libre
DEALERS = {"abuela": {"name": "Abuela Carmen", "level": 1,
                      "menu": {"buys": [{"rarity": "common"}, {"rarity": "uncommon"}]}}}

# Las ocho noticias reales de GET /api/news en t_hours 8.6
NEWS = [radio.news_item(n) for n in [
    {"id": 8, "source": "radio", "source_name": "Radio Rastro", "at_hours": 8.2833, "tick": 838,
     "headline": "Half-hour queue at the San Ginés churro shop", "body": ""},
    {"id": 7, "source": "tablon", "source_name": "El Tablón", "at_hours": 7.6833, "tick": 763,
     "headline": "Abuela stops buying common cards from today",
     "body": "That is what they say at the next stall."},
    {"id": 6, "source": "boletin", "source_name": "Boletín del Bazar", "at_hours": 6.6833, "tick": 643,
     "headline": "Abuela Carmen gives out packs for her saint's day",
     "body": "A neighbourhood pack for every team in one hour."},
    {"id": 4, "source": "tablon", "source_name": "El Tablón", "at_hours": 5.4833, "tick": 499,
     "headline": "El Chato gives a legendary to anyone who says hello!", "body": "My cousin saw it."},
    {"id": 3, "source": "radio", "source_name": "Radio Rastro", "at_hours": 4.6833, "tick": 403,
     "headline": "El Chato is looking for rare Malasaña cards",
     "body": "They say he pays above the usual price today. One hour, no more."},
    {"id": 2, "source": "radio", "source_name": "Radio Rastro", "at_hours": 4.0833, "tick": 331,
     "headline": "Atleti win 2-1 and Madrid goes out to celebrate", "body": "Car horns on Gran Vía."},
]]


def at(hours, **kw):
    return {item["id"]: item for item in radio.interpret(
        NEWS, CATALOG, GUIDE, t_hours=hours, holdings=HOLDINGS, **kw)}


class Sources(unittest.TestCase):
    def test_ya_no_descarta_el_boletin_ni_el_tablon(self):
        """El filtro anterior era payload['source'] != 'radio': de 8 noticias en 3 fuentes
        el panel podia mostrar como maximo las de una."""
        got = {i["source_id"] for i in radio.interpret(NEWS, CATALOG, GUIDE, t_hours=8.6)}
        self.assertEqual(got, {"radio", "tablon", "boletin"})

    def test_radio_event_acepta_las_tres_fuentes(self):
        for source in ("radio", "tablon", "boletin"):
            event = {"type": "news.posted", "id": 1, "tick": 10,
                     "payload": {"source": source, "headline": "x", "at_hours": 1.0}}
            self.assertEqual(radio.radio_event(event)["source_id"], source)
        self.assertIsNone(radio.radio_event({"type": "settlement"}))

    def test_cada_fuente_lleva_su_fiabilidad(self):
        by = at(8.6)
        self.assertEqual(by[6]["reliability"], "oficial")
        self.assertEqual(by[8]["reliability"], "rumor")
        self.assertEqual(by[4]["reliability"], "cebo")


class Verdicts(unittest.TestCase):
    def test_el_tablon_siempre_se_ignora(self):
        self.assertEqual(at(8.6)[4]["verdict"], "ignore")
        self.assertIn("enfriamiento", at(8.6)[4]["action"])

    def test_refuta_el_cebo_contra_el_endpoint_de_vendedores(self):
        """El Tablon dice que la abuela deja de comprar comunes y su menu sigue listando common."""
        item = at(8.6, dealers=DEALERS)[7]
        self.assertEqual(item["verdict"], "ignore")
        self.assertIn("/api/dealers contradice", item["action"])
        self.assertIn("common", item["action"])

    def test_un_rumor_sobre_cartas_que_tenemos_pide_verificar(self):
        items = radio.interpret(NEWS, CATALOG, GUIDE, t_hours=5.0, holdings={"MAL-09": 1})
        item = next(i for i in items if i["id"] == 3)
        self.assertEqual(item["verdict"], "verify")
        self.assertEqual(item["ours"], ["MAL-09"])
        self.assertIn("cotización real", item["action"])

    def test_un_rumor_sobre_cartas_que_no_tenemos_no_mueve_precios(self):
        item = at(5.0)[3]                                  # pide raras de MAL; tenemos MAL-01 y MAL-06
        self.assertEqual(item["verdict"], "noise")
        self.assertEqual(item["ours"], [])
        self.assertIn("no tenemos", item["action"])

    def test_una_noticia_sin_cartas_es_ruido(self):
        self.assertEqual(at(8.6)[8]["verdict"], "noise")   # la cola de los churros
        self.assertEqual(at(8.6)[2]["verdict"], "noise")   # el Atleti


class Windows(unittest.TestCase):
    def test_el_plazo_se_mide_en_horas_de_juego_no_en_ticks(self):
        """`current_tick > tick + 120` suponia 30 s/tick: el viernes iba a 60 s y el domingo a 15 s."""
        win = radio.window(at(8.6)[3])
        self.assertEqual(win["declared"], "una hora")
        self.assertEqual(win["expires_at_hours"], 5.6833)

    def test_dentro_del_plazo_no_esta_vencido(self):
        self.assertFalse(radio.interpret(NEWS, CATALOG, GUIDE, t_hours=5.0,
                                         holdings={"MAL-09": 1})[0]["expired"])

    def test_pasado_el_plazo_se_marca_vencido(self):
        item = radio.interpret(NEWS, CATALOG, GUIDE, t_hours=8.6, holdings={"MAL-09": 1})
        chato = next(i for i in item if i["id"] == 3)
        self.assertTrue(chato["expired"])
        self.assertEqual(chato["verdict"], "expired")
        self.assertIn("5.68", chato["action"])

    def test_el_sobre_del_boletin_vence_una_hora_despues(self):
        vivo = radio.interpret(NEWS, CATALOG, GUIDE, t_hours=7.0)
        self.assertEqual(next(i for i in vivo if i["id"] == 6)["verdict"], "act")
        tarde = at(8.6)
        self.assertEqual(tarde[6]["verdict"], "expired")

    def test_sin_plazo_declarado_no_se_inventa_uno(self):
        self.assertIsNone(radio.window({"at_hours": 3.0, "headline": "algo", "body": ""})["expires_at_hours"])


class Summary(unittest.TestCase):
    def test_resume_por_veredicto_y_senala_el_cebo(self):
        out = radio.radio_summary(radio.interpret(NEWS, CATALOG, GUIDE, t_hours=8.6,
                                                  holdings=HOLDINGS, dealers=DEALERS))
        self.assertEqual(out["total"], 6)
        self.assertEqual(out["by_verdict"]["ignore"], 2)
        self.assertTrue(out["bait_present"])

    def test_lo_accionable_se_lista_aparte(self):
        items = radio.interpret(NEWS, CATALOG, GUIDE, t_hours=5.0, holdings={"MAL-09": 1})
        out = radio.radio_summary(items)
        # ordenadas por hora descendente: el boletin (6.68) va antes del rumor del Chato (4.68)
        self.assertEqual([a["verdict"] for a in out["actionable"]], ["act", "verify"])
        self.assertEqual([a["ours"] for a in out["actionable"]], [[], ["MAL-09"]])


if __name__ == "__main__":
    unittest.main()
