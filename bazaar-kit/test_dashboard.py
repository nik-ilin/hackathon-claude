"""Pruebas de `dashboard`. Sin red, sin servidor, sin tocar los `.jsonl`.

El HTML se genera a partir de un `Oracle` alimentado con eventos de prueba,
que es justo por qué `render_page()` está separado del servidor: lo que hay
que poder verificar es la página, no el `http.server`.

Los esquemas de evento son los del feed real (ticks 189-220), copiados aquí
en lugar de importarse de otro test para que este archivo se sostenga solo.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import dashboard as D
from feed_oracle import Oracle


# ------------------------------------------------------------ eventos de prueba

def dealer_msg(eid, tick, dealer, team, ref, cash, *, final=False, thread=1):
    give = {"cash": 0, "assets": [], "types": [f"card:{ref}"]}
    want = {"cash": cash, "assets": [], "types": []}
    return {"id": eid, "tick": tick, "type": "thread.message",
            "payload": {"thread": thread, "kind": "persona", "sender": dealer,
                        "team": team, "with": dealer,
                        "offer": {"id": eid, "maker": dealer, "to": team, "venue": None,
                                  "thread": thread, "status": "open", "give": give,
                                  "want": want, "final": final}}}


def listed(eid, tick, team, ref, cash, *, rarity="common", bid=False, venue="v02"):
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


def settled(eid, tick, frm, to, ref, price, *, rarity="common"):
    item = {"id": 600 + eid, "kind": "card", "ref": ref, "serial": 2, "rarity": rarity,
            "set": ref[:3], "print_run": 300, "frm": frm, "to": to, "name": "X"}
    return {"id": eid, "tick": tick, "type": "settlement",
            "payload": {"settlement": eid, "tick": tick, "kind": "trade",
                        "parties": [frm, to], "venue": "rastro", "persona": None,
                        "fee": 2, "items": [item], "price": price}}


CATALOG = {"sets": [{"id": "RET", "cards": [
    {"id": "RET-02", "rarity": "common", "book": 10, "print_run": 300, "minted": 20,
     "page": True},
    {"id": "RET-09", "rarity": "rare", "book": 80, "print_run": 30, "minted": 4,
     "page": True},
]}]}


def loaded_oracle() -> Oracle:
    """Un Oracle con de todo: suelo de dealer, mercado, arbitraje y sobreprecio."""
    o = Oracle()
    o.load_catalog(CATALOG)
    ev = [
        # escalera de abuela con RET-02: abre en 20, baja a 12 y lo liquida
        dealer_msg(1, 200, "abuela", "t18", "RET-02", 20, thread=1),
        dealer_msg(2, 201, "abuela", "t18", "RET-02", 16, thread=1),
        dealer_msg(3, 202, "abuela", "t18", "RET-02", 12, thread=1, final=True),
        settled(4, 203, "abuela", "t18", "RET-02", 12),
        # un equipo la pide a 7: arbitraje contra el suelo 12
        listed(5, 204, "t06", "RET-02", 7),
        listed(6, 204, "t13", "RET-02", 9),
        listed(7, 205, "t02", "RET-02", 11, bid=True),
        # alguien paga 49 por una común de book 10: sobreprecio de urgencia
        settled(8, 206, "t06", "t18", "RET-02", 49),
        settled(9, 207, "t13", "t02", "RET-02", 11),
        # una rara, con muestra de 1 a propósito
        dealer_msg(10, 208, "chato", "t12", "RET-09", 120, thread=2),
    ]
    o.ingest(ev)
    return o


CLOCK = {"tick": 253, "t_hours": 3.4333, "tick_seconds": 30.0, "paused": False,
         "next_tick_in": 6.7, "round_name": "Saturday · Gran Vía",
         "today_name": "Saturday", "closes": "2026-10-03T23:00:00+02:00"}

SCHEDULE = {"now_hours": 3.433, "upcoming": [
    {"at_hours": 3.0, "action": "bench", "note": "ya pasó"},
    {"at_hours": 4.0, "action": "day_closes", "note": "cierre de jornada"},
    {"at_hours": 3.5, "action": "bench", "note": "The Market Test"},
    {"at_hours": 5.15, "action": "duels", "note": "Duels I", "params": {"rounds": 1}},
]}

LEADERBOARD = {"tick": 253, "teams": [
    {"team": "t18", "name": "Team 18", "score": 28.5, "negotiating": 22.6, "market": 0.0,
     "level": 2, "album_filled": 32, "album_slots": 50, "pages_complete": 2,
     "deals": 27, "rank": 1},
    {"team": "t15", "name": "Team 15", "score": 9.1, "negotiating": 9.1, "market": 0.0,
     "level": 1, "album_filled": 12, "album_slots": 50, "pages_complete": 0,
     "deals": 4, "rank": 14},
]}


def snap(**kw) -> D.Snapshot:
    base = dict(oracle=loaded_oracle(), clock=CLOCK, schedule=SCHEDULE,
                leaderboard=LEADERBOARD, team="t15", catalog_refs=2,
                sources={"feed_history.jsonl": 9}, mods={})
    base.update(kw)
    return D.Snapshot(**base)


# ------------------------------------------------------------------- el reloj

class TestReloj(unittest.TestCase):
    def test_refresco_sigue_al_tick_pero_acotado(self):
        self.assertEqual(D.refresh_seconds({"tick_seconds": 30.0}), 30)
        self.assertEqual(D.refresh_seconds({"tick_seconds": 15.0}), 15)
        self.assertEqual(D.refresh_seconds({"tick_seconds": 5.0}), 15)   # domingo rápido
        self.assertEqual(D.refresh_seconds({"tick_seconds": 60.0}), 30)
        self.assertEqual(D.refresh_seconds({}), 30)
        self.assertEqual(D.refresh_seconds({"tick_seconds": "raro"}), 30)

    def test_horas_de_juego_a_minutos_reales(self):
        # 120 ticks por hora de juego: a 30 s/tick una hora son 60 min reales,
        # y el domingo a 15 s/tick son 30.
        self.assertAlmostEqual(D.real_minutes(1.0, 30.0), 60.0)
        self.assertAlmostEqual(D.real_minutes(1.0, 15.0), 30.0)
        self.assertAlmostEqual(D.real_minutes(0.5, 30.0), 30.0)

    def test_urgencias_ordenadas_sin_lo_pasado_ni_las_jornadas(self):
        rows = D.urgencies(CLOCK, SCHEDULE)
        acciones = [r["action"] for r in rows]
        self.assertEqual(acciones, ["bench", "duels"])
        self.assertNotIn("day_closes", acciones)      # va en la cabecera, no aquí
        self.assertAlmostEqual(rows[0]["mins"], D.real_minutes(0.067, 30.0), places=2)
        self.assertEqual(rows[0]["ticks"], 8)

    def test_urgencias_sin_agenda_no_estalla(self):
        self.assertEqual(D.urgencies({}, {}), [])
        self.assertEqual(D.urgencies({}, {"upcoming": [{"note": "sin hora"}]}), [])

    def test_cuenta_atras_al_cierre(self):
        ahora = datetime(2026, 10, 3, 20, 0, tzinfo=timezone(timedelta(hours=2)))
        dc = D.doors_countdown(CLOCK, now=ahora)
        self.assertAlmostEqual(dc["mins"], 180.0, places=3)
        self.assertIsNone(D.doors_countdown({}))
        self.assertIsNone(D.doors_countdown({"closes": "mañana"}))


# ------------------------------------------------------------------- paneles

class TestPaneles(unittest.TestCase):
    def test_arbitraje_y_sobreprecio_por_primas(self):
        acts = D.now_actions(loaded_oracle())
        self.assertTrue(acts["arbitrage"])
        a = acts["arbitrage"][0]
        self.assertEqual((a["ref"], a["team_ask"], a["dealer_floor"], a["saving"]),
                         ("RET-02", 7, 12, 5))
        self.assertEqual(acts["premium"][0]["team"], "t18")
        self.assertEqual(acts["premium"][0]["extra"], 39)       # 49 pagados, book 10
        self.assertEqual(acts["hot_sets"], {"RET": 1})

    def test_chuleta_empieza_por_lo_liquidado(self):
        rows = D.cheatsheet(loaded_oracle())
        self.assertTrue(rows)
        self.assertEqual(rows[0]["confidence"], "SETTLED")
        self.assertEqual(rows[0]["ref"], "RET-02")
        self.assertEqual(rows[0]["never"], 20)        # su apertura: nunca aceptar
        self.assertEqual(rows[0]["floor"], 12)
        self.assertLess(rows[0]["open_at"], rows[0]["floor"])
        # la rara con muestra de 1 queda detrás de lo liquidado
        self.assertEqual([r["n"] for r in rows if r["ref"] == "RET-09"], [1])

    def test_chuleta_con_playbook_roto_cae_al_oracle(self):
        class Roto:
            @staticmethod
            def playbook(_oracle):
                raise RuntimeError("a medio escribir")
        rows = D.cheatsheet(loaded_oracle(), {"playbook": Roto()})
        self.assertTrue(rows)
        self.assertEqual({r["source"] for r in rows}, {"oracle"})

    def test_chuleta_usa_playbook_si_esta(self):
        class Falso:
            @staticmethod
            def playbook(_oracle):
                return [{"dealer": "abuela", "ref": "RET-02", "side": "ask",
                         "floor_seen": 12, "never_accept": 20, "open_at": 11,
                         "step": 2, "walk_away": 14, "max_rounds": 5, "spread": 8,
                         "confidence": "SETTLED", "observations": 3}]
        rows = D.cheatsheet(loaded_oracle(), {"playbook": Falso()})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["walk_away"]), ("playbook", 14))

    def test_mercado_por_valor_no_por_orden_alfabetico(self):
        rows = D.card_rows(loaded_oracle())
        self.assertTrue(rows)
        valores = [r["fair"] or 0 for r in rows]
        self.assertEqual(valores, sorted(valores, reverse=True))
        ret02 = next(r for r in rows if r["ref"] == "RET-02")
        # el sobreprecio de 49 no entra en el precio justo de una común de book 10
        self.assertLessEqual(ret02["fair"], 25)
        self.assertEqual(ret02["undercut"], 6)        # un paso por debajo del 7

    def test_clasificacion_destaca_nuestro_equipo(self):
        rows = D.leaderboard_rows(LEADERBOARD, "t15")
        self.assertEqual([r["rank"] for r in rows], [1, 14])
        self.assertEqual([r["us"] for r in rows], [False, True])
        self.assertEqual(D.leaderboard_rows({}, "t15"), [])

    def test_confianza_señala_la_muestra_de_uno(self):
        t = D.trust(snap())
        self.assertEqual(t["coverage"]["settled"], 2)
        flacas = {(r["dealer"], r["ref"]) for r in t["thin_lines"]}
        self.assertIn(("chato", "RET-09"), flacas)
        self.assertNotIn(("abuela", "RET-02"), flacas)
        self.assertEqual(t["errors"], [])

    def test_filas_de_rivales_solo_si_el_modulo_las_dio(self):
        self.assertEqual(D.rival_rows(snap()), [])
        s = snap(rivals={"teams": [{"team": "t18", "sample": 9, "held": ["RET-02"]}]})
        self.assertEqual(D.rival_rows(s)[0]["team"], "t18")

    def test_texto_opcional_tolera_modulos_sin_informe(self):
        o = loaded_oracle()
        self.assertIsNone(D.optional_text({}, "signals", o))

        class Mudo:
            pass

        class Roto:
            @staticmethod
            def report(_o):
                raise RuntimeError("boom")

        class Bueno:
            @staticmethod
            def report(_o):
                return "una línea"
        self.assertIsNone(D.optional_text({"x": Mudo()}, "x", o))
        self.assertIsNone(D.optional_text({"x": Roto()}, "x", o))
        self.assertEqual(D.optional_text({"x": Bueno()}, "x", o), "una línea")


# ---------------------------------------------------------------------- HTML

SECCIONES = ("urgencias", "qué hacer AHORA", "chuleta por dealer",
             "mercado por carta", "clasificación", "confianza")


class TestHTML(unittest.TestCase):
    def test_la_pagina_trae_todas_las_secciones(self):
        page = D.render_page(snap())
        self.assertTrue(page.startswith("<!doctype html>"))
        for s in SECCIONES:
            self.assertIn(s, page, f"falta la sección «{s}»")
        self.assertIn("</html>", page)

    def test_la_pagina_muestra_las_cifras_accionables(self):
        page = D.render_page(snap())
        self.assertIn("RET-02", page)
        self.assertIn("SETTLED", page)
        self.assertIn("n=1", page)                    # la anécdota marcada como tal
        self.assertIn('class="us"', page)             # t15 destacado
        self.assertIn("Saturday", page)
        self.assertIn("var REFRESH=30", page)

    def test_sin_dependencias_externas(self):
        """Tiene que funcionar sin internet salvo la propia API."""
        page = D.render_page(snap())
        for prohibido in ("http://", "https://", "//cdn", "fonts.googleapis"):
            self.assertNotIn(prohibido, page, f"la página referencia {prohibido}")

    def test_con_datos_vacios_no_estalla(self):
        page = D.render_page(D.Snapshot(oracle=Oracle()))
        for s in SECCIONES:
            self.assertIn(s, page)
        self.assertIn("nada ahora mismo", page)
        self.assertIn("ningún dealer observado", page)

    def test_sin_modulos_opcionales_no_hay_paneles_opcionales(self):
        page = D.render_page(snap(mods={}))
        self.assertNotIn("manos rivales", page)
        self.assertNotIn("extra ·", page)
        self.assertIn("chuleta por dealer", page)     # lo esencial sigue ahí

    def test_con_modulos_opcionales_rotos_tampoco(self):
        class Roto:
            def __getattr__(self, _name):
                raise RuntimeError("módulo a medio escribir")
        page = D.render_page(snap(mods={"playbook": Roto(), "signals": Roto(),
                                        "rivals": Roto()}))
        self.assertIn("chuleta por dealer", page)

    def test_los_errores_de_red_se_ven_y_no_tumban_la_pagina(self):
        page = D.render_page(snap(errors=["feed: timeout"]))
        self.assertIn("feed: timeout", page)
        self.assertIn("mercado por carta", page)

    def test_el_texto_del_feed_va_escapado(self):
        o = Oracle()
        o.ingest([listed(1, 200, "t06", "<script>x</script>", 7)])
        page = D.render_page(snap(oracle=o))
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_la_cuenta_atras_al_cierre_sale_en_la_cabecera(self):
        page = D.render_page(snap())
        self.assertIn("cierra 23:00", page)


# ------------------------------------------------------- lectura de los .jsonl

class TestTail(unittest.TestCase):
    def test_lee_solo_lo_nuevo_y_no_escribe(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "feed.jsonl"
            p.write_text(json.dumps({"id": 1}) + "\n", encoding="utf-8")
            antes = p.read_bytes()
            t = D.Tail()
            self.assertEqual(t.read(p), [{"id": 1}])
            self.assertEqual(t.read(p), [])           # nada nuevo
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": 2}) + "\n")
            self.assertEqual(t.read(p), [{"id": 2}])
            self.assertEqual(p.read_bytes()[:len(antes)], antes)

    def test_una_linea_a_medias_se_deja_para_la_proxima(self):
        """Otro proceso está escribiendo: la última línea puede estar cortada."""
        with TemporaryDirectory() as d:
            p = Path(d) / "feed.jsonl"
            p.write_text(json.dumps({"id": 1}) + "\n" + '{"id": 2, "ti',
                         encoding="utf-8")
            t = D.Tail()
            self.assertEqual(t.read(p), [{"id": 1}])
            with p.open("a", encoding="utf-8") as fh:
                fh.write('ck": 9}\n')
            self.assertEqual(t.read(p), [{"id": 2, "tick": 9}])

    def test_una_linea_corrupta_no_invalida_el_resto(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "feed.jsonl"
            p.write_text('{"id": 1}\nno es json\n{"id": 3}\n', encoding="utf-8")
            self.assertEqual(D.Tail().read(p), [{"id": 1}, {"id": 3}])

    def test_un_archivo_que_encoge_se_relee(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "feed.jsonl"
            p.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
            t = D.Tail()
            self.assertEqual(len(t.read(p)), 2)
            p.write_text('{"id": 9}\n', encoding="utf-8")       # rotación
            self.assertEqual(t.read(p), [{"id": 9}])

    def test_un_archivo_que_no_existe_no_es_un_error(self):
        self.assertEqual(D.Tail().read(Path("/no/existe.jsonl")), [])


# ------------------------------------------------------------------- builder

class ClienteFalso:
    """Sirve respuestas de prueba. Sólo `get`: no existe otro verbo."""

    def __init__(self, respuestas: dict, revienta: tuple = ()) -> None:
        self.respuestas = respuestas
        self.revienta = revienta
        self.pedidos: list[str] = []

    def get(self, path: str) -> dict:
        self.pedidos.append(path)
        if any(path.startswith(x) for x in self.revienta):
            raise OSError("sin red")
        for k, v in self.respuestas.items():
            if path.startswith(k):
                return v
        return {}


class TestBuilder(unittest.TestCase):
    def _store(self, d: str, events: list[dict]) -> Path:
        root = Path(d)
        (root / "data").mkdir()
        p = root / "data" / "feed_history.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return root

    def test_sin_cliente_construye_desde_los_jsonl(self):
        with TemporaryDirectory() as d:
            root = self._store(d, [settled(1, 203, "abuela", "t18", "RET-02", 12),
                                   listed(2, 204, "t06", "RET-02", 7)])
            b = D.Builder(None, root=root, stores=("data/feed_history.jsonl",), mods={})
            s = b.build()
            self.assertEqual(s.errors, [])
            self.assertEqual(s.oracle.coverage["events"], 2)
            self.assertEqual(s.sources["feed_history.jsonl"], 2)
            self.assertIn("mercado por carta", D.render_page(s))

    def test_los_jsonl_no_se_reescriben(self):
        with TemporaryDirectory() as d:
            root = self._store(d, [listed(1, 204, "t06", "RET-02", 7)])
            p = root / "data" / "feed_history.jsonl"
            antes = p.read_bytes()
            b = D.Builder(None, root=root, stores=("data/feed_history.jsonl",), mods={})
            b.build()
            b.build()
            self.assertEqual(p.read_bytes(), antes)

    def test_un_endpoint_caido_se_anota_y_la_pagina_sigue(self):
        with TemporaryDirectory() as d:
            root = self._store(d, [])
            c = ClienteFalso({"/api/clock": CLOCK, "/api/catalog": CATALOG},
                             revienta=("/api/feed", "/api/leaderboard"))
            b = D.Builder(c, root=root, stores=("data/feed_history.jsonl",), mods={})
            s = b.build()
            self.assertEqual(len(s.errors), 2)
            page = D.render_page(s)
            self.assertIn("sin respuesta del servidor", page)
            self.assertIn("confianza", page)

    def test_el_catalogo_se_pide_una_sola_vez(self):
        with TemporaryDirectory() as d:
            root = self._store(d, [])
            c = ClienteFalso({"/api/catalog": CATALOG, "/api/clock": CLOCK})
            b = D.Builder(c, root=root, stores=("data/feed_history.jsonl",), mods={})
            b.build()
            b.build()
            self.assertEqual(c.pedidos.count("/api/catalog"), 1)
            self.assertEqual(b._catalog_refs, 2)

    def test_solo_se_leen_endpoints_publicos(self):
        """Nada de escribir: otra persona está jugando en vivo con la clave."""
        with TemporaryDirectory() as d:
            root = self._store(d, [])
            c = ClienteFalso({"/api/clock": CLOCK})
            D.Builder(c, root=root, stores=("data/feed_history.jsonl",), mods={}).build()
            permitidos = ("/api/feed", "/api/clock", "/api/leaderboard", "/api/venues",
                          "/api/catalog", "/api/levels", "/api/schedule")
            for path in c.pedidos:
                self.assertTrue(path.startswith(permitidos), path)

    def test_snapshot_se_cachea_entre_peticiones(self):
        with TemporaryDirectory() as d:
            root = self._store(d, [])
            c = ClienteFalso({"/api/clock": CLOCK})
            b = D.Builder(c, root=root, stores=("data/feed_history.jsonl",), mods={})
            s1, s2 = b.snapshot(max_age=60), b.snapshot(max_age=60)
            self.assertIs(s1, s2)
            self.assertIsNot(s1, b.snapshot(max_age=-1))

    def test_los_agregadores_opcionales_que_fallan_se_desenganchan(self):
        class Agg:
            def __init__(self):
                self.lotes = 0

            def ingest(self, events):
                self.lotes += 1
                raise RuntimeError("roto")

        class Mod:
            Rivals = Agg
        with TemporaryDirectory() as d:
            root = self._store(d, [listed(1, 204, "t06", "RET-02", 7)])
            b = D.Builder(None, root=root, stores=("data/feed_history.jsonl",),
                          mods={"rivals": Mod()})
            s = b.build()
            self.assertEqual(b.aggs, {})              # se cayó solo, sin arrastrar nada
            self.assertEqual(s.rivals, {})
            self.assertEqual(s.oracle.coverage["events"], 1)

    def test_el_modulo_opcional_recibe_los_mismos_eventos(self):
        class Agg:
            def __init__(self):
                self.vistos = 0

            def ingest(self, events):
                self.vistos += len(list(events))

            def to_json(self):
                return {"teams": [{"team": "t06", "sample": self.vistos}]}

        class Mod:
            Rivals = Agg
        with TemporaryDirectory() as d:
            root = self._store(d, [listed(1, 204, "t06", "RET-02", 7),
                                   listed(2, 205, "t13", "RET-02", 9)])
            b = D.Builder(None, root=root, stores=("data/feed_history.jsonl",),
                          mods={"rivals": Mod()})
            s = b.build()
            self.assertEqual(s.rivals["teams"][0]["sample"], 2)
            self.assertIn("manos rivales", D.render_page(s))

    def test_load_optional_ignora_lo_que_no_existe(self):
        mods = D.load_optional(("no_existe_este_modulo", "json"))
        self.assertNotIn("no_existe_este_modulo", mods)
        self.assertIn("json", mods)


if __name__ == "__main__":
    unittest.main()
