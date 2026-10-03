"""Servicio ÚNICO de oportunidades: lo consumen el coordinador (decide) y el dashboard (muestra). Lógica pura, stdlib.

Antes había dos cálculos independientes que no podían coincidir: el dashboard comparaba `min(asks históricos)` con el
«suelo» de un dealer (`Oracle.arbitrage`), y el coordinador decidía sobre las ofertas vivas del servidor. Ninguna señal del
dashboard llegaba a una candidata del agente. Ahora:

  * EJECUTABLE   = una candidata del coordinador (oferta viva y verificada en el tick, o conversación con un vendedor).
                   Es la ÚNICA fuente de «qué se puede hacer»; el dashboard solo la muestra.
  * PISTA        = evidencia histórica (feed, memoria, market.db). NUNCA se opera directamente: se contrasta con las
                   ofertas vivas del servidor. Si no está viva (caducada, retirada, no verificada) queda como REFERENCIA.
  * REFERENCIA DE PRECIOS = diferencia entre dos precios de VENTA (p. ej. un equipo y un dealer). No es arbitraje.
  * ARBITRAJE    = SOLO si existen las dos patas VIVAS (un ask comprable y una puja que paga más, netos de comisión).

Roles inequívocos (sustituyen a «a quién»):
  vendedor confirmado    tiene un ask abierto, verificado en el servidor en este tick (o un vendedor con oferta estructurada)
  comprador confirmado   tiene una puja abierta, verificada en el servidor en este tick
  poseedor histórico     publicó o recibió la carta en el pasado; NO implica que la tenga hoy ni que la venda
  buscador histórico     pujó o pidió la carta en el pasado
  anónimo (alias)        el tablón no revela el equipo: nunca se atribuye a un equipo
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

TEAM = re.compile(r"^t\d+$")
SELLER, BUYER = "vendedor confirmado", "comprador confirmado"
HOLDER, SEEKER, ALIAS = "poseedor histórico", "buscador histórico", "anónimo (alias)"


def is_team(x) -> bool:
    return isinstance(x, str) and bool(TEAM.match(x))


def single_card(o: dict):
    """(lado, carta, precio) de una oferta de UNA carta contra efectivo; (None, None, None) en otro caso."""
    g, w = o.get("give") or {}, o.get("want") or {}
    ga = [a for a in g.get("assets") or [] if isinstance(a, dict) and a.get("ref")]
    wt = [t[5:] for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []] if str(t).startswith("card:")]
    if len(ga) == 1 and w.get("cash") and not wt and not g.get("cash"):
        return "ask", ga[0]["ref"], int(w["cash"])
    if g.get("cash") and not ga and len(wt) == 1 and not w.get("cash"):
        return "bid", wt[0], int(g["cash"])
    return None, None, None


def live_index(snap: dict) -> dict:
    """id → oferta ABIERTA y vigente en el servidor en este tick (tablones de todos los venues + mis ofertas)."""
    tick = (snap.get("clock") or {}).get("tick") or 0
    pool = list((snap.get("offers") or {}).get("offers", []))
    for b in (snap.get("boards") or {}).values():
        pool += (b or {}).get("offers", [])
    pool += (snap.get("board") or {}).get("offers", [])
    out = {}
    for o in pool:
        if o.get("id") is None or o.get("status") != "open":
            continue
        if o.get("expires_tick") is not None and o["expires_tick"] <= tick:
            continue
        out.setdefault(o["id"], o)
    return out


# ------------------------------------------------------------------ pistas históricas (nunca ejecutables por sí solas)

def historical_leads(events: Iterable[dict], tick: int, live: Optional[dict] = None, refs: Optional[set] = None,
                     exclude_team: Optional[str] = None, limit: int = 40) -> list:
    """Pistas a partir de eventos `offer.listed`. `live` = `live_index(snap)`; si es None no se puede verificar nada y
    ninguna pista es ejecutable. Cada pista indica por qué NO se puede operar (o que SÍ está viva y la evalúa el agente)."""
    cancelled, listed = set(), {}
    for e in events or []:
        p = e.get("payload") or {}
        if e.get("type") == "offer.cancelled" and p.get("offer") is not None:
            cancelled.add(p["offer"])
        elif e.get("type") == "offer.listed" and isinstance(p.get("offer"), dict) and p["offer"].get("id") is not None:
            listed[p["offer"]["id"]] = (e, p["offer"])
    out = []
    for oid, (e, o) in listed.items():
        side, ref, price = single_card(o)
        maker = o.get("maker") or e.get("actor")
        if side is None or (refs is not None and ref not in refs) or maker == exclude_team:
            continue
        in_live = (live or {}).get(oid)
        if in_live is not None:
            validity, reason = "VIGENTE", "viva en el servidor: la evalúa el agente como candidata"
        elif live is None:
            validity, reason = "SIN VERIFICAR", "sin instantánea del servidor: no se puede comprobar"
        elif oid in cancelled:
            validity, reason = "RETIRADA", "el feed registra su cancelación"
        elif o.get("expires_tick") is not None and o["expires_tick"] <= tick:
            validity, reason = "CADUCADA", f"caducó en el tick {o['expires_tick']} (ahora {tick})"
        else:
            validity, reason = "NO VIGENTE", "ya no figura abierta en ningún tablón del servidor"
        if not is_team(maker):
            role = ALIAS
        elif validity == "VIGENTE":
            role = SELLER if side == "ask" else BUYER
        else:
            role = HOLDER if side == "ask" else SEEKER
        out.append({"kind": "PISTA_HISTORICA", "source": "feed", "tick": e.get("tick"), "evidence_event": e.get("id"),
                    "offer_id": oid, "ref": ref, "side": side, "price": price, "venue": o.get("venue"),
                    "counterparty": maker if is_team(maker) else None, "role": role, "valid_until": o.get("expires_tick"),
                    "validity": validity, "executable": validity == "VIGENTE" and role in (SELLER, BUYER),
                    "status": "EVALUADA POR EL AGENTE" if validity == "VIGENTE" else "SOLO REFERENCIA", "reason": reason})
    out.sort(key=lambda r: (not r["executable"], -(r["tick"] or 0)))
    return out[:limit]


def price_reference(oracle_rows: list, leads: list) -> list:
    """`Oracle.arbitrage()` compara dos precios de VENTA: es una referencia, no arbitraje. Se anota con la vigencia de la
    oferta que la sustenta (si una pista coincide en carta y precio); sin pista vigente queda como histórica."""
    by = {}
    for l in leads:
        if l["side"] == "ask":
            by.setdefault((l["ref"], l["price"]), []).append(l)
    out = []
    for r in oracle_rows or []:
        hit = by.get((r["ref"], r["team_ask"]), [])
        live = next((l for l in hit if l["validity"] == "VIGENTE"), None)
        out.append({"kind": "REFERENCIA_DE_PRECIOS", "ref": r["ref"], "team_ask": r["team_ask"],
                    "dealer": r["dealer"], "dealer_price": r["dealer_floor"], "difference": r["saving"],
                    "offer_id": live["offer_id"] if live else (hit[0]["offer_id"] if hit else None),
                    "validity": "VIGENTE" if live else (hit[0]["validity"] if hit else "SIN OFERTA IDENTIFICADA"),
                    "executable": bool(live),
                    "holders_historical": r.get("holders") or [],
                    "note": "diferencia entre dos precios de venta; no es arbitraje (no hay pata de venta)"})
    return out


def executable_arbitrage(snap: dict, fee_of, margin: float = 1.0) -> list:
    """ARBITRAJE solo con las DOS patas vivas y verificadas: un ask que podemos aceptar y una puja de OTRO equipo que paga
    más, neto de comisiones. No atómico (dos operaciones): se informa con ese riesgo."""
    live = live_index(snap)
    team = (snap.get("me") or {}).get("id")
    asks, bids = [], []
    for oid, o in live.items():
        if o.get("maker") == team or o.get("to") not in (None, team):
            continue
        side, ref, price = single_card(o)
        (asks if side == "ask" else bids if side == "bid" else []).append((oid, o, ref, price))
    out = []
    for aid, ao, ref, ap in asks:
        for bid_, bo, bref, bp in bids:
            if bref != ref or ao.get("maker") == bo.get("maker"):
                continue
            cost, net = ap + fee_of(ao.get("venue"), ap), bp - fee_of(bo.get("venue"), bp)
            if net - cost >= margin:
                out.append({"kind": "ARBITRAJE_DOS_PATAS_VIVAS", "ref": ref, "buy_offer": aid, "buy_cost": cost,
                            "sell_offer": bid_, "sell_net": net, "gross": net - cost, "executable": False,
                            "note": "dos operaciones no atómicas: riesgo de inventario; solo informe"})
    return sorted(out, key=lambda r: -r["gross"])


# ------------------------------------------------------------------ candidatas del coordinador → oportunidades

def role_of(c: dict, live: dict) -> str:
    t = c["type"]
    off = live.get(c.get("offer")) if c.get("offer") is not None else None
    maker = c.get("maker") or (off or {}).get("maker")
    if t.startswith("dealer_"):
        return f"{SELLER} (dealer {c.get('dealer')})" if t in ("dealer_accept", "dealer_counter", "dealer_sell_accept") \
            and c.get("offer") else f"vendedor sin oferta aún (dealer {c.get('dealer')})"
    if t in ("accept", "team_accept"):
        if not is_team(maker):
            return ALIAS
        return SELLER if c.get("receive") and not c.get("deliver") else BUYER if c.get("deliver") and not c.get("receive") \
            else f"{SELLER} (trueque)"
    if c.get("to") or c.get("team"):
        return "destinatario de nuestra propuesta (sin confirmar)"
    return "sin contraparte (publicación abierta)"


def from_candidate(c: dict, snap: dict, live: dict, chosen_keys: set, cost_fn) -> dict:
    tick = (snap.get("clock") or {}).get("tick")
    off = live.get(c.get("offer")) if c.get("offer") is not None else None
    blockers = list(c.get("blockers") or [])
    key = (c["type"], c.get("offer"), c.get("ref"), c.get("thread"), c.get("to"))
    status = "SELECCIONADA" if key in chosen_keys else ("BLOQUEADA" if blockers else "EJECUTABLE")
    fee = int(c.get("fee") or 0)
    cost = cost_fn(c)
    return {"kind": "EJECUTABLE" if not blockers else "BLOQUEADA", "source": c.get("module") or "coordinador",
            "tick": tick, "offer_id": c.get("offer"), "thread": c.get("thread"), "type": c["type"],
            "ref": (c.get("ref") or "").removeprefix("card:") or ",".join((c.get("receive") or {}).keys())
            or ",".join((c.get("deliver") or {}).keys()) or None,
            "venue": c.get("venue") or (off or {}).get("venue"),
            "counterparty": (c.get("maker") if is_team(c.get("maker")) else c.get("to") or c.get("dealer")) or None,
            "role": role_of(c, live), "valid_until": c.get("expires") or (off or {}).get("expires_tick"),
            "verified_live": bool(off) or c["type"].startswith("dealer_"),
            "price": c.get("price"), "fee": fee, "marginal_value": c.get("dv"), "net_surplus": c.get("du"),
            "capital_needed": (cost + fee) if cost else 0, "status": status,
            "reason": "; ".join(blockers) if blockers else (c.get("reason") or "")[:200],
            "score": c.get("score")}


def blockers_histogram(cands: list, top: int = 10) -> list:
    """Motivos de bloqueo agrupados por su causa principal (la primera de cada candidata), para ver qué frena más."""
    counts = {}
    for c in cands:
        for b in (c.get("blockers") or [])[:1]:
            k = re.sub(r"\d+(\.\d+)?", "N", b)[:110]
            counts[k] = counts.get(k, 0) + 1
    return [{"reason": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:top]]


# ------------------------------------------------------------------ revalidación antes de actuar

def revalidate_accept(c: dict, *, fresh_board: list, my_offers: list, me: dict, tick: int, team: str) -> list:
    """Contrasta una aceptación con el servidor JUSTO antes de enviarla: la oferta sigue abierta y vigente, mismo
    maker/precio/estructura, tenemos los activos que entregamos (y no están en otra oferta) y alcanza el efectivo.
    Lista de problemas; vacía = se puede enviar."""
    problems = []
    oid = c.get("offer")
    pool = {o.get("id"): o for o in list(fresh_board) + [o for o in my_offers if o.get("maker") != team]}
    o = pool.get(oid)
    if o is None:
        return [f"la oferta {oid} ya no figura abierta en el servidor"]
    if o.get("status") != "open":
        problems.append(f"la oferta {oid} está {o.get('status')}")
    if o.get("expires_tick") is not None and o["expires_tick"] <= tick:
        problems.append(f"la oferta {oid} caducó en el tick {o['expires_tick']}")
    if o.get("to") not in (None, team):
        problems.append("ya no está dirigida a nosotros")
    if c.get("maker") and o.get("maker") != c["maker"]:
        problems.append(f"el maker cambió: {c['maker']} → {o.get('maker')}")
    g, w = o.get("give") or {}, o.get("want") or {}
    live_price = int(g.get("cash") or 0) or int(w.get("cash") or 0)
    if c.get("price") is not None and live_price and live_price != int(c["price"]):
        problems.append(f"el precio cambió: {c['price']} → {live_price} P")
    want_ids = {a["id"] if isinstance(a, dict) else a for a in w.get("assets") or []}
    mine = {a["id"]: a for a in me.get("assets", []) if a.get("kind") == "card"}
    if c.get("assets") and not set(c["assets"]) <= set(mine):
        problems.append("ya no tenemos alguno de los activos que entregaríamos")
    if want_ids - set(c.get("assets") or []) and want_ids - set(mine):
        problems.append("la oferta pide activos que no tenemos")
    locked = set()
    for m in my_offers:
        if m.get("maker") == team and m.get("status") == "open":
            for a in (m.get("give") or {}).get("assets") or []:
                locked.add(a["id"] if isinstance(a, dict) else a)
    if set(c.get("assets") or []) & locked:
        problems.append("un activo que entregaríamos ya está comprometido en otra oferta abierta")
    need = max(0, -int(c.get("cash") or 0))
    if need and me.get("cash", 0) < need:
        problems.append(f"efectivo insuficiente: {me.get('cash')} P < {need} P")
    return problems


# ------------------------------------------------------------------ ficheros compartidos (escritura atómica)

def write_json(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    tmp.replace(path)


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def age_seconds(payload: Optional[dict]) -> Optional[float]:
    return None if not payload or "generated" not in payload else max(0.0, time.time() - float(payload["generated"]))
