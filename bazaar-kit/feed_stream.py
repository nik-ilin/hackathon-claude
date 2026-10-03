#!/usr/bin/env python3
"""Recolección robusta del feed público, con detección de huecos.

Add-on independiente: no modifica nada del repo, importa `feed_oracle` sólo
para leer, y **nunca escribe en el juego**. Sin POST, sin clave de equipo.

    python3 feed_stream.py                 # informe de salud, sin red
    python3 feed_stream.py --probe         # ¿sirve el SSE sin clave? formato
    python3 feed_stream.py --collect       # backfill + SSE hasta Ctrl-C
    python3 feed_stream.py --collect --poll-only
    python3 -m unittest test_feed_stream

Por qué existe
--------------
`feed_watch.py` sondea `/api/feed?limit=500`. El servidor nunca da más de 500
eventos, y con mediana 17 y máximo 40 eventos públicos por tick eso son ~30
ticks de ventana. El domingo el tick baja a 15 s: la misma ventana son ~7
minutos de reloj. Un sondeo perdido abre un hueco **permanente**: el feed no
tiene parámetro de rebobinado (comprobado: `since`, `after` y `Last-Event-ID`
los ignora). Dos defensas:

1. `GET /api/events/stream` **funciona sin clave** (`scope: "public"`) y empuja
   cada evento en el momento, así que no hay ventana que agotar.
2. Lo que el stream no puede dar —lo ocurrido mientras estábamos desconectados—
   se rellena con un sondeo a `/api/feed` en cada (re)conexión, y lo que ni así
   se recupera **se cuenta y se publica** como hueco en vez de disimularse.

Honestidad sobre los huecos
---------------------------
Los `id` son incrementales pero el feed público **no es contiguo**: los eventos
de ámbito privado consumen `id` y no se publican. En el historial real hay 409
saltos de `id` y cero ticks ausentes, así que un salto de `id` por sí solo no
es pérdida. Se distinguen tres cosas, y se informan separadas:

- `ticks ausentes`  → pérdida **confirmada**: no se vio ningún evento público
  de un tick que está dentro del rango observado.
- `saltos en frontera` → el salto cae donde se interrumpió la recolección
  (arranque, reconexión, caída). Sospechoso: puede ser pérdida.
- `saltos internos` → el salto ocurrió dentro de una racha de observación
  ininterrumpida, luego son eventos privados. **No** es pérdida nuestra.

Nunca se afirma cobertura que no se tiene: `health()` devuelve los tres números
y `missing_ticks` manda sobre los demás.

Convivencia con `feed_watch.py`
-------------------------------
Mismo `data/feed_history.jsonl`, mismo formato (una línea JSON por evento),
dedup por `id`, y **sólo se añade** al final: otro proceso lo está leyendo.
Los eventos `type: "tick"` que trae el stream (y que `/api/feed` no incluye)
se usan en memoria como latido para medir cobertura, pero **no** se escriben,
para que el archivo siga conteniendo exactamente lo que ya contenía.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from feed_oracle import Oracle

HERE = Path(__file__).resolve().parent
STORE = HERE / "data" / "feed_history.jsonl"
DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")

#: Techo duro del servidor: `/api/feed` nunca devuelve más de esto.
FEED_LIMIT = 500
#: Tick del domingo, el caso malo que hay que poder resistir.
SUNDAY_TICK_SECONDS = 15.0
#: Cuántos `id` recientes se recuerdan para deduplicar. El servidor no sirve
#: nada anterior a los últimos 500 eventos, así que recordar 10x eso es de
#: sobra y mantiene la memoria plana aunque el proceso corra horas.
DEDUP_WINDOW = 5000


# ------------------------------------------------------------------ SSE

def parse_sse_blocks(lines: Iterable[bytes]) -> Iterator[tuple[str, str]]:
    """Trocea líneas crudas de SSE en pares `(nombre, data)`.

    Función pura: no sabe de red. Un bloque termina en línea vacía; `data:`
    puede repetirse y se concatena con `\\n`, como manda el protocolo. Un
    bloque sin `data` se descarta: el servidor manda `event: hello` con datos
    pero un comentario `:` suelto no lleva ninguno.
    """
    name = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        if line == "":
            if data:
                yield name, "\n".join(data)
            name, data = "message", []
            continue
        if line.startswith(":"):
            continue                      # comentario / keep-alive
        field_, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_ == "event":
            name = value
        elif field_ == "data":
            data.append(value)
        # `id` y `retry` se ignoran: este servidor no admite rebobinado
    if data:                              # el stream se cortó sin línea vacía
        yield name, "\n".join(data)


def sse_events(lines: Iterable[bytes]) -> Iterator[dict]:
    """Eventos del juego que trae el stream, ya decodificados.

    Filtra lo que no es un evento del feed: `hello` no trae `id`, y sin `id`
    no hay dedup ni secuencia posible, así que no sirve para nada.
    """
    for name, data in parse_sse_blocks(lines):
        if name == "hello":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue                      # bloque partido por un corte de red
        if isinstance(obj, dict) and obj.get("id") is not None:
            yield obj


# ------------------------------------------------------------- transporte

class HttpTransport:
    """La única parte con red, y sólo de lectura. Nunca hace POST."""

    def __init__(self, url: str = DEFAULT_URL, key: Optional[str] = None,
                 timeout: float = 20.0) -> None:
        self.url = url.rstrip("/")
        self.key = key or None
        self.timeout = timeout

    def _request(self, path: str, *, accept: Optional[str] = None):
        req = urllib.request.Request(self.url + path)
        if self.key:
            req.add_header("X-Team-Key", self.key)
        if accept:
            req.add_header("Accept", accept)
        return req

    def _json(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(self._request(path), timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # La clave de .env está rechazada (bad_key). El feed es público:
            # reintentar sin clave en vez de quedarnos sin datos por eso.
            if e.code in (401, 403) and self.key:
                self.key = None
                with urllib.request.urlopen(self._request(path), timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            raise

    def poll(self, limit: int = FEED_LIMIT) -> list[dict]:
        return self._json(f"/api/feed?limit={limit}").get("events") or []

    def clock(self) -> dict:
        try:
            return self._json("/api/clock")
        except Exception:
            return {}

    def stream(self, *, read_timeout: float = 90.0) -> Iterator[bytes]:
        """Líneas crudas del SSE. Cierra sola si el servidor calla demasiado.

        `read_timeout` es generoso a propósito: el servidor emite un evento
        `tick` cada tick, así que un silencio largo ya es una conexión muerta.
        """
        req = self._request("/api/events/stream", accept="text/event-stream")
        resp = urllib.request.urlopen(req, timeout=read_timeout)
        try:
            for line in resp:
                yield line
        finally:
            resp.close()


# ------------------------------------------------------------ persistencia

def _defer_interrupt():
    """Contexto que aplaza Ctrl-C para no partir una línea del jsonl.

    Devuelve un par (entrar, salir). Si el hilo no es el principal (p. ej. en
    un test) `signal` falla: no pasa nada, se sigue sin aplazar.
    """
    pending = []

    def handler(sig, frame):
        pending.append(sig)

    try:
        prev = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        return (lambda: None), (lambda: None)

    def restore():
        signal.signal(signal.SIGINT, prev)
        if pending:
            raise KeyboardInterrupt

    return (lambda: None), restore


def append_lines(path: Path, lines: list[str]) -> int:
    """Añade líneas completas al jsonl, o ninguna. Nunca reescribe.

    Tres cuidados, porque otro proceso lee este archivo y un Ctrl-C puede caer
    en cualquier instante:

    1. Si el archivo no acaba en `\\n` (línea truncada por una caída anterior)
       se añade uno antes de escribir, para no fundir basura con un evento
       bueno. Es un append, no una reescritura.
    2. Todo el lote va en un solo buffer y `os.write` se reintenta hasta
       agotarlo, así que no queda una línea a medias por una escritura corta.
    3. Ctrl-C se aplaza durante la escritura y se vuelve a lanzar después.
    """
    if not lines:
        return 0
    blob = "".join(l if l.endswith("\n") else l + "\n" for l in lines).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                blob = b"\n" + blob
    _enter, _exit = _defer_interrupt()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(blob)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
        _exit()
    return len(lines)


@dataclass
class LoadResult:
    events: list[dict]
    truncated: int = 0          # líneas ilegibles: una caída a media escritura
    no_id: int = 0


def load_store(path: Path = STORE) -> LoadResult:
    """Lee el historial tolerando una última línea truncada, y lo **cuenta**.

    `feed_watch.load_store` también la salta, pero en silencio; aquí el número
    sale en el informe, porque una línea perdida es un evento perdido.
    """
    res = LoadResult([])
    if not path.exists():
        return res
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                res.truncated += 1
                continue
            if not isinstance(obj, dict) or obj.get("id") is None:
                res.no_id += 1
                continue
            res.events.append(obj)
    return res


# -------------------------------------------------------------- huecos

@dataclass
class Gap:
    """Un salto en la secuencia de `id`. `boundary` = cayó donde paramos."""
    lo: int                     # último id visto antes del salto
    hi: int                     # primer id visto después
    tick_lo: Optional[int]
    tick_hi: Optional[int]
    boundary: bool

    @property
    def missing(self) -> int:
        return self.hi - self.lo - 1

    @property
    def lost_ticks(self) -> int:
        """Ticks públicos perdidos con seguridad dentro de este salto."""
        if self.tick_lo is None or self.tick_hi is None:
            return 0
        return max(0, self.tick_hi - self.tick_lo - 1)

    @property
    def verdict(self) -> str:
        if self.lost_ticks:
            return "PERDIDO"        # faltan ticks enteros: sin discusión
        if self.boundary:
            return "sospechoso"     # frontera de recolección: pudo perderse
        return "privado"            # racha intacta: eventos de ámbito privado

    def __str__(self) -> str:
        t = f" ticks {self.tick_lo}->{self.tick_hi}" if self.tick_lo is not None else ""
        return (f"ids {self.lo}->{self.hi} ({self.missing} ausentes{t}) "
                f"[{self.verdict}]")


class Ledger:
    """Libro de cobertura: qué se tiene, qué falta, y qué se acaba de añadir.

    Memoria plana a propósito: el set de dedup se poda a los últimos
    `DEDUP_WINDOW` ids, porque el servidor no sirve nada más antiguo, y los
    huecos se guardan agregados (son pocos) en vez de evento por evento. Así
    el proceso aguanta horas sin crecer.
    """

    def __init__(self, path: Path = STORE, *, dedup_window: int = DEDUP_WINDOW):
        self.path = path
        self.dedup_window = dedup_window
        self.seen: set[int] = set()
        self.high_water: int = 0
        #: Sólo se guarda el detalle de los saltos que importan (perdidos y
        #: sospechosos). Los privados son cientos y todos iguales: basta
        #: contarlos, y así la memoria no crece con las horas.
        self.gaps: list[Gap] = []
        self.counts: dict[str, int] = {"PERDIDO": 0, "sospechoso": 0, "privado": 0}
        self.unseen: dict[str, int] = {"PERDIDO": 0, "sospechoso": 0, "privado": 0}
        self.ticks_seen: set[int] = set()
        self.live_ticks: set[int] = set()      # latidos `tick` vistos por SSE
        self.per_tick: dict[int, int] = {}     # eventos públicos por tick
        self.written = 0
        self.truncated = 0
        self.adopted = 0
        #: Contador propio: `seen` se poda, así que no sirve para contar.
        self.total = 0
        #: La próxima tanda abre frontera: al arrancar y tras cada reconexión
        #: no sabemos qué pasó mientras no mirábamos.
        self._boundary = True
        self._last_id: Optional[int] = None
        self._last_tick: Optional[int] = None

    # -- carga del historial ya existente --------------------------------
    def adopt(self, res: LoadResult) -> int:
        """Incorpora el historial en disco sin volver a escribirlo."""
        self.truncated += res.truncated
        got = self._absorb(res.events, boundary_first=True)
        self.adopted = len(got)
        self._boundary = True      # lo que venga tras el historial es frontera
        return len(got)

    # -- ingesta ----------------------------------------------------------
    def offer(self, events: Iterable[dict], *, source: str = "poll") -> list[dict]:
        """Clasifica una tanda y devuelve los eventos nuevos a persistir.

        Los `tick` se registran como latido pero no se devuelven: no están en
        `/api/feed` y escribirlos cambiaría el contenido del archivo que otro
        proceso ya está leyendo.
        """
        batch = sorted((e for e in events if e.get("id") is not None),
                       key=lambda e: e["id"])
        fresh = self._absorb(batch, boundary_first=self._boundary)
        if batch:
            self._boundary = False
        if source == "stream":
            for e in batch:
                if e.get("type") == "tick" and e.get("tick") is not None:
                    self.live_ticks.add(int(e["tick"]))
        return [e for e in fresh if e.get("type") != "tick"]

    def _absorb(self, batch: list[dict], *, boundary_first: bool) -> list[dict]:
        fresh = []
        # Frontera = hasta que entre el primer evento NUEVO. Un backfill empieza
        # casi siempre con duplicados (la ventana de 500 solapa lo que ya hay),
        # así que contar iteraciones en vez de eventos nuevos clasificaría mal
        # justo el salto que más importa.
        at_boundary = boundary_first
        for e in batch:
            eid = int(e["id"])
            if eid in self.seen or (self.high_water and eid <= self.high_water
                                    - self.dedup_window):
                continue                   # duplicado, o tan viejo que ya pasó
            tick = e.get("tick")
            tick = int(tick) if tick is not None else None
            if self._last_id is not None and eid > self._last_id + 1:
                self._record_gap(Gap(self._last_id, eid, self._last_tick, tick,
                                     boundary=at_boundary))
            at_boundary = False
            self.seen.add(eid)
            self.high_water = max(self.high_water, eid)
            if tick is not None:
                self.ticks_seen.add(tick)
                if e.get("type") != "tick":
                    self.per_tick[tick] = self.per_tick.get(tick, 0) + 1
            if self._last_id is None or eid > self._last_id:
                self._last_id, self._last_tick = eid, tick
            fresh.append(e)
            self.total += 1
        self._prune()
        return fresh

    def _record_gap(self, g: Gap) -> None:
        v = g.verdict
        self.counts[v] += 1
        self.unseen[v] += g.missing
        if v != "privado" and len(self.gaps) < 500:
            self.gaps.append(g)

    def _prune(self) -> None:
        """Poda el set de dedup. Lo que cae fuera ya no puede volver a venir."""
        if len(self.seen) <= self.dedup_window * 2:
            return
        floor = self.high_water - self.dedup_window
        self.seen = {i for i in self.seen if i > floor}

    def reconnected(self) -> None:
        """Marca que hubo un corte: el siguiente salto es frontera, no privado."""
        self._boundary = True

    # -- escritura --------------------------------------------------------
    def persist(self, events: list[dict]) -> int:
        n = append_lines(self.path,
                         [json.dumps(e, ensure_ascii=False) for e in events])
        self.written += n
        return n

    # -- salud ------------------------------------------------------------
    @property
    def missing_ticks(self) -> list[int]:
        """Ticks del rango observado sin ningún evento público. Pérdida real."""
        if not self.ticks_seen:
            return []
        lo, hi = min(self.ticks_seen), max(self.ticks_seen)
        return [t for t in range(lo, hi + 1) if t not in self.ticks_seen]

    def health(self, *, tick_seconds: float = SUNDAY_TICK_SECONDS,
               feed_limit: int = FEED_LIMIT) -> dict:
        rates = sorted(self.per_tick.values())
        median = statistics.median(rates) if rates else 0.0
        peak = rates[-1] if rates else 0
        # Margen = cuántos ticks caben en la ventana de 500 del feed. Con el
        # pico, porque el margen que importa es el del peor momento.
        def margin(rate: float) -> dict:
            if rate <= 0:
                return {"ticks": None, "seconds": None}
            t = feed_limit / rate
            return {"ticks": round(t, 1), "seconds": round(t * tick_seconds, 1)}

        miss = self.missing_ticks
        return {
            "events": self.total,
            "adopted": self.adopted,
            "written_this_run": self.written,
            "truncated_lines": self.truncated,
            "tick_min": min(self.ticks_seen) if self.ticks_seen else None,
            "tick_max": max(self.ticks_seen) if self.ticks_seen else None,
            "ticks_covered": len(self.ticks_seen),
            "ticks_missing": len(miss),
            "ticks_missing_list": miss[:40],
            "live_ticks": len(self.live_ticks),
            "events_per_tick_median": median,
            "events_per_tick_peak": peak,
            "gaps_total": sum(self.counts.values()),
            "gaps_lost": self.counts["PERDIDO"],
            "gaps_suspect": self.counts["sospechoso"],
            "gaps_private": self.counts["privado"],
            "ids_unseen": sum(self.unseen.values()),
            "ids_unseen_lost": self.unseen["PERDIDO"],
            "ids_unseen_suspect": self.unseen["sospechoso"],
            "tick_seconds": tick_seconds,
            "margin_typical": margin(median),
            "margin_peak": margin(peak),
            # Completo sólo si no falta ningún tick Y ningún salto perdió ticks.
            # Un salto "sospechoso" no basta para declarar pérdida, pero
            # tampoco se oculta: sale contado en el informe.
            "complete": not miss and not self.counts["PERDIDO"],
        }


# ------------------------------------------------------------- recolector

class Collector:
    """Backfill por sondeo + SSE en vivo, con reconexión y respaldo a sondeo.

    El transporte se inyecta para que los tests corran sin red.
    """

    def __init__(self, transport, ledger: Ledger, *,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print) -> None:
        self.t = transport
        self.led = ledger
        self.sleep = sleep
        self.log = log

    def backfill(self, limit: int = FEED_LIMIT) -> int:
        """Rellena por `/api/feed` lo ocurrido mientras no escuchábamos.

        Es la única vía: el stream no rebobina (ni `since`, ni `after`, ni
        `Last-Event-ID`), sólo empuja desde el momento de conectar.
        """
        events = self.t.poll(limit)
        fresh = self.led.offer(events, source="poll")
        self.led.persist(fresh)
        return len(fresh)

    def run(self, *, use_stream: bool = True, poll_every: float = 10.0,
            max_cycles: Optional[int] = None, backoff_cap: float = 30.0) -> dict:
        """Recoge hasta Ctrl-C. `max_cycles` existe para los tests."""
        cycle = 0
        backoff = 1.0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            try:
                # Primero el sondeo: tapa el agujero del arranque o del corte
                # anterior antes de ponerse a escuchar.
                n = self.backfill()
                if n:
                    self.log(f"  backfill: +{n}")
                if use_stream:
                    got = self._consume_stream()
                    self.log(f"  stream cerrado tras {got} eventos")
                else:
                    self.sleep(poll_every)
                backoff = 1.0
            except KeyboardInterrupt:
                self.log("\ndetenido; el historial queda consistente")
                break
            except Exception as exc:
                self.log(f"  corte ({type(exc).__name__}: {exc}); "
                         f"reintento en {backoff:.0f}s")
                self.sleep(backoff)
                backoff = min(backoff * 2, backoff_cap)
            finally:
                # Sea corte limpio o sucio, dejamos de ver: el siguiente salto
                # de id es frontera y hay que tratarlo como sospechoso.
                self.led.reconnected()
        return self.led.health()

    def _consume_stream(self) -> int:
        """Escucha el SSE y persiste en cuanto llega. Nada se queda en RAM."""
        got = 0
        for e in sse_events(self.t.stream()):
            fresh = self.led.offer([e], source="stream")
            if fresh:
                self.led.persist(fresh)
            got += 1
        return got


# ---------------------------------------------------------------- informe

def print_health(h: dict, *, gaps: list[Gap], oracle: Optional[Oracle] = None,
                 out=print) -> None:
    out("\n== SALUD DE LA RECOLECCIÓN ==")
    out(f"eventos únicos      {h['events']}"
        + (f"  (+{h['written_this_run']} escritos ahora)" if h["written_this_run"] else ""))
    out(f"ticks               {h['tick_min']}-{h['tick_max']} · "
        f"{h['ticks_covered']} cubiertos · {h['ticks_missing']} AUSENTES")
    if h["ticks_missing_list"]:
        out(f"  ticks sin un solo evento público: {h['ticks_missing_list']}")
    if h["live_ticks"]:
        out(f"latidos por SSE     {h['live_ticks']} ticks confirmados en vivo")
    if h["truncated_lines"]:
        out(f"líneas ilegibles    {h['truncated_lines']} (caída a media escritura)")
    out(f"eventos/tick        mediana {h['events_per_tick_median']:.0f} · "
        f"pico {h['events_per_tick_peak']}")

    out(f"\nsaltos de id        {h['gaps_total']} "
        f"({h['ids_unseen']} ids no vistos)")
    out(f"  PERDIDO           {h['gaps_lost']:>4}  ← faltan ticks enteros: "
        f"{h['ids_unseen_lost']} ids irrecuperables")
    out(f"  sospechoso        {h['gaps_suspect']:>4}  ← en una frontera de "
        f"recolección ({h['ids_unseen_suspect']} ids); pudo ser pérdida")
    out(f"  privado           {h['gaps_private']:>4}  ← dentro de una racha "
        f"intacta: eventos de ámbito privado, no es pérdida nuestra")
    if h.get("adopted"):
        out(f"  (de los {h['adopted']} eventos que ya estaban en disco no se sabe "
            f"dónde se interrumpió el proceso anterior: sus saltos salen como "
            f"'privado'. El juez ahí son los ticks ausentes, no los ids.)")
    worst = sorted((g for g in gaps if g.verdict != "privado"),
                   key=lambda g: (-g.lost_ticks, -g.missing))[:8]
    for g in worst:
        out(f"    {g}")

    mt, mp = h["margin_typical"], h["margin_peak"]
    out(f"\nmargen a {h['tick_seconds']:.0f} s/tick con ventana de {FEED_LIMIT}:")
    if mt["ticks"]:
        out(f"  al ritmo mediano  {mt['ticks']} ticks = "
            f"{mt['seconds'] / 60:.1f} min sin sondear antes de perder datos")
    if mp["ticks"]:
        out(f"  al ritmo de pico  {mp['ticks']} ticks = "
            f"{mp['seconds'] / 60:.1f} min  ← este es el plazo real")
    out("  con SSE no hay ventana que agotar; el margen sólo aplica al respaldo.")

    out(f"\nveredicto           "
        + ("historial COMPLETO en el rango observado"
           if h["complete"] else
           "historial INCOMPLETO: hay huecos contados arriba"))
    if oracle is not None:
        c = oracle.coverage
        out(f"conocimiento        {c['cards']} cartas · {c['settled']} "
            f"liquidaciones · {c['dealer_lines']} líneas de dealer")


def probe(transport: HttpTransport, *, seconds: float = 20.0, out=print) -> dict:
    """Comprueba en vivo si el SSE sirve sin clave y con qué formato."""
    transport.key = None
    out("sondeando /api/events/stream sin clave…")
    names: dict[str, int] = {}
    ids: list[int] = []
    t0 = time.time()
    lines: list[bytes] = []
    try:
        # Holgura grande: entre ticks el stream calla, y en silencio un
        # timeout corto haría pasar por roto algo que funciona.
        for line in transport.stream(read_timeout=seconds + 45):
            lines.append(line)
            if time.time() - t0 > seconds:
                break
    except Exception as exc:
        out(f"  NO usable: {type(exc).__name__}: {exc}")
        return {"usable": False, "error": str(exc)}
    for name, data in parse_sse_blocks(lines):
        names[name] = names.get(name, 0) + 1
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("id") is not None:
            ids.append(obj["id"])
    out(f"  usable sin clave: {bool(ids)}")
    out(f"  formato: SSE, bloques 'event: <nombre>' + 'data: <json>', "
        f"separados por línea vacía; sin línea 'id:' ni rebobinado")
    out(f"  tipos vistos: {names}")
    if ids:
        out(f"  ids {min(ids)}-{max(ids)} en {seconds:.0f}s "
            f"({len(ids)} eventos con id)")
    else:
        out("  conectó pero no llegó ningún evento con id en la ventana; "
            "fuera de horario de juego es lo normal")
    return {"usable": bool(ids), "names": names, "ids": ids}


# ------------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--collect", action="store_true",
                    help="recoger hasta Ctrl-C (backfill + SSE)")
    ap.add_argument("--probe", action="store_true",
                    help="comprobar el SSE y salir")
    ap.add_argument("--poll-only", action="store_true",
                    help="no usar SSE; sólo sondear /api/feed")
    ap.add_argument("--poll-every", type=float, default=10.0,
                    help="segundos entre sondeos con --poll-only")
    ap.add_argument("--tick-seconds", type=float, default=0.0,
                    help="para el margen (0 = /api/clock, o 15 sin red)")
    ap.add_argument("--store", default=str(STORE))
    a = ap.parse_args(argv)

    key = os.environ.get("BAZAAR_KEY") or None
    if key in ("tk-xxxx-xxxx", ""):
        key = None
    t = HttpTransport(a.url, key)

    if a.probe:
        return 0 if probe(t)["usable"] else 1

    path = Path(a.store)
    if not path.is_absolute():
        path = HERE / path
    led = Ledger(path)
    res = load_store(path)
    led.adopt(res)
    print(f"historial: {len(res.events)} eventos leídos de {path.name}"
          + (f" ({res.truncated} líneas ilegibles)" if res.truncated else ""))

    tick_seconds = a.tick_seconds
    if a.collect:
        # Un `kill` o cerrar la terminal manda SIGTERM: tratarlo como Ctrl-C
        # para salir por el camino limpio e imprimir el informe igualmente.
        try:
            signal.signal(signal.SIGTERM,
                          lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        except (ValueError, OSError):
            pass
        if tick_seconds <= 0:
            tick_seconds = float(t.clock().get("tick_seconds") or SUNDAY_TICK_SECONDS)
        print("recogiendo; Ctrl-C para parar sin corromper nada")
        Collector(t, led).run(use_stream=not a.poll_only,
                              poll_every=a.poll_every)
    if tick_seconds <= 0:
        tick_seconds = SUNDAY_TICK_SECONDS

    o = Oracle()
    o.ingest(load_store(path).events)
    print_health(led.health(tick_seconds=tick_seconds), gaps=led.gaps, oracle=o)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
