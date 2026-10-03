"""NIVELES NUEVOS — Los Pícaros, The Workshop (taller) y sobres regalados (opt-in; solo lectura por defecto).

Sondea `GET /api/levels` y `GET /api/dealers` y, cuando un nivel anunciado pasa a activo, imprime su `how`, menú y
rasgos con un plan:

  picaros   dealer anunciado con «Quick deals. Few questions.»: probablemente MIENTE (RULES: «Some lie»). Plan:
            negociar como con Chato pero comprobar SIEMPRE `flagger.offer_matches_words` antes de aceptar y pasar
            `flagger.py` (dry run; `--execute` para marcar) sobre sus hilos.
  taller    «Three spares. One surprise.»: se entregan tres cartas sobrantes a cambio de una sorpresa. Elegimos tres
            copias SOBRANTES de UNA MISMA rareza (nunca la única copia de una carta ni la que completa página:
            page_guard.tradeable_assets) de menor valor privado, y solo se envían si el valor esperado (media de
            value_next de las cartas publicadas de la rareza siguiente) supera la pérdida + `--workshop-margin`.
            La ruta sale del `how` (`POST /api/taller {"assets": [a, b, c]}` desde t706); sin ruta no se envía nada.
  sobres    lista los sobres propios sin abrir (regalos de la Abuela, sobre_plata...) y, con `--open-packs`, los abre
            (dry run salvo `--execute`).

    python3 new_levels.py                      # una pasada: estado de los niveles + plan + spares + sobres
    python3 new_levels.py --watch 60           # repite cada 60 s y avisa cuando algo pasa a activo
    python3 new_levels.py --open-packs         # dry run de abrir los sobres sin abrir
    python3 new_levels.py --workshop           # dry run del envío al taller (si `how` ya trae la ruta y EV compensa)
    python3 new_levels.py --workshop --workshop-margin 5   # exigir más excedente esperado
    ... --execute                              # solo entonces escribe en el servidor
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from typing import Optional

import page_guard as pg
import trading as tr

DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
WATCHED = ("picaros", "taller")
SPARES = 3


# ------------------------------------------------------------------ niveles y dealers

def _list(raw, key: str) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in (key, "personas", "dealers", "levels"):
            if isinstance(raw.get(k), list):
                return raw[k]
    return []


def level_of(levels: dict, lid: str) -> Optional[dict]:
    return next((x for x in _list(levels, "levels") if x.get("id") == lid), None)


def dealer_of(dealers: dict, did: str) -> Optional[dict]:
    return next((x for x in _list(dealers, "personas") if x.get("id") == did), None)


def is_active(level: Optional[dict], dealer: Optional[dict] = None) -> bool:
    return bool((level and level.get("state") == "active") or (dealer and dealer.get("status") == "active"))


def describe(lid: str, level: Optional[dict], dealer: Optional[dict]) -> list:
    """Líneas legibles: nombre, estado, how, menú y rasgos (lo que haya)."""
    if not level and not dealer:
        return [f"{lid}: no aparece en /api/levels ni /api/dealers"]
    lv, dl = level or {}, dealer or {}
    out = [f"{lv.get('name') or dl.get('name') or lid} [{lid}] · estado: {lv.get('state') or dl.get('status')} · "
           f"{lv.get('teaser') or ''}"]
    if lv.get("how"):
        out.append(f"  how: {lv['how']}")
    if lv.get("opens_to_all_at_hours") is not None:
        out.append(f"  abre a todos a las +{lv['opens_to_all_at_hours']} h (open_to_all={lv.get('open_to_all')})")
    if dl.get("menu"):
        out.append(f"  menu: {json.dumps(dl['menu'], ensure_ascii=False)}")
    if dl.get("traits"):
        out.append(f"  traits: {json.dumps(dl['traits'], ensure_ascii=False)}")
    if dl.get("unlock"):
        out.append(f"  unlock: {json.dumps(dl['unlock'], ensure_ascii=False)}")
    return out


def plan_picaros(dealer: Optional[dict]) -> list:
    tr = (dealer or {}).get("traits") or {}
    out = ["  plan Pícaros:",
           "   - antes de aceptar CUALQUIER oferta suya: flagger.safe_to_accept(thread, offer_id, catalog) "
           "(si las palabras no casan con la estructura, no se acepta)",
           "   - flagger.py en dry run tras cada ronda; revisar y lanzar con --execute los candidatos fuertes "
           "(un flag erróneo resta: nunca faroles)",
           "   - leer la estructura: precio (want/give.cash), carta (types/assets) y lado; ignorar las palabras"]
    if tr.get("shrewdness", 0) >= 0.7 or tr.get("strictness", 0) >= 0.7:
        out.append("   - astuto/estricto: pasos pequeños como con Chato; no repetir precio")
    if (dealer or {}).get("menu"):
        out.append("   - menú disponible: comparar list_price con nuestro valor privado antes de abrir hilo")
    return out


# ------------------------------------------------------------------ taller: tres spares de UNA rareza

RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
WORKSHOP_MARGIN = 2.0


def next_rarity(rarity: Optional[str]) -> Optional[str]:
    return RARITIES[RARITIES.index(rarity) + 1] if rarity in RARITIES[:-1] else None


def _rarity_of(a: dict, catalog: dict) -> Optional[str]:
    if a.get("rarity"):
        return a["rarity"]
    for s in catalog.get("sets", []):
        for c in s.get("cards", []):
            if c.get("id") == a["ref"]:
                return c.get("rarity")
    return None


def spare_pool(me: dict, catalog: dict, my_offers: list = ()) -> dict:
    """rareza -> copias sobrantes [{id, ref, your_value, rarity}] de menor a mayor valor privado. Por referencia se
    conserva siempre una copia (la de menor id no comprometida) y nunca se toca la protegida por una página completa
    (page_guard) ni una ya comprometida en una oferta abierta."""
    assets = [a for a in me.get("assets") or [] if a.get("kind") == "card"]
    counts = Counter(a["ref"] for a in assets)
    committed = pg.committed_assets(list(my_offers or []), me.get("id"))
    by_id = {a["id"]: a for a in assets}
    pool: dict = {}
    for ref, c in counts.items():
        if c < 2:
            continue
        free = pg.tradeable_assets(ref, counts, catalog, assets, committed)
        staying = sorted(a["id"] for a in assets if a["ref"] == ref and a["id"] not in committed)
        if len(staying) < 2:
            continue
        keep = staying[0]                                  # la copia que siempre se queda
        for i in free:
            if i != keep:
                a = by_id[i]
                pool.setdefault(_rarity_of(a, catalog), []).append(
                    {"id": a["id"], "ref": a["ref"], "your_value": a.get("your_value"), "rarity": _rarity_of(a, catalog)})
    for v in pool.values():
        v.sort(key=lambda a: (a.get("your_value") or 0, a["id"]))
    return pool


def workshop_ev(me: dict, catalog: dict, spares: list, values: Optional[dict] = None) -> dict:
    """Valor esperado del taller con esas copias: media del valor privado de UNA copia más (value_next, con bono de
    página si la completa) de cada carta publicada (set released, no oculta) de la rareza siguiente, tras entregar las
    tres; frente al valor privado que perdemos. Supone sorteo uniforme entre esas cartas («the pull is luck»).
    `values` (ref -> value_next, p. ej. de /api/me/value o my_values) sustituye al modelo del catálogo."""
    rarity = spares[0].get("rarity") if spares else None
    to = next_rarity(rarity)
    out = {"rarity": rarity, "to": to, "loss": None, "ev": None, "pool": 0, "gain": None}
    val = tr.Valuation(catalog, me.get("affinity") or {})
    counts = tr.counts_of(me.get("assets") or [])
    removed = Counter(s["ref"] for s in spares)
    if any(val.unit(r) is None for r in removed):
        out["loss"] = round(sum(s.get("your_value") or 0 for s in spares), 2)  # sin book: valor del servidor
    else:
        out["loss"] = round(-val.delta(counts, Counter(), removed)[0], 2)
    after = +(counts - removed)
    pool = [r for r, c in val.cards.items() if c.get("released") and not c.get("hidden") and c.get("rarity") == to]
    vals = []
    for r in pool:
        if values and r in values:
            vals.append(float(values[r]))
        elif val.unit(r) is not None:
            vals.append(val.delta(after, Counter({r: 1}), Counter())[0])
    if vals:
        out.update(pool=len(vals), ev=round(sum(vals) / len(vals), 2))
        out["gain"] = round(out["ev"] - out["loss"], 2)
    return out


def workshop_plan(me: dict, catalog: dict, my_offers: list = (), n: int = SPARES, margin: float = WORKSHOP_MARGIN,
                  values: Optional[dict] = None) -> dict:
    """Mejor envío al taller: por rareza, las `n` sobrantes más baratas; se elige la de mayor EV − pérdida.
    recommend = EV > pérdida + margen (y hay `n` copias de UNA misma rareza)."""
    best = None
    for rarity, spares in spare_pool(me, catalog, my_offers).items():
        if len(spares) < n:
            continue
        ev = workshop_ev(me, catalog, spares[:n], values)
        cand = dict(ev, spares=spares[:n])
        key = (cand["gain"] is not None, cand["gain"] if cand["gain"] is not None else -cand["loss"])
        if best is None or key > best[0]:
            best = (key, cand)
    if best is None:
        return {"spares": [], "recommend": False, "margin": margin,
                "why": f"no hay {n} sobrantes seguras de una misma rareza"}
    plan = dict(best[1], margin=margin)
    plan["recommend"] = plan["ev"] is not None and plan["ev"] > plan["loss"] + margin
    plan["why"] = (f"EV {plan['ev']} P ({plan['pool']} cartas {plan['to']}) frente a pérdida {plan['loss']} P + margen "
                   f"{margin}" if plan["ev"] is not None else f"sin valores para la rareza {plan['to']}: no se envía")
    return plan


def workshop_spares(me: dict, catalog: dict, my_offers: list = (), n: int = SPARES) -> list:
    """Hasta `n` copias sobrantes de UNA misma rareza [{id, ref, your_value, rarity}], de menor valor privado: la
    rareza del mejor plan (EV − pérdida) si alguna llega a `n`; si no, la rareza con más sobrantes."""
    plan = workshop_plan(me, catalog, my_offers, n)
    if plan["spares"]:
        return plan["spares"]
    pool = spare_pool(me, catalog, my_offers)
    if not pool:
        return []
    best = max(pool.values(), key=lambda v: (len(v), -sum(a.get("your_value") or 0 for a in v)))
    return best[:n]


_ROUTE_RE = re.compile(r"\b(POST)\s+(/api/[A-Za-z0-9_\-/{}]+)")


def workshop_request(how: Optional[str], asset_ids: list) -> Optional[dict]:
    """(method, path, body) del envío al taller, extraído de su `how`; None mientras no se conozca la ruta.
    Cuerpo: si `how` da un JSON de ejemplo con una lista, se usa su clave; si no, {"assets": ids}."""
    if not how:
        return None
    m = _ROUTE_RE.search(how)
    if not m:
        return None
    key = "assets"
    ex = re.search(r"\{\s*\"(\w+)\"\s*:\s*\[", how)
    if ex:
        key = ex.group(1)
    return {"method": m.group(1), "path": m.group(2), "body": {key: list(asset_ids)}}


# ------------------------------------------------------------------ sobres sin abrir

def unopened_packs(me: dict) -> list:
    return [{"id": a["id"], "ref": a.get("ref"), "name": a.get("name"), "your_value": a.get("your_value")}
            for a in me.get("assets") or [] if a.get("kind") == "pack"]


def open_packs(api, packs: list, execute: bool) -> list:
    done = []
    for p in packs:
        if not execute:
            print(f"[dry] abrir sobre #{p['id']} ({p['ref']})")
            continue
        r = api.open_pack(p["id"])
        print(f"abierto #{p['id']}: {[c.get('ref') for c in (r.get('cards') or [])]} luck={r.get('luck')}")
        done.append(p["id"])
    return done


# ------------------------------------------------------------------ una pasada

def report(levels: dict, dealers: dict, me: Optional[dict] = None, catalog: Optional[dict] = None,
           my_offers: list = (), margin: float = WORKSHOP_MARGIN) -> list:
    lines = []
    for lid in WATCHED:
        lv, dl = level_of(levels, lid), dealer_of(dealers, lid)
        lines += describe(lid, lv, dl)
        if lid == "picaros" and is_active(lv, dl):
            lines += plan_picaros(dl)
        if lid == "taller" and me is not None and catalog is not None:
            plan = workshop_plan(me, catalog, my_offers, margin=margin)
            spares = plan["spares"] or workshop_spares(me, catalog, my_offers)
            lines.append(f"  spares para el taller ({plan.get('rarity') or 'una rareza'} → {plan.get('to') or '?'}, "
                         f"menor valor primero): "
                         f"{[(s['id'], s['ref'], s['your_value']) for s in spares] or 'ninguno seguro'}")
            lines.append(f"  valor esperado: {'RECOMENDADO' if plan['recommend'] else 'NO recomendado'} · {plan['why']}")
            req = workshop_request((lv or {}).get("how"), [s["id"] for s in spares])
            lines.append(f"  envío: {req['method']} {req['path']} {json.dumps(req['body'])}" if req and spares else
                         "  envío: nada (sin sobrantes seguras de una rareza)" if req else
                         "  envío: pendiente (el taller aún no publica su ruta en `how`)")
    if me is not None:
        packs = unopened_packs(me)
        lines.append(f"sobres sin abrir: {[(p['id'], p['ref']) for p in packs] or 'ninguno'}")
    return lines


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=float, default=0, help="segundos entre sondeos (0 = una pasada)")
    ap.add_argument("--open-packs", action="store_true", help="abrir los sobres sin abrir (dry run sin --execute)")
    ap.add_argument("--workshop", action="store_true", help="enviar los spares al taller (dry run sin --execute)")
    ap.add_argument("--workshop-margin", type=float, default=WORKSHOP_MARGIN,
                    help="solo se envía al taller si EV > pérdida + margen (P; por defecto 2)")
    ap.add_argument("--execute", action="store_true", help="escribe en el servidor (por defecto, nunca)")
    a = ap.parse_args(argv)

    from bazaar_sdk import Bazaar
    key = os.environ.get("BAZAAR_KEY")
    api = Bazaar(DEFAULT_URL, key or "")
    seen: dict = {}
    while True:
        levels, dealers = api.levels(), api.dealers()
        me = catalog = None
        offers: list = []
        if key:
            me, catalog = api.me(), api.catalog()
            offers = (api.my_offers() or {}).get("offers", [])
        for lid in WATCHED:
            act = is_active(level_of(levels, lid), dealer_of(dealers, lid))
            if act and not seen.get(lid):
                print(f"*** {lid} ACTIVO ***")
            seen[lid] = act
        print("\n".join(report(levels, dealers, me, catalog, offers, a.workshop_margin)))
        if me is not None and a.open_packs:
            open_packs(api, unopened_packs(me), a.execute)
        if me is not None and a.workshop:
            plan = workshop_plan(me, catalog, offers, margin=a.workshop_margin)
            spares = plan["spares"]
            req = workshop_request((level_of(levels, "taller") or {}).get("how"), [s["id"] for s in spares])
            if not req or len(spares) < SPARES:
                print("taller: nada que enviar (ruta desconocida o menos de tres spares seguros de una misma rareza)")
            elif not plan["recommend"]:
                print(f"taller: no compensa · {plan['why']}")
            elif a.execute:
                print("taller:", api.call(req["method"], req["path"], req["body"]))
            else:
                print(f"[dry] taller: {req['method']} {req['path']} {json.dumps(req['body'])}")
        if not a.watch:
            return 0
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
