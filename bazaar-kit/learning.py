"""APRENDIZAJE desde evidencia PÚBLICA de otros equipos. Reutiliza la memoria existente (agent_memory.sqlite3); no crea otra
base de datos ni otro simulador. Lógica pura salvo `load_events`/`mark_seen` (SQLite local, solo nuestra memoria).

    eventos del feed (agent_memory.events) → experiencias normalizadas → ciclos de vida de ofertas → estimadores con
    decaimiento, agrupación e incertidumbre → parámetros de política ACOTADOS y versionados (no se promueven solos)

Reglas:
  · Sin fuga temporal: `as_of(t)` solo deja eventos con tick ≤ t y, si sabemos cuándo los vimos, vistos en ≤ t.
  · Identidades: solo ids de equipo/dealer que el servidor publica; un alias (`m…`) se conserva como alias y nunca se
    atribuye a un equipo. El texto de terceros es DATO (no se interpreta como instrucción).
  · Un precio liquidado no es el máximo ni el mínimo de nadie: es UN punto aceptado por ambas partes.
  · Posesión histórica ≠ inventario actual. Pocos datos → estimación agrupada con intervalo y etiqueta «insuficiente».
  · Dealers y equipos, compras y ventas, por separado.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

TEAM = re.compile(r"^t\d+$")
HALF_LIFE = 240.0          # ticks: la evidencia de hace 240 ticks pesa la mitad
MIN_N = 3.0                # tamaño efectivo mínimo para un perfil propio (si no, se usa el agrupado)


def kind_of(actor) -> str:
    a = str(actor or "")
    return "team" if TEAM.match(a) else ("alias" if a.startswith("m") and len(a) > 3 else ("dealer" if a else "unknown"))


# ------------------------------------------------------------------ memoria (reutiliza agent_memory.sqlite3)

def ensure_seen_table(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS event_seen (id INTEGER PRIMARY KEY, first_seen_tick INTEGER, source TEXT)")


def mark_seen(db, events: Iterable[dict], tick: int, source: str) -> int:
    """Momento en que NOSOTROS observamos cada evento (para el replay sin fuga). Idempotente por id."""
    ensure_seen_table(db)
    before = db.total_changes
    for e in events:
        if isinstance(e, dict) and isinstance(e.get("id"), int):
            db.execute("INSERT OR IGNORE INTO event_seen VALUES (?,?,?)", (e["id"], tick, source))
    return db.total_changes - before


def load_events(path, tick: Optional[int] = None) -> list:
    """Eventos de la memoria con su tick de observación (`_seen`, None si es anterior a este registro)."""
    p = Path(path)
    if not p.exists():
        return []
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        has_seen = db.execute("SELECT 1 FROM sqlite_master WHERE name='event_seen'").fetchone() is not None
        q = ("SELECT e.body, s.first_seen_tick FROM events e LEFT JOIN event_seen s ON s.id = e.id" if has_seen
             else "SELECT body, NULL FROM events")
        out = []
        for body, seen in db.execute(q):
            e = json.loads(body)
            e["_seen"] = seen
            out.append(e)
    finally:
        db.close()
    out.sort(key=lambda e: (e.get("tick") or 0, e.get("id") or 0))
    return as_of(out, tick) if tick is not None else out


def as_of(events: list, tick: int) -> list:
    """Solo lo que era conocible en `tick`: sin eventos futuros ni vistos después."""
    return [e for e in events if (e.get("tick") or 0) <= tick and (e.get("_seen") is None or e["_seen"] <= tick)]


def coverage(events: list) -> dict:
    """Cobertura del historial: fracción de ids presentes en su rango (el feed solo entrega una ventana por sondeo)."""
    ids = sorted({e["id"] for e in events if isinstance(e.get("id"), int)})
    if not ids:
        return {"events": 0, "id_coverage": None, "ticks": None}
    ticks = [e.get("tick") or 0 for e in events]
    return {"events": len(ids), "id_coverage": round(len(ids) / (ids[-1] - ids[0] + 1), 3), "ticks": [min(ticks), max(ticks)],
            "note": "cobertura parcial: ausencia de un evento no prueba que no ocurriera"}


# ------------------------------------------------------------------ experiencias normalizadas

def experiences(events: list) -> list:
    """Ofertas publicadas/canceladas y liquidaciones como filas trazables (id y tipo originales, tick y tick visto)."""
    rows, seen = [], set()
    for e in events:
        t, p = e.get("type"), e.get("payload") or {}
        # una liquidación vista por varias fuentes (feed, recolector, otra instantánea) cuenta UNA vez: por su id propio
        key = ("settlement", p.get("settlement")) if t == "settlement" and p.get("settlement") is not None else (t, e.get("id"))
        if key in seen:
            continue
        seen.add(key)
        base = {"source_id": e.get("id"), "type": t, "tick": e.get("tick"), "seen": e.get("_seen")}
        if t == "offer.listed":
            o = p.get("offer") or {}
            g, w = o.get("give") or {}, o.get("want") or {}
            ga = [a for a in g.get("assets") or [] if isinstance(a, dict)]
            wt = [x[5:] for x in w.get("types") or [] if str(x).startswith("card:")]
            if len(ga) == 1 and w.get("cash") and not g.get("cash"):
                side, ref, price, inst = "ask", ga[0].get("ref"), int(w["cash"]), ga[0].get("id")
            elif g.get("cash") and len(wt) == 1 and not ga:
                side, ref, price, inst = "bid", wt[0], int(g["cash"]), None
            else:
                continue
            rows.append(dict(base, offer=o.get("id"), maker=o.get("maker"), maker_kind=kind_of(o.get("maker")), side=side,
                             ref=ref, instance=inst, qty=1, price=price, venue=o.get("venue"), to=o.get("to"),
                             expires=o.get("expires_tick"), created=o.get("created_tick")))
        elif t == "offer.cancelled":
            rows.append(dict(base, offer=p.get("offer"), venue=p.get("venue")))
        elif t == "settlement":
            items = p.get("items") or []
            if len(items) != 1 or items[0].get("kind") != "card":
                continue
            it = items[0]
            rows.append(dict(base, settlement=p.get("settlement"), seller=it.get("frm"), buyer=it.get("to"),
                             seller_kind=kind_of(it.get("frm")), buyer_kind=kind_of(it.get("to")), ref=it.get("ref"),
                             instance=it.get("id"), qty=1, price=p.get("price") or 0, fee=p.get("fee") or 0,
                             venue=p.get("venue"), persona=p.get("persona")))
    return rows


def lifecycles(rows: list) -> list:
    """Por oferta publicada: liquidada (inferido: misma carta, precio, venue y la contraparte publicadora), cancelada,
    caducada o desconocida (cobertura). La liquidación no trae id de oferta: la coincidencia se marca como INFERIDA."""
    offers = {r["offer"]: dict(r, outcome="desconocido", end=None) for r in rows if r["type"] == "offer.listed"}
    for r in rows:
        if r["type"] == "offer.cancelled" and r["offer"] in offers and offers[r["offer"]]["outcome"] == "desconocido":
            offers[r["offer"]].update(outcome="cancelada", end=r["tick"])
    used = set()
    for s in (r for r in rows if r["type"] == "settlement"):
        for o in sorted(offers.values(), key=lambda o: o["tick"]):
            if o["outcome"] != "desconocido" or o["offer"] in used or o["tick"] > s["tick"] or o["ref"] != s["ref"] \
                    or o["price"] != s["price"] or o["venue"] != s["venue"]:
                continue
            if (o["side"] == "ask" and o["maker"] == s["seller"]) or (o["side"] == "bid" and o["maker"] == s["buyer"]):
                o.update(outcome="liquidada", end=s["tick"], settlement=s["settlement"], inferred=True)
                used.add(o["offer"])
                break
    last = max((r["tick"] or 0 for r in rows), default=0)
    for o in offers.values():
        if o["outcome"] == "desconocido" and o.get("expires") is not None and o["expires"] < last:
            o.update(outcome="caducada", end=o["expires"])
    return list(offers.values())


# ------------------------------------------------------------------ estimadores con decaimiento e incertidumbre

def weight(age: float, half_life: float = HALF_LIFE) -> float:
    return 0.5 ** (max(0.0, age) / half_life)


def wilson(k: float, n: float, z: float = 1.645) -> tuple:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def rate(outcomes: list, tick: int, prior: Optional[tuple] = None, half_life: float = HALF_LIFE) -> dict:
    """Tasa ponderada por antigüedad (`outcomes` = [(tick, 0/1)]). Con muestra efectiva < MIN_N se mezcla con el `prior`
    agrupado (k, n) como pseudo-recuentos y se etiqueta «insuficiente»."""
    k = sum(weight(tick - t, half_life) * y for t, y in outcomes)
    n = sum(weight(tick - t, half_life) for t, _ in outcomes)
    pk, pn = (prior or (0.0, 0.0))
    shrink = n < MIN_N and pn > 0
    kk, nn = (k + pk * MIN_N / max(pn, 1e-9), n + MIN_N) if shrink else (k, n)
    lo, hi = wilson(kk, nn)
    return {"rate": round(kk / nn, 3) if nn else None, "ci90": [lo, hi], "n_eff": round(n, 2), "n_raw": len(outcomes),
            "label": "insuficiente (agrupado)" if shrink else ("sin datos" if not outcomes else "propia"),
            "pooled": shrink}


def weighted_median(pairs: list) -> Optional[float]:
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    if tot <= 0:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def price_estimate(rows: list, ref: str, tick: int, card_group: Optional[dict] = None, half_life: float = HALF_LIFE,
                   exclude_dealers: bool = True) -> dict:
    """Precio liquidado en operaciones COMPARABLES (misma carta, entre equipos/alias, sin dealers por defecto), mediana
    ponderada por antigüedad. Con muestra efectiva < MIN_N se agrupa con cartas de la misma rareza y barrio (`card_group`
    ref -> (set, rareza)), escalando por nada: el grupo solo acota el rango y baja la confianza."""
    def pts(pred):
        return [(r["price"], weight(tick - r["tick"], half_life), r["tick"]) for r in rows
                if r["type"] == "settlement" and r["tick"] <= tick and pred(r) and r["price"] > 0
                and not (exclude_dealers and (r.get("persona") or r.get("seller_kind") == "dealer" or r.get("buyer_kind") == "dealer"))]
    own = pts(lambda r: r["ref"] == ref)
    n = sum(w for _, w, _ in own)
    out = {"ref": ref, "n_raw": len(own), "n_eff": round(n, 2)}
    if n >= MIN_N:
        vals = sorted(v for v, _, _ in own)
        return dict(out, value=weighted_median([(v, w) for v, w, _ in own]), range=[vals[0], vals[-1]],
                    confidence="media" if n < 2 * MIN_N else "alta", basis="liquidaciones comparables de la carta",
                    change=change_point([(t, v) for v, _, t in own], tick, half_life))
    grp = (card_group or {}).get(ref)
    if grp:
        peers = pts(lambda r: r["ref"] != ref and (card_group or {}).get(r["ref"]) == grp)
        if sum(w for _, w, _ in peers) >= MIN_N:
            vals = sorted(v for v, _, _ in peers + own)
            return dict(out, value=weighted_median([(v, w) for v, w, _ in peers + own]), range=[vals[0], vals[-1]],
                        confidence="baja", basis=f"agrupado: misma rareza y barrio {grp} (pocos datos propios)")
    return dict(out, value=None, range=None, confidence="insuficiente", basis="muestra insuficiente")


def change_point(series: list, tick: int, half_life: float = HALF_LIFE) -> Optional[dict]:
    """Cambio de conducta: mitad reciente frente a mitad antigua sin solaparse los rangos intercuartílicos (≥3 por lado)."""
    s = sorted(series)
    if len(s) < 6:
        return None
    old, new = [v for _, v in s[:len(s) // 2]], [v for _, v in s[len(s) // 2:]]
    q = lambda xs, f: sorted(xs)[min(len(xs) - 1, int(f * len(xs)))]
    if q(new, 0.25) > q(old, 0.75):
        return {"direction": "sube", "old_median": q(old, 0.5), "new_median": q(new, 0.5)}
    if q(new, 0.75) < q(old, 0.25):
        return {"direction": "baja", "old_median": q(old, 0.5), "new_median": q(new, 0.5)}
    return None


def counterparty_profiles(life: list, tick: int, half_life: float = HALF_LIFE) -> dict:
    """Por publicador (equipo o dealer; los alias, aparte) y LADO: tasa de llenado de sus ofertas, tiempo hasta llenarse,
    tasa de cancelación y concesiones observadas al republicar la misma carta más barata (o más cara, si puja).
    Agrupa con todos los publicadores del mismo tipo y lado cuando la muestra efectiva es pequeña."""
    done = [o for o in life if o["outcome"] in ("liquidada", "cancelada", "caducada")]
    pooled = defaultdict(lambda: [0.0, 0.0])
    for o in done:
        w = weight(tick - o["tick"], half_life)
        pooled[(o["maker_kind"], o["side"])][0] += w * (o["outcome"] == "liquidada")
        pooled[(o["maker_kind"], o["side"])][1] += w
    out = {}
    by = defaultdict(list)
    for o in done:
        by[(o["maker"], o["side"])].append(o)
    for (maker, side), os_ in by.items():
        kind = os_[0]["maker_kind"]
        fill = rate([(o["tick"], 1 if o["outcome"] == "liquidada" else 0) for o in os_], tick,
                    tuple(pooled[(kind, side)]), half_life)
        ttf = sorted(o["end"] - o["tick"] for o in os_ if o["outcome"] == "liquidada" and o["end"] is not None)
        conc = []
        seq = defaultdict(list)
        for o in sorted(os_, key=lambda o: o["tick"]):
            seq[o["ref"]].append(o["price"])
        for prices in seq.values():
            conc += [(a - b) if side == "ask" else (b - a) for a, b in zip(prices, prices[1:]) if a != b]
        out[f"{maker}:{side}"] = {"maker": maker, "kind": kind, "side": side, "fill": fill,
                                  "ticks_to_fill": {"median": ttf[len(ttf) // 2], "n": len(ttf)} if ttf else None,
                                  "cancel_rate": round(sum(o["outcome"] == "cancelada" for o in os_) / len(os_), 2),
                                  "concessions": {"median": sorted(conc)[len(conc) // 2], "n": len(conc)} if conc else None,
                                  "n": len(os_), "note": "alias: no se atribuye a ningún equipo" if kind == "alias" else ""}
    return out


# ------------------------------------------------------------------ política versionada y acotada

DEFAULTS = {"ticks": 6, "dry_windows": 2, "min_step": 2, "target_adj": 0.0, "contact_order": "evidence", "use_learned_price": False}
BOUNDS = {"ticks": (4, 10), "dry_windows": (1, 3), "min_step": (2, 4), "target_adj": (-0.10, 0.10)}
CHOICES = {"contact_order": ("evidence", "fill_rate"), "use_learned_price": (False, True)}
FORBIDDEN = ("max_spend", "reserve", "per_card", "margin", "allow_last_copy", "deny_teams", "key", "cancel_unsafe")


def clamp_params(params: dict) -> tuple:
    """Solo parámetros permitidos y dentro de rango; devuelve (parámetros, avisos)."""
    out, notes = dict(DEFAULTS), []
    for k, v in (params or {}).items():
        if k in FORBIDDEN or (k not in BOUNDS and k not in CHOICES):
            notes.append(f"{k}: no ajustable por aprendizaje (ignorado)")
            continue
        if k in BOUNDS:
            lo, hi = BOUNDS[k]
            vv = type(DEFAULTS[k])(max(lo, min(hi, v)))
            if vv != v:
                notes.append(f"{k}={v} fuera de [{lo}, {hi}] → {vv}")
            out[k] = vv
        elif v in CHOICES[k]:
            out[k] = v
        else:
            notes.append(f"{k}={v!r} no es una opción válida")
    return out, notes


def store_path(data_dir) -> Path:
    return Path(data_dir) / "policy_versions.json"


def load_store(data_dir) -> dict:
    try:
        return json.loads(store_path(data_dir).read_text())
    except (OSError, ValueError):
        return {"active": "v0", "versions": {"v0": {"params": dict(DEFAULTS), "evidence": "configuración actual (fallback)",
                                                     "parent": None}}}


def save_store(data_dir, store: dict) -> None:
    p = store_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, ensure_ascii=False, indent=1))


def add_version(store: dict, params: dict, evidence: str, metrics: Optional[dict] = None) -> str:
    """Registra una versión CANDIDATA (no la activa). Cada versión guarda la evidencia que la motivó."""
    clean, notes = clamp_params(params)
    vid = f"v{len(store['versions'])}"
    store["versions"][vid] = {"params": clean, "evidence": evidence, "notes": notes, "metrics": metrics or {},
                              "parent": store.get("active"), "status": "candidata"}
    return vid


def promote(store: dict, vid: str) -> None:
    """Promoción EXPLÍCITA del operador (nunca automática). `rollback` vuelve a la anterior."""
    if vid not in store["versions"]:
        raise KeyError(vid)
    store["versions"][vid]["status"] = "activa"
    store["previous"] = store.get("active")
    store["active"] = vid


def rollback(store: dict) -> str:
    store["active"] = store.get("previous") or "v0"
    return store["active"]


def params_for(data_dir, vid: Optional[str]) -> tuple:
    """Parámetros de la versión pedida; si no existe o no es válida, FALLBACK a los valores actuales."""
    store = load_store(data_dir)
    v = store["versions"].get(vid or "")
    if v is None:
        return dict(DEFAULTS), f"versión {vid!r} no encontrada: se usa la configuración actual"
    p, notes = clamp_params(v["params"])
    return p, "; ".join(notes)
