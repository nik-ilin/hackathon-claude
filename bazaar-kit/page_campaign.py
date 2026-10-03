"""Campaña de COMPLETAR UNA PÁGINA (objetivo actual: Malasaña). Lógica pura: propone candidatas; el coordinador
decide, protege y es el único que envía.

    FIND OWNERS → UNDERSTAND WHAT THEY WANT → SWAP DUPLICATES / USE CASH WHERE SUPERIOR → CANCEL REDUNDANT
    PURSUITS → RECALCULATE AFTER EVERY ACQUISITION → SECURE THE FINAL CARD → PAGE PROTECTED → NEXT OBJECTIVE

- Objetivos = cartas de página que FALTAN según el inventario actual (no una lista fija): need(ref) = 1 si no la
  tenemos, 0 si ya la tenemos. Nunca se persigue una segunda copia.
- El valor es NO LINEAL y se recalcula cada tick con trading.Valuation.delta sobre el inventario ACTUAL: la última
  carta incluye el bono de página.
- La prioridad estratégica (HIGH → VERY HIGH → CRITICAL) solo ordena candidatas: nunca autoriza ΔU < margen.
- Solo se dirige a equipos identificables (ids `tNN` expuestos por el juego); una oferta pública anónima se acepta por
  el mercado normal. Nunca se inventa posesión: solo evidencia publicada o liquidada.
- El texto nunca revela valor privado, bono, techo ni efectivo: la oferta estructurada es el mensaje.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

import page_guard as pg
import trading as tr

TEAM_ID = re.compile(r"^t\d+$")
LEVELS = {3: "HIGH", 2: "VERY HIGH", 1: "CRITICAL", 0: "COMPLETE"}
PRIORITY = {"HIGH": 1.2 * 10 ** 5, "VERY HIGH": 2 * 10 ** 5, "CRITICAL": 3.5 * 10 ** 5}  # ordenación, no valor


@dataclass
class CampaignConfig:
    set_id: str = "MAL"
    margin: float = 2.0
    per_card: int = 100
    open_frac: float = 0.6          # apertura de una puja dirigida: fracción del ancla (sin revelar el techo)
    anchor_near: bool = False       # perfil fast-close: abrir cerca de una referencia comparable (ask/mercado)
    min_open_frac: float = 0.6      # una puja financiable por debajo de esta fracción del ancla no es creíble: se descarta
    strong_frac: float = 0.2        # ask "muy rentable": ΔU ≥ 20 % de la ganancia → aceptar ya
    directed_expiry: int = 20


def is_team(x) -> bool:
    return isinstance(x, str) and bool(TEAM_ID.match(x))


def campaign_state(snap: dict, val: tr.Valuation, set_id: str) -> dict:
    counts = tr.counts_of(snap["me"]["assets"])
    page = list(val.pages.get(set_id, []))
    owned = [r for r in page if counts.get(r, 0) > 0]
    missing = [r for r in page if counts.get(r, 0) == 0]
    level = LEVELS.get(len(missing), "HIGH") if missing else "COMPLETE"
    gains = {}
    for r in missing:  # valor marginal AHORA, con el inventario actual (bono de página si es la última)
        g, notes = val.delta(counts, Counter({r: 1}), Counter())
        gains[r] = {"gain": round(g, 2), "completes_page": any("completa la página" in n for n in notes)}
    return {"set": set_id, "page": page, "owned": owned, "missing": missing, "progress": f"{len(owned)}/{len(page)}",
            "state": f"{len(missing)}_MISSING" if missing else "COMPLETE", "level": level, "gains": gains,
            "complete": bool(page) and not missing}


def our_trade_assets(snap: dict, val: tr.Valuation, committed) -> list:
    """Duplicados entregables: nunca la copia protegida de una página completa, nunca una copia ya comprometida y
    nunca la última copia de una carta. Con la pérdida privada de entregar UNA copia."""
    me = snap["me"]
    counts = tr.counts_of(me["assets"])
    committed = set(committed)
    out = []
    for ref, n in counts.items():
        mine = [a["id"] for a in me["assets"] if a.get("kind") == "card" and a.get("ref") == ref]
        free = n - len(set(mine) & committed)
        ids = pg.tradeable_assets(ref, counts, snap["catalog"], me["assets"], committed)
        if free < 2 or not ids:
            continue
        dv, _ = val.delta(counts, Counter(), Counter({ref: 1}))
        out.append({"ref": ref, "asset": ids[-1], "copies": n, "loss": round(-dv, 2)})
    return sorted(out, key=lambda x: x["loss"])


def market_quotes(snap: dict, ref: str, venues: dict) -> list:
    """Asks abiertos de `ref` en TODOS los venues usables (públicos o dirigidos a nosotros), con coste total."""
    team, tick = snap["me"]["id"], snap["clock"]["tick"]
    pool = list((snap.get("offers") or {}).get("offers", []))
    for b in (snap.get("boards") or {}).values():
        pool += (b or {}).get("offers", [])
    seen, out = set(), []
    for o in pool:
        if o.get("id") in seen or o.get("maker") == team or o.get("status") != "open":
            continue
        seen.add(o.get("id"))
        g, w = o.get("give") or {}, o.get("want") or {}
        assets = [a for a in g.get("assets") or [] if isinstance(a, dict)]
        if (len(assets) != 1 or assets[0].get("ref") != ref or g.get("cash") or g.get("types")
                or o.get("to") not in (None, team) or o.get("venue") not in venues
                or (o.get("expires_tick") is not None and o["expires_tick"] <= tick)):
            continue
        wanted = [t[5:] for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]
                  if isinstance(t, str) and t.startswith("card:")]
        cash = int(w.get("cash") or 0)
        if cash and not wanted and not w.get("assets"):
            out.append({"kind": "ask", "offer": o["id"], "maker": o.get("maker"), "venue": o["venue"], "price": cash,
                        "fee": venues[o["venue"]].fee(cash, 1), "directed": o.get("to") == team,
                        "expires": o.get("expires_tick")})
        elif len(wanted) == 1 and not cash and not w.get("assets"):
            out.append({"kind": "swap", "offer": o["id"], "maker": o.get("maker"), "venue": o["venue"],
                        "wants": wanted[0], "fee": venues[o["venue"]].fee(0, 2), "directed": o.get("to") == team,
                        "expires": o.get("expires_tick")})
    return out


def owners_ranked(ref: str, intel, snap: dict, trade_assets: list) -> list:
    """Equipos IDENTIFICABLES con evidencia de tener `ref`, con lo que probablemente quieren de lo nuestro."""
    team = snap["me"]["id"]
    rows = {}
    if intel is not None:
        try:
            for x in intel.best_counterparties(ref, [a["ref"] for a in trade_assets], limit=10):
                if not is_team(x["team"]) or x["team"] == team:
                    continue
                own = intel.p_owns(x["team"], ref)
                ctx = intel.context(x["team"])
                last = intel.db.execute("SELECT max(tick) FROM inventory_evidence WHERE team_id=? AND ref=? AND weight>0",
                                        (x["team"], ref)).fetchone()[0]
                rows[x["team"]] = {"team": x["team"], "ownership_confidence": own["p"],
                                   "owner_wants_target": intel.p_wants(x["team"], ref)["p"],
                                   "confidence": own["confidence"], "evidence": own["evidence"],
                                   "last_seen_tick": last, "wants": [w["ref"] for w in ctx.likely_wants][:6],
                                   "wants_of_ours": x["wants_of_ours"], "trade_compatibility": x["score"],
                                   "response_history": x["directed_to_us"],
                                   "profile": ctx.profile.get("dominant_strategy")}
        except Exception as e:  # la inteligencia es opcional: nunca bloquea la campaña
            rows["_error"] = {"team": None, "error": type(e).__name__}
    rows.pop("_error", None)
    return sorted(rows.values(), key=lambda r: (-r["ownership_confidence"], -r["trade_compatibility"]))


def p_response(owner: dict) -> float:
    """HEURISTIC: 0.25 base + hasta 0.45 si ya nos ha dirigido ofertas (respuesta observada), multiplicado por
    (1 − P(el dueño también la quiere)): quien está completando esa colección difícilmente la suelta."""
    base = min(0.7, 0.25 + 0.15 * min(3, owner.get("response_history") or 0))
    return round(base * (1 - min(0.9, owner.get("owner_wants_target") or 0)), 3)


def plan_target(ref: str, gain: float, completes: bool, level: str, snap: dict, val: tr.Valuation, cfg: CampaignConfig,
                venues: dict, intel, trade_assets: list, market: Optional[dict] = None,
                afford: Optional[int] = None) -> dict:
    """Rutas para UNA carta objetivo, ordenadas por utilidad esperada: ask existente, oferta dirigida, trueque con
    un dueño que quiere un duplicado nuestro, puja dirigida al dueño. Devuelve informe + candidatas."""
    counts = tr.counts_of(snap["me"]["assets"])
    ceiling = math.floor(min(gain - cfg.margin, cfg.per_card))
    rep = {"ref": ref, "gain": gain, "completes_page": completes, "ceiling": ceiling, "level": level, "routes": [],
           "owners": owners_ranked(ref, intel, snap, trade_assets), "quotes": market_quotes(snap, ref, venues)}
    base = PRIORITY.get(level, PRIORITY["HIGH"])
    for q in rep["quotes"]:
        if q["kind"] == "ask":
            cost = q["price"] + q["fee"]
            du = round(gain - cost, 2)
            strong = du >= max(cfg.margin, cfg.strong_frac * gain) or (completes and du >= cfg.margin)
            rep["routes"].append({"route": "E/F ask" + (" dirigido" if q["directed"] else " público"), "eu": du, "p": 0.95,
                                  "du": du, "cost": cost, "via": f"#{q['offer']} {q['maker']} en {q['venue']}",
                                  "candidate": None if du < cfg.margin else {
                                      "type": "accept", "kind": f"comprar {ref} (campaña {cfg.set_id})",
                                      "venue": q["venue"], "offer": q["offer"], "maker": q["maker"],
                                      "receive": {ref: 1}, "deliver": {}, "assets": [], "ref": ref,
                                      "price": q["price"], "fee": q["fee"], "cash": -cost, "dv": gain, "du": du,
                                      "expected_du": round(0.95 * du, 2), "score": base + du + (10 ** 4 if strong else 0),
                                      "blockers": [], "page_campaign": ref,
                                      "reason": f"{ref}: ask {q['price']} P + {q['fee']} P comisión, ΔU {du} P"
                                                + (" · ÚLTIMA carta: se asegura" if completes else "")}})
        elif q["kind"] == "swap":
            give = next((a for a in trade_assets if a["ref"] == q["wants"]), None)
            if give:
                du, _ = val.delta(counts, Counter({ref: 1}), Counter({give["ref"]: 1}))
                du = round(du - q["fee"], 2)
                rep["routes"].append({"route": "C trueque existente", "eu": du, "p": 0.95, "du": du, "cost": 0,
                                      "via": f"#{q['offer']} {q['maker']} pide {q['wants']}",
                                      "candidate": None if du < cfg.margin else {
                                          "type": "accept", "kind": f"trueque por {ref} (campaña {cfg.set_id})",
                                          "venue": q["venue"], "offer": q["offer"], "maker": q["maker"],
                                          "receive": {ref: 1}, "deliver": {give["ref"]: 1}, "assets": [give["asset"]],
                                          "ref": ref, "price": 0, "fee": q["fee"], "cash": -q["fee"], "du": du,
                                          "dv": du + q["fee"], "expected_du": round(0.95 * du, 2),
                                          "score": base + du + 10 ** 4, "blockers": [], "page_campaign": ref,
                                          "reason": f"{ref}: trueque existente, damos {give['ref']} (#{give['asset']})"}})
    anchor = (market or {}).get("value") or (market or {}).get("best_ask") or gain
    for o in rep["owners"][:3]:
        pr = p_response(o)
        give = next((a for a in trade_assets if a["ref"] in o["wants_of_ours"]), None)
        if give:
            du, _ = val.delta(counts, Counter({ref: 1}), Counter({give["ref"]: 1}))
            du = round(du, 2)
            if du >= cfg.margin:
                rep["routes"].append({
                    "route": "C trueque dirigido", "eu": round(o["ownership_confidence"] * pr * du, 2), "p": pr,
                    "du": du, "cost": 0, "via": f"{o['team']}: {give['ref']} → {ref}",
                    "candidate": {"type": "swap_list", "kind": f"trueque dirigido a {o['team']} (campaña {cfg.set_id})",
                                  "venue": None, "to": o["team"], "ref": ref, "asset": give["asset"],
                                  "give_ref": give["ref"], "price": 0, "fee": 0, "cash": 0, "du": du, "dv": du,
                                  "expected_du": round(o["ownership_confidence"] * pr * du, 2),
                                  "score": base * 0.8 + du, "blockers": [], "page_campaign": ref,
                                  "expires_in": cfg.directed_expiry,
                                  "reason": f"{o['team']} probablemente tiene {ref} ({o['confidence']}) y pide "
                                            f"{give['ref']}; damos un duplicado (pérdida {give['loss']} P)"}})
        frac = cfg.open_frac
        if cfg.anchor_near and market and (market.get("best_ask") or market.get("value")):
            frac = max(cfg.open_frac, 0.92)  # cerca de un comparable real, no una apertura extrema
        wanted_open = int(max(1, min(ceiling - 1, round(frac * anchor))))
        opening = wanted_open if afford is None else int(min(wanted_open, max(0, afford)))
        if afford is not None and wanted_open > afford:
            if opening < math.floor(cfg.min_open_frac * anchor) or opening < 1:
                # NUNCA abrir a una cifra que no podemos pagar ni a una tan baja que no sea creíble
                rep.setdefault("unfunded_routes", []).append({
                    "route": "B puja dirigida", "via": f"{o['team']} a {wanted_open} P", "need": wanted_open,
                    "afford": afford, "deficit": wanted_open - afford, "team": o["team"]})
                continue
        if ceiling >= 1 and opening >= 1:
            du = round(gain - opening, 2)
            rep["routes"].append({
                "route": "B puja dirigida", "eu": round(o["ownership_confidence"] * pr * du, 2), "p": pr, "du": du,
                "cost": opening, "via": f"{o['team']} a {opening} P",
                "candidate": {"type": "bid", "kind": f"puja dirigida a {o['team']} (campaña {cfg.set_id})",
                              "venue": None, "to": o["team"], "ref": ref, "price": opening, "fee": 0,
                              "cash": -opening, "dv": gain, "du": du,
                              "expected_du": round(o["ownership_confidence"] * pr * du, 2),
                              "score": base * 0.7 + du, "blockers": [], "page_campaign": ref,
                              "expires_in": cfg.directed_expiry,
                              "reason": f"{o['team']} probablemente tiene {ref} ({o['confidence']}); abrimos en "
                                        f"{opening} P, por debajo de nuestro techo (no se revela)"}})
    rep["routes"].sort(key=lambda r: (r.get("candidate") is None, -r["eu"], -(r["du"] or 0)))
    viable = [r for r in rep["routes"] if r.get("candidate") is not None]
    rep["best"] = viable[0] if viable else None          # solo rutas con ΔU ≥ margen
    rep["rejected_routes"] = [r for r in rep["routes"] if r.get("candidate") is None]
    return rep


def acquired_target_cancels(my_offers: list, team: str, counts: Counter) -> list:
    """Ya tenemos la carta: TODA oferta propia abierta que la pide (puja, trueque, dirigida) sobra."""
    out = []
    for o in my_offers or []:
        if o.get("maker") != team or o.get("status") != "open" or o.get("thread"):
            continue
        w = o.get("want") or {}
        wanted = [t[5:] for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]
                  if isinstance(t, str) and t.startswith("card:")]
        if len(wanted) == 1 and counts.get(wanted[0], 0) >= 1:
            why = f"ya tenemos {wanted[0]}: la oferta {o['id']} compraría una copia innecesaria"
            out.append({"type": "cancel", "kind": "retirar búsqueda ya cumplida", "offer": o["id"],
                        "venue": o.get("venue"), "ref": wanted[0], "price": int((o.get("give") or {}).get("cash") or 0),
                        "du": 0.0, "score": 2.5 * 10 ** 5, "blockers": [], "reason": why, "why": [why],
                        "target_acquired": True, "notes": [], "uncertainty": ""})
    return out


def report_block(state: dict, plans: list, next_action: str) -> str:
    rows = [f"=== {state['set']} COMPLETION CAMPAIGN === ({state['state']}, prioridad {state['level']})"]
    total_cost = total_gain = 0.0
    for p in plans:
        b = p.get("best")
        rows += [f"{p['ref']}", "  status: MISSING", f"  private gain if acquired: {p['gain']} P"
                 + (" (incluye bono de página)" if p["completes_page"] else ""),
                 f"  private ceiling: {p['ceiling']} P (no se revela)",
                 f"  best market ask: {min((q['price'] + q['fee'] for q in p['quotes'] if q['kind'] == 'ask'), default='—')}",
                 "  known owners: " + (", ".join(f"{o['team']} {o['confidence']} ({o['ownership_confidence']})"
                                                 for o in p["owners"][:4]) or "— (sin evidencia publicada)"),
                 f"  best route: {b['route'] + ' · ' + b['via'] if b else '—'}",
                 *(f"  no rentable ahora: {r['route']} · {r['via']} (ΔU {r['du']} P)" for r in p.get("rejected_routes", [])[:2]),
                 f"  expected ΔU: {b['eu'] if b else '—'} P (ΔU si se ejecuta {b['du'] if b else '—'} P)"]
        if b:
            total_cost += b["cost"]
            total_gain += p["gain"]
    rows += [f"PAGE: {state['progress']} → targets remaining {len(state['missing'])}",
             f"Estimated total acquisition cost: {round(total_cost, 1)} P (rutas mejores actuales)",
             f"Estimated total collection gain: {round(total_gain, 1)} P (suma de ganancias marginales actuales; "
             "se recalcula tras cada compra)", f"Expected completion surplus: {round(total_gain - total_cost, 1)} P",
             f"NEXT ACTION: {next_action}"]
    if len(state["missing"]) == 1 and plans:
        p = plans[0]
        b = p.get("best")
        rows += ["*** FINAL MALASAÑA PIECE ***" if state["set"] == "MAL" else f"*** FINAL {state['set']} PIECE ***",
                 f"Target: {p['ref']}", f"Private gain: {p['gain']} P",
                 f"Page completion bonus included: {'YES' if p['completes_page'] else 'NO'}",
                 "Known owners: " + (", ".join(o["team"] for o in p["owners"]) or "—"),
                 f"Best executable route: {b['route'] + ' · ' + b['via'] if b else '—'}",
                 f"Economic ceiling: {p['ceiling']} P", f"Recommended: {next_action}"]
    return "\n".join(rows)


def choose_page(snap: dict, val: tr.Valuation, venues: dict, dealers: dict, budget: float, margin: float = 1.0) -> dict:
    """Página principal por VIABILIDAD (no por número de huecos). Para cada página incompleta del inventario ACTUAL:
    ganancia = ΔV de conseguir TODAS las que faltan (Valuation.delta: el bono de página entra una sola vez);
    coste estimado por carta = mínimo entre el ask ejecutable más barato (con comisión) y el precio de lista de un
    vendedor que venda esa rareza; sin ninguna fuente, la carta es "sin oferta" y la página queda bloqueada.
    Puntuación = (ganancia − coste) / huecos, solo si el coste total cabe en el presupuesto. Heurística declarada."""
    counts = tr.counts_of(snap["me"]["assets"])
    out = []
    for sid, page in val.pages.items():
        missing = [r for r in page if counts.get(r, 0) == 0]
        if not page or not missing or len(missing) == len(page):
            continue
        gain, _ = val.delta(counts, Counter({r: 1 for r in missing}), Counter())
        costs, blocked = {}, []
        for r in missing:
            asks = [q["price"] + q["fee"] for q in market_quotes(snap, r, venues) if q["kind"] == "ask"]
            rar = val.cards[r].get("rarity")
            lists = [row.get("list_price") for d in (dealers or {}).values()
                     for row in (d.get("menu") or {}).get("sells", []) if row.get("rarity") == rar and row.get("list_price")]
            src = asks + lists
            if src:
                costs[r] = min(src)
            else:
                blocked.append(r)
        cost = sum(costs.values())
        feasible = not blocked and cost <= budget and gain - cost >= margin * len(missing)
        out.append({"set": sid, "missing": missing, "gain_all": round(gain, 2), "est_cost": cost, "costs": costs,
                    "blocked": blocked, "feasible": feasible,
                    "score": round((gain - cost) / len(missing), 2) if not blocked else None})
    ranked = sorted(out, key=lambda x: (not x["feasible"], -(x["score"] or -1e9)))
    return {"choice": ranked[0]["set"] if ranked and ranked[0]["feasible"] else None, "pages": ranked}
