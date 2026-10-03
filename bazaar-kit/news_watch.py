"""RADIO RASTRO — lee las noticias, las clasifica y propone acciones para t15 (solo lectura; nunca escribe).

`GET /api/news` (más nuevas primero; también `news.posted` en el feed) trae noticias de tres fuentes: el Boletín del
Bazar (oficial), Radio Rastro y El Tablón (rumores). Según su `how`, unas son ciertas y el mercado se mueve como dicen,
otras son rumores y otras «solo Madrid». Este módulo:

  1. clasifica cada noticia: fuente, vendedor / barrio / rareza mencionados, ventana («one hour» = 3600 s /
     tick_seconds ticks) y tipo: `demanda` (un vendedor compra / paga más), `oferta` (vende barato), `regalo`
     (regala cartas o sobres), `irrelevante` (fútbol, metro...) u `otra`;
  2. calibra la fiabilidad por fuente con market.db (`--calibrate`, offline): una demanda es CIERTA si en su ventana
     el menú del vendedor gana una fila de compra explícita para ese barrio/rareza o sus compras liquidadas suben de
     precio; un regalo es CIERTO si en su ventana hay `gift.given` de ese vendedor con lo prometido (o un grant_all);
  3. en vivo, cruza cada noticia con el menú ACTUAL del vendedor (`/api/dealers`) y con nuestra colección, y propone
     acciones concretas: «el Chato compra raras MAL: tenemos MAL-09 #12 (valor privado 77 P) → abrir venta pidiendo
     P (suelo F)». Las copias propuestas pasan por page_guard (nunca rompen una página completa ni tocan copias
     comprometidas en ofertas abiertas) y el suelo es el valor privado + margen;
  4. lee las fiebres de `/api/schedule` (persona_patch «Doña Pilar pays 25 % over book for Salamanca», hasta el
     siguiente patch de ese vendedor) y, mientras están activas, propone vender ese barrio al vendedor.

    python3 news_watch.py --calibrate --db ../intel/market.db   # tabla de calibración por fuente (offline)
    python3 news_watch.py                                       # en vivo: noticias + acciones sugeridas
    python3 news_watch.py --json sugerencias.json               # además, exporta las sugerencias (o «-» = stdout)
    python3 news_watch.py --watch 60                            # repite cada 60 s

El coordinador usa `sell_suggestions` con `--news-sell` (opt-in): ver coordinator.news_sell_candidates.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from typing import Iterable, Optional

import page_guard as pg
import trading as tr

DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
SOURCES = ("boletin", "radio", "tablon")
RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
DEFAULT_TICK_SECONDS = 30.0
RELIABLE = 0.6          # fiabilidad mínima de la fuente para actuar sin confirmación en el menú
# Calibración con market.db (tick ~739, 3-oct): Radio 1/1 (t403 Chato MAL raras: fila de menú buys rare [MAL] vista en
# t467/t508, ausente en t265 y t590), Boletín 1/1 verificada (t283 la radio está en antena; t643 sobres: pendiente),
# Tablón 0/1 (t499 legendaria del Chato: ningún regalo suyo). Laplace: (ciertas + 1) / (verificadas + 2).
PRIORS = {"boletin": 2 / 3, "radio": 2 / 3, "tablon": 1 / 3}

DEALERS = {"chato": ("el chato", "chato"), "abuela": ("abuela", "carmen"), "pilar": ("pilar",),
           "picaros": ("pícaros", "picaros"), "banco": ("don ernesto", "ernesto")}
SET_NAMES = {"MAL": ("malasaña", "malasana"), "LAT": ("la latina", "latina"), "LAV": ("lavapiés", "lavapies"),
             "SAL": ("salamanca",), "RET": ("el retiro", "retiro"), "CHA": ("chamberí", "chamberi")}
RARITY_WORDS = {"common": ("common",), "uncommon": ("uncommon",), "rare": ("rare",), "epic": ("epic",),
                "legendary": ("legendary", "legend")}
NUMBERS = {"one": 1, "an": 1, "a": 1, "two": 2, "three": 3, "four": 4, "half an": 0.5}

DEMAND = re.compile(r"\b(looking for|pays? (?:above|over|more|double)|paying|buys?|buying|wants?|hunting|collects?)\b")
SUPPLY = re.compile(r"\b(sells? (?:cheap|below|at a discount)|discount|sale|cheap|clearing)\b")
GIFT = re.compile(r"\b(gives?|giving|hands? out|free|gift|presents?)\b")
WINDOW = re.compile(r"\b(half an|one|an|a|two|three|four|\d+(?:\.\d+)?)\s+hours?\b")
# «stops buying», «no longer pays», «won't buy»: la demanda se RETIRA; no es una demanda positiva.
NEGATION = re.compile(r"\b(stops?|stopped|stopping|no longer|no more|won'?t|will not|doesn'?t|does not|ceases?|quits?|"
                      r"refuses?|not)\b(?:\s+\w+){0,2}?\s+(?:buy(?:ing)?|pay(?:s|ing)?|looking|wants?|hunting|collect(?:s|ing)?|"
                      r"sell(?:s|ing)?)\b")
# Horarios que NO son una duración medible («until teatime», «after ten», «tonight»): no se inventa una caducidad.
AMBIGUOUS_TIME = re.compile(r"\b(until|till|after|before|by|at|around)\s+(teatime|tea time|lunch(?:time)?|noon|midnight|"
                            r"dinner|dusk|dawn|sunset|sunrise|nightfall|closing time|siesta|"
                            r"ten|nine|eight|seven|six|five|four|three|two|one|eleven|twelve|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b|"
                            r"\b(today|tonight|this (?:morning|afternoon|evening)|later|soon|for a while|all day|the whole day|"
                            r"from today|from now on)\b")
CAUSES = (("meteorología", r"\b(degrees|sunny|sun and|rain|storm|cloud|wind|heat ?wave|snow)\b"),
          ("deporte / ocio", r"\b(win|wins|won|beat|goal|match|league|atleti|madrid goes|football|derby|concert)\b"),
          ("transporte", r"\b(metro|bus|line \d+|train|traffic|road|taxi|station|closed between)\b"),
          ("vida cotidiana", r"\b(queue|shop|churro|bakery|market stall|festival|parade|fair)\b"),
          ("emisión de la propia radio", r"\b(on the air|broadcast|radio rastro)\b"))


# ------------------------------------------------------------------ clasificación

def news_items(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("news", "items", "events"):
            if isinstance(raw.get(k), list):
                return raw[k]
    return []


def _has(text: str, words: Iterable[str]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text) for w in words)


def set_names(catalog: Optional[dict] = None) -> dict:
    out = {k: list(v) for k, v in SET_NAMES.items()}
    for s in (catalog or {}).get("sets", []):
        if s.get("name"):
            out.setdefault(s["id"], []).append(s["name"].lower())
    return out


def window_hours(text: str) -> Optional[float]:
    m = WINDOW.search(text)
    if not m:
        return None
    w = m.group(1)
    return float(NUMBERS.get(w, w)) if w in NUMBERS or re.fullmatch(r"\d+(?:\.\d+)?", w) else None


def discard_cause(text: str) -> str:
    """Causa ESPECÍFICA de un descarte: qué tipo de noticia es y por qué no mueve el mercado."""
    for name, rx in CAUSES:
        if re.search(rx, text):
            return f"{name}: sin vendedor, barrio ni rareza del mercado"
    return "sin vendedor, barrio ni rareza reconocidos (no se identifica efecto de mercado)"


def classify(item: dict, catalog: Optional[dict] = None) -> dict:
    """Noticia -> {id, tick, source, headline, kind, dealer, set, rarity, item, window_hours, rumour, negated,
    time_ambiguous, time_text, reason}. `kind`: demanda / oferta / regalo (efecto afirmado), demanda_negada /
    oferta_negada (se RETIRA), pendiente (hay vendedor/barrio/rareza pero el efecto no se reconoce: por verificar),
    irrelevante (con `reason` específica)."""
    text = f"{item.get('headline') or ''}. {item.get('body') or ''}".lower()
    dealer = next((d for d, ws in DEALERS.items() if _has(text, ws)), None)
    set_id = next((s for s, ws in set_names(catalog).items() if _has(text, ws)), None)
    rarity = next((r for r in reversed(RARITIES) if _has(text, RARITY_WORDS[r])), None)
    thing = "pack" if _has(text, ("pack", "packs", "sobre", "sobres")) else ("card" if rarity or set_id else None)
    neg = NEGATION.search(text)
    amb = AMBIGUOUS_TIME.search(text)
    reason = None
    if dealer and neg and (DEMAND.search(text) or SUPPLY.search(text) or re.search(r"\bsell", text)):
        kind = "oferta_negada" if re.search(r"\bsell", neg.group(0)) else "demanda_negada"
        reason = f"negación «{neg.group(0).strip()}»: no es demanda positiva"
    elif dealer and SUPPLY.search(text):
        kind = "oferta"
    elif dealer and GIFT.search(text):
        kind = "regalo"
    elif dealer and DEMAND.search(text):
        kind = "demanda"
    elif not dealer and not set_id and not rarity:
        kind, reason = "irrelevante", discard_cause(text)
    else:
        kind, reason = "pendiente", "efecto no reconocido: hay entidades de mercado pero ninguna acción conocida"
    # La ventana solo vale si es una DURACIÓN («one hour»); «until teatime» no se convierte en ticks.
    hours = window_hours(text)
    return {"id": item.get("id"), "tick": item.get("tick"), "source": item.get("source"),
            "source_name": item.get("source_name"), "headline": item.get("headline"), "kind": kind,
            "dealer": dealer, "set": set_id, "rarity": rarity, "item": thing, "window_hours": hours,
            "rumour": item.get("source") == "tablon", "negated": bool(neg),
            "time_ambiguous": bool(amb) and hours is None, "time_text": amb.group(0) if amb and hours is None else None,
            "reason": reason}


def window_ticks(c: dict, tick_seconds: Optional[float]) -> Optional[int]:
    if c.get("window_hours") is None:
        return None
    return int(round(c["window_hours"] * 3600 / (tick_seconds or DEFAULT_TICK_SECONDS)))


def news_ticks(events: Iterable[dict]) -> dict:
    """id de noticia -> tick, a partir de los eventos `news.posted` del feed (si /api/news no trae el tick)."""
    out = {}
    for e in events or []:
        if str(e.get("type") or e.get("kind") or "") == "news.posted":
            p = e.get("payload") if isinstance(e.get("payload"), dict) else e
            if p.get("id") is not None and e.get("tick") is not None:
                out[p["id"]] = int(e["tick"])
    return out


# ------------------------------------------------------------------ menú del vendedor

def dealer_map(raw) -> dict:
    """/api/dealers ({personas: [...]}, lista) o el dict id -> vendedor del coordinador -> dict id -> vendedor."""
    if isinstance(raw, dict) and not any(isinstance(raw.get(k), list) for k in ("personas", "dealers")):
        return raw
    rows = raw if isinstance(raw, list) else (raw.get("personas") or raw.get("dealers") or [])
    return {d["id"]: d for d in rows if isinstance(d, dict) and d.get("id")}


def explicit_menu_row(dealer: Optional[dict], side: str, set_id: Optional[str], rarity: Optional[str]) -> Optional[dict]:
    """Fila del menú (buys/sells) que nombra EXPLÍCITAMENTE el barrio (lista de sets): la huella de una noticia cierta."""
    for row in ((dealer or {}).get("menu") or {}).get(side, []):
        sets = row.get("sets")
        if isinstance(sets, list) and set_id in sets and (rarity is None or row.get("rarity") == rarity):
            return row
    return None


def dealer_buys(dealer: Optional[dict], rarity: Optional[str], set_id: Optional[str]) -> bool:
    for row in ((dealer or {}).get("menu") or {}).get("buys", []):
        sets = row.get("sets")
        if (rarity is None or row.get("rarity") == rarity) and (not isinstance(sets, list) or set_id in sets):
            return True
    return False


def dealer_list_price(dealer: Optional[dict], rarity: Optional[str]) -> Optional[int]:
    """Precio de lista al que el vendedor VENDE esa rareza: referencia de «lo habitual» para pedir por encima."""
    for row in ((dealer or {}).get("menu") or {}).get("sells", []):
        if row.get("rarity") == rarity and row.get("list_price") is not None:
            return int(row["list_price"])
    return None


# ------------------------------------------------------------------ calibración offline (market.db, solo lectura)

def _menus(db, did: str) -> list:
    out = []
    for tick, payload in db.execute("select tick, payload from json_snapshots where kind='dealers' order by tick"):
        try:
            d = json.loads(payload)
        except ValueError:
            continue
        rows = d if isinstance(d, list) else (d.get("personas") or d.get("dealers") or [])
        row = next((x for x in rows if isinstance(x, dict) and x.get("id") == did), None)
        if row:
            out.append((tick, row))
    return out


def _dealer_buys_prices(db, did: str, set_id, rarity, lo: int, hi: int) -> list:
    q = ("select s.price from settlements s join settlement_items i on i.settlement = s.id where s.persona = ? and "
         "i.too = ? and s.n_items = 1 and s.tick >= ? and s.tick < ?")
    args = [did, did, lo, hi]
    if set_id:
        q += " and i.set_id = ?"
        args.append(set_id)
    if rarity:
        q += " and i.rarity = ?"
        args.append(rarity)
    return [r[0] for r in db.execute(q, args)]


def verify(db, c: dict, last_tick: int, tick_seconds: float = DEFAULT_TICK_SECONDS) -> tuple:
    """(veredicto, evidencia) de una noticia pasada: cierta / falsa / pendiente / sin datos / n/a."""
    t = int(c.get("tick") or 0)
    w = window_ticks(c, tick_seconds) or int(3600 / tick_seconds)
    end = t + w
    if c["kind"] in ("demanda_negada", "oferta_negada"):
        return "n/a", "retirada de demanda/oferta: no se verifica como alza"
    if c["kind"] == "irrelevante" or (c["kind"] in ("otra", "pendiente") and not c.get("dealer")):
        if c.get("source") == "boletin" and "on the air" in str(c.get("headline") or "").lower():
            n = db.execute("select count(*) from events where type='news.posted'").fetchone()[0]
            return "cierta", f"la radio emite: {n} noticias en el feed"
        return "n/a", "sin efecto de mercado verificable"
    did = c.get("dealer")
    if c["kind"] in ("demanda", "oferta"):
        side = "buys" if c["kind"] == "demanda" else "sells"
        snaps = _menus(db, did)
        inside = [(tk, row) for tk, row in snaps if t <= tk <= end]
        before = [(tk, row) for tk, row in snaps if tk < t]
        hit = [tk for tk, row in inside if explicit_menu_row(row, side, c.get("set"), c.get("rarity"))]
        was = [tk for tk, row in before if explicit_menu_row(row, side, c.get("set"), c.get("rarity"))]
        if hit and not (was and was[-1] == before[-1][0]):
            return "cierta", f"menú de {did}: fila {side} explícita {c.get('rarity')} [{c.get('set')}] en ticks {hit}"
        if c["kind"] == "demanda":
            now = _dealer_buys_prices(db, did, c.get("set"), c.get("rarity"), t, end)
            base = _dealer_buys_prices(db, did, c.get("set"), c.get("rarity"), t - 2 * w, t)
            if now and base and sum(now) / len(now) > 1.05 * sum(base) / len(base):
                return "cierta", f"compras de {did}: media {sum(now) / len(now):.1f} P frente a {sum(base) / len(base):.1f} P"
            if now and base:
                if len(now) >= MIN_OBS and len(base) >= MIN_OBS:  # contradicha: evidencia ACTIVA con muestra suficiente
                    return "falsa", (f"compras de {did} sin subida con muestra suficiente ({len(now)}/{len(base)}): "
                                     f"{sum(now) / len(now):.1f} P frente a {sum(base) / len(base):.1f} P")
                return "sin confirmar", (f"pocas compras de {did} para concluir ({len(now)}/{len(base)} < {MIN_OBS}): "
                                         "no prueba que fuese falsa")
        if end > last_tick:
            return "pendiente", f"la ventana acaba en t{end} (datos hasta t{last_tick})"
        if inside:
            return "sin confirmar", (f"{len(inside)} menús de {did} en la ventana sin la fila prometida: ausencia de "
                                     "operaciones visibles, no prueba de falsedad")
        return "sin datos", f"sin menús ni liquidaciones de {did} en t{t}–t{end}"
    if c["kind"] == "regalo":
        gifts = []
        for (payload,) in db.execute("select payload from events where type='gift.given' and actor=? and tick>=? and "
                                     "tick<=?", (did, t, end)):
            g = json.loads(payload)
            if c.get("item") == "pack" and not g.get("packs"):
                continue  # la Abuela regala cartas sueltas a diario: eso no confirma «sobres para todos»
            gifts.append(g)
        grants = [json.loads(p).get("note") for (p,) in db.execute(
            "select payload from events where type='schedule.fired' and tick>=? and tick<=?", (t, end))
            if json.loads(p).get("action") == "grant_all"]
        if gifts or grants:
            return "cierta", f"{len(gifts)} regalos de {did} y {len(grants)} grant_all en t{t}–t{end}"
        if end > last_tick:
            return "pendiente", f"la ventana acaba en t{end} (datos hasta t{last_tick})"
        return "sin confirmar", (f"ningún regalo de {did} ({c.get('item') or 'lo prometido'}) visible en t{t}–t{end}: "
                                 "no prueba falsedad")
    return "pendiente", "efecto desconocido: por verificar, no es un hecho confirmado"


def calibrate(db_path: str, catalog: Optional[dict] = None) -> dict:
    """Clasifica y verifica cada `news.posted` de market.db; resumen por fuente con fiabilidad de Laplace."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        last = int(db.execute("select max(tick) from events").fetchone()[0] or 0)
        ts = DEFAULT_TICK_SECONDS
        row = db.execute("select payload from json_snapshots where kind='clock' order by tick desc limit 1").fetchone()
        if row:
            ts = float(json.loads(row[0]).get("tick_seconds") or ts)
        rows = []
        for tick, payload in db.execute("select tick, payload from events where type='news.posted' order by tick"):
            item = dict(json.loads(payload), tick=tick)
            c = classify(item, catalog)
            c["verdict"], c["evidence"] = verify(db, c, last, ts)
            rows.append(c)
    finally:
        db.close()
    return {"last_tick": last, "tick_seconds": ts, "news": rows, "sources": summarize(rows)}


def interval(k: int, n: int) -> tuple:
    """Intervalo de Wilson al 90 % de la proporción de noticias ciertas entre las VERIFICADAS (ciertas + contradichas).
    Con n = 0 devuelve (0, 1): no se sabe nada; las no confirmadas no cuentan ni a favor ni en contra."""
    if n <= 0:
        return 0.0, 1.0
    z, p = 1.645, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 2), round(min(1.0, c + h), 2)


def summarize(rows: list) -> dict:
    out = {}
    for src in SOURCES + tuple(sorted({r.get("source") for r in rows} - set(SOURCES) - {None})):
        mine = [r for r in rows if r.get("source") == src]
        n_true = sum(r["verdict"] == "cierta" for r in mine)
        n_false = sum(r["verdict"] == "falsa" for r in mine)
        lo, hi = interval(n_true, n_true + n_false)
        out[src] = {"news": len(mine), "true": n_true, "false": n_false, "n": n_true + n_false, "ci90": [lo, hi],
                    "unconfirmed": sum(r["verdict"] == "sin confirmar" for r in mine),
                    "label": "sin datos suficientes" if n_true + n_false < MIN_OBS else "con muestra",
                    "pending": sum(r["verdict"] == "pendiente" for r in mine),
                    "irrelevant": sum(r["verdict"] in ("n/a", "sin datos") for r in mine),
                    "reliability": round((n_true + 1) / (n_true + n_false + 2), 2)}
    return out


def reliability(calib: Optional[dict] = None) -> dict:
    rel = dict(PRIORS)
    for src, row in ((calib or {}).get("sources") or {}).items():
        if row.get("n", row["true"] + row["false"]) > 0:  # sin observaciones verificadas se conserva el a priori
            rel[src] = row["reliability"]
    return rel


def observations(calib: Optional[dict] = None) -> dict:
    """Noticias VERIFICADAS (ciertas + falsas) por fuente: con pocas, la fiabilidad de Laplace no basta para actuar."""
    return {src: row["true"] + row["false"] for src, row in ((calib or {}).get("sources") or {}).items()}


def calibration_lines(cal: dict) -> list:
    out = [f"CALIBRACIÓN (market.db hasta t{cal['last_tick']}, {cal['tick_seconds']:.0f} s/tick)",
           f"{'tick':>5} {'fuente':8} {'tipo':11} {'veredicto':10} titular / evidencia"]
    for r in cal["news"]:
        out.append(f"{r.get('tick') or '?':>5} {r.get('source') or '?':8} {r['kind']:11} {r['verdict']:10} "
                   f"{r.get('headline')}")
        out.append(f"{'':38}↳ {r['evidence']}")
    out.append(f"{'fuente':8} {'noticias':>8} {'ciertas':>8} {'falsas':>7} {'pend.':>6} {'irrel.':>7} {'fiabilidad':>11}  muestra / IC90")
    for src, s in cal["sources"].items():
        out.append(f"{src:8} {s['news']:>8} {s['true']:>8} {s['false']:>7} {s['pending']:>6} {s['irrelevant']:>7} "
                   f"{s['reliability']:>11.2f}  n={s['n']} [{s['ci90'][0]:.2f}–{s['ci90'][1]:.2f}] {s['label']}")
    return out


# ------------------------------------------------------------------ fiebres de /api/schedule (persona_patch)

TICKS_PER_GAME_HOUR = 120.0   # dashboard.py: 120 ticks por hora de juego, sea cual sea tick_seconds
FEVER_RE = re.compile(r"(?P<pct>\d+(?:\.\d+)?)\s*%\s*over book for (?P<what>[^.,;]+?)(?:\s+until\b|[.,;]|$)", re.I)


def schedule_items(payload) -> list:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    for k in ("upcoming", "schedule", "items", "events"):
        if isinstance((payload or {}).get(k), list):
            return [x for x in payload[k] if isinstance(x, dict)]
    return []


def fevers(schedule, catalog: Optional[dict] = None) -> list:
    """persona_patch «X pays N % over book for <barrio>» -> {dealer, set, pct, start, end, note} en horas de juego; el
    final es el siguiente persona_patch del mismo vendedor («The fever breaks»)."""
    items = sorted((x for x in schedule_items(schedule) if x.get("action") == "persona_patch"
                    and isinstance(x.get("at_hours"), (int, float))), key=lambda x: x["at_hours"])
    names = set_names(catalog)
    out = []
    for i, x in enumerate(items):
        m = FEVER_RE.search(x.get("note") or "")
        if not m:
            continue
        what = m.group("what").lower()
        set_id = next((sid for sid, ws in names.items() if _has(what, ws)), None)
        did = (x.get("params") or {}).get("id") or next((d for d, ws in DEALERS.items() if _has(
            (x.get("note") or "").lower(), ws)), None)
        end = next((y["at_hours"] for y in items[i + 1:] if (y.get("params") or {}).get("id") == did), None)
        out.append({"dealer": did, "set": set_id, "pct": float(m.group("pct")), "start": x["at_hours"], "end": end,
                    "note": x.get("note")})
    return out


def schedule_now(schedule, clock: Optional[dict] = None) -> Optional[float]:
    v = (schedule or {}).get("now_hours") if isinstance(schedule, dict) else None
    v = v if isinstance(v, (int, float)) else (clock or {}).get("t_hours")
    return float(v) if isinstance(v, (int, float)) else None


def fever_state(f: dict, now_h: Optional[float], tick: Optional[int]) -> dict:
    """activa / próxima / pasada, con los ticks aproximados de inicio y fin (120 ticks por hora de juego)."""
    to_tick = lambda h: int(round(tick + (h - now_h) * TICKS_PER_GAME_HOUR)) if h is not None and tick is not None \
        and now_h is not None else None
    if now_h is None:
        state = "desconocida"
    elif now_h < f["start"]:
        state = "próxima"
    elif f["end"] is None or now_h < f["end"]:
        state = "activa"
    else:
        state = "pasada"
    return dict(f, state=state, start_tick=to_tick(f["start"]), end_tick=to_tick(f["end"]),
                hours_to_start=round(f["start"] - now_h, 3) if now_h is not None else None)


def fever_suggestions(fv: list, me: dict, catalog: Optional[dict], my_offers: list = (), margin: float = 2.0) -> list:
    """Durante una fiebre activa: copias del barrio que page_guard deja salir, con lo que el vendedor pagaría
    (book × (1 + pct)) frente a nuestro valor privado. Solo las que dejan excedente sobre el suelo."""
    if not catalog:
        return []
    assets = [a for a in me.get("assets") or [] if a.get("kind") == "card"]
    counts = Counter(a["ref"] for a in assets)
    val = tr.Valuation(catalog, me.get("affinity") or {})
    committed = pg.committed_assets(list(my_offers or []), me.get("id"))
    out, seen = [], set()
    for f in fv:
        if f.get("state") != "activa" or not f.get("set"):
            continue
        for a in sorted(assets, key=lambda x: x["id"]):
            card = val.cards.get(a["ref"]) or {}
            if card.get("set") != f["set"] or card.get("book") is None or a["id"] in seen:
                continue
            if a["id"] not in pg.tradeable_assets(a["ref"], counts, catalog, assets, committed):
                continue
            loss = copy_loss(val, counts, a)
            floor = max(1, math.ceil(loss + margin - 1e-9))
            pays = math.floor(card["book"] * (1 + f["pct"] / 100))
            if pays < floor:
                continue
            seen.add(a["id"])
            out.append({"type": "fever_sell", "dealer": f["dealer"], "set": f["set"], "asset": a["id"], "ref": a["ref"],
                        "value": round(loss, 2), "floor": floor, "ask": pays, "until_tick": f.get("end_tick"),
                        "text": f"fiebre {f['set']} de {f['dealer']} (+{f['pct']:g} % sobre book) hasta "
                                f"t~{f.get('end_tick')}: {a['ref']} #{a['id']} vale {loss:.1f} P para nosotros → "
                                f"vender a {f['dealer']} pidiendo ~{pays} P (suelo {floor} P)"})
    return out


# ------------------------------------------------------------------ sugerencias en vivo

def copy_loss(val: Optional[tr.Valuation], counts: Counter, asset: dict) -> float:
    """Valor privado que perdemos al entregar esa copia (modelo del catálogo; si no, your_value del servidor)."""
    if val is not None and val.unit(asset["ref"]) is not None:
        return -val.delta(counts, Counter(), Counter({asset["ref"]: 1}))[0]
    return float(asset.get("your_value") or 0.0)


MIN_OBS = 3  # observaciones verificadas mínimas para fiarse de una fuente sin confirmación en el menú


def status_of(c: dict, tick: Optional[int], tick_seconds: Optional[float], rel: dict, dealer: Optional[dict],
              obs: Optional[dict] = None) -> dict:
    """¿Sigue viva la noticia? Ventana (solo si es una DURACIÓN) + fiabilidad de la fuente + confirmación en el menú.
    `obs` (observaciones verificadas por fuente): con menos de MIN_OBS la fuente es «delgada» y solo vale si el menú
    actual lo confirma. Un horario ambiguo («until teatime») no tiene caducidad: nunca está «dentro de ventana»."""
    w = window_ticks(c, tick_seconds)
    until = c["tick"] + w if c.get("tick") is not None and w is not None else None
    side = "sells" if c["kind"] == "oferta" else "buys"
    positive = c["kind"] in ("demanda", "oferta")
    confirmed = bool(positive and explicit_menu_row(dealer, side, c.get("set"), c.get("rarity")))
    in_window = until is not None and tick is not None and c["tick"] <= tick <= until
    r = rel.get(c.get("source"), 0.5)
    n_obs = None if obs is None else obs.get(c.get("source"), 0)
    thin = n_obs is not None and n_obs < MIN_OBS
    trusted = in_window and r >= RELIABLE and not c.get("rumour") and not thin
    expired = until is not None and tick is not None and tick > until
    return {"until_tick": until, "in_window": in_window, "confirmed_by_menu": confirmed, "reliability": round(r, 2),
            "observations": n_obs, "thin_source": thin, "expired": expired,
            "time_ambiguous": bool(c.get("time_ambiguous")), "time_text": c.get("time_text"),
            "actionable": positive and (confirmed or trusted)}


def sell_suggestions(news: list, dealers: dict, me: dict, catalog: Optional[dict], my_offers: list = (),
                     tick: Optional[int] = None, tick_seconds: Optional[float] = None, rel: Optional[dict] = None,
                     margin: float = 2.0, ticks_by_id: Optional[dict] = None, obs: Optional[dict] = None,
                     skip: Optional[callable] = None) -> list:
    """Ventas a un vendedor que, según una noticia de demanda viva, paga más por un barrio/rareza.
    Solo copias que page_guard deja salir (ni protegidas por página completa ni comprometidas); suelo = valor privado
    + margen; petición = máx(suelo, precio de lista al que el vendedor vende esa rareza)."""
    rel = rel or dict(PRIORS)
    assets = [a for a in me.get("assets") or [] if a.get("kind") == "card"]
    counts = Counter(a["ref"] for a in assets)
    val = tr.Valuation(catalog, me.get("affinity") or {}) if catalog else None
    cards = val.cards if val else {}
    committed = pg.committed_assets(list(my_offers or []), me.get("id"))
    out, seen = [], set()
    for raw in news:
        c = raw if "kind" in raw else classify(raw, catalog)
        if c.get("tick") is None and ticks_by_id and c.get("id") in ticks_by_id:
            c = dict(c, tick=ticks_by_id[c["id"]])
        if c["kind"] != "demanda" or not c.get("dealer"):
            continue
        dealer = (dealers or {}).get(c["dealer"])
        st = status_of(c, tick, tick_seconds, rel, dealer, obs)
        if not st["actionable"] or not dealer_buys(dealer, c.get("rarity"), c.get("set")):
            continue
        if skip is not None and skip(c):  # ya se actuó por este anuncio (registro persistente)
            continue
        for a in sorted(assets, key=lambda x: x["id"]):
            card = cards.get(a["ref"]) or {}
            rarity, set_id = a.get("rarity") or card.get("rarity"), a.get("set") or card.get("set")
            if (c.get("set") and set_id != c["set"]) or (c.get("rarity") and rarity != c["rarity"]):
                continue
            if a["id"] in seen or a["id"] not in pg.tradeable_assets(a["ref"], counts, catalog or {}, assets, committed):
                continue
            seen.add(a["id"])
            loss = copy_loss(val, counts, a)
            floor = max(1, math.ceil(loss + margin - 1e-9))
            ask = max(floor, dealer_list_price(dealer, rarity) or floor)
            out.append({"type": "news_sell", "news_id": c.get("id"), "source": c.get("source"),
                        "dealer": c["dealer"], "set": set_id, "rarity": rarity, "asset": a["id"], "ref": a["ref"],
                        "value": round(loss, 2), "floor": floor, "ask": ask, "price_confirmed": False, **st,
                        "text": f"{c['dealer']} compra {rarity or ''} {set_id or ''} (prima NO confirmada: el menú no "
                                f"trae precio){' · vigente hasta t' + str(st['until_tick']) if st['until_tick'] else ''}: tenemos "
                                f"{a['ref']} #{a['id']} con valor privado {loss:.1f} P → abrir venta con {c['dealer']} "
                                f"pidiendo {ask} P (suelo {floor} P)"})
    return out


def not_actionable_causes(c: dict, st: dict) -> list:
    """Causa ESPECÍFICA de cada descarte (nunca un genérico «no accionable»)."""
    out = []
    if c.get("rumour"):
        out.append("rumor del Tablón (fuente no fiable)")
    if st.get("time_ambiguous"):
        out.append(f"caducidad ambigua «{st.get('time_text')}»: no se inventa una ventana")
    elif st.get("expired"):
        out.append(f"caducada en t{st['until_tick']}")
    elif st.get("until_tick") is None and not st.get("time_ambiguous"):
        out.append("sin ventana temporal declarada")
    if st.get("thin_source"):
        out.append(f"fuente con pocas observaciones verificadas ({st.get('observations')} < {MIN_OBS})")
    elif st.get("reliability", 1) < RELIABLE and not c.get("rumour"):
        out.append(f"fiabilidad de la fuente {st['reliability']} < {RELIABLE}")
    if not st.get("confirmed_by_menu"):
        out.append("sin fila explícita en el menú actual del vendedor")
    return out


def advice(c: dict, st: dict) -> str:
    if c["kind"] == "irrelevante":
        return "ignorar: " + (c.get("reason") or "sin efecto de mercado")
    if c["kind"] == "regalo":
        if c.get("rumour") or st["reliability"] < RELIABLE:
            return "rumor de fuente poco fiable: ignorar, no abrir conversaciones por esto"
        return ("vigilar sobres sin abrir y abrirlos (new_levels.py --open-packs, dry run)" if c.get("item") == "pack"
                else "vigilar gift.given; no gastar nada por esto")
    if c["kind"] in ("demanda_negada", "oferta_negada"):
        return "NO vender/comprar por esta noticia: la demanda se retira (evitar ofrecerle esa rareza)"
    if c["kind"] == "pendiente":
        return "pendiente de verificar: investigar menú y liquidaciones; no actuar sin evidencia"
    if c["kind"] == "demanda":
        if not st["actionable"]:
            return "no accionable: " + "; ".join(not_actionable_causes(c, st))
        return "vender a ese vendedor las copias que encajen (ver sugerencias; coordinator --news-sell)"
    if c["kind"] == "oferta":
        return ("investigar: comprar SOLO si hay precio ejecutable bajo nuestro valor privado (ruta normal del coordinador)"
                if st["actionable"] else "no accionable: " + "; ".join(not_actionable_causes(c, st)))
    return "sin acción"


def live_report(news_raw, dealers_raw, clock: dict, me: Optional[dict] = None, catalog: Optional[dict] = None,
                my_offers: list = (), feed_events: Iterable[dict] = (), rel: Optional[dict] = None,
                margin: float = 2.0, schedule=None, obs: Optional[dict] = None) -> dict:
    rel = rel or dict(PRIORS)
    tick, ts = clock.get("tick"), clock.get("tick_seconds") or DEFAULT_TICK_SECONDS
    dealers = dealer_map(dealers_raw)
    ticks = news_ticks(feed_events)
    rows = []
    for item in news_items(news_raw):
        c = classify(item, catalog)
        if c.get("tick") is None and c.get("id") in ticks:
            c["tick"] = ticks[c["id"]]
        st = status_of(c, tick, ts, rel, dealers.get(c.get("dealer")), obs)
        rows.append(dict(c, **st, advice=advice(c, st)))
    sugg = sell_suggestions(rows, dealers, me, catalog, my_offers, tick, ts, rel, margin, None, obs) if me else []
    now_h = schedule_now(schedule, clock)
    fv = [fever_state(f, now_h, tick) for f in fevers(schedule, catalog)] if schedule is not None else []
    if me:
        sugg += fever_suggestions(fv, me, catalog, my_offers, margin)
    return {"tick": tick, "tick_seconds": ts, "reliability": rel, "news": rows, "fevers": fv, "suggestions": sugg}


def report_lines(rep: dict) -> list:
    out = [f"RADIO RASTRO · tick {rep['tick']} · fiabilidad {json.dumps(rep['reliability'])}"]
    for r in rep["news"]:
        tags = [x for x in (r.get("dealer"), r.get("set"), r.get("rarity")) if x]
        out.append(f"  #{r.get('id')} t{r.get('tick') or '?'} [{r.get('source')}] {r['kind']}"
                   f"{' ' + '/'.join(tags) if tags else ''}: {r.get('headline')}")
        extra = []
        if r.get("until_tick") is not None:
            extra.append(f"ventana hasta t{r['until_tick']} ({'dentro' if r['in_window'] else 'fuera'})")
        if r.get("confirmed_by_menu"):
            extra.append("CONFIRMADA en el menú actual")
        out.append(f"      → {r['advice']}{' · ' + ', '.join(extra) if extra else ''}")
    for f in rep.get("fevers") or []:
        out.append(f"  FIEBRE {f['state']}: {f['dealer']} +{f['pct']:g} % por {f['set']} · t~{f['start_tick']}–"
                   f"t~{f['end_tick']} · {f['note']}")
    out.append("ACCIONES SUGERIDAS (INFORMATIVAS: el agente solo las recibe si arranca con --radio):"
               if rep["suggestions"] else "ACCIONES SUGERIDAS: ninguna")
    out += [f"  - {s['text']}" for s in rep["suggestions"]]
    return out


def shared_lines(st: dict) -> list:
    """Vista del estado que escribe el agente (data/radio_state.json): sin sondeos redundantes a /api/news."""
    p = st.get("poll") or {}
    out = [f"RADIO RASTRO · estado del AGENTE · sondeo t{p.get('tick')} ({p.get('n_items')} noticias, nuevas: "
           f"{p.get('new_ids') or 'ninguna'}) · {st.get('counts')}"]
    for r in st.get("recent") or []:
        out.append(f"  #{r['id']} t{r.get('tick')} [{r.get('source')}] {r.get('kind')} → {r.get('status')}: "
                   f"{r.get('headline')}" + (f"\n      ↳ {'; '.join(r['causes'])}" if r.get("causes") else ""))
    d = st.get("decision") or {}
    out.append(f"DECISIÓN DEL AGENTE (noticia #{d.get('news_id')}, t{d.get('tick')}): {d.get('action')}")
    return out


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calibrate", action="store_true", help="tabla de calibración por fuente con market.db (offline)")
    ap.add_argument("--db", default=os.environ.get("BAZAAR_MARKET_DB"),
                    help="market.db (solo lectura) para --calibrate y la fiabilidad en vivo; env BAZAAR_MARKET_DB")
    ap.add_argument("--json", metavar="FICHERO", help="exportar noticias + sugerencias en JSON («-» = stdout)")
    ap.add_argument("--margin", type=float, default=2.0, help="suelo de venta = valor privado + margen (P)")
    ap.add_argument("--watch", type=float, default=0, help="segundos entre sondeos (0 = una pasada)")
    a = ap.parse_args(argv)

    cal = calibrate(a.db) if a.db and os.path.exists(a.db) else None
    if a.calibrate:
        if cal is None:
            ap.error("--calibrate necesita --db con market.db")
        print("\n".join(calibration_lines(cal)))
        if a.json:
            _dump(cal, a.json)
        return 0

    import radio
    from bazaar_sdk import Bazaar
    key = os.environ.get("BAZAAR_KEY")
    api = Bazaar(DEFAULT_URL, key or "")
    while True:
        # Lectura única: si el agente (--news-sell) escribió su estado hace poco, se muestra ESE y no se sondea la API.
        st = radio.fresh_state(max(90.0, 3 * (a.watch or 30)))
        if st is not None:
            print("\n".join(shared_lines(st)))
            if a.json:
                _dump(st, a.json)
            if not a.watch:
                return 0
            time.sleep(a.watch)
            continue
        me = catalog = None
        offers: list = []
        if key:
            me, catalog = api.me(), api.catalog()
            offers = (api.my_offers() or {}).get("offers", [])
        rep = live_report(api.call("GET", "/api/news"), api.dealers(), api.clock(), me, catalog, offers,
                          (api.feed(400) or {}).get("events", []), reliability(cal), a.margin, api.schedule(),
                          observations(cal))
        print("\n".join(report_lines(rep)))
        if a.json:
            _dump(rep, a.json)
        if not a.watch:
            return 0
        time.sleep(a.watch)


def _dump(obj, path: str) -> None:
    txt = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if path == "-":
        print(txt)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)


if __name__ == "__main__":
    sys.exit(main())
