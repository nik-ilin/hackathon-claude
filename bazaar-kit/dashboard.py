#!/usr/bin/env python3
"""Panel de mando para quien negocia en vivo.

    python3 dashboard.py          # y abre http://localhost:8765

Add-on independiente y de **sólo lectura**: no importa ningún módulo del
repo (los opcionales se cargan con `importlib` y su ausencia no rompe nada),
no escribe en el juego y no reescribe los `.jsonl` que otro proceso está
alimentando. Sólo biblioteca estándar: arranca en un portátil sin preparar.

Qué resuelve, y por qué así:

- Quien usa esto tiene ~30 s entre tick y tick y está escribiendo a un
  dealer. No es un informe: es una pantalla que se mira de reojo. De ahí el
  orden de los paneles (reloj → qué hacer ahora → chuleta → mercado) y la
  tipografía grande en lo accionable.
- Todo lo numérico lleva su confianza al lado. Un suelo con n=1 es una
  anécdota, y la pantalla lo dice en vez de dejarlo en la letra pequeña.
- La página se recarga sola cada tick; el buscador y la pausa de recarga
  sobreviven a la recarga para que no se pierda lo que estabas mirando.

Lectura de datos: `data/feed_history.jsonl` + `data/feed_spool.jsonl` (lo que
recogen `feed_watch.py` y `feed_stream.py`), deduplicado por `id`, más un
sondeo de `/api/feed` en cada refresco para ir al día.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Optional

from feed_oracle import Oracle

HERE = Path(__file__).resolve().parent
DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
DEFAULT_PORT = 8765
DEFAULT_TEAM = os.environ.get("BAZAAR_TEAM", "t15")

#: Los dos almacenes del add-on. Se leen; nunca se escriben desde aquí.
STORES = ("data/feed_history.jsonl", "data/feed_spool.jsonl")

#: Verificado contra `/api/clock`: dos ticks mueven el reloj 0.0167 h, o sea
#: 120 ticks por hora de juego, independientemente de `tick_seconds`. Con eso
#: un `at_hours` de `/api/schedule` se traduce a minutos reales de verdad.
TICKS_PER_HOUR = 120.0

#: Límites del auto-refresco. Por debajo de 15 s la página parpadea más de lo
#: que informa; por encima de 30 s se queda vieja dentro de un tick.
REFRESH_MIN, REFRESH_MAX = 15.0, 30.0

#: Módulos hermanos que pueden existir o no: se están escribiendo en paralelo.
OPTIONAL = ("playbook", "rivals", "signals", "feed_stream")

#: Orden de fiabilidad, para que la chuleta empiece por lo que está probado.
CONF_RANK = {"SETTLED": 5, "FINAL": 4, "HIGH": 3, "MEDIUM": 2, "RARITY": 1,
             "LOW": 1, "UNKNOWN": 0, "NONE": 0}


# --------------------------------------------------------------------- red

class ReadOnlyClient:
    """Lector de los endpoints públicos. Aquí sólo existe el GET.

    Otra persona del equipo está jugando en vivo con la clave: una escritura
    nuestra le arruinaría la partida, así que esta clase no sabe escribir.
    La clave no se envía: `/api/feed` y compañía son públicos y la del `.env`
    está rechazada por el servidor.
    """

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 12.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str) -> dict:
        req = urllib.request.Request(self.url + path)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------- lectura incremental

class Tail:
    """Lee sólo lo nuevo de cada `.jsonl`, sin tocarlo.

    Otro proceso está escribiendo esos archivos ahora mismo, así que la
    última línea puede estar a medias: se corta en el último `\\n` y el resto
    se deja para la pasada siguiente. Si el archivo encoge (rotación), se
    vuelve a leer desde el principio.
    """

    def __init__(self) -> None:
        self.offsets: dict[Path, int] = {}
        self.counts: dict[str, int] = {}

    def read(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        pos = self.offsets.get(path, 0)
        size = path.stat().st_size
        if size < pos:
            pos = 0
        with path.open("rb") as fh:
            fh.seek(pos)
            data = fh.read()
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        self.offsets[path] = pos + cut + 1
        out = []
        for raw in data[:cut].split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue           # una línea truncada no invalida el resto
        self.counts[path.name] = self.counts.get(path.name, 0) + len(out)
        return out


def attr(obj: Any, name: str) -> Any:
    """`getattr` que nunca propaga. Un módulo a medio escribir puede explotar
    en el propio acceso al atributo, y eso no debe tumbar un panel."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def load_optional(names: Iterable[str] = OPTIONAL) -> dict[str, Any]:
    """Importa los módulos hermanos que existan. Nunca propaga un fallo.

    Se usa `importlib` a propósito: un `import playbook` arriba convertiría
    un módulo opcional en obligatorio en cuanto alguien lo renombre.
    """
    mods: dict[str, Any] = {}
    for name in names:
        try:
            mods[name] = importlib.import_module(name)
        except Exception:          # ImportError, o un módulo a medio escribir
            continue
    return mods


# ------------------------------------------------------------------ snapshot

@dataclass
class Snapshot:
    """Todo lo que la página necesita, ya recogido. Render sin red."""
    oracle: Oracle
    built_at: float = field(default_factory=time.time)
    clock: dict = field(default_factory=dict)
    schedule: dict = field(default_factory=dict)
    leaderboard: dict = field(default_factory=dict)
    venues: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    team: str = DEFAULT_TEAM
    catalog_refs: int = 0
    sources: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    mods: dict = field(default_factory=dict)
    rivals: dict = field(default_factory=dict)
    signals_text: str = ""
    build_ms: int = 0


class Builder:
    """Mantiene el Oracle vivo entre refrescos y lo pone al día.

    El Oracle es acumulativo e idempotente por `id`, así que volver a meterle
    lo mismo no cuesta nada; lo caro sería releer 700 KB de `.jsonl` cada 15 s,
    y de eso se encarga `Tail`.
    """

    def __init__(self, client: Optional[ReadOnlyClient] = None, *,
                 team: str = DEFAULT_TEAM, root: Path = HERE,
                 stores: Iterable[str] = STORES,
                 mods: Optional[dict] = None) -> None:
        self.client = client
        self.team = team
        self.root = root
        self.stores = [root / s for s in stores]
        self.oracle = Oracle()
        self.tail = Tail()
        self.mods = load_optional() if mods is None else mods
        self.aggs = self._new_aggs()
        self._catalog_refs = 0
        self._cache: Optional[Snapshot] = None
        self._lock = threading.Lock()

    def _new_aggs(self) -> dict:
        """Los agregadores de los módulos hermanos que existan.

        Cada uno deduplica por `id` igual que el Oracle, así que se les da el
        mismo lote y punto. Si uno falla se desengancha en vez de tumbar el
        panel: estos módulos se están escribiendo en paralelo.
        """
        out = {}
        for mod_name, cls_name in (("rivals", "Rivals"), ("signals", "Signals")):
            cls = attr(self.mods.get(mod_name), cls_name)
            if cls is None:
                continue
            try:
                out[mod_name] = cls()
            except Exception:
                continue
        return out

    def _each(self, method: str, *args) -> None:
        for name, agg in list(self.aggs.items()):
            fn = attr(agg, method)
            if not callable(fn):
                continue
            try:
                fn(*args)
            except Exception:
                self.aggs.pop(name, None)

    def _feed(self, events: list[dict]) -> None:
        """Un lote va al Oracle y a cada agregador opcional."""
        if not events:
            return
        self.oracle.ingest(events)
        self._each("ingest", events)

    def _rivals_json(self) -> dict:
        agg = self.aggs.get("rivals")
        if agg is None:
            return {}
        try:
            return agg.to_json()
        except Exception:
            return {}

    def _signals_text(self) -> str:
        """`Signals.report()` es texto ya pensado para leerse: se muestra tal cual."""
        fn = attr(self.aggs.get("signals"), "report")
        if not callable(fn):
            return ""
        try:
            out = fn()
        except Exception:
            return ""
        return out if isinstance(out, str) else ""

    # -- construcción -----------------------------------------------------
    def snapshot(self, max_age: float = 5.0) -> Snapshot:
        """Devuelve el último snapshot, o construye uno si ya está viejo."""
        with self._lock:
            c = self._cache
            if c is not None and time.time() - c.built_at < max_age:
                return c
            self._cache = self.build()
            return self._cache

    def build(self) -> Snapshot:
        t0 = time.time()
        errors: list[str] = []

        def fetch(path: str, label: str) -> dict:
            if self.client is None:
                return {}
            try:
                return self.client.get(path)
            except Exception as exc:                 # red inestable: avisar, no morir
                errors.append(f"{label}: {exc}")
                return {}

        # El catálogo es estático: una vez cargado, no se vuelve a pedir.
        if not self._catalog_refs:
            cat = fetch("/api/catalog", "catálogo")
            if cat:
                self._catalog_refs = self.oracle.load_catalog(cat)
                self._each("load_catalog", cat)

        for p in self.stores:
            try:
                self._feed(self.tail.read(p))
            except OSError as exc:
                errors.append(f"{p.name}: {exc}")

        feed = fetch("/api/feed?limit=500", "feed")
        if feed:
            self._feed(feed.get("events") or [])

        snap = Snapshot(
            oracle=self.oracle,
            clock=fetch("/api/clock", "reloj"),
            schedule=fetch("/api/schedule", "agenda"),
            leaderboard=fetch("/api/leaderboard", "clasificación"),
            venues=fetch("/api/venues", "venues"),
            levels=fetch("/api/levels", "niveles"),
            team=self.team,
            catalog_refs=self._catalog_refs,
            sources=dict(self.tail.counts),
            errors=errors,
            mods=self.mods,
            rivals=self._rivals_json(),
            signals_text=self._signals_text(),
        )
        snap.build_ms = int((time.time() - t0) * 1000)
        return snap


# ------------------------------------------------------------- cálculos puros

def refresh_seconds(clock: dict) -> int:
    """El refresco sigue al tick del servidor, acotado a 15-30 s."""
    try:
        ts = float(clock.get("tick_seconds") or REFRESH_MAX)
    except (TypeError, ValueError):
        ts = REFRESH_MAX
    return int(max(REFRESH_MIN, min(REFRESH_MAX, ts)))


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def real_minutes(delta_hours: float, tick_seconds: float) -> float:
    """Horas de juego → minutos reales. 120 ticks = 1 hora de juego."""
    return delta_hours * TICKS_PER_HOUR * tick_seconds / 60.0


def urgencies(clock: dict, schedule: dict, limit: int = 6) -> list[dict]:
    """Próximos eventos de la agenda con los minutos reales que quedan.

    Un Market Test o unos duelos cambian lo que conviene hacer ahora mismo:
    saber que faltan 12 minutos no es lo mismo que saber que faltan 94.
    """
    now = _f(schedule.get("now_hours"))
    if now is None:
        now = _f(clock.get("t_hours"))
    ts = _f(clock.get("tick_seconds")) or REFRESH_MAX
    out = []
    for item in schedule.get("upcoming") or []:
        at = _f(item.get("at_hours"))
        action = str(item.get("action") or "?")
        if at is None or (now is not None and at < now - 1e-9):
            continue
        # Las aperturas y cierres de jornada están en horas de juego y no
        # cuadran con el reloj de pared; la cuenta atrás real al cierre va en
        # la cabecera, sacada de `closes`. Aquí sólo lo que cambia la jugada.
        if action.startswith("day_"):
            continue
        delta = (at - now) if now is not None else None
        out.append({
            "action": action,
            "note": item.get("note") or "",
            "at_hours": at,
            "mins": real_minutes(delta, ts) if delta is not None else None,
            "ticks": int(round(delta * TICKS_PER_HOUR)) if delta is not None else None,
            "params": item.get("params") or {},
        })
        if len(out) >= limit:
            break
    return out


def doors_countdown(clock: dict, now: Optional[datetime] = None) -> Optional[dict]:
    """Minutos reales de reloj de pared hasta el cierre de la jornada."""
    raw = clock.get("closes")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    mins = (when - ref).total_seconds() / 60.0
    return {"closes": when, "mins": mins, "day": clock.get("today_name") or ""}


def now_actions(oracle: Oracle, limit: int = 10) -> dict:
    """Las dos jugadas que no requieren negociar: comprar barato, vender caro.

    Ambas listas van ordenadas por primas en juego, que es el único orden que
    importa cuando sólo hay tiempo para una.
    """
    arb = oracle.arbitrage()[:limit]
    prem = []
    for r in oracle.premium_buyers():
        extra = (r["paid"] - r["book"]) if r.get("book") else None
        r = dict(r, extra=extra)
        prem.append(r)
    prem.sort(key=lambda r: -(r["extra"] or 0))
    return {"arbitrage": arb, "premium": prem[:limit], "hot_sets": oracle.hot_sets()}


def cheatsheet(oracle: Oracle, mods: Optional[dict] = None,
               limit: int = 24) -> list[dict]:
    """Una fila por dealer y carta: dónde abrir, qué no aceptar, cuánto aguanta.

    Si `playbook` está disponible se usa su tabla, que añade el precio de
    retirada y el techo de rondas. Si no, se reconstruye con `Oracle.advise()`:
    el panel no desaparece porque falte un módulo.
    """
    rows: list[dict] = []
    fn = attr((mods or {}).get("playbook"), "playbook")
    if callable(fn):
        try:
            for r in fn(oracle):
                rows.append({
                    "dealer": r.get("dealer"), "ref": r.get("ref"),
                    "side": r.get("side"), "rarity": r.get("rarity"),
                    "book": r.get("book"), "floor": r.get("floor_seen"),
                    "never": r.get("never_accept"), "open_at": r.get("open_at"),
                    "step": r.get("step"), "walk_away": r.get("walk_away"),
                    "rounds": r.get("max_rounds"), "ratio": None,
                    "spread": r.get("spread"), "n": r.get("observations") or 0,
                    "confidence": r.get("confidence") or "NONE",
                    "source": "playbook",
                })
        except Exception:
            rows = []              # un módulo a medio escribir no tumba el panel
    if not rows:
        for (dealer, ref, side), dl in oracle.dealers.items():
            a = oracle.advise(dealer, ref, side=side)
            floor, never = a.get("floor"), a.get("never_accept")
            spread = abs(never - floor) if (floor is not None and never is not None) else None
            rows.append({
                "dealer": dealer, "ref": ref, "side": side,
                "rarity": (oracle.cards.get(ref).rarity if oracle.cards.get(ref) else None),
                "book": a.get("book"), "floor": floor, "never": never,
                "open_at": a.get("open_at"), "step": a.get("step"),
                "walk_away": None, "rounds": a.get("rounds_before_final"),
                "ratio": a.get("ratio"), "spread": spread,
                "n": a.get("observations") or 0,
                "confidence": a.get("confidence") or "NONE",
                "source": "oracle",
            })
    # Primero lo probado, y dentro de eso lo que más primas mueve: ahí es
    # donde una ronda bien jugada paga de verdad.
    rows.sort(key=lambda r: (-CONF_RANK.get(r["confidence"], 0),
                             -(r["spread"] or 0), r["dealer"] or "", r["ref"] or ""))
    return rows[:limit]


def card_rows(oracle: Oracle, limit: int = 60) -> list[dict]:
    """El mercado por carta, de la más valiosa a la menos."""
    rows = []
    for cm in oracle.cards.values():
        if not (cm.settled or cm.asks or cm.bids):
            continue
        rows.append({
            "ref": cm.ref, "rarity": cm.rarity, "book": cm.book,
            "fair": cm.fair, "confidence": cm.confidence,
            "settled": list(cm.settled), "asks": sorted(cm.asks),
            "bids": sorted(cm.bids), "undercut": cm.undercut(),
            "minted": cm.minted, "print_run": cm.print_run,
            "scarcity": cm.scarcity,
            "holders": sorted(cm.holders), "seekers": sorted(cm.seekers),
        })
    rows.sort(key=lambda r: (-(r["fair"] or 0), -len(r["settled"]), r["ref"]))
    return rows[:limit]


def leaderboard_rows(lb: dict, team: str) -> list[dict]:
    rows = []
    for t in lb.get("teams") or []:
        rows.append({
            "rank": t.get("rank"), "team": t.get("team"), "name": t.get("name"),
            "score": t.get("score"), "negotiating": t.get("negotiating"),
            "market": t.get("market"), "level": t.get("level"),
            "filled": t.get("album_filled"), "slots": t.get("album_slots"),
            "pages": t.get("pages_complete"), "deals": t.get("deals"),
            "us": t.get("team") == team,
        })
    rows.sort(key=lambda r: (r["rank"] if r["rank"] is not None else 999))
    return rows


def trust(snap: Snapshot) -> dict:
    """Cuánta muestra hay detrás de todo lo anterior, y dónde es de uno."""
    o = snap.oracle
    cov = o.coverage
    thin = sorted(
        ({"dealer": dl.dealer, "ref": dl.ref, "side": dl.side}
         for dl in o.dealers.values() if dl.n == 1 and not dl.settled),
        key=lambda r: (r["dealer"], r["ref"]))
    one_settle = sorted(cm.ref for cm in o.cards.values() if len(cm.ordinary) == 1)
    return {
        "coverage": cov,
        "thin_lines": thin,
        "one_settlement": one_settle,
        "rarity_floors": [(r, o.rarity_floor(r))
                          for r in ("common", "uncommon", "rare", "epic", "legendary")],
        "mods": sorted(snap.mods),
        "sources": snap.sources,
        "catalog_refs": snap.catalog_refs,
        "errors": snap.errors,
    }


def rival_rows(snap: "Snapshot", limit: int = 12) -> list[dict]:
    """Filas de `rivals` ya agregadas. Lista vacía si el módulo no está."""
    teams = (snap.rivals or {}).get("teams") or []
    return [t for t in teams if isinstance(t, dict)][:limit]


def optional_text(mods: dict, name: str, oracle: Oracle) -> Optional[str]:
    """Texto que ofrezca un módulo opcional, si ofrece alguno.

    Se prueban los nombres convencionales del repo (`report`, `summary`) y se
    acepta sólo una cadena: un módulo que devuelva otra cosa no se renderiza
    a medias.
    """
    mod = mods.get(name)
    if mod is None:
        return None
    for name_attr in ("report", "summary", "text"):
        fn = attr(mod, name_attr)
        if not callable(fn):
            continue
        try:
            out = fn(oracle)
        except Exception:
            continue
        if isinstance(out, str) and out.strip():
            return out
    return None


# ----------------------------------------------------------------- render

def e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def num(v: Any, dash: str = "—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"
    return str(v)


def badge(conf: Optional[str], n: Optional[int] = None) -> str:
    """La confianza al lado de la cifra, siempre. Y `n=1` en rojo: es anécdota."""
    c = (conf or "NONE").upper()
    bits = [f'<span class="b b-{e(c.lower())}">{e(c)}</span>']
    if n is not None:
        cls = "b-one" if n <= 1 else "b-n"
        bits.append(f'<span class="b {cls}">n={n}</span>')
    return "".join(bits)


CSS = """
:root{--bg:#0b0e13;--card:#151a23;--line:#242c39;--fg:#eef2f8;--dim:#93a1b5;
--hot:#ff5c5c;--warm:#ffb020;--good:#3ddc97;--cool:#4aa8ff;--us:#ffd166}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:17px/1.4 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--cool)}
.wrap{max-width:1500px;margin:0 auto;padding:14px 16px 60px}
header.sticky{position:sticky;top:0;z-index:5;box-shadow:0 10px 24px -12px #000}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;
background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px}
h1{font-size:19px;margin:0;letter-spacing:.04em;text-transform:uppercase;color:var(--dim)}
h2{font-size:15px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);
margin:28px 0 8px;display:flex;gap:10px;align-items:baseline}
h2 small{font-size:13px;letter-spacing:0;text-transform:none;color:#6c7a8d}
.big{font-size:40px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.huge{font-size:56px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.kv{display:flex;flex-direction:column;gap:3px}
.kv span{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card.hot{border-color:#5a2230;background:#1b1317}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;font-size:12px;letter-spacing:.07em;text-transform:uppercase;
color:var(--dim);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:7px 8px;border-bottom:1px solid #1b222d;white-space:nowrap}
tbody tr:hover{background:#1b2230}
.ref{font-weight:700;font-size:19px;letter-spacing:.02em}
.money{font-size:21px;font-weight:700}
.win{color:var(--good)}.warn{color:var(--warm)}.bad{color:var(--hot)}.dim{color:var(--dim)}
tr.us{background:#2a2412;outline:2px solid var(--us)}
tr.us td{color:var(--us);font-weight:700}
.b{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;
padding:2px 6px;border-radius:5px;margin-left:5px;background:#2a3342;color:var(--dim)}
.b-settled{background:#10472f;color:#6ff0b4}.b-final{background:#0e3d47;color:#73e2f2}
.b-high{background:#13385c;color:#8cc8ff}.b-medium{background:#4a3a0e;color:#ffd27a}
.b-low,.b-one{background:#55191f;color:#ff9c9c}.b-rarity{background:#2f2a4a;color:#bdb0ff}
.b-unknown,.b-none{background:#2a3342;color:#8a97a9}
.pill{display:inline-block;padding:3px 9px;border-radius:999px;font-size:13px;
font-weight:700;background:#222b38}
.pill.hot{background:#55191f;color:#ffb3b3}.pill.soon{background:#4a3a0e;color:#ffd27a}
.pill.calm{background:#15323f;color:#8fd8ef}
input[type=search]{background:#0e131b;border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:9px 12px;font-size:17px;width:260px}
label.chk{font-size:13px;color:var(--dim);display:flex;gap:6px;align-items:center}
.empty{color:var(--dim);padding:10px 2px;font-size:15px}
pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;
color:#c6d2e2;margin:0;max-height:340px;overflow:auto}
.err{background:#2a1418;border:1px solid #5a2230;color:#ffb3b3;border-radius:10px;
padding:9px 12px;margin-top:10px;font-size:14px}
.spacer{flex:1}
@media(max-width:700px){.huge{font-size:40px}.big{font-size:30px}td,th{padding:5px 5px}}
"""


def _urgency_pill(mins: Optional[float]) -> str:
    if mins is None:
        return '<span class="pill">?</span>'
    cls = "hot" if mins <= 10 else ("soon" if mins <= 30 else "calm")
    return f'<span class="pill {cls}">{mins:.0f} min</span>'


def _panel_clock(snap: Snapshot, refresh: int) -> str:
    c = snap.clock
    tick = c.get("tick")
    nxt = _f(c.get("next_tick_in"))
    ts = _f(c.get("tick_seconds")) or 0
    dc = doors_countdown(c)
    paused = bool(c.get("paused"))
    bits = [
        '<header class="sticky">',
        f'<div class="kv"><span>tick</span><div class="huge">{e(num(tick))}</div></div>',
        f'<div class="kv"><span>siguiente tick</span>'
        f'<div class="big" id="nxt" data-left="{nxt if nxt is not None else ""}">'
        f'{e(num(nxt))}s</div></div>',
        f'<div class="kv"><span>ritmo</span><div class="big">{e(num(ts))}s</div></div>',
        f'<div class="kv"><span>jornada</span><div class="big">'
        f'{e(c.get("round_name") or c.get("today_name") or "?")}</div></div>',
    ]
    if dc:
        mins = dc["mins"]
        cls = "bad" if mins <= 30 else ("warn" if mins <= 90 else "")
        bits.append(
            f'<div class="kv"><span>cierra {e(dc["closes"].strftime("%H:%M"))}</span>'
            f'<div class="big {cls}">{int(mins // 60)}h {int(mins % 60):02d}m</div></div>')
    if paused:
        bits.append('<div class="pill hot">PAUSADO</div>')
    bits.append('<div class="spacer"></div>')
    bits.append(
        '<div class="kv"><span>buscar carta, dealer o equipo</span>'
        '<input type="search" id="q" placeholder="RET-09, abuela, t18…" '
        'autocomplete="off"></div>')
    bits.append(
        '<div class="kv"><span>panel</span>'
        f'<label class="chk"><input type="checkbox" id="pause"> no recargar</label>'
        f'<small class="dim">refresco {refresh}s · {snap.build_ms} ms</small></div>')
    bits.append('</header>')
    if snap.errors:
        bits.append('<div class="err">sin respuesta del servidor en: '
                    + e(" · ".join(snap.errors)) + '</div>')
    return "".join(bits)


def _panel_urgencies(snap: Snapshot) -> str:
    rows = urgencies(snap.clock, snap.schedule)
    out = ['<h2>1 · urgencias <small>agenda del servidor, en minutos reales</small></h2>']
    if not rows:
        out.append('<div class="empty">sin agenda (¿sin red?)</div>')
        return "".join(out)
    out.append('<div class="grid">')
    for r in rows:
        cls = "card hot" if (r["mins"] is not None and r["mins"] <= 10) else "card"
        out.append(
            f'<div class="{cls}">{_urgency_pill(r["mins"])} '
            f'<b>{e(r["action"])}</b> <span class="dim">· t+{num(r["ticks"])} ticks</span>'
            f'<div class="dim" style="margin-top:6px">{e(r["note"])}</div></div>')
    out.append('</div>')
    return "".join(out)


def _panel_now(snap: Snapshot) -> str:
    acts = now_actions(snap.oracle)
    out = ['<h2>2 · qué hacer AHORA <small>ordenado por primas en juego</small></h2>',
           '<div class="grid">']

    out.append('<div class="card"><h2 style="margin-top:0">arbitraje'
               ' <small>un equipo lo pide por debajo del suelo del dealer</small></h2>')
    if not acts["arbitrage"]:
        out.append('<div class="empty">nada ahora mismo</div>')
    else:
        out.append('<table><thead><tr><th>carta</th><th>equipo pide</th>'
                   '<th>suelo dealer</th><th>ahorro</th><th>a quién</th></tr></thead><tbody>')
        for a in acts["arbitrage"]:
            out.append(
                f'<tr class="row" data-q="{e(a["ref"])} {e(a.get("rarity"))}">'
                f'<td class="ref">{e(a["ref"])}<span class="b b-{e((a.get("rarity") or "none").lower())}">'
                f'{e(a.get("rarity") or "?")}</span></td>'
                f'<td class="money win">{e(a["team_ask"])}</td>'
                f'<td>{e(a["dealer_floor"])} <span class="dim">{e(a["dealer"])}</span></td>'
                f'<td class="money win">+{e(a["saving"])}</td>'
                f'<td class="dim">{e(", ".join(a["holders"]) or "?")}</td></tr>')
        out.append('</tbody></table>')
    out.append('</div>')

    out.append('<div class="card"><h2 style="margin-top:0">pagan sobreprecio'
               ' <small>a quién venderle caro, y qué set le falta</small></h2>')
    if not acts["premium"]:
        out.append('<div class="empty">ningún sobreprecio observado todavía</div>')
    else:
        out.append('<table><thead><tr><th>equipo</th><th>carta</th><th>pagó</th>'
                   '<th>book</th><th>×</th><th>set</th></tr></thead><tbody>')
        for r in acts["premium"]:
            out.append(
                f'<tr class="row" data-q="{e(r["ref"])} {e(r["team"])}">'
                f'<td class="ref">{e(r["team"])}</td>'
                f'<td class="ref">{e(r["ref"])}</td>'
                f'<td class="money warn">{e(r["paid"])}</td>'
                f'<td class="dim">{e(num(r.get("book")))}</td>'
                f'<td class="money bad">x{e(num(r.get("multiple")))}</td>'
                f'<td class="dim">{e(r.get("set"))}</td></tr>')
        out.append('</tbody></table>')
        if acts["hot_sets"]:
            out.append('<div class="dim" style="margin-top:8px">sets calientes: '
                       + e(", ".join(f"{k} ({v})" for k, v in acts["hot_sets"].items()))
                       + '</div>')
    out.append('</div></div>')
    return "".join(out)


def _panel_cheatsheet(snap: Snapshot) -> str:
    rows = cheatsheet(snap.oracle, snap.mods)
    src = rows[0]["source"] if rows else "oracle"
    out = [f'<h2>3 · chuleta por dealer <small>fuente: {e(src)} · '
           f'«no aceptar» es su apertura: un trato ahí no puntúa</small></h2>']
    if not rows:
        out.append('<div class="empty">ningún dealer observado todavía</div>')
        return "".join(out)
    out.append('<table><thead><tr><th>dealer</th><th>carta</th><th>sentido</th>'
               '<th>NO aceptar</th><th>suelo visto</th><th>abre en</th><th>paso</th>'
               '<th>retirada</th><th>rondas</th><th>ratio</th><th>margen</th>'
               '<th>confianza</th></tr></thead><tbody>')
    for r in rows:
        verb = "te la vende" if r["side"] == "ask" else "te la compra"
        out.append(
            f'<tr class="row" data-q="{e(r["ref"])} {e(r["dealer"])} {e(r.get("rarity"))}">'
            f'<td class="ref">{e(r["dealer"])}</td>'
            f'<td class="ref">{e(r["ref"])}</td>'
            f'<td class="dim">{e(verb)}</td>'
            f'<td class="money bad">{e(num(r["never"]))}</td>'
            f'<td class="money">{e(num(r["floor"]))}</td>'
            f'<td class="money win">{e(num(r["open_at"]))}</td>'
            f'<td>{e(num(r["step"]))}</td>'
            f'<td>{e(num(r["walk_away"]))}</td>'
            f'<td>{e(num(r["rounds"]))}</td>'
            f'<td>{e(num(r["ratio"]))}</td>'
            f'<td class="dim">{e(num(r["spread"]))}</td>'
            f'<td>{badge(r["confidence"], r["n"])}</td></tr>')
    out.append('</tbody></table>')
    return "".join(out)


def _panel_cards(snap: Snapshot) -> str:
    rows = card_rows(snap.oracle)
    out = ['<h2>4 · mercado por carta <small>liquidado manda sobre pedido</small></h2>']
    if not rows:
        out.append('<div class="empty">sin mercado observado todavía</div>')
        return "".join(out)
    out.append('<table><thead><tr><th>carta</th><th>rareza</th><th>book</th>'
               '<th>justo</th><th>liquidado</th><th>piden</th><th>ofrecen</th>'
               '<th>para ser el + barato</th><th>acuñadas</th><th>escasez</th>'
               '<th>la tienen</th><th>la buscan</th><th>confianza</th></tr></thead><tbody>')
    for r in rows:
        sett = ", ".join(map(str, r["settled"][-4:])) or "—"
        asks = (f'{r["asks"][0]}–{r["asks"][-1]}' if r["asks"] else "—")
        bids = (f'{r["bids"][0]}–{r["bids"][-1]}' if r["bids"] else "—")
        acu = (f'{r["minted"]}/{r["print_run"]}'
               if r["minted"] is not None and r["print_run"] else "—")
        esc = f'{r["scarcity"]:.0%}' if r["scarcity"] is not None else "—"
        out.append(
            f'<tr class="row" data-q="{e(r["ref"])} {e(r.get("rarity"))}">'
            f'<td class="ref">{e(r["ref"])}</td>'
            f'<td class="dim">{e(r.get("rarity") or "?")}</td>'
            f'<td class="dim">{e(num(r["book"]))}</td>'
            f'<td class="money">{e(num(r["fair"]))}</td>'
            f'<td class="win">{e(sett)}</td>'
            f'<td>{e(asks)}</td><td>{e(bids)}</td>'
            f'<td class="money warn">{e(num(r["undercut"]))}</td>'
            f'<td class="dim">{e(acu)}</td><td class="dim">{e(esc)}</td>'
            f'<td class="dim">{e(", ".join(r["holders"]) or "—")}</td>'
            f'<td class="dim">{e(", ".join(r["seekers"]) or "—")}</td>'
            f'<td>{badge(r["confidence"], len(r["settled"]) or None)}</td></tr>')
    out.append('</tbody></table>')
    return "".join(out)


def _panel_leaderboard(snap: Snapshot) -> str:
    rows = leaderboard_rows(snap.leaderboard, snap.team)
    out = [f'<h2>5 · clasificación <small>{e(snap.team)} destacado · '
           f'«market» = market-making</small></h2>']
    if not rows:
        out.append('<div class="empty">sin clasificación (¿sin red?)</div>')
        return "".join(out)
    mk = [r["market"] or 0 for r in rows]
    out.append('<table><thead><tr><th>#</th><th>equipo</th><th>puntos</th>'
               '<th>negociando</th><th>market</th><th>nivel</th><th>álbum</th>'
               '<th>páginas</th><th>tratos</th></tr></thead><tbody>')
    for r in rows:
        out.append(
            f'<tr class="{"us" if r["us"] else ""}"><td>{e(num(r["rank"]))}</td>'
            f'<td class="ref">{e(r["team"])} <span class="dim">{e(r["name"])}</span></td>'
            f'<td class="money">{e(num(r["score"]))}</td>'
            f'<td>{e(num(r["negotiating"]))}</td>'
            f'<td>{e(num(r["market"]))}</td>'
            f'<td>{e(num(r["level"]))}</td>'
            f'<td>{e(num(r["filled"]))}/{e(num(r["slots"]))}</td>'
            f'<td>{e(num(r["pages"]))}</td><td>{e(num(r["deals"]))}</td></tr>')
    out.append('</tbody></table>')
    if mk and max(mk) <= 0:
        out.append('<div class="dim" style="margin-top:8px">market-making a 0.0 en '
                   'los 18 equipos: nadie ha cobrado esa columna todavía.</div>')
    return "".join(out)


def _panel_optional(snap: Snapshot) -> str:
    """Paneles de los módulos hermanos que existan. Si no hay, no hay sección."""
    out = []
    teams = rival_rows(snap)
    if teams:
        out.append('<h2>6 · manos rivales <small>rivals.py · evidencia publicada</small></h2>'
                   '<table><thead><tr><th>equipo</th><th>muestra</th><th>tiene</th>'
                   '<th>busca</th><th>duplicados en venta</th></tr></thead><tbody>')
        for t in teams:
            out.append(
                f'<tr><td class="ref">{e(t.get("team"))}</td>'
                f'<td>{e(num(t.get("sample")))}</td>'
                f'<td class="dim">{e(", ".join((t.get("held") or [])[:10]) or "—")}</td>'
                f'<td class="dim">{e(", ".join((t.get("sought") or [])[:10]) or "—")}</td>'
                f'<td class="dim">{e(", ".join((t.get("duplicates_offered") or [])[:8]) or "—")}</td>'
                f'</tr>')
        out.append('</tbody></table>')
    extras = [("signals", snap.signals_text),
              ("playbook", optional_text(snap.mods, "playbook", snap.oracle))]
    for name, txt in extras:
        if txt and txt.strip():
            out.append(f'<h2>extra · {e(name)}.py</h2>'
                       f'<div class="card"><pre>{e(txt)}</pre></div>')
    return "".join(out)


def _panel_trust(snap: Snapshot) -> str:
    t = trust(snap)
    cov = t["coverage"]
    out = ['<h2>confianza <small>nada debe parecer más firme de lo que es</small></h2>',
           '<div class="grid">']
    out.append(
        '<div class="card"><div class="kv"><span>cobertura</span>'
        f'<div class="big">{e(cov["ticks"])} ticks</div></div>'
        f'<div class="dim" style="margin-top:6px">ticks {e(cov["tick_min"])}–{e(cov["tick_max"])} · '
        f'{e(cov["events"])} eventos · {e(cov["cards"])} cartas · '
        f'{e(cov["dealer_lines"])} líneas de dealer</div></div>')
    out.append(
        '<div class="card"><div class="kv"><span>liquidaciones entre equipos</span>'
        f'<div class="big {"bad" if cov["settled"] < 5 else ""}">{e(cov["settled"])}</div></div>'
        '<div class="dim" style="margin-top:6px">es la única evidencia de precio '
        'pagado; todo lo demás es lo que alguien pide.</div></div>')
    thin = t["thin_lines"]
    out.append(
        f'<div class="card {"hot" if thin else ""}"><div class="kv">'
        f'<span>muestra de 1 (anécdota, no suelo)</span>'
        f'<div class="big {"bad" if thin else "win"}">{len(thin)}</div></div>'
        '<div class="dim" style="margin-top:6px">'
        + (e(", ".join(f'{r["dealer"]}/{r["ref"]}' for r in thin[:14])) or "ninguna")
        + '</div></div>')
    rar = [(r, v) for r, v in t["rarity_floors"] if v is not None]
    out.append('<div class="card"><div class="kv"><span>suelo típico por rareza</span>'
               '<div class="big">' + (e(" · ".join(f"{r[:4]} {v:.0f}" for r, v in rar))
                                      or "—") + '</div></div>'
               '<div class="dim" style="margin-top:6px">mediana de los suelos vistos; '
               'sirve para una carta nunca observada.</div></div>')
    srcs = " · ".join(f"{k}: {v}" for k, v in sorted(t["sources"].items())) or "ninguno"
    out.append('<div class="card"><div class="kv"><span>de dónde sale</span></div>'
               f'<div class="dim" style="margin-top:6px">{e(srcs)}<br>'
               f'catálogo: {e(t["catalog_refs"])} referencias<br>'
               f'módulos opcionales: {e(", ".join(t["mods"]) or "ninguno")}</div></div>')
    out.append('</div>')
    if t["one_settlement"]:
        out.append('<div class="err">una sola liquidación en: '
                   + e(", ".join(t["one_settlement"][:24]))
                   + ' — su «precio justo» es ese único trato.</div>')
    return "".join(out)


JS = """
(function(){
  var K='bz.dash.';
  function get(k,d){try{var v=sessionStorage.getItem(K+k);return v===null?d:v}catch(e){return d}}
  function set(k,v){try{sessionStorage.setItem(K+k,v)}catch(e){}}
  var q=document.getElementById('q'), pause=document.getElementById('pause');
  function filter(){
    var s=(q.value||'').trim().toLowerCase();
    var rows=document.querySelectorAll('tr.row');
    for(var i=0;i<rows.length;i++){
      var hay=(rows[i].getAttribute('data-q')||'').toLowerCase();
      rows[i].style.display = (!s || hay.indexOf(s)>=0) ? '' : 'none';
    }
    set('q', q.value||'');
  }
  q.value=get('q','');
  q.addEventListener('input', filter);
  filter();
  pause.checked = get('pause','0')==='1';
  pause.addEventListener('change', function(){set('pause', pause.checked?'1':'0')});
  // Cuenta atrás al tick: el número de arriba se mueve aunque el refresco
  // tarde, para que se vea de un golpe cuánto margen queda.
  var n=document.getElementById('nxt'), left=parseFloat(n.getAttribute('data-left'));
  setInterval(function(){
    if(!isFinite(left)) return;
    left-=1; if(left<0) left=0;
    n.textContent=Math.round(left)+'s';
  },1000);
  // El refresco respeta la pausa: nadie quiere perder lo que estaba leyendo
  // justo cuando el dealer contesta.
  setInterval(function(){ if(!pause.checked) location.reload(); }, REFRESH*1000);
})();
"""


def render_page(snap: Snapshot) -> str:
    """HTML completo a partir de un snapshot. Pura: no toca la red ni el disco."""
    refresh = refresh_seconds(snap.clock)
    stamp = time.strftime("%H:%M:%S", time.localtime(snap.built_at))
    body = [
        _panel_clock(snap, refresh),
        _panel_urgencies(snap),
        _panel_now(snap),
        _panel_cheatsheet(snap),
        _panel_cards(snap),
        _panel_leaderboard(snap),
        _panel_optional(snap),
        _panel_trust(snap),
        f'<p class="dim" style="margin-top:26px">panel de sólo lectura · '
        f'construido {e(stamp)} · equipo {e(snap.team)}</p>',
    ]
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Bazaar · panel de negociación</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">"
        "<header style=\"margin-bottom:6px\"><h1>Bazaar · panel de negociación "
        "(sólo lectura)</h1></header>"
        + "".join(body) +
        f"</div><script>var REFRESH={refresh};{JS}</script></body></html>"
    )


# ------------------------------------------------------------------ servidor

class Handler(BaseHTTPRequestHandler):
    """Sirve la página y un volcado JSON. Sólo GET: no hay otro verbo."""

    builder: Builder = None            # inyectado por `serve()`
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                      # noqa: N802 (API de stdlib)
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                snap = self.builder.snapshot()
                self._send(render_page(snap).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/oracle.json":
                snap = self.builder.snapshot()
                self._send(json.dumps(snap.oracle.to_json(), ensure_ascii=False,
                                      indent=2).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send(b"ok\n", "text/plain; charset=utf-8")
            else:
                self._send(b"no such page\n", "text/plain; charset=utf-8", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:                   # una pantalla rota es peor que un error
            self._send(f"error: {exc}\n".encode("utf-8"),
                       "text/plain; charset=utf-8", 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass                                       # el log por petición sólo estorba


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(builder: Builder, port: int = DEFAULT_PORT,
          host: str = "127.0.0.1") -> None:
    handler = type("BoundHandler", (Handler,), {"builder": builder})
    with Server((host, port), handler) as srv:
        print(f"panel en http://localhost:{port}   (Ctrl-C para parar)")
        srv.serve_forever()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--team", default=DEFAULT_TEAM, help="equipo a destacar")
    ap.add_argument("--offline", action="store_true",
                    help="sólo los .jsonl guardados, sin tocar la red")
    ap.add_argument("--once", metavar="RUTA", nargs="?", const="-",
                    help="escribe el HTML y sale (- para stdout)")
    a = ap.parse_args(argv)

    builder = Builder(None if a.offline else ReadOnlyClient(a.url), team=a.team)
    if a.once:
        page = render_page(builder.build())
        if a.once == "-":
            print(page)
        else:
            Path(a.once).write_text(page, encoding="utf-8")
            print(f"escrito {a.once}")
        return 0
    builder.snapshot()                             # primer tirón antes de abrir el puerto
    serve(builder, port=a.port, host=a.host)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ndetenido")
        raise SystemExit(130)
