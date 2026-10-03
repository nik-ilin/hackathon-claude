"""CAMPAÑA DE VENTAS RÁPIDAS: convertir copias prescindibles en efectivo sin tocar páginas completas ni comprar nada.

Por copia autorizada (lista cerrada de cartas) y por asset_id:
  mínimo    = pérdida marginal COMPLETA (valoración, incluye bono de página) + costes a nuestro cargo + margen (≥ 2 P)
  cierre    = mejor contraprestación EJECUTABLE neta ahora (puja abierta válida, neta de la comisión que pagaríamos)
  objetivo  = precio defendible: cierres comparables recientes, sin superar al competidor vivo más barato
Rutas, en orden: pujas ejecutables → comprador identificado con puja reciente → una propuesta concreta al objetivo.
Máximo UNA oferta de salida por asset_id; presupuesto de ticks desde la primera propuesta; ≤ N reprecios (contraofertas);
tras agotarlo: cerrar si hay oferta válida o liberar la copia (con enfriamiento). Nada de lo que prometa un tercero
(comisión de Team 5, interés histórico, precio de venta de un dealer) entra en el mínimo ni se toma como compra actual.
Lógica pura: el coordinador valida (page_guard, exposición, oferta viva) y envía.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import page_guard as pg
import team_sale as ts
import trading as tr

MODULE = "ventas rápidas"


@dataclass
class Config:
    refs: tuple = ()
    ticks: int = 6              # presupuesto desde la primera propuesta
    counters: int = 2           # reprecios / contraofertas por activo
    margin: float = 2.0         # margen económico mínimo (el del operador manda si es mayor)
    stale: int = 3              # ticks sin interés antes de revisar precio
    cooldown: int = 6           # ticks sin volver a proponer tras liberar
    comparable_age: int = 300   # antigüedad máxima de un cierre comparable
    denied: frozenset = field(default_factory=frozenset)
    allow_last: frozenset = field(default_factory=frozenset)   # --allow-last-copy: única forma de vender la última copia


def parse_refs(spec) -> tuple:
    return tuple(x.strip().upper() for x in str(spec or "").split(",") if x.strip())


def min_net(loss: float, margin: float) -> int:
    return int(math.ceil(loss + margin - 1e-9))


def history(led: dict, asset) -> list:
    return [a for a in led.get("actions", []) if a.get("module") == MODULE and (a.get("asset") == asset or asset in (a.get("assets") or []))]


def stage(led: dict, asset, tick: int, cfg: Config) -> dict:
    """Estado derivado del registro (sobrevive a reinicios): primera propuesta vigente, reprecios y enfriamiento."""
    h = history(led, asset)
    # Solo una LIBERACIÓN real (prefijo «LIBERA») reinicia el presupuesto y abre el enfriamiento; los cancelaciones para
    # repreciar o aceptar una puja no cuentan como contraofertas nuevas ni dan una segunda ventana.
    last_cancel = max([a["tick"] for a in h if a["type"] == "cancel" and str(a.get("reason") or "").startswith("LIBERA")
                       and a.get("status") in ("submitted", "settled", "released")], default=None)
    lists = [a for a in h if a["type"] == "list" and a.get("status") in ("submitted", "settled", "released", "intent")
             and (last_cancel is None or a["tick"] > last_cancel)]
    first = min((a["tick"] for a in lists), default=None)
    asks = [a.get("price") for a in sorted(lists, key=lambda a: a["tick"])]
    last_ask = max((a["tick"] for a in lists), default=None)
    cooling = (last_cancel + cfg.cooldown) if last_cancel is not None else None
    if first is not None and tick - first >= cfg.ticks + cfg.cooldown:
        first, asks, last_ask = None, [], None          # ventana agotada y enfriada: nueva ventana con el precio revisado
    elif first is not None and tick - first >= cfg.ticks:  # la oferta caducó sin cancelación: equivale a liberar
        cooling = max(cooling or 0, first + cfg.ticks + cfg.cooldown)
    return {"first_tick": first, "asks": asks, "last_ask_tick": last_ask, "cooling_until": cooling,
            "age": (tick - first) if first is not None else 0}


# ------------------------------------------------------------------ inventario revalidado y evidencia

def revalidate(s: dict, committed: set, cfg: Config, val, counts) -> dict:
    """Por carta de la lista cerrada: copias, protegidas, libres, comprometidas (y dónde) y pérdida marginal."""
    me, cat = s["me"], s["catalog"]
    own = [o for o in s["offers"].get("offers", []) if o.get("maker") == me["id"] and o.get("status") == "open"]
    threads = [t for t in s["threads"]["open"] if t.get("kind") == "persona"]
    out = {}
    for ref in cfg.refs:
        copies = sorted(a["id"] for a in me["assets"] if a.get("kind") == "card" and a.get("ref") == ref)
        surplus = pg.tradeable_assets(ref, counts, cat, me["assets"], ())          # sin contar compromisos
        in_threads = {x for t in threads for x in ((t.get("topic") or {}).get("sell") or {}).get("assets", [])}
        free = [i for i in surplus if i not in committed and i not in in_threads]
        locked = {}
        for i in surplus:
            if i in committed or i in in_threads:
                o = next((o for o in own if any(isinstance(x, dict) and x.get("id") == i for x in (o.get("give") or {}).get("assets") or [])), None)
                t = next((t for t in threads if i in ((t.get("topic") or {}).get("sell") or {}).get("assets", [])), None)
                if o is not None and o.get("venue") is None and t is not None:
                    o = None   # la oferta sin venue es la contraoferta del hilo con un vendedor: se rige por el hilo
                locked[i] = {"offer": o.get("id") if o else None, "venue": o.get("venue") if o else None,
                             "thread": t.get("id") if t else None, "dealer": t.get("with") if t else None}
        loss = None
        if copies:
            loss = round(-val.delta(counts, Counter(), Counter({ref: 1}))[0], 2)
        why = []
        if len(copies) == 1 and surplus and ref not in cfg.allow_last:
            surplus, free, locked = [], [], {}     # regla permanente del operador: la última copia solo sale si figura en --allow-last-copy
            why.append("es la ÚLTIMA copia y la carta no figura en --allow-last-copy: NO SE VENDE")
        if not copies:
            why.append("no tenemos la carta")
        elif not surplus and not why:
            why.append(f"{len(copies)} copia(s): protegida(s) por una página completa o sin excedente; NO SE VENDE")
        out[ref] = {"ref": ref, "copies": copies, "surplus": surplus, "free": free, "locked": locked, "loss": loss,
                    "authorized": bool(surplus), "reasons": why,
                    "pages_after": sorted(pg.completed_pages(Counter({r: n - (r == ref) for r, n in counts.items()}), cat))
                    if copies else None}
    return out


def comparable_closes(events: list, ref: str, tick: int, max_age: int) -> list:
    seen, out = set(), []
    for e in events or []:
        if e.get("type") != "settlement":
            continue
        p = e.get("payload") or {}
        sid = p.get("settlement") if p.get("settlement") is not None else p.get("id")
        items = p.get("items") or []
        if sid in seen or len(items) != 1 or items[0].get("ref") != ref or not p.get("price"):
            continue
        t = p.get("tick", e.get("tick"))
        if t is None or tick - t > max_age:
            continue
        seen.add(sid)
        out.append({"settlement": sid, "tick": t, "price": p["price"], "venue": p.get("venue")})
    return out


def evidence(s: dict, ref: str, cfg: Config, venues: dict, intel=None) -> dict:
    """Ofertas ejecutables de compra (estructura validada), cierres comparables, competidores vivos y compradores con
    evidencia RECIENTE. Una puja dirigida a otro equipo no es ejecutable para nosotros."""
    tick, team = s["clock"]["tick"], s["me"]["id"]
    bids, rejected = [], []
    for x in ts.directed_interest(s, ref):
        if x["kind"] != "cash":
            continue
        problems = ts.validate_sale_offer(x["offer"], team=team, ref=ref, buyer=x["team"], tick=tick, venues=venues)
        if x["team"] in cfg.denied:
            problems.append(f"{x['team']} está en --deny-teams")
        if x["expires"] is not None and x["expires"] - tick < 1:
            problems.append("caduca en este tick")
        v = venues.get(x["venue"])
        fee = v.fee(x["price"], 1) if v else 0
        row = {"offer": x["id"], "team": x["team"], "venue": x["venue"], "price": x["price"], "fee": fee,
               "net": x["price"] - fee, "expires": x["expires"], "directed": x["directed"]}
        (rejected if problems else bids).append(dict(row, problems=problems) if problems else row)
    bids.sort(key=lambda b: (-b["net"], b["offer"]))
    pool = list(s["offers"].get("offers", [])) + [o for b in (s.get("boards") or {}).values() for o in (b or {}).get("offers", [])]
    asks, seen = [], set()
    for o in pool:
        if o.get("id") in seen or o.get("maker") in (team, None) or o.get("status") != "open":
            continue
        seen.add(o["id"])
        g, w = o.get("give") or {}, o.get("want") or {}
        if w.get("cash") and not w.get("assets") and not g.get("cash") and any(
                isinstance(a, dict) and a.get("ref") == ref for a in g.get("assets") or []):
            asks.append({"offer": o["id"], "team": o["maker"], "price": int(w["cash"]), "venue": o.get("venue")})
    closes = comparable_closes(s["feed"].get("events", []), ref, tick, cfg.comparable_age)
    buyers = []
    if intel is not None:
        try:
            for b in intel.best_buyers(ref):
                ticks = [int(m) for e in b.get("evidence", []) if str(e).startswith("bid") for m in re.findall(r"\bt(\d+)\b", str(e))]
                if ticks and tick - max(ticks) <= 60 and b["team"] not in cfg.denied:
                    buyers.append({"team": b["team"], "last_bid_tick": max(ticks), "note": "puja reciente; no es una oferta vigente"})
        except Exception:  # noqa: BLE001 — la inteligencia es opcional
            pass
    return {"bids": bids, "rejected_bids": rejected, "asks": sorted(asks, key=lambda a: a["price"]), "closes": closes,
            "recent_buyers": buyers}


def three_prices(rep: dict, ev: dict, cfg: Config, book: Optional[float] = None, market: Optional[dict] = None) -> dict:
    loss = rep["loss"] or 0.0
    floor = min_net(loss, cfg.margin)
    closes = sorted(c["price"] for c in ev["closes"])
    basis, base = [], None
    if len(closes) >= 2:
        base = closes[len(closes) // 2]
        basis.append(f"mediana de {len(closes)} cierres comparables recientes = {base} P")
    if base is None and market and market.get("value") and str(market.get("confidence")).upper() in ("MEDIUM", "HIGH"):
        base = market["value"]   # estimación del módulo de mercado sobre precios ejecutados (confianza media/alta)
        basis.append(f"estimación de mercado {market['value']:g} P sobre precios ejecutados (confianza {market['confidence']})")
    ask = ev["asks"][0]["price"] if ev["asks"] else None
    if ask is not None:
        basis.append(f"competidor vivo más barato = {ask} P")
        base = ask - 1 if base is None else min(base, ask - 1)
    seen_bid = max((b["net"] for b in ev.get("rejected_bids", []) if not any("estructura" in p or "no pide" in p or "caduc" in p
                                                                           for p in b.get("problems", []))), default=None)
    if base is None and seen_bid:
        base = seen_bid
        basis.append(f"demanda observada en un venue vetado o equipo denegado: {seen_bid} P (no ejecutable por restricción del operador)")
    if base is None and book:
        base = book
        basis.append(f"sin evidencia de mercado: referencia de catálogo {book:g} P (no es un valor de mercado)")
    objective = max(floor, int(math.ceil(base))) if base is not None else floor
    quick = ev["bids"][0]["net"] if ev["bids"] else None
    return {"minimum": floor, "quick_close": quick, "objective": objective, "basis": basis or ["sin evidencia: se propone el mínimo"],
            "loss": loss, "margin": cfg.margin}


def expiry_for(ticks: int, ratio: float) -> int:
    return max(1, int(round(ticks * max(1.0, ratio))))


def decide(rep: dict, asset: int, ev: dict, pr: dict, st: dict, own_offer: Optional[dict], cfg: Config, tick: int,
           urgent: bool, venue: str, ratio: float = 1.0) -> dict:
    """Una decisión por activo: accept / list / cancel / wait, con motivo. Máximo una oferta de salida por asset_id."""
    ref, mn, obj = rep["ref"], pr["minimum"], pr["objective"]
    valid = [b for b in ev["bids"] if b["net"] >= mn]
    best = valid[0] if valid else None
    deadline = st["first_tick"] is not None and st["age"] >= cfg.ticks
    if own_offer is not None:                                   # ya hay una oferta de salida nuestra para este activo
        if best is not None and (best["net"] >= obj or urgent or deadline):
            return {"action": "cancel", "offer": own_offer["id"],
                    "reason": f"ACEPTAR: llega una puja ejecutable de {best['price']} P ({best['net']} netos ≥ "
                              f"{'objetivo' if best['net'] >= obj else 'mínimo'}): se libera la copia y se acepta al confirmarse"}
        if deadline:
            return {"action": "cancel", "offer": own_offer["id"], "reason": f"LIBERA: presupuesto de {cfg.ticks} ticks agotado sin oferta válida"}
        last = st["asks"][-1] if st["asks"] else None
        reprices = max(0, len(st["asks"]) - 1)
        if last is not None and tick - (st["last_ask_tick"] or tick) >= cfg.stale and reprices < cfg.counters and obj < last:
            return {"action": "cancel", "offer": own_offer["id"], "reprice": obj,
                    "reason": f"REPRECIO: {tick - st['last_ask_tick']} ticks sin interés y la evidencia justifica {obj} P < {last} P"}
        return {"action": "wait", "reason": f"oferta {own_offer['id']} en pie ({st['age']}/{cfg.ticks} ticks); sin mensajes repetidos"}
    if best is not None:
        if best["net"] >= obj:
            return {"action": "accept", "bid": best, "reason": f"puja de {best['price']} P ({best['net']} netos) ≥ objetivo {obj} P: cerrar sin esperar"}
        if urgent or deadline:
            return {"action": "accept", "bid": best, "reason": f"{best['net']} netos entre mínimo {mn} P y objetivo {obj} P y "
                                                                f"{'cierre cercano' if urgent else 'presupuesto agotado'}: se cierra"}
        better = len(ev["closes"]) >= 2 and pr["objective"] > best["net"] + 1
        if not better:
            return {"action": "accept", "bid": best, "reason": f"{best['net']} netos ≥ mínimo {mn} P y sin evidencia de mejora probable "
                                                                f"(no se prolonga por {obj - best['net']} P)"}
    if st["cooling_until"] is not None and tick < st["cooling_until"]:
        return {"action": "wait", "reason": f"liberada hace poco: enfriamiento hasta el tick {st['cooling_until']}"}
    if deadline and st["asks"]:
        return {"action": "wait", "reason": "presupuesto agotado"}
    price = max(obj, mn)
    buyer = ev["recent_buyers"][0]["team"] if ev["recent_buyers"] else None
    return {"action": "list", "price": price, "to": buyer, "venue": venue, "expires_in": expiry_for(cfg.ticks, ratio),
            "reason": f"propuesta al objetivo {price} P" + (f" dirigida a {buyer} (puja reciente)" if buyer else " (pública)")
                      + f" · mínimo {mn} P"}


def accept_candidate(rep: dict, asset: int, bid: dict, pr: dict, why: str) -> dict:
    ref, loss = rep["ref"], rep["loss"] or 0.0
    return {"type": "accept", "kind": "venta rápida: aceptar puja", "module": MODULE, "venue": bid["venue"], "offer": bid["offer"],
            "maker": bid["team"], "assets": [asset], "asset": asset, "deliver": {ref: 1}, "receive": {}, "ref": f"card:{ref}",
            "price": bid["price"], "fee": bid["fee"], "cash": bid["net"], "dv": -loss, "du": round(bid["net"] - loss, 2),
            "expected_du": round(bid["net"] - loss, 2), "p_fill": None, "score": 2.9 * 10 ** 5 if bid["net"] >= pr["objective"] else 2.7 * 10 ** 5,
            "blockers": [], "reason": why, "fast_sale": ref, "notes": [f"mínimo {pr['minimum']} · objetivo {pr['objective']}"]}


def list_candidate(rep: dict, asset: int, d: dict, pr: dict) -> dict:
    ref, loss = rep["ref"], rep["loss"] or 0.0
    return {"type": "list", "kind": "venta rápida: propuesta al objetivo", "module": MODULE, "venue": d["venue"], "ref": ref,
            "asset": asset, "price": d["price"], "to": d["to"], "fee": 0, "cash": d["price"], "dv": -loss,
            "du": round(d["price"] - loss, 2), "expected_du": None, "p_fill": None, "score": 1.5 * 10 ** 5, "blockers": [],
            "expires_in": d["expires_in"], "reason": d["reason"], "fast_sale": ref,
            "notes": [f"mínimo {pr['minimum']} P · objetivo {pr['objective']} P · {'; '.join(pr['basis'])}"]}


def cancel_candidate(rep: dict, asset: int, offer: dict, d: dict) -> dict:
    return {"type": "cancel", "kind": "venta rápida: liberar copia", "module": MODULE, "venue": offer.get("venue"),
            "offer": offer["id"], "ref": rep["ref"], "asset": asset, "price": None, "du": 0.0, "score": 3.5 * 10 ** 5,
            "blockers": [], "reason": d["reason"], "notes": [], "fast_sale": rep["ref"], "reprice": d.get("reprice")}


def lock_cancels(rep: dict, s: dict, led: dict) -> list:
    """Copias excedentes comprometidas en una oferta NUESTRA de otro módulo: se cancela mediante el coordinador y la copia
    se reutiliza solo cuando el servidor confirme su liberación (el siguiente tick ya no figura comprometida)."""
    out = []
    for asset, info in rep["locked"].items():
        if info["offer"] is None or any(a.get("module") == MODULE and a.get("asset") == asset for a in led.get("actions", [])
                                        if a["type"] == "list" and a.get("status") in ("submitted",)):
            continue
        out.append({"type": "cancel", "kind": "venta rápida: liberar compromiso anterior", "module": MODULE, "venue": info["venue"],
                    "offer": info["offer"], "ref": rep["ref"], "asset": asset, "price": None, "du": 0.0, "score": 3.4 * 10 ** 5,
                    "blockers": [], "reason": f"COMPROMISO: la campaña usa esta copia de {rep['ref']}: se cancela el compromiso anterior "
                                              f"(oferta {info['offer']}) y se confirma su liberación antes de reutilizarla",
                    "notes": [], "fast_sale": rep["ref"]})
    return out


def buyer_log(led: dict) -> dict:
    """Por comprador: propuestas, respuestas (liquidaciones), precio y tiempo de cierre, rechazos. Derivado del registro."""
    out: dict = {}
    for a in led.get("actions", []):
        if a.get("module") != MODULE or a["type"] not in ("list", "accept"):
            continue
        who = a.get("to") or a.get("maker") or "público"
        r = out.setdefault(who, {"proposals": 0, "settled": 0, "released": 0, "prices": [], "ticks_to_close": []})
        r["proposals"] += 1
        if a.get("status") == "settled":
            r["settled"] += 1
            r["prices"].append(a.get("price"))
            if a.get("settled_tick") is not None:
                r["ticks_to_close"].append(a["settled_tick"] - a["tick"])
        elif a.get("status") == "released":
            r["released"] += 1
    return out
