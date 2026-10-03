"""Rendimiento REALIZADO frente a ABIERTO y ESTIMADO, a partir del registro del coordinador (sin red).

realized_delta_u de una operación liquidada = efectivo atribuible + cambio de valor de colección verificado:
- venta propia (somos maker):   + precio cobrado (la comisión la paga quien acepta) − valor de la copia entregada
- puja propia llenada:          − precio pagado + valor de la copia recibida
- trueque propio:               0 P + Δvalor (recibida − entregada)
- aceptación nuestra:           efectivo neto CON nuestra comisión + Δvalor
- vendedor:                     − precio liquidado (el del servidor) + valor de la copia
El Δvalor es el del modelo verificado contra collection_value en el momento de decidir. Una oferta abierta NO es un
beneficio: va en ABIERTO; el ΔU esperado de lo abierto va en ESTIMADO.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

DEALER = ("dealer_accept", "dealer_counter")
POSTS = ("list", "bid", "swap_list")


def _dv(a: dict):
    if a.get("dv") is not None:
        return float(a["dv"])
    if a.get("du") is None:
        return None
    t, price = a["type"], a.get("price") or 0
    if t == "list":
        return float(a["du"]) - price
    if t == "bid":
        return float(a["du"]) + price
    if t == "swap_list":
        return float(a["du"])
    if t in DEALER:
        return float(a["du"]) + price  # du = valor − nuestro precio en la decisión
    if t in ("accept", "team_accept") and a.get("cash") is not None:
        return float(a["du"]) - a["cash"]
    return None


def _cash(a: dict):
    t, price = a["type"], a.get("price") or 0
    if t == "list":
        return price
    if t == "bid":
        return -price
    if t == "swap_list":
        return 0
    if t in DEALER:
        return -(a.get("paid") if a.get("paid") is not None else price)
    if a.get("cash") is not None:
        return a["cash"]  # aceptación nuestra: neto con la comisión que pagamos
    return None


def realized(actions: list, ladder: dict | None = None, open_offers: int = 0) -> dict:
    rows, ttf = [], []
    fills = defaultdict(lambda: [0, 0])  # (venue, tipo) -> [liquidadas, cerradas sin llenar]
    for a in actions:
        if a["type"] in POSTS and a["status"] in ("settled", "released"):
            fills[(a.get("venue") or "rastro", a["type"])][0 if a["status"] == "settled" else 1] += 1
        if a["status"] != "settled" or a["type"] not in POSTS + DEALER + ("accept", "team_accept"):
            continue
        cash, dv = _cash(a), _dv(a)
        du = None if cash is None or dv is None else round(cash + dv, 2)
        rows.append({"type": a["type"], "ref": a.get("ref") or a.get("item"), "tick": a.get("tick"),
                     "settled_tick": a.get("settled_tick"), "cash": cash, "dv": dv, "realized_du": du})
        if a.get("settled_tick") is not None and a.get("tick") is not None:
            ttf.append(a["settled_tick"] - a["tick"])
    opened = sum(1 for a in actions if a["type"] == "dealer_open" and a["status"] == "settled")
    closed = sum(1 for a in actions if a["type"] in DEALER and a["status"] == "settled")
    abandoned = sum(1 for a in actions if a["type"] == "dealer_close" and a["status"] == "settled")
    open_est = round(sum(float(a.get("expected_du") or 0) for a in actions
                         if a["type"] in POSTS and a["status"] == "submitted"), 2)
    known = [r["realized_du"] for r in rows if r["realized_du"] is not None]
    return {"realized_surplus": round(sum(known), 2), "settlement_count": len(rows),
            "unattributed": len(rows) - len(known), "median_ticks_to_fill": statistics.median(ttf) if ttf else None,
            "fill_rate": {f"{v}/{t}": f"{s}/{s + r}" for (v, t), (s, r) in sorted(fills.items())},
            "dealer_conversations": opened, "dealer_deals": closed, "dealer_abandoned": abandoned,
            "dealer_close_rate": round(closed / opened, 2) if opened else None,
            "dealer_qualifying": {d: len(x) for d, x in (ladder or {}).items()},
            "open_offers": open_offers, "open_expected_du": open_est, "rows": rows}


def line(p: dict) -> str:
    return (f"RENDIMIENTO realizado {p['realized_surplus']} P en {p['settlement_count']} liquidaciones"
            f"{' (' + str(p['unattributed']) + ' sin atribuir)' if p['unattributed'] else ''} · mediana hasta llenarse "
            f"{p['median_ticks_to_fill']} ticks · llenado {p['fill_rate'] or '—'} · vendedores: "
            f"{p['dealer_deals']} tratos / {p['dealer_conversations']} conversaciones, {p['dealer_abandoned']} abandonadas,"
            f" escalera {p['dealer_qualifying'] or '—'} · ABIERTO {p['open_offers']} ofertas (no es beneficio) · "
            f"ESTIMADO {p['open_expected_du']} P esperado de lo abierto")
