"""Venta TÁCTICA de un duplicado a una contraparte real (p. ej. LAT-10 ~86 P). Lógica pura: propone candidatas; el
coordinador decide, valida (page_guard, exposición, oferta estructurada) y envía.

    VERIFY DUPLICATE → PROTECT PAGE COPY → IDENTIFY BUYER → ESTIMATE INTEREST → NEGOTIATE FROM ABOVE → AIM ~TARGET
    → NEVER BELOW ECONOMIC FLOOR → CLOSE WHEN GOOD → VERIFY SETTLEMENT → REALLOCATE CASH

- El objetivo (86 P) es TÁCTICO, no una constante: suelo económico = pérdida privada de la copia + margen; si el
  suelo supera el objetivo, manda el suelo.
- Nunca se vende la copia protegida de una página completa: con una sola copia, NO HAY VENTA.
- La oferta estructurada manda (Words persuade, structure binds): se valida maker, destinatario, venue, estado,
  caducidad, efectivo exacto y que pida exactamente UNA copia de la carta y nada más.
- Una contraparte dispuesta a pagar un buen precio ya vale más que una publicación incierta: se compara la utilidad
  esperada de aceptar con la de publicar.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Optional

import page_guard as pg
import trading as tr

STATES = ("DISCOVERED", "CONTACTED", "OFFERED", "COUNTERED", "NEAR_AGREEMENT", "ACCEPTED", "SETTLED", "BLOCKED")


@dataclass
class SaleConfig:
    ref: str
    target: int
    tolerance: int = 2           # dentro de esto alrededor del objetivo se CIERRA (no se pierde por 1-3 P)
    anchor_frac: float = 1.12    # apertura por encima del objetivo para dejar margen de concesión
    concession: float = 0.5      # cada contraoferta cede esta fracción de lo que queda hasta el objetivo
    p_accept_fill: float = 0.95  # aceptar una oferta en pie: casi segura (otro podría tomarla antes)
    margin: float = 2.0


def parse_targets(spec) -> list:
    out = []
    for s in spec or []:
        if "=" in str(s):
            ref, price = str(s).split("=", 1)
            out.append((ref.strip(), int(price)))
    return out


def directed_interest(snap: dict, ref: str) -> list:
    """Ofertas estructuradas ABIERTAS de otros equipos que piden `ref` (dirigidas a nosotros o públicas)."""
    team, tick = snap["me"]["id"], snap["clock"]["tick"]
    pool = list((snap.get("offers") or {}).get("offers", []))
    for b in (snap.get("boards") or {}).values():
        pool += (b or {}).get("offers", [])
    seen, out = set(), []
    for o in pool:
        if o.get("id") in seen or o.get("maker") in (team, None) or o.get("status") != "open":
            continue
        seen.add(o.get("id"))
        if o.get("to") not in (None, team) or (o.get("expires_tick") is not None and o["expires_tick"] < tick):
            continue
        w, g = o.get("want") or {}, o.get("give") or {}
        wanted = [t[5:] for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]
                  if isinstance(t, str) and t.startswith("card:")]
        if ref not in wanted:
            continue
        kind = "cash" if g.get("cash") and not g.get("assets") else "swap" if g.get("assets") else "other"
        out.append({"offer": o, "id": o["id"], "team": o.get("maker"), "kind": kind, "directed": o.get("to") == team,
                    "price": int(g.get("cash") or 0), "venue": o.get("venue"), "expires": o.get("expires_tick"),
                    "gives": [a.get("ref") for a in g.get("assets") or [] if isinstance(a, dict)]})
    return sorted(out, key=lambda x: (x["kind"] != "cash", -x["price"], not x["directed"]))


def validate_sale_offer(o: dict, *, team: str, ref: str, buyer: str, tick: int, venues: dict) -> list:
    """Problemas de la estructura (lista vacía = aceptable): solo efectivo a cambio de EXACTAMENTE una copia de `ref`."""
    problems = []
    g, w = o.get("give") or {}, o.get("want") or {}
    if o.get("maker") != buyer:
        problems.append(f"la hace {o.get('maker')!r}, no {buyer!r}")
    if o.get("to") not in (None, team):
        problems.append(f"dirigida a {o.get('to')!r}")
    if o.get("status") != "open":
        problems.append(f"no está abierta ({o.get('status')!r})")
    if o.get("expires_tick") is not None and o["expires_tick"] < tick:
        problems.append(f"caducada en el tick {o['expires_tick']}")
    if o.get("venue") not in venues:
        problems.append(f"venue {o.get('venue')!r} desconocido o cerrado")
    if not isinstance(g.get("cash"), int) or g.get("cash", 0) < 1:
        problems.append(f"efectivo no válido: {g.get('cash')!r}")
    if g.get("assets") or g.get("types"):
        problems.append("además del efectivo ofrece cartas (no es una venta simple)")
    wanted = [t for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]]
    if wanted != [f"card:{ref}"] or w.get("assets") or w.get("cash"):
        problems.append(f"no pide exactamente una copia de {ref} (pide {wanted}, activos {w.get('assets')}, "
                        f"efectivo {w.get('cash')})")
    return problems


def our_history(snap: dict, ledger_actions: list, ref: str, buyer: str) -> list:
    """Nuestros precios pedidos a `buyer` por `ref` (ofertas dirigidas abiertas + registro), en orden."""
    prices = []
    for a in ledger_actions or []:
        if a.get("type") == "list" and a.get("ref") == ref and a.get("to") == buyer and a.get("status") in \
                ("submitted", "settled", "released") and a.get("price"):
            prices.append((a.get("tick") or 0, int(a["price"])))
    return [p for _, p in sorted(prices)]


def next_ask(cfg: SaleConfig, floor: int, their: Optional[int], ours: list) -> int:
    """Ancla por encima del objetivo y concesiones DECRECIENTES hacia él; nunca por debajo de max(objetivo, suelo),
    nunca repetir ni subir respecto a nuestra última petición, y nunca pedir menos de lo que ya nos ofrecen + 1."""
    goal = max(cfg.target, floor)
    if not ours:
        p = max(math.ceil(cfg.target * cfg.anchor_frac), goal + 2)
    else:
        last = ours[-1]
        p = last - max(1, math.ceil(cfg.concession * (last - goal)))
        if their is not None and last - their <= cfg.tolerance + 4:
            # muy cerca: punto medio entre nuestra última y el objetivo, por encima del objetivo y de su oferta
            p = max(goal + 1, their + 1, math.ceil((last + goal) / 2))
        p = min(p, last - 1) if last > goal else last
    if their is not None:
        p = max(p, their + 1)
    return int(max(p, goal))


def assess(snap: dict, cfg: SaleConfig, val: tr.Valuation, committed_ids, ledger_actions: list = (),
           intel=None, market: Optional[dict] = None, listing_p_fill: float = 0.2, venues: Optional[dict] = None) -> dict:
    """Informe + candidatas (accept / list dirigida) para vender UNA copia excedente de cfg.ref."""
    me, tick, team = snap["me"], snap["clock"]["tick"], snap["me"]["id"]
    venues = venues or {}
    counts = tr.counts_of(me["assets"])
    copies = sorted(a["id"] for a in me["assets"] if a.get("kind") == "card" and a.get("ref") == cfg.ref)
    committed = set(committed_ids)
    prot = pg.protected_assets(counts, snap["catalog"], me["assets"], committed)
    protected = [i for i in copies if i in prot]
    tradeable = pg.tradeable_assets(cfg.ref, counts, snap["catalog"], me["assets"], committed)
    pages = sorted(pg.completed_pages(counts, snap["catalog"]))
    rep = {"ref": cfg.ref, "target": cfg.target, "copies": len(copies), "asset_ids": copies, "protected": protected,
           "tradeable": tradeable, "locked": sorted(set(copies) & committed), "completed_pages": pages,
           "state": "DISCOVERED", "candidates": [], "buyers": [], "recommendation": None, "reasons": []}
    interest = directed_interest(snap, cfg.ref)
    rep["interest"] = [{k: v for k, v in x.items() if k != "offer"} for x in interest]
    if intel is not None:
        try:
            rep["buyers"] = intel.best_buyers(cfg.ref)
        except Exception as e:  # la inteligencia es opcional
            rep["buyers"] = [{"error": type(e).__name__}]
    loss = None
    if copies:
        dv, _ = val.delta(counts, Counter(), Counter({cfg.ref: 1}))
        loss = round(-dv, 2)
    rep["marginal_loss"] = loss
    rep["market"] = market or {}
    floor = math.ceil((loss or 0) + cfg.margin)
    rep["economic_floor"] = floor
    page_after = None
    if copies:
        after = Counter(counts)
        after[cfg.ref] -= 1
        page_after = sorted(pg.completed_pages(+after, snap["catalog"]))
    rep["pages_after_sale"] = page_after
    if not tradeable:
        rep["state"] = "BLOCKED"
        why = ("no tenemos la carta" if not copies else
               f"solo {len(copies)} copia: es la copia PROTEGIDA de una página completa ({pages}); NO SE VENDE"
               if len(copies) == 1 and protected else
               "las copias excedentes ya están comprometidas en otra oferta" if set(copies) & committed else
               "ninguna copia excedente libre")
        rep["reasons"].append(why)
        rep["recommendation"] = "NO VENDER"
        cash_bids = [x for x in interest if x["kind"] == "cash"]
        if cash_bids and market and market.get("best_ask") is not None:
            ask, fee_buy = market["best_ask"], market.get("ask_fee", 0)
            gross = cash_bids[0]["price"] - (ask + fee_buy)
            rep["acquire_path"] = (f"existe un ask de {cfg.ref} a {ask} P (+{fee_buy} comisión): comprar una copia y "
                                   f"vender la excedente a {cash_bids[0]['team']} por {cash_bids[0]['price']} P dejaría "
                                   f"{gross} P brutos ANTES de valorar la copia; dos patas NO atómicas (se informa, no "
                                   f"se ejecuta)")
        return rep
    asset = tradeable[-1]
    rep["selected_asset"] = asset
    cash = [x for x in interest if x["kind"] == "cash"]
    if not cash:
        rep["reasons"].append("no hay ninguna oferta estructurada en efectivo por esta carta")
        rep["recommendation"] = "ESPERAR / PUBLICAR"
        return rep
    best = cash[0]
    buyer, their, vid = best["team"], best["price"], best["venue"]
    rep.update(buyer=buyer, their_latest=their, venue=vid, offer=best["id"], expires=best["expires"])
    v = venues.get(vid)
    fee = v.fee(their, 1) if v else 0  # al ACEPTAR pagamos nosotros la comisión del venue
    rep["fee_if_accept"] = fee
    problems = validate_sale_offer(best["offer"], team=team, ref=cfg.ref, buyer=buyer, tick=tick, venues=venues)
    ours = our_history(snap, ledger_actions, cfg.ref, buyer)
    rep["our_asks"] = ours
    open_ours = [o for o in (snap.get("offers") or {}).get("offers", []) if o.get("maker") == team
                 and o.get("to") == buyer and o.get("status") == "open"
                 and any((a.get("ref") if isinstance(a, dict) else None) == cfg.ref for a in (o.get("give") or {}).get("assets") or [])]
    rep["state"] = ("NEAR_AGREEMENT" if ours and ours[-1] - their <= cfg.tolerance + 2 else
                    "COUNTERED" if ours and len(ours) >= 1 and their > 0 else
                    "OFFERED" if open_ours else "DISCOVERED")
    net = their - fee
    eu_accept = round(cfg.p_accept_fill * (net - (loss or 0)), 2)
    anchor = next_ask(cfg, floor, None, [])
    eu_list = round(listing_p_fill * (anchor - (loss or 0)), 2)
    rep.update(eu_accept=eu_accept, eu_public_listing=eu_list, listing_price_for_eu=anchor)
    close_ok = net >= floor and their >= cfg.target - cfg.tolerance
    if problems:
        rep["reasons"] += problems
        rep["recommendation"] = "NO ACEPTAR: estructura no válida"
    elif close_ok or (net >= floor and eu_accept >= eu_list and ours and their >= ours[-1] - cfg.tolerance):
        urgency = 5 * 10 ** 4 if best["expires"] is not None and best["expires"] - tick <= 2 else 0
        rep["recommendation"] = f"ACCEPT {their} P"
        rep["reasons"].append(f"duplicado (página {'intacta' if page_after == pages else 'ROTA'}), {net} P netos ≥ suelo "
                              f"{floor} P, cerca del objetivo {cfg.target} P; EU aceptar {eu_accept} ≥ EU publicar {eu_list}")
        rep["candidates"].append({
            "type": "accept", "kind": "vender (venta táctica)", "module": "venta táctica", "venue": vid,
            "offer": best["id"], "maker": buyer, "assets": [asset], "deliver": {cfg.ref: 1}, "receive": {},
            "price": their, "fee": fee, "cash": net, "dv": -(loss or 0), "du": round(net - (loss or 0), 2),
            "expected_du": eu_accept, "p_fill": cfg.p_accept_fill, "score": 2.8 * 10 ** 5 + urgency,
            "blockers": [], "reason": rep["reasons"][-1], "notes": [f"copia vendida #{asset}; protegida {protected}"],
            "tactical_sale": cfg.ref})
    else:
        p = next_ask(cfg, floor, their if their > 0 else None, ours)
        rep["recommendation"] = f"COUNTER {p} P"
        rep["reasons"].append(f"su última {their} P < objetivo {cfg.target} P (suelo {floor} P): contraoferta dirigida "
                              f"a {buyer}, concesión decreciente desde {ours[-1] if ours else 'apertura'}")
        if open_ours:
            rep["reasons"].append(f"ya tenemos una petición abierta para {buyer}: se espera su respuesta")
        else:
            rep["candidates"].append({
                "type": "list", "kind": "contraoferta dirigida (venta táctica)", "module": "venta táctica", "venue": vid,
                "ref": cfg.ref, "asset": asset, "price": p, "to": buyer, "fee": 0, "cash": p, "dv": -(loss or 0),
                "du": round(p - (loss or 0), 2), "expected_du": round(0.5 * (p - (loss or 0)), 2),
                "score": 2 * 10 ** 5, "blockers": [], "expires_in": 20, "tactical_sale": cfg.ref,
                "reason": rep["reasons"][-1], "notes": [f"copia #{asset}; protegida {protected}"]})
    return rep


def report_block(rep: dict) -> str:
    m = rep.get("market") or {}
    rows = [f"=== {rep['ref']} NEGOTIATION ===", f"Copies: {rep['copies']} {rep['asset_ids']}",
            f"Protected: {['asset #' + str(i) for i in rep['protected']] or '—'}",
            f"Tradeable: {['asset #' + str(i) for i in rep['tradeable']] or '—'}"
            + (f" (en otra oferta: {rep['locked']})" if rep.get("locked") else ""),
            f"Completed pages: {rep['completed_pages']} → after selling one copy: {rep.get('pages_after_sale')}",
            f"Interest: {[(x['team'], x['kind'], x['price'], 'dirigida' if x['directed'] else 'pública', '#' + str(x['id'])) for x in rep.get('interest', [])] or '—'}",
            f"Buyers (intel): {[(b.get('team'), b.get('interest'), b.get('reservation')) for b in rep.get('buyers', [])] or '—'}",
            f"Market estimate: {m.get('value')} P ({m.get('confidence')}) · best bid {m.get('best_bid')} · best ask {m.get('best_ask')}",
            f"Our marginal loss: {rep.get('marginal_loss')} P · Economic floor: {rep.get('economic_floor')} P · "
            f"Target: ~{rep['target']} P",
            f"Counterparty: {rep.get('buyer', '—')} · their latest: {rep.get('their_latest', '—')} P · our asks: "
            f"{rep.get('our_asks', [])} · state: {rep['state']}",
            f"EU accept: {rep.get('eu_accept', '—')} · EU public listing: {rep.get('eu_public_listing', '—')}",
            f"Recommended next action: {rep['recommendation']}",
            "Reason: " + ("; ".join(rep["reasons"]) or "—")]
    if rep.get("acquire_path"):
        rows.append("Acquire path: " + rep["acquire_path"])
    return "\n".join(rows)
