#!/usr/bin/env python3
"""Pruebas de `feed_stream`, todas sin red y sin mocks de terceros.

El transporte se inyecta, así que basta una clase de prueba propia que
devuelva lo que haría el servidor (o que falle cuando toque). Los esquemas de
evento son los reales del feed, recortados a los campos que se usan.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import feed_stream as fs


# ------------------------------------------------------------- utilidades

def ev(eid: int, tick: int, type_: str = "offer.listed", **extra) -> dict:
    e = {"id": eid, "tick": tick, "t": tick / 72.0, "type": type_,
         "scope": "public", "actor": "t02", "payload": {}}
    e.update(extra)
    return e


def sse(*blocks: tuple[str, dict]) -> list[bytes]:
    """Convierte (nombre, objeto) en las líneas crudas que manda el servidor."""
    out: list[bytes] = []
    for name, obj in blocks:
        out.append(f"event: {name}\n".encode())
        out.append(f"data: {json.dumps(obj)}\n".encode())
        out.append(b"\n")
    return out


class FakeTransport:
    """Servidor de mentira. `polls` y `streams` se consumen uno por llamada."""

    def __init__(self, polls=None, streams=None):
        self.polls = list(polls or [])
        self.streams = list(streams or [])
        self.poll_calls = 0
        self.stream_calls = 0
        self.writes = 0          # debe quedarse en 0: esto nunca hace POST

    def poll(self, limit=fs.FEED_LIMIT):
        self.poll_calls += 1
        return self.polls.pop(0) if self.polls else []

    def clock(self):
        return {"tick_seconds": 15.0}

    def stream(self, *, read_timeout=90.0):
        self.stream_calls += 1
        if not self.streams:
            return iter(())
        nxt = self.streams.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return iter(nxt)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.path = Path(self._d.name) / "feed_history.jsonl"

    def tearDown(self):
        self._d.cleanup()

    def ledger(self):
        return fs.Ledger(self.path, dedup_window=1000)

    def lines(self):
        return [l for l in self.path.read_text(encoding="utf-8").splitlines() if l]


# ------------------------------------------------------------ parser SSE

class TestSSE(unittest.TestCase):
    def test_formato_real_del_servidor(self):
        raw = (b"event: hello\n"
               b'data: {"tick": 240, "scope": "public"}\n'
               b"\n"
               b"event: tick\n"
               b'data: {"id": 15053, "tick": 241, "type": "tick"}\n'
               b"\n")
        names = [n for n, _ in fs.parse_sse_blocks(raw.splitlines(keepends=True))]
        self.assertEqual(names, ["hello", "tick"])
        # `hello` no trae id: no es un evento del feed y se descarta
        got = list(fs.sse_events(raw.splitlines(keepends=True)))
        self.assertEqual([e["id"] for e in got], [15053])

    def test_data_multilinea_y_comentarios(self):
        raw = [b": keep-alive\n", b"event: x\n", b'data: {"id": 1,\n',
               b'data:  "tick": 2}\n', b"\n"]
        self.assertEqual([e["id"] for e in fs.sse_events(raw)], [1])

    def test_bloque_truncado_por_corte_no_explota(self):
        raw = [b"event: x\n", b'data: {"id": 1, "ti\n']   # sin línea vacía
        self.assertEqual(list(fs.sse_events(raw)), [])


# --------------------------------------------------------- huecos de id

class TestHuecos(Tmp):
    def test_salto_dentro_de_una_racha_es_privado(self):
        led = self.ledger()
        # Una sola tanda ininterrumpida: el hueco 101->105 son eventos de
        # ámbito privado, que consumen id y no se publican. No es pérdida.
        led.offer([ev(100, 10), ev(105, 10), ev(106, 11)])
        h = led.health()
        self.assertEqual(h["gaps_private"], 1)
        self.assertEqual(h["gaps_lost"], 0)
        self.assertEqual(h["gaps_suspect"], 0)
        self.assertEqual(h["ids_unseen"], 4)
        self.assertTrue(h["complete"])

    def test_salto_en_frontera_es_sospechoso(self):
        led = self.ledger()
        led.offer([ev(100, 10), ev(101, 10)])
        led.reconnected()                   # hubo un corte
        led.offer([ev(104, 10)])            # mismo tick: no falta ningún tick
        h = led.health()
        self.assertEqual(h["gaps_suspect"], 1)
        self.assertEqual(h["gaps_lost"], 0)
        self.assertEqual(h["ids_unseen_suspect"], 2)
        self.assertTrue(h["complete"])      # sospecha no es pérdida probada

    def test_ticks_enteros_ausentes_son_perdida_confirmada(self):
        led = self.ledger()
        led.offer([ev(100, 10)])
        led.reconnected()
        led.offer([ev(200, 20)])            # ticks 11..19 sin un solo evento
        h = led.health()
        self.assertEqual(h["gaps_lost"], 1)
        self.assertEqual(h["ids_unseen_lost"], 99)
        self.assertEqual(h["ticks_missing"], 9)
        self.assertEqual(h["ticks_missing_list"], list(range(11, 20)))
        self.assertFalse(h["complete"])
        self.assertIn("PERDIDO", str(led.gaps[0]))

    def test_frontera_tras_duplicados_sigue_siendo_frontera(self):
        # El backfill empieza casi siempre con eventos ya guardados; el salto
        # que importa viene después, y debe clasificarse como frontera.
        led = self.ledger()
        led.offer([ev(100, 10), ev(101, 10)])
        led.reconnected()
        led.offer([ev(100, 10), ev(101, 10), ev(110, 10)])
        h = led.health()
        self.assertEqual(h["gaps_suspect"], 1)
        self.assertEqual(h["gaps_private"], 0)

    def test_detalle_de_privados_no_se_acumula_en_memoria(self):
        led = self.ledger()
        led.offer([ev(2 * i, 10) for i in range(300)])
        self.assertEqual(led.counts["privado"], 299)
        self.assertEqual(led.gaps, [])      # sólo se detallan los que importan


# ----------------------------------------------------------- duplicados

class TestDuplicados(Tmp):
    def test_mismo_evento_dos_veces_se_escribe_una(self):
        led = self.ledger()
        led.persist(led.offer([ev(1, 1), ev(2, 1)]))
        led.persist(led.offer([ev(2, 1), ev(3, 1)]))   # 2 repetido
        self.assertEqual([json.loads(l)["id"] for l in self.lines()], [1, 2, 3])
        self.assertEqual(led.health()["events"], 3)

    def test_duplicado_no_inventa_un_hueco(self):
        led = self.ledger()
        led.offer([ev(1, 1), ev(2, 1), ev(3, 1)])
        led.offer([ev(2, 1)])
        self.assertEqual(led.health()["gaps_total"], 0)

    def test_tick_del_stream_cuenta_como_latido_pero_no_se_escribe(self):
        # `/api/feed` no incluye los eventos `tick`; escribirlos cambiaría el
        # contenido del archivo que otro proceso ya está leyendo.
        led = self.ledger()
        fresh = led.offer([ev(1, 7, "tick"), ev(2, 7)], source="stream")
        led.persist(fresh)
        self.assertEqual([json.loads(l)["id"] for l in self.lines()], [2])
        self.assertEqual(led.health()["live_ticks"], 1)
        self.assertIn(7, led.ticks_seen)


# ------------------------------------------------------------ jsonl roto

class TestJsonlRoto(Tmp):
    def test_linea_truncada_se_cuenta_y_el_resto_se_lee(self):
        self.path.write_text('{"id": 1, "tick": 1}\n'
                             '{"id": 2, "ti\n'
                             '{"id": 3, "tick": 1}\n', encoding="utf-8")
        res = fs.load_store(self.path)
        self.assertEqual([e["id"] for e in res.events], [1, 3])
        self.assertEqual(res.truncated, 1)
        led = self.ledger()
        led.adopt(res)
        self.assertEqual(led.health()["truncated_lines"], 1)

    def test_evento_sin_id_no_cuenta(self):
        self.path.write_text('{"tick": 1}\n{"id": 5, "tick": 1}\n', encoding="utf-8")
        res = fs.load_store(self.path)
        self.assertEqual(res.no_id, 1)
        self.assertEqual(len(res.events), 1)

    def test_archivo_sin_salto_final_no_funde_lineas(self):
        # Una caída a media escritura deja el archivo sin `\n` final; el
        # siguiente append debe abrir línea nueva, no pegarse a la basura.
        self.path.write_text('{"id": 1, "tick": 1}\n{"id": 2, "tic',
                             encoding="utf-8")
        fs.append_lines(self.path, [json.dumps(ev(3, 1))])
        res = fs.load_store(self.path)
        self.assertEqual([e["id"] for e in res.events], [1, 3])
        self.assertEqual(res.truncated, 1)   # la basura sigue aislada

    def test_append_siempre_escribe_lineas_completas(self):
        fs.append_lines(self.path, [json.dumps(ev(i, 1)) for i in range(1, 4)])
        raw = self.path.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(len(raw.splitlines()), 3)
        for l in raw.splitlines():
            json.loads(l)                    # ninguna a medias

    def test_append_vacio_no_crea_nada(self):
        self.assertEqual(fs.append_lines(self.path, []), 0)
        self.assertFalse(self.path.exists())


# ---------------------------------------------------------- reconexión

class TestReconexion(Tmp):
    def test_el_stream_cae_y_el_backfill_tapa_el_hueco(self):
        t = FakeTransport(
            polls=[
                [ev(1, 1), ev(2, 1)],                     # backfill inicial
                [ev(2, 1), ev(3, 2), ev(4, 2)],           # tras el corte
            ],
            streams=[
                sse(("offer.listed", ev(2, 1))),          # stream 1, se cierra
                ConnectionResetError("peer reset"),       # stream 2, revienta
            ])
        led = self.ledger()
        slept: list[float] = []
        c = fs.Collector(t, led, sleep=slept.append, log=lambda *_: None)
        h = c.run(max_cycles=2)
        self.assertEqual(t.poll_calls, 2)
        self.assertEqual(t.stream_calls, 2)
        self.assertEqual(slept, [1.0])                    # un reintento
        # El hueco lo tapó el sondeo, así que no falta ningún tick
        self.assertEqual([json.loads(l)["id"] for l in self.lines()], [1, 2, 3, 4])
        self.assertEqual(h["ticks_missing"], 0)
        self.assertTrue(h["complete"])
        self.assertEqual(t.writes, 0)                     # nunca escribe al juego

    def test_backoff_exponencial_con_techo(self):
        t = FakeTransport(polls=[], streams=[OSError("1"), OSError("2"),
                                            OSError("3"), OSError("4"),
                                            OSError("5")])
        slept: list[float] = []
        c = fs.Collector(t, self.ledger(), sleep=slept.append,
                         log=lambda *_: None)
        c.run(max_cycles=5, backoff_cap=4.0)
        self.assertEqual(slept, [1.0, 2.0, 4.0, 4.0, 4.0])

    def test_tras_reconectar_el_salto_es_sospechoso_no_privado(self):
        # Sin backfill que lo tape: el salto 2->9 cae en la frontera.
        t = FakeTransport(polls=[[], []],
                          streams=[sse(("x", ev(1, 1)), ("x", ev(2, 1))),
                                   sse(("x", ev(9, 1)))])
        led = self.ledger()
        c = fs.Collector(t, led, sleep=lambda *_: None, log=lambda *_: None)
        h = c.run(max_cycles=2)
        self.assertEqual(h["gaps_suspect"], 1)
        self.assertEqual(h["gaps_private"], 0)

    def test_modo_solo_sondeo(self):
        t = FakeTransport(polls=[[ev(1, 1)], [ev(2, 2)]])
        led = self.ledger()
        slept: list[float] = []
        c = fs.Collector(t, led, sleep=slept.append, log=lambda *_: None)
        c.run(use_stream=False, poll_every=7.0, max_cycles=2)
        self.assertEqual(t.stream_calls, 0)
        self.assertEqual(slept, [7.0, 7.0])
        self.assertEqual(len(self.lines()), 2)

    def test_ctrl_c_durante_la_recoleccion_para_limpio(self):
        class Boom(FakeTransport):
            def poll(self, limit=fs.FEED_LIMIT):
                super().poll(limit)
                raise KeyboardInterrupt

        t = Boom(polls=[[ev(1, 1)]])
        led = self.ledger()
        c = fs.Collector(t, led, sleep=lambda *_: None, log=lambda *_: None)
        h = c.run(max_cycles=5)
        self.assertEqual(t.poll_calls, 1)       # salió del bucle, no reintentó
        self.assertIsInstance(h, dict)


# -------------------------------------------------------------- margen

class TestMargen(Tmp):
    def _led_con_ritmo(self, por_tick: dict[int, int]) -> fs.Ledger:
        led = self.ledger()
        eid = 1
        batch = []
        for tick, n in sorted(por_tick.items()):
            for _ in range(n):
                batch.append(ev(eid, tick))
                eid += 1
        led.offer(batch)
        return led

    def test_margen_a_15s_por_tick(self):
        # 20 eventos/tick en todos los ticks: 500/20 = 25 ticks de ventana,
        # que a 15 s son 375 s = 6,25 min. Eso es todo el aire que hay.
        led = self._led_con_ritmo({t: 20 for t in range(1, 11)})
        h = led.health(tick_seconds=15.0)
        self.assertEqual(h["events_per_tick_median"], 20)
        self.assertEqual(h["margin_typical"]["ticks"], 25.0)
        self.assertEqual(h["margin_typical"]["seconds"], 375.0)

    def test_el_pico_manda_sobre_la_mediana(self):
        # Medido hoy: mediana 17, pico 40. El plazo real es el del pico.
        led = self._led_con_ritmo({1: 17, 2: 17, 3: 17, 4: 40})
        h = led.health(tick_seconds=15.0)
        self.assertEqual(h["events_per_tick_peak"], 40)
        self.assertEqual(h["margin_peak"]["ticks"], 12.5)
        self.assertEqual(h["margin_peak"]["seconds"], 187.5)
        self.assertLess(h["margin_peak"]["seconds"], h["margin_typical"]["seconds"])

    def test_el_tick_de_30s_da_el_doble_de_aire_que_el_de_15s(self):
        led = self._led_con_ritmo({t: 25 for t in range(1, 6)})
        a = led.health(tick_seconds=30.0)["margin_typical"]["seconds"]
        b = led.health(tick_seconds=15.0)["margin_typical"]["seconds"]
        self.assertAlmostEqual(a, b * 2)

    def test_sin_datos_no_se_inventa_un_margen(self):
        h = self.ledger().health(tick_seconds=15.0)
        self.assertIsNone(h["margin_typical"]["ticks"])
        self.assertIsNone(h["margin_typical"]["seconds"])
        self.assertEqual(h["ticks_missing"], 0)


# -------------------------------------------------------------- memoria

class TestMemoria(Tmp):
    def test_el_set_de_dedup_se_poda(self):
        # Horas de ejecución no deben hacer crecer el set sin control: lo que
        # queda fuera de la ventana el servidor ya no lo sirve nunca.
        led = fs.Ledger(self.path, dedup_window=50)
        for i in range(1, 601):
            led.offer([ev(i, i // 10 + 1)])
        self.assertLessEqual(len(led.seen), 101)
        self.assertEqual(led.high_water, 600)
        # y aun así sigue deduplicando lo reciente
        self.assertEqual(led.offer([ev(600, 61)]), [])
        # el recuento no se poda con el set: sería afirmar menos de lo que hay
        self.assertEqual(led.health()["events"], 600)
        self.assertEqual(led.health()["gaps_total"], 0)


# ------------------------------------------------------- informe legible

class TestInforme(Tmp):
    def test_el_informe_nombra_los_huecos_en_vez_de_taparlos(self):
        led = self.ledger()
        led.offer([ev(100, 10)])
        led.reconnected()
        led.offer([ev(200, 20)])
        out: list[str] = []
        fs.print_health(led.health(tick_seconds=15.0), gaps=led.gaps,
                        out=out.append)
        txt = "\n".join(out)
        self.assertIn("INCOMPLETO", txt)
        self.assertIn("PERDIDO", txt)
        self.assertIn("AUSENTES", txt)

    def test_informe_limpio_no_dice_perdido(self):
        led = self.ledger()
        led.offer([ev(i, 1 + i // 5) for i in range(1, 21)])
        out: list[str] = []
        fs.print_health(led.health(tick_seconds=15.0), gaps=led.gaps,
                        out=out.append)
        self.assertIn("COMPLETO", "\n".join(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
