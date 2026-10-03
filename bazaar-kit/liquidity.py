"""CAMPAÑA DE LIQUIDEZ: reconciliación de inventario y capital, viabilidad de la meta de caja al cierre y análisis de
ofertas EXCEPCIONALES por cartas protegidas (nunca se ejecutan solas). Lógica pura; el coordinador aporta el estado.

Definiciones (sin confundir magnitudes):
  cartas físicas   = instancias de carta que tenemos      cartas distintas = referencias distintas (huecos del álbum = slots − llenos)
  excedente        = copias por encima de la que conserva el álbum/página (vender una NO reduce el contador de distintas)
  efectivo libre   = caja − pujas abiertas − exposición con vendedores − aceptaciones pendientes (lo calcula el coordinador)
  ingreso confirmado = solo lo liquidado; ventas publicadas e incentivos prometidos son PENDIENTES, nunca efectivo
Escenarios al cierre, sin probabilidades:
  confirmado   = efectivo libre ahora
  conservador  = + ventas ejecutables YA (pujas vigentes válidas ≥ mínimo) de activos autorizados
  optimista    = + todos los activos autorizados vendidos a su objetivo (máximo observado, no previsión)
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

import page_guard as pg


def inventory(me: dict, catalog: dict, committed: set) -> dict:
    cards = [a for a in me.get("assets") or [] if a.get("kind") == "card"]
    counts = Counter(a["ref"] for a in cards)
    sc = me.get("score") or {}
    surplus = {r: n - 1 for r, n in counts.items() if n > 1}
    free_surplus = {}
    for r in surplus:
        ids = pg.tradeable_assets(r, counts, catalog, cards, committed)
        if ids:
            free_surplus[r] = ids
    return {"physical": len(cards), "distinct": len(counts), "surplus_copies": sum(surplus.values()), "surplus": surplus,
            "free_surplus": free_surplus, "album": [sc.get("album_filled"), sc.get("album_slots")],
            "album_gaps": (sc.get("album_slots") or 0) - (sc.get("album_filled") or 0) if sc.get("album_slots") else None,
            "pages_complete": sorted(pg.completed_pages(counts, catalog)),
            "committed_cards": sorted(i for i in committed if any(a["id"] == i for a in cards))}


def scenarios(free_cash: int, fast_rows: list) -> dict:
    """Escenarios con las ventas AUTORIZADAS de la campaña (no se incluyen cartas protegidas ni incentivos)."""
    exec_now, at_objective, items = 0, 0, []
    for r in fast_rows or []:
        if not r.get("authorized"):
            continue
        p = r["prices"]
        n = len(r.get("free") or []) + len(r.get("locked") or {})
        q = p.get("quick_close") if p.get("quick_close") is not None and p["quick_close"] >= p["minimum"] else None
        exec_now += (q or 0) * n
        at_objective += max(p["objective"], p["minimum"]) * n
        items.append({"ref": r["ref"], "copies": n, "minimum": p["minimum"], "objective": p["objective"], "executable_now": q})
    return {"confirmed": free_cash, "conservative": free_cash + exec_now, "optimistic": free_cash + at_objective,
            "items": items}


def viability(sc: dict, target_min: int = 150, target_stretch: int = 200) -> dict:
    out = {}
    for name, t in (("min", target_min), ("stretch", target_stretch)):
        out[name] = {"target": t, "deficit_now": max(0, t - sc["confirmed"]),
                     "gap_conservative": max(0, t - sc["conservative"]), "gap_optimistic": max(0, t - sc["optimistic"])}
    out["reachable_min"] = sc["optimistic"] >= target_min
    out["note"] = ("incluso vendiendo todo lo autorizado a su objetivo faltarían "
                   f"{out['min']['gap_optimistic']} P para {target_min} P: no se liquidan páginas ni se inventan compradores"
                   if not out["reachable_min"] else "alcanzable solo si se venden los activos autorizados")
    return out


def lot_loss(val, counts: Counter, refs: list) -> float:
    """Pérdida de valor privado al entregar un LOTE completo: un solo delta (el bono de página se pierde UNA vez)."""
    return round(-val.delta(counts, Counter(), Counter(refs))[0], 2)


def exceptional_offers(s: dict, val, counts: Counter, venues: dict, margin: float, denied=frozenset()) -> list:
    """Pujas ejecutables por cartas cuya ÚNICA copia protege una página completa. Se calcula el valor antes y después
    (lote por página: todas las cartas de esa página con puja, en un solo delta), el ingreso neto y la diferencia. Solo se
    proponen al operador si el neto supera la pérdida total + margen. Nunca se ejecutan: requieren autorización expresa.
    No se supone recompra barata posterior."""
    me, team = s["me"], s["me"]["id"]
    cards = [a for a in me.get("assets") or [] if a.get("kind") == "card"]
    prot = pg.protected_assets(counts, s["catalog"], cards, ())
    prot_refs = {a["ref"] for a in cards if a["id"] in prot and counts[a["ref"]] == 1}
    pool = list(s["offers"].get("offers", [])) + [o for b in (s.get("boards") or {}).values() for o in (b or {}).get("offers", [])]
    best, seen = {}, set()
    for o in pool:
        if o.get("id") in seen or o.get("maker") in (team, None) or o.get("status") != "open" or o.get("to") not in (None, team):
            continue
        seen.add(o["id"])
        g, w = o.get("give") or {}, o.get("want") or {}
        wt = [t[5:] for t in w.get("types") or [] if str(t).startswith("card:")] + list(w.get("cards") or [])
        if not g.get("cash") or g.get("assets") or len(wt) != 1 or wt[0] not in prot_refs or o.get("maker") in denied:
            continue
        v = venues.get(o.get("venue"))
        net = g["cash"] - (v.fee(g["cash"], 1) if v else 0)
        if wt[0] not in best or net > best[wt[0]]["net"]:
            best[wt[0]] = {"ref": wt[0], "offer": o["id"], "maker": o["maker"], "venue": o.get("venue"), "price": g["cash"], "net": net}
    pages = {}
    for ref, b in best.items():
        page = next((sid["id"] for sid in s["catalog"].get("sets", []) for c in sid.get("cards", []) if c["id"] == ref), "?")
        pages.setdefault(page, []).append(b)
    out = []
    before = round(val.total(counts), 2) if hasattr(val, "total") else None
    for page, bids in pages.items():
        refs = [b["ref"] for b in bids]
        loss = lot_loss(val, counts, refs)
        net = sum(b["net"] for b in bids)
        out.append({"page": page, "refs": refs, "bids": bids, "net": net, "loss_lot": loss,
                    "value_before": before, "value_after": round(before - loss, 2) if before is not None else None,
                    "surplus": round(net - loss, 2), "justified": net >= loss + margin,
                    "status": "PROPUESTA AL OPERADOR (requiere autorización expresa)" if net >= loss + margin else "no compensa",
                    "note": "lote valorado en un solo delta (bono de página una vez); sin suponer recompra"})
    return out


def lines(rep: dict) -> list:
    inv, cap, sc, vi = rep["inventory"], rep["capital"], rep["scenarios"], rep["viability"]
    out = [f"LIQUIDEZ · tick {rep['tick']} · físicas {inv['physical']} · distintas {inv['distinct']} · álbum {inv['album'][0]}/"
           f"{inv['album'][1]} ({inv['album_gaps']} huecos) · excedentes {inv['surplus']} (libres {sorted(inv['free_surplus'])}) · "
           f"páginas {inv['pages_complete']}",
           f"  CAJA {cap['cash']} P · libre {cap['free']} P · comprometido {cap['committed']} P · confirmado histórico "
           f"{cap['income_confirmed']} P · ventas publicadas pendientes {cap['published_sales']} P · incentivo t05 pendiente "
           f"{cap['incentive_pending']} P (no es efectivo)",
           f"  ESCENARIOS al cierre: confirmado {sc['confirmed']} · conservador {sc['conservative']} · optimista {sc['optimistic']} P"
           f" · déficit 150: {vi['min']['deficit_now']} (opt. {vi['min']['gap_optimistic']}) · déficit 200: "
           f"{vi['stretch']['deficit_now']} (opt. {vi['stretch']['gap_optimistic']}) · {vi['note']}"]
    for x in sc["items"]:
        out.append(f"    vendible {x['ref']} ×{x['copies']}: mínimo {x['minimum']} · objetivo {x['objective']} · ejecutable ya "
                   f"{x['executable_now'] if x['executable_now'] is not None else '—'}")
    for e in rep.get("exceptional") or []:
        out.append(f"  EXCEPCIONAL {e['page']} {e['refs']}: neto {e['net']} P frente a pérdida del lote {e['loss_lot']} P → "
                   f"{e['status']}")
    for a in rep.get("resale_watch") or []:
        out.append(f"  EN OBSERVACIÓN (reventa, no se compra): {a}")
    return out
