"""NIVELES NUEVOS — Los Pícaros, The Workshop (taller) y sobres regalados (opt-in; solo lectura por defecto).

Sondea `GET /api/levels` y `GET /api/dealers` y, cuando un nivel anunciado pasa a activo, imprime su `how`, menú y
rasgos con un plan:

  picaros   dealer anunciado con «Quick deals. Few questions.»: probablemente MIENTE (RULES: «Some lie»). Plan:
            negociar como con Chato pero comprobar SIEMPRE `flagger.offer_matches_words` antes de aceptar y pasar
            `flagger.py` (dry run; `--execute` para marcar) sobre sus hilos.
  taller    «Three spares. One surprise.»: se entregan tres cartas sobrantes a cambio de una sorpresa. Elegimos tres
            copias SOBRANTES (nunca la única copia de una carta ni la que completa página: page_guard.tradeable_assets)
            de menor valor privado. Mientras el endpoint no se conozca, `workshop_request()` devuelve None y no se
            envía nada: al activarse, `how` trae la ruta («POST /api/...») y la función la extrae. Si el formato del
            cuerpo no es `{"assets": [...]}`, se ajusta SOLO esa función.
  sobres    lista los sobres propios sin abrir (regalos de la Abuela, sobre_plata...) y, con `--open-packs`, los abre
            (dry run salvo `--execute`).

    python3 new_levels.py                      # una pasada: estado de los niveles + plan + spares + sobres
    python3 new_levels.py --watch 60           # repite cada 60 s y avisa cuando algo pasa a activo
    python3 new_levels.py --open-packs         # dry run de abrir los sobres sin abrir
    python3 new_levels.py --workshop           # dry run del envío al taller (si `how` ya trae la ruta)
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


# ------------------------------------------------------------------ taller: tres spares seguros

def workshop_spares(me: dict, catalog: dict, my_offers: list = (), n: int = SPARES) -> list:
    """Hasta `n` copias sobrantes [{id, ref, your_value}] de menor valor privado. Por referencia se conserva siempre
    una copia (la de menor id) y nunca se toca la protegida por una página completa ni una ya comprometida."""
    assets = [a for a in me.get("assets") or [] if a.get("kind") == "card"]
    counts = Counter(a["ref"] for a in assets)
    committed = pg.committed_assets(list(my_offers or []), me.get("id"))
    by_id = {a["id"]: a for a in assets}
    pool = []
    for ref, c in counts.items():
        if c < 2:
            continue
        free = pg.tradeable_assets(ref, counts, catalog, assets, committed)
        staying = sorted(a["id"] for a in assets if a["ref"] == ref and a["id"] not in committed)
        if len(staying) < 2:
            continue
        keep = staying[0]                                  # la copia que siempre se queda
        pool += [by_id[i] for i in free if i != keep]
    pool.sort(key=lambda a: (a.get("your_value") or 0, a["id"]))
    return [{"id": a["id"], "ref": a["ref"], "your_value": a.get("your_value")} for a in pool[:n]]


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
           my_offers: list = ()) -> list:
    lines = []
    for lid in WATCHED:
        lv, dl = level_of(levels, lid), dealer_of(dealers, lid)
        lines += describe(lid, lv, dl)
        if lid == "picaros" and is_active(lv, dl):
            lines += plan_picaros(dl)
        if lid == "taller" and me is not None and catalog is not None:
            spares = workshop_spares(me, catalog, my_offers)
            lines.append(f"  spares para el taller (menor valor primero): "
                         f"{[(s['id'], s['ref'], s['your_value']) for s in spares] or 'ninguno seguro'}")
            req = workshop_request((lv or {}).get("how"), [s["id"] for s in spares])
            lines.append(f"  envío: {req['method']} {req['path']} {json.dumps(req['body'])}" if req else
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
        print("\n".join(report(levels, dealers, me, catalog, offers)))
        if me is not None and a.open_packs:
            open_packs(api, unopened_packs(me), a.execute)
        if me is not None and a.workshop:
            spares = workshop_spares(me, catalog, offers)
            req = workshop_request((level_of(levels, "taller") or {}).get("how"), [s["id"] for s in spares])
            if not req or len(spares) < SPARES:
                print("taller: nada que enviar (ruta desconocida o menos de tres spares seguros)")
            elif a.execute:
                print("taller:", api.call(req["method"], req["path"], req["body"]))
            else:
                print(f"[dry] taller: {req['method']} {req['path']} {json.dumps(req['body'])}")
        if not a.watch:
            return 0
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
