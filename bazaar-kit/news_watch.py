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
     comprometidas en ofertas abiertas) y el suelo es el valor privado + margen.

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


def classify(item: dict, catalog: Optional[dict] = None) -> dict:
    """Noticia -> {id, tick, source, headline, kind, dealer, set, rarity, item, window_hours, rumour}."""
    text = f"{item.get('headline') or ''}. {item.get('body') or ''}".lower()
    dealer = next((d for d, ws in DEALERS.items() if _has(text, ws)), None)
    set_id = next((s for s, ws in set_names(catalog).items() if _has(text, ws)), None)
    rarity = next((r for r in reversed(RARITIES) if _has(text, RARITY_WORDS[r])), None)
    thing = "pack" if _has(text, ("pack", "packs", "sobre", "sobres")) else ("card" if rarity or set_id else None)
    if dealer and SUPPLY.search(text):
        kind = "oferta"
    elif dealer and GIFT.search(text):
        kind = "regalo"
    elif dealer and DEMAND.search(text):
        kind = "demanda"
    elif not dealer and not set_id and not rarity:
        kind = "irrelevante"
    else:
        kind = "otra"
    return {"id": item.get("id"), "tick": item.get("tick"), "source": item.get("source"),
            "source_name": item.get("source_name"), "headline": item.get("headline"), "kind": kind,
            "dealer": dealer, "set": set_id, "rarity": rarity, "item": thing, "window_hours": window_hours(text),
            "rumour": item.get("source") == "tablon"}


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
    if c["kind"] == "irrelevante" or (c["kind"] == "otra" and not c.get("dealer")):
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
                return "falsa", f"compras de {did} sin subida: {sum(now) / len(now):.1f} P frente a {sum(base) / len(base):.1f} P"
        if end > last_tick:
            return "pendiente", f"la ventana acaba en t{end} (datos hasta t{last_tick})"
        if inside:
            return "falsa", f"{len(inside)} menús de {did} en la ventana, ninguno con la fila prometida"
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
        return "falsa", f"ningún regalo de {did} ({c.get('item') or 'lo prometido'}) en t{t}–t{end}"
    return "n/a", "tipo sin verificación"


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


def summarize(rows: list) -> dict:
    out = {}
    for src in SOURCES + tuple(sorted({r.get("source") for r in rows} - set(SOURCES) - {None})):
        mine = [r for r in rows if r.get("source") == src]
        n_true = sum(r["verdict"] == "cierta" for r in mine)
        n_false = sum(r["verdict"] == "falsa" for r in mine)
        out[src] = {"news": len(mine), "true": n_true, "false": n_false,
                    "pending": sum(r["verdict"] == "pendiente" for r in mine),
                    "irrelevant": sum(r["verdict"] in ("n/a", "sin datos") for r in mine),
                    "reliability": round((n_true + 1) / (n_true + n_false + 2), 2)}
    return out


def reliability(calib: Optional[dict] = None) -> dict:
    rel = dict(PRIORS)
    for src, row in ((calib or {}).get("sources") or {}).items():
        rel[src] = row["reliability"]
    return rel


def calibration_lines(cal: dict) -> list:
    out = [f"CALIBRACIÓN (market.db hasta t{cal['last_tick']}, {cal['tick_seconds']:.0f} s/tick)",
           f"{'tick':>5} {'fuente':8} {'tipo':11} {'veredicto':10} titular / evidencia"]
    for r in cal["news"]:
        out.append(f"{r.get('tick') or '?':>5} {r.get('source') or '?':8} {r['kind']:11} {r['verdict']:10} "
                   f"{r.get('headline')}")
        out.append(f"{'':38}↳ {r['evidence']}")
    out.append(f"{'fuente':8} {'noticias':>8} {'ciertas':>8} {'falsas':>7} {'pend.':>6} {'irrel.':>7} {'fiabilidad':>11}")
    for src, s in cal["sources"].items():
        out.append(f"{src:8} {s['news']:>8} {s['true']:>8} {s['false']:>7} {s['pending']:>6} {s['irrelevant']:>7} "
                   f"{s['reliability']:>11.2f}")
    return out


# ------------------------------------------------------------------ sugerencias en vivo

def copy_loss(val: Optional[tr.Valuation], counts: Counter, asset: dict) -> float:
    """Valor privado que perdemos al entregar esa copia (modelo del catálogo; si no, your_value del servidor)."""
    if val is not None and val.unit(asset["ref"]) is not None:
        return -val.delta(counts, Counter(), Counter({asset["ref"]: 1}))[0]
    return float(asset.get("your_value") or 0.0)


def status_of(c: dict, tick: Optional[int], tick_seconds: Optional[float], rel: dict, dealer: Optional[dict]) -> dict:
    """¿Sigue viva la noticia? Ventana (si hay tick) + fiabilidad de la fuente + confirmación en el menú actual."""
    w = window_ticks(c, tick_seconds)
    until = c["tick"] + w if c.get("tick") is not None and w is not None else None
    side = "sells" if c["kind"] == "oferta" else "buys"
    confirmed = bool(c["kind"] in ("demanda", "oferta") and explicit_menu_row(dealer, side, c.get("set"), c.get("rarity")))
    in_window = until is not None and tick is not None and c["tick"] <= tick <= until
    r = rel.get(c.get("source"), 0.5)
    return {"until_tick": until, "in_window": in_window, "confirmed_by_menu": confirmed, "reliability": round(r, 2),
            "actionable": confirmed or (in_window and r >= RELIABLE and not c.get("rumour"))}


def sell_suggestions(news: list, dealers: dict, me: dict, catalog: Optional[dict], my_offers: list = (),
                     tick: Optional[int] = None, tick_seconds: Optional[float] = None, rel: Optional[dict] = None,
                     margin: float = 2.0, ticks_by_id: Optional[dict] = None) -> list:
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
        st = status_of(c, tick, tick_seconds, rel, dealer)
        if not st["actionable"] or not dealer_buys(dealer, c.get("rarity"), c.get("set")):
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
                        "value": round(loss, 2), "floor": floor, "ask": ask, **st,
                        "text": f"{c['dealer']} compra {rarity or ''} {set_id or ''} por encima de lo habitual"
                                f"{' hasta t' + str(st['until_tick']) if st['until_tick'] else ''}: tenemos "
                                f"{a['ref']} #{a['id']} con valor privado {loss:.1f} P → abrir venta con {c['dealer']} "
                                f"pidiendo {ask} P (suelo {floor} P)"})
    return out


def advice(c: dict, st: dict) -> str:
    if c["kind"] == "irrelevante":
        return "ignorar (sin efecto de mercado)"
    if c["kind"] == "regalo":
        if c.get("rumour") or st["reliability"] < RELIABLE:
            return "rumor de fuente poco fiable: ignorar, no abrir conversaciones por esto"
        return ("vigilar sobres sin abrir y abrirlos (new_levels.py --open-packs, dry run)" if c.get("item") == "pack"
                else "vigilar gift.given; no gastar nada por esto")
    if c["kind"] == "demanda":
        if not st["actionable"]:
            return "no accionable (fuera de ventana, fuente poco fiable o sin fila en el menú)"
        return "vender a ese vendedor las copias que encajen (ver sugerencias; coordinator --news-sell)"
    if c["kind"] == "oferta":
        return ("comprar SOLO si el precio queda bajo nuestro valor privado (coordinador)" if st["actionable"]
                else "no accionable")
    return "sin acción"


def live_report(news_raw, dealers_raw, clock: dict, me: Optional[dict] = None, catalog: Optional[dict] = None,
                my_offers: list = (), feed_events: Iterable[dict] = (), rel: Optional[dict] = None,
                margin: float = 2.0) -> dict:
    rel = rel or dict(PRIORS)
    tick, ts = clock.get("tick"), clock.get("tick_seconds") or DEFAULT_TICK_SECONDS
    dealers = dealer_map(dealers_raw)
    ticks = news_ticks(feed_events)
    rows = []
    for item in news_items(news_raw):
        c = classify(item, catalog)
        if c.get("tick") is None and c.get("id") in ticks:
            c["tick"] = ticks[c["id"]]
        st = status_of(c, tick, ts, rel, dealers.get(c.get("dealer")))
        rows.append(dict(c, **st, advice=advice(c, st)))
    sugg = sell_suggestions(rows, dealers, me, catalog, my_offers, tick, ts, rel, margin) if me else []
    return {"tick": tick, "tick_seconds": ts, "reliability": rel, "news": rows, "suggestions": sugg}


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
    out.append("ACCIONES SUGERIDAS:" if rep["suggestions"] else "ACCIONES SUGERIDAS: ninguna")
    out += [f"  - {s['text']}" for s in rep["suggestions"]]
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

    from bazaar_sdk import Bazaar
    key = os.environ.get("BAZAAR_KEY")
    api = Bazaar(DEFAULT_URL, key or "")
    while True:
        me = catalog = None
        offers: list = []
        if key:
            me, catalog = api.me(), api.catalog()
            offers = (api.my_offers() or {}).get("offers", [])
        rep = live_report(api.call("GET", "/api/news"), api.dealers(), api.clock(), me, catalog, offers,
                          (api.feed(400) or {}).get("events", []), reliability(cal), a.margin)
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
