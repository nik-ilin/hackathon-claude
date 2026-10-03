"""Oportunidades entre TERCEROS en nuestro venue (v15) para market making. Lógica pura: detecta, rastrea el estado y
redacta un contacto breve. No envía nada y NUNCA compra ni vende nosotros esos activos.

Qué cuenta (RULES.md): el market making puntúa el valor que crean OTROS equipos entre ellos en tu venue; no se puede
comerciar en el venue propio con la clave del equipo. Con `mechanism: auto` el motor cruza el mejor bid con el mejor ask
cada tick por sí solo (un broker solo actúa en venues `board`): nuestro trabajo es conseguir que ambos lados PUBLIQUEN en
v15, no emparejarlos. No se abre otro mercado, no se cambia el mecanismo y no se inmoviliza fianza.

Estados: OPPORTUNITY → INTEREST_CONFIRMED → TERMS_AGREED → POSTED → PENDING → SETTLED | EXPIRED | REJECTED
Las dos primeras transiciones (confirmar interés y acordar condiciones) solo las puede dar una respuesta real de las
contrapartes: las marca el operador; el resto se deriva de la evidencia del servidor. Sin interés confirmado no hay
promesa de tráfico: es una hipótesis con vendedor, comprador, carta, precios y evidencia.
"""
from __future__ import annotations

import re

STATES = ("OPPORTUNITY", "INTEREST_CONFIRMED", "TERMS_AGREED", "POSTED", "PENDING", "SETTLED", "EXPIRED", "REJECTED")
ORDER = {s: i for i, s in enumerate(STATES)}
TEAM = re.compile(r"^t\d+$")


def _card_side(o):
    g, w = o.get("give") or {}, o.get("want") or {}
    ga = [a for a in g.get("assets") or [] if isinstance(a, dict)]
    wt = [t[5:] for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []] if str(t).startswith("card:")]
    if len(ga) == 1 and w.get("cash") and not wt and not g.get("cash"):
        return "ask", ga[0].get("ref"), int(w["cash"])
    if g.get("cash") and not ga and len(wt) == 1 and not w.get("cash"):
        return "bid", wt[0], int(g["cash"])
    return None, None, None


def detect(snap: dict, own_venue: str, team: str, fees: dict | None = None) -> list:
    """Pares (vendedor, comprador) de equipos IDENTIFICABLES distintos de nosotros con un ask y un bid de la misma carta
    cuyo precio cruza (bid ≥ ask) y que NO están ya en nuestro venue. Los alias anónimos se ignoran."""
    asks, bids = [], []
    pool = []
    for vid, b in (snap.get("boards") or {}).items():
        pool += [(vid, o) for o in (b or {}).get("offers", [])]
    for vid, o in pool:
        mk = o.get("maker")
        if o.get("status") != "open" or not TEAM.match(str(mk or "")) or mk == team:
            continue
        if o.get("to") not in (None, team) and TEAM.match(str(o.get("to") or "")):
            pass  # una oferta dirigida a otro equipo sigue siendo evidencia de interés
        kind, ref, price = _card_side(o)
        if kind == "ask":
            asks.append((vid, o, ref, price))
        elif kind == "bid":
            bids.append((vid, o, ref, price))
    out = []
    for av, ao, ref, ap in asks:
        for bv, bo, bref, bp in bids:
            if bref != ref or ao["maker"] == bo["maker"] or bp < ap:
                continue
            if av == own_venue and bv == own_venue:
                continue  # ya están aquí
            fee_saved = sum((fees or {}).get(v, 0) for v in {av, bv} if v != own_venue)
            out.append({"key": f"{ref}:{ao['maker']}:{bo['maker']}", "ref": ref, "seller": ao["maker"],
                        "buyer": bo["maker"], "ask": ap, "bid": bp, "seller_venue": av, "buyer_venue": bv,
                        "evidence": [ao["id"], bo["id"]], "spread": bp - ap,
                        "est_fee_saved": fee_saved})
    return sorted(out, key=lambda x: (-x["spread"], x["key"]))


def draft(opp: dict, own_venue: str, team_name: str = "Team 15") -> dict:
    """Mensaje corto y veraz (sin valores privados) para cada lado; se envía a mano o por una acción autorizada."""
    return {
        "seller": f"Hola, somos {team_name}. Vemos tu venta de {opp['ref']} a {opp['ask']} P. En nuestro venue "
                  f"{own_venue} no hay comisión y cruza ofertas solo cada tick: ¿la publicas ahí?",
        "buyer": f"Hola, somos {team_name}. Vemos tu puja por {opp['ref']} a {opp['bid']} P. En {own_venue} no hay "
                 f"comisión y cruza ofertas solo cada tick: ¿la publicas ahí?"}


def update(state: dict, snap: dict, own_venue: str, team: str, tick: int, fees: dict | None = None) -> list:
    """Actualiza el registro persistente con la evidencia del servidor y devuelve los cambios de estado."""
    reg = state.setdefault("opps", {})
    changes = []
    found = {o["key"]: o for o in detect(snap, own_venue, team, fees)}
    for k, o in found.items():
        if k not in reg:
            reg[k] = {**o, "state": "OPPORTUNITY", "created_tick": tick, "last_tick": tick,
                      "next_action": f"contactar a {o['seller']} y {o['buyer']} (mensaje breve; sin spam: 1 contacto por lado)"}
            changes.append((k, None, "OPPORTUNITY"))
    own_offers = [o for o in (snap.get("boards") or {}).get(own_venue, {}).get("offers", []) if o.get("status") == "open"]
    settled = [e for e in (snap.get("feed") or {}).get("events", []) if e.get("type") == "settlement"
               and (e.get("payload") or {}).get("venue") == own_venue]
    for k, r in reg.items():
        if r["state"] in ("SETTLED", "EXPIRED", "REJECTED"):
            continue
        r["last_tick"] = tick
        sides = {"seller": False, "buyer": False}
        for o in own_offers:
            kind, ref, _ = _card_side(o)
            if ref == r["ref"] and o.get("maker") == r["seller"] and kind == "ask":
                sides["seller"] = True
            if ref == r["ref"] and o.get("maker") == r["buyer"] and kind == "bid":
                sides["buyer"] = True
        new = r["state"]
        if any(r["seller"] in (e["payload"].get("parties") or []) and r["buyer"] in (e["payload"].get("parties") or [])
               and any(i.get("ref") == r["ref"] for i in e["payload"].get("items") or []) for e in settled):
            new = "SETTLED"
        elif all(sides.values()):
            new = "PENDING"
        elif any(sides.values()):
            new = "POSTED"
        elif ORDER[r["state"]] >= ORDER["POSTED"]:
            new = "EXPIRED"  # estuvieron publicadas y ya no están, sin liquidación
        elif k not in found and ORDER[r["state"]] < ORDER["POSTED"]:
            new = "EXPIRED"  # la evidencia que la sostenía desapareció
        if new != r["state"]:
            changes.append((k, r["state"], new))
            r["state"] = new
    return changes


def mark(state: dict, key: str, new: str, note: str = "") -> None:
    """Transición confirmada por una respuesta REAL de las contrapartes (la da el operador)."""
    r = (state.get("opps") or {}).get(key)
    if r is None or new not in STATES:
        raise KeyError(f"oportunidad o estado desconocido: {key} {new}")
    if ORDER[new] < ORDER[r["state"]] and new != "REJECTED":
        raise ValueError("no se retrocede de estado")
    r["state"], r["note"] = new, note


def report(state: dict, own_venue: str) -> list:
    lines = []
    for k, r in sorted((state.get("opps") or {}).items()):
        lines.append(f"v15 {r['state']:<18} {r['ref']} vende {r['seller']} {r['ask']} P ({r['seller_venue']}) → compra "
                     f"{r['buyer']} {r['bid']} P ({r['buyer_venue']}) · evidencia ofertas {r['evidence']} · "
                     f"siguiente: {r['next_action']}")
    return lines
