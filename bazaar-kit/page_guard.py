"""PROTECCIÓN DE PÁGINAS COMPLETAS — restricción dura, prioridad n.º 1 sobre cualquier estrategia de mercado.

Una vez completada una página, el agente trata el conjunto mínimo de cartas necesario para conservarla como inventario
NO NEGOCIABLE. Solo las copias duplicadas por encima de ese mínimo pueden venderse o intercambiarse.

Orden de seguridad:
    1. NUNCA romper una página completa            (este módulo)
    2. NUNCA comprometer dos veces un activo bloqueado (trading.resources / coordinator._cards_of)
    3. NUNCA superar efectivo / precio de reserva   (trading.free_cash, presupuesto del coordinador)
    4. optimización económica (ΔU)
    5. estrategia de mercado

La protección NO depende del valor estimado del bono de página: si el modelo de valoración cambia o es incierto, la
página sigue protegida. Una acción que rompería una página es INVIABLE (se bloquea), no "muy negativa".

Solo un humano puede cambiar esta regla, editando PROTECTION_ENABLED o PROTECTED_REQUIRED_COPIES en este fichero; no
hay flag de línea de comandos ni parámetro que otro módulo pueda pasar para desactivarla.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Iterable, Optional

PROTECTION_ENABLED = True       # cambiar solo a mano, por decisión humana explícita
PROTECTED_REQUIRED_COPIES = 1   # copias por referencia que exige una página estándar
OPEN_STATES = {"open", "pending", "queued"}
# tipos de acción que nunca entregan cartas (solo efectivo, mensajes sin compromiso o cancelaciones)
NON_DELIVERING = {"bid", "cancel", "team_cancel", "team_close", "dealer_close", "dealer_open", "dealer_counter", "info"}

log = logging.getLogger("page_guard")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.WARNING)
    log.propagate = False
EVENTS: list = []        # bloqueos registrados en este proceso (para auditoría y tests)
_logged: set = set()     # evita repetir la misma línea de registro en cada tick


# ------------------------------------------------------------------ páginas

def pages_of(catalog: dict) -> dict:
    """set_id -> referencias que forman su página (misma definición que trading.Valuation.pages)."""
    out = {}
    for s in catalog.get("sets", []):
        out[s["id"]] = [c["id"] for c in s.get("cards", []) if c.get("page") and not c.get("hidden")]
    return out


def completed_pages(counts: Counter, catalog: dict) -> set:
    return {sid for sid, req in pages_of(catalog).items() if req and all(counts.get(r, 0) > 0 for r in req)}


def protected_requirements(counts: Counter, catalog: dict) -> dict:
    """ref -> (copias protegidas, [páginas completas que la necesitan])."""
    out = {}
    if not PROTECTION_ENABLED:
        return out
    pages = pages_of(catalog)
    for sid in sorted(completed_pages(counts, catalog)):
        for ref in pages[sid]:
            n, ps = out.get(ref, (0, []))
            out[ref] = (max(n, PROTECTED_REQUIRED_COPIES), ps + [sid])
    return out


def protected_page_cards(counts: Counter, catalog: dict) -> set:
    """Todas las referencias necesarias para mantener completas las páginas completas actuales."""
    return set(protected_requirements(counts, catalog))


def protected_required_count(ref: str, counts: Counter, catalog: dict) -> int:
    return protected_requirements(counts, catalog).get(ref, (0, []))[0]


def tradeable_surplus(ref: str, counts: Counter, catalog: dict, committed: Optional[Counter] = None) -> int:
    """Copias de `ref` que pueden salir sin tocar el mínimo protegido (ya descontadas las comprometidas)."""
    have = counts.get(ref, 0) - (committed or Counter()).get(ref, 0)
    return max(have - protected_required_count(ref, counts, catalog), 0)


# ------------------------------------------------------------------ validación

def refs_of_assets(asset_ids: Iterable, my_assets: list) -> Counter:
    mine = {a["id"]: a["ref"] for a in my_assets if a.get("kind") == "card" and "id" in a}
    return Counter(mine[i] for i in asset_ids if i in mine)


def committed_assets(my_offers: list, team: str, extra_ids: Iterable = ()) -> set:
    """Ids de nuestras copias ya comprometidas en ofertas abiertas propias (y en acciones pendientes)."""
    out = set(extra_ids)
    for o in my_offers or []:
        if o.get("maker") != team or o.get("status") not in OPEN_STATES:
            continue
        for a in (o.get("give") or {}).get("assets") or []:
            out.add(a["id"] if isinstance(a, dict) else a)
    return out


def validate_protected_assets(*, counts: Counter, catalog: dict, deliver: Counter, receive: Optional[Counter] = None,
                              committed: Optional[Counter] = None, asset_ids: Iterable = (), my_assets: list = (),
                              action_type: str = "?", log_blocks: bool = True) -> list:
    """Simula el inventario tras la acción. Si alguna página completa ANTES deja de estarlo DESPUÉS → bloqueos.

    Antes = inventario actual menos copias ya comprometidas en otras ofertas (se asume lo peor: que se llenan).
    Las páginas protegidas se calculan sobre el inventario actual, sin descontar compromisos.
    Devuelve [] si la acción es segura; si no, una lista de textos "BLOCKED: …" (la acción es inviable)."""
    if not PROTECTION_ENABLED or not deliver:
        return []
    receive, committed = receive or Counter(), committed or Counter()
    req = protected_requirements(counts, catalog)
    if not req:
        return []
    after = Counter(counts)
    after.subtract(committed)
    after.subtract(deliver)
    after.update(receive)
    ids_by_ref, wanted = {}, set(asset_ids)
    for a in my_assets or []:
        if a.get("id") in wanted:
            ids_by_ref.setdefault(a.get("ref"), []).append(a["id"])
    out = []
    for ref, (need, pages) in sorted(req.items()):
        if not deliver.get(ref) or after.get(ref, 0) >= need:
            continue
        for page in pages:
            asset = ",".join(str(i) for i in ids_by_ref.get(ref, [])) or "?"
            reason = f"would break completed page {page}"
            out.append(f"BLOCKED: card belongs to completed page — {reason} ({ref}, asset {asset}, {action_type}; "
                       f"tenemos {counts.get(ref, 0)}, comprometidas {committed.get(ref, 0)}, protegidas {need})")
            if log_blocks:
                _log_block(page, ref, asset, action_type, reason)
    return out


def _log_block(page, ref, asset, action_type, reason):
    key = (page, ref, asset, action_type)
    EVENTS.append({"page": page, "card": ref, "asset": asset, "action": action_type, "reason": reason})
    if key in _logged:
        return
    _logged.add(key)
    log.warning(f"PROTECTED_PAGE_BLOCK page={page} card={ref} asset={asset} action={action_type} reason={reason}")


# ------------------------------------------------------------------ qué entrega una acción

def _side_delivery(side: dict, my_assets: list) -> tuple[Counter, list]:
    side = side or {}
    ids = [a["id"] if isinstance(a, dict) else a for a in side.get("assets") or []]
    refs = refs_of_assets(ids, my_assets)
    for t in list(side.get("types") or []) + [f"card:{c}" for c in side.get("cards") or []]:
        if isinstance(t, str) and t.startswith("card:"):
            refs[t[5:]] += 1
    return refs, ids


def _merge_max(a: Counter, b: Counter) -> Counter:
    return Counter({k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)})


def delivery_of(c: dict, snap: dict) -> tuple[Counter, list, list]:
    """(referencias que entregaríamos, ids concretos, referencias que recibiríamos) para CUALQUIER candidata.

    Se miran todos los campos conocidos y se toma el máximo por referencia: vale para venta, publicación, trueque,
    lote, oferta dirigida, vendedores, campañas y tipos futuros que usen los mismos campos."""
    my_assets = (snap.get("me") or {}).get("assets") or []
    t = c.get("type")
    if t in NON_DELIVERING:
        return Counter(), [], Counter()
    ids = list(c.get("assets") or [])
    if c.get("asset") is not None and t not in ("bid",):
        ids.append(c["asset"])
    opp = c.get("opp") or {}
    if opp.get("asset") is not None:
        ids.append(opp["asset"])
    by_ids = refs_of_assets(set(ids), my_assets)
    named = Counter(c.get("deliver") or {}) if isinstance(c.get("deliver"), dict) else Counter()
    if c.get("give_ref"):
        named[c["give_ref"]] = max(named.get(c["give_ref"], 0), 1)
    if isinstance(opp.get("deliver"), str):
        named[opp["deliver"]] = max(named.get(opp["deliver"], 0), 1)
    receive = Counter(c.get("receive") or {}) if isinstance(c.get("receive"), dict) else Counter()
    if t == "swap_list" and c.get("ref"):
        receive[c["ref"]] += 1  # el trueque liquida las dos patas a la vez
    # oferta estructurada que enviamos (propuesta en conversación) o que aceptamos (lo que nos piden)
    sides = []
    if isinstance(c.get("offer"), dict):
        sides.append(c["offer"].get("give"))
    elif t in ("accept", "team_accept", "dealer_accept") and c.get("offer") is not None:
        o = find_offer(snap, c["offer"])
        if o:
            sides.append(o.get("want"))
            if not receive:
                for a in (o.get("give") or {}).get("assets") or []:
                    if isinstance(a, dict) and a.get("ref"):
                        receive[a["ref"]] += 1
    if isinstance(c.get("give"), dict):
        sides.append(c["give"])
    for sd in sides:
        r, i = _side_delivery(sd, my_assets)
        named = _merge_max(named, r)
        ids += i
    return +_merge_max(by_ids, named), sorted(set(ids)), +receive


def find_offer(snap: dict, offer_id) -> Optional[dict]:
    pool = list((snap.get("board") or {}).get("offers", [])) + list((snap.get("offers") or {}).get("offers", []))
    for b in (snap.get("boards") or {}).values():
        pool += (b or {}).get("offers", [])
    for t in ((snap.get("threads") or {}).get("open") or []) + ((snap.get("threads") or {}).get("deal") or []):
        pool += t.get("standing_offers") or []
    return next((o for o in pool if o.get("id") == offer_id), None)


def guard_candidate(c: dict, snap: dict, committed_ids: Iterable = ()) -> list:
    """Bloqueos de protección de página para una candidata (sin modificarla)."""
    me = snap.get("me") or {}
    my_assets = me.get("assets") or []
    counts = Counter(a["ref"] for a in my_assets if a.get("kind") == "card")
    deliver, ids, receive = delivery_of(c, snap)
    if not deliver:
        return []
    if PROTECTION_ENABLED and not (snap.get("catalog") or {}).get("sets"):
        return ["BLOCKED: sin catálogo no se puede verificar la protección de páginas completas"]  # falla cerrado
    own = set(ids)
    committed = refs_of_assets(set(committed_ids) - own, my_assets)
    return validate_protected_assets(counts=counts, catalog=snap.get("catalog") or {}, deliver=deliver,
                                     receive=receive, committed=committed, asset_ids=ids, my_assets=my_assets,
                                     action_type=str(c.get("type")))


def apply_guard(cands: list, snap: dict, committed_ids: Iterable = ()) -> int:
    """Marca con bloqueos (antes de ordenar) toda candidata que rompería una página completa. Devuelve cuántas."""
    n = 0
    for c in cands:
        if c.get("protected_page_block"):  # ya marcada por el planificador
            n += 1
            continue
        b = guard_candidate(c, snap, committed_ids)
        if b:
            c["blockers"] = list(c.get("blockers") or []) + b
            c["protected_page_block"] = True
            n += 1
    return n


# ------------------------------------------------------------------ ofertas propias que ya no son seguras

def unsafe_open_offers(my_offers: list, team: str, counts: Counter, catalog: dict, my_assets: list = ()) -> list:
    """Ofertas abiertas propias que, si se llenan, dejarían una página completa sin una carta necesaria.

    Caso típico: publicamos un duplicado cuando la página estaba incompleta y después la completamos. Se acumula en
    orden de id y se cancela la oferta que cruza el mínimo protegido (prioridad máxima de seguridad)."""
    req = protected_requirements(counts, catalog)
    if not req:
        return []
    out, used = [], Counter()
    for o in sorted(my_offers or [], key=lambda o: o.get("id", 0)):
        if o.get("maker") != team or o.get("status") not in OPEN_STATES:
            continue
        give, ids = _side_delivery(o.get("give"), my_assets)
        for a in (o.get("give") or {}).get("assets") or []:  # la oferta trae la ref aunque no tengamos el inventario
            if isinstance(a, dict) and a.get("ref") and not refs_of_assets([a.get("id")], my_assets):
                give[a["ref"]] += 1
        hits = [(ref, req[ref]) for ref in give if ref in req and counts.get(ref, 0) - used[ref] - give[ref] < req[ref][0]]
        if not hits:
            used.update(give)
            continue
        why = []
        for ref, (need, pages) in hits:
            for page in pages:
                reason = "open offer would break completed page"
                why.append(f"BLOCKED: card belongs to completed page {page} ({ref}); cancelación de seguridad")
                _log_block(page, ref, ",".join(map(str, ids)) or "?", "open_offer", reason)
        out.append({"type": "cancel", "kind": "PROTECCIÓN PÁGINA: retirar oferta", "offer": o["id"],
                    "venue": o.get("venue"), "ref": ",".join(give), "price": int((o.get("want") or {}).get("cash") or 0),
                    "du": 0.0, "score": 10 ** 7, "blockers": [], "why": why, "reason": "; ".join(why),
                    "protected_page_cancel": True, "notes": ["prioridad 1: nunca romper una página completa"],
                    "uncertainty": ""})
    return out
