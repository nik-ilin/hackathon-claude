#!/usr/bin/env python3
"""Capa fina entre `broker_engine` y la API: lee el libro y envía los cruces.

NO SE HA EJECUTADO NUNCA, ni en seco. Otra persona del equipo está jugando en
vivo con la clave; una escritura de prueba podría arruinarle la partida o
gastar la cuota. Se construyó, se documentó y se dejó quieto.

Toda la decisión vive en `broker_engine` (puro, sin red, 54 pruebas offline).
Aquí sólo hay: leer, registrar y enviar. Si algo se tiene que cambiar a las
23:00 de hoy, se cambia en el motor y se prueba sin tocar el servidor.

CÓMO SE USA (quien tenga la clave de broker)
--------------------------------------------
Requisito: el mercado tiene que ser `board`. En un mercado `auto` —el puesto
gratuito incluido— el motor cruza cada par antes de que el broker lea el
libro, así que este programa no tiene nada que hacer (RULES.md).

    export BAZAAR_URL=https://bazaar.causaprima.ai
    export BROKER_KEY=bk_...

    # 1. Mirar sin enviar nada. Imprime el libro, el plan y lo que haría el
    #    puesto gratuito al lado. Hace sólo GET. Empezar siempre por aquí.
    BROKER_KEY=bk_... python3 broker_run.py --dry-run --once

    # 2. Un tick de verdad, un envío como máximo, para ver la respuesta real.
    BROKER_KEY=bk_... python3 broker_run.py --send --once --max-matches 1

    # 3. La sesión entera. El Market Test dura 16 ticks y se repite cada 2 h.
    BROKER_KEY=bk_... python3 broker_run.py --send

Sin `--send` no se envía nada: `--dry-run` es el modo por defecto a propósito.
Cada cruce se registra en `data/broker_log.jsonl` ANTES de enviarse, con el
libro que lo justificó, para poder auditar después qué se mandó y por qué.

LÍMITES DE RITMO
----------------
- Un plan por estado del libro: si el libro no ha cambiado no se reenvía nada
  (un cruce rechazado no se reintenta cada segundo).
- `--max-matches` por tick (10 por defecto, el tope del tablón público).
- `--min-gap` segundos entre POST (0.25 s: la API corta por encima de 5 req/s).
- `--max-sends` corta el programa tras N envíos en total, como red de
  seguridad para una primera prueba.

POR QUÉ NO IMPORTA `bazaar_sdk`
-------------------------------
El add-on no depende de ningún módulo del repo (`test_addon_isolation` lo
comprueba): si mañana alguien cambia el SDK, el broker sigue funcionando. Las
dos rutas que necesita son dos líneas de `urllib`, y así se ven enteras aquí
mismo, con sus reintentos, en vez de a dos archivos de distancia. Se replica
el comportamiento del SDK en lo que importa: nunca se reintenta un POST a
ciegas (puede haber llegado) y se respeta `rate_limited`.

LO QUE NO ESTÁ VERIFICADO
-------------------------
No se ha leído un `GET /api/broker/book` real: sin clave responde 401
`{"error": "bad_key"}`. El parser de `broker_engine` está escrito contra el
formato que usa `starter_broker.py` (código oficial) y contra las ofertas
reales de `GET /api/venues/{id}/offers`, que sí se leyeron. Si el libro del
banco trae otra forma, `--dry-run --once` lo enseña en la primera línea
(`unsupported`) sin enviar nada: ese es el primer comando a correr.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

import broker_engine as be

LOG = pathlib.Path(__file__).resolve().parent / "data" / "broker_log.jsonl"
MATCHES = "/api/broker/matches"  # la única ruta de escritura de todo el add-on


class ApiError(Exception):
    """Una petición rechazada. `code` es el motivo del servidor ("bad_key", ...)."""

    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.status = code, message, status


class Api:
    """Las dos rutas del broker (header X-Broker-Key), con reintentos.

    `GET /api/broker/book` es idempotente y se reintenta. El envío de un
    emparejamiento NUNCA se reintenta tras un fallo de red: pudo haber llegado,
    y un cruce duplicado es exactamente el error que no se puede deshacer.
    """

    def __init__(self, url: str, broker_key: str, *, timeout: float = 15.0, retries: int = 3):
        self.url, self.timeout, self.retries = url.rstrip("/"), timeout, retries
        self.headers = {"X-Broker-Key": broker_key, "Accept": "application/json"}

    def _once(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def call(self, method: str, path: str, body: dict | None = None):
        for attempt in range(1, self.retries + 2):
            try:
                return self._once(method, path, body)
            except urllib.error.HTTPError as e:
                try:
                    payload = json.loads(e.read() or b"{}")
                except Exception:
                    payload = {}
                payload = payload if isinstance(payload, dict) else {}
                err = ApiError(str(payload.get("error") or f"http_{e.code}"),
                               str(payload.get("message") or e.reason), e.code)
                if err.code != "rate_limited" or attempt > self.retries:
                    raise err from None
                time.sleep(0.25 * attempt)
            except json.JSONDecodeError as e:
                raise ApiError("bad_response", f"{method} {path}: no es JSON ({e})") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                err = ApiError("network", f"{method} {path}: {e}")
                if method != "GET" or attempt > self.retries:
                    raise err from None  # una escritura pudo llegar: no se repite nunca
                time.sleep(0.5 * attempt)
        raise ApiError("network", f"{method} {path}: sin respuesta")

    def book(self) -> dict:
        return self.call("GET", "/api/broker/book")

    def clock(self) -> dict:
        return self.call("GET", "/api/clock")

    def send(self, match: be.Match) -> dict:
        return self.call("POST", MATCHES, match.as_payload())


def record(kind: str, payload: dict) -> None:
    """Deja constancia antes de actuar. `data/` está en .gitignore."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "kind": kind, **payload}, ensure_ascii=False) + "\n")


def describe(book: be.Book, plan: list[be.Match], base: list[be.Match]) -> str:
    """Lo que se imprime cada tick: el plan y, al lado, lo que haría el puesto."""
    model = be.estimate_limits(book.offers, be.BrokerConfig())
    ok, bad = be.legal_matches(base, book)
    lines = [
        f"tick {book.tick}  comisión {book.fees.bps} bps + {book.fees.per_card} P/carta  "
        f"banco {len(book.bench)}  tablón {len(book.public)}  sin entender {len(book.unsupported)}",
        f"  sombreado estimado θ={model.shade:.2f} ({model.source})  "
        f"puesto gratuito: {len(ok)} cruces legales, {len(bad)} que el motor rechazaría",
    ]
    for m in plan:
        lines.append(f"  {m.run:<12} {str(m.sell):>10} x {str(m.buy):<10} a {m.price:>5} P  "
                     f"excedente estimado {m.est_surplus:6.1f} P  {m.reason}")
    if book.unsupported:
        lines.append(f"  OJO: {len(book.unsupported)} ofertas con una forma que el parser no conoce; "
                     f"la primera: {json.dumps(book.unsupported[0], ensure_ascii=False)[:300]}")
    return "\n".join(lines) if plan else "\n".join(lines + ["  (nada que cruzar)"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--send", action="store_true",
                   help="enviar de verdad POST /api/broker/matches. Sin esto no se escribe nada.")
    p.add_argument("--dry-run", action="store_true", help="sólo leer (el modo por defecto)")
    p.add_argument("--once", action="store_true", help="un solo ciclo y salir")
    p.add_argument("--max-matches", type=int, default=10, help="cruces por tick (defecto 10)")
    p.add_argument("--max-sends", type=int, default=0, help="cortar tras N envíos en total (0 = sin tope)")
    p.add_argument("--min-gap", type=float, default=0.25, help="segundos entre POST (defecto 0.25)")
    p.add_argument("--poll", type=float, default=1.0, help="segundos entre lecturas del libro")
    p.add_argument("--policy", default="ask", choices=["ask", "mid_capped", "mid"],
                   help="precio de cruce. 'ask' es el legal más bajo y el que menos comisión fuga.")
    p.add_argument("--defer", action="store_true",
                   help="apartar cruces esperando que se relajen las cotizaciones. "
                        "Apagado por defecto: en el simulador rinde menos (ver test_broker_engine).")
    p.add_argument("--ticks", type=int, default=16, help="duración del banco, para --defer (bench.started: 16)")
    args = p.parse_args(argv)

    if not args.send:
        print("modo lectura: no se enviará ningún emparejamiento (usa --send para enviar)", file=sys.stderr)
    key = os.environ.get("BROKER_KEY")
    if not key:
        print("falta BROKER_KEY (la clave del mercado, header X-Broker-Key)", file=sys.stderr)
        return 2

    api = Api(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key)
    cfg = be.BrokerConfig(price_policy=args.policy, max_matches=args.max_matches, defer=args.defer)
    seen, sent, started = None, 0, None

    while True:
        try:
            raw = api.book()
            book = be.parse_book(raw)
            tick = book.tick if book.tick is not None else api.clock()["tick"]
            book = be.Book(tick, book.fees, book.bench, book.public, book.unsupported, book.raw)
            state = (tick, tuple(sorted(str(o.id) for o in book.offers)))
            if state != seen:  # un plan por estado del libro: nada se reintenta a ciegas
                seen = state
                started = started if started is not None else tick
                left = max(1, args.ticks - (tick - started)) if args.defer else None
                plan = be.plan(book, cfg, ticks_left=left)
                print(describe(book, plan, be.plan_quote_cross(book, cfg)), flush=True)
                record("plan", {"tick": tick, "matches": [m.as_payload() for m in plan],
                                "book": raw if args.dry_run or not args.send else None})
                for m in plan:
                    if args.max_sends and sent >= args.max_sends:
                        print(f"tope de --max-sends ({args.max_sends}) alcanzado; se para", flush=True)
                        return 0
                    record("send", {"tick": tick, **m.as_payload(), "est_surplus": m.est_surplus})
                    if not args.send:
                        continue
                    try:
                        api.send(m)
                        sent += 1
                    except ApiError as e:  # oferta tomada entre la lectura y el envío, forma que el
                        record("refused", {"tick": tick, **m.as_payload(),  # mercado no cruza, precio ilegal...
                                           "code": e.code, "message": e.message})
                        print(f"  rechazado {m.sell} x {m.buy} a {m.price}: {e}", flush=True)
                    time.sleep(args.min_gap)
        except ApiError as e:  # el servidor reiniciando: en un mercado board nada cruza sin nosotros
            print(f"no se pudo leer el libro ({e}); se reintenta", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
