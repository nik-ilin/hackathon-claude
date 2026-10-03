"""Estrategia por FASES hacia el cierre del día: operar activamente, transición y tesorería. Lógica pura, sin red.

    A · OPERACIÓN ACTIVA   hasta cierre − `transition_min` (90 min): comportamiento normal del coordinador.
    B · TRANSICIÓN         de cierre − 90 a cierre − 30: prioridad creciente al efectivo neto; las compras que inmovilizan
                           capital deben dejar al cierre al menos `w × meta` de efectivo libre (w sube de 0 a 1), o tener una
                           salida respaldada; caducidades y capital pasivo se reducen.
    C · TESORERÍA          últimos 30 min: sin compras ordinarias; solo ventas rentables, cobros y cancelaciones de lo que
                           pueda gastar mañana el capital; los últimos `final_ticks` ticks no se publica nada nuevo.

El CIERRE es el oficial del servidor (`clock()["closes"]`, con su zona horaria), no una hora fija; los umbrales son
configurables. La META de efectivo libre (150, ideal 200 P) NO es una garantía ni permite liquidar inventario con pérdida:
las ventas siguen sujetas al margen económico, y las páginas completas siguen protegidas por page_guard.

Definiciones:
  efectivo libre         = caja − efectivo reservado en ofertas abiertas (pueden aceptarse a la vez) − exposición con
                           vendedores − aceptaciones pendientes. NUNCA incluye ventas futuras ni comisiones prometidas.
  escenarios al cierre   (sin probabilidades): CONFIRMADO = efectivo libre ahora; +PUBLICADAS = si se llenaran todas las
                           ventas ya publicadas (precio neto); +EJECUTABLES = si además se aceptaran las ventas ejecutables
                           hoy. El último es el máximo OBSERVADO, no una previsión.
  adelanto de la transición: si incluso el máximo observado no alcanza la meta, la transición y la tesorería empiezan antes,
                           de forma gradual (hasta `accelerate_max` min según el déficit), y se explica por qué.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")
NOW_OVERRIDE: Optional[datetime] = None  # solo para pruebas y simulaciones


@dataclass
class PhaseConfig:
    enabled: bool = False
    transition_min: int = 90       # minutos antes del cierre en que empieza la transición
    treasury_min: int = 30         # minutos antes del cierre en que empieza la tesorería
    target_min: int = 150          # meta mínima de efectivo libre al cierre
    target_stretch: int = 200      # meta deseable
    accelerate_max: int = 60       # adelanto máximo (min) cuando la meta es inalcanzable con lo observado
    final_ticks: int = 2           # últimos ticks: no se publica nada nuevo (queda margen para liquidar)
    exit_haircut: float = 0.25     # descuento de PARÁMETRO (no una probabilidad) sobre una salida de reventa por si el comprador desaparece
    allow_campaign_in_transition: bool = False  # el operador permite que la campaña de página compre en B (nunca en C)


def now() -> datetime:
    return NOW_OVERRIDE or datetime.now(TZ)


def minutes_to_close(clock: dict, at: Optional[datetime] = None) -> Optional[float]:
    closes = clock.get("closes")
    if not closes:
        return None
    try:
        end = datetime.fromisoformat(closes)
    except ValueError:
        return None
    at = at or now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ)
    at = at.astimezone(end.tzinfo) if at.tzinfo else at.replace(tzinfo=end.tzinfo)
    return (end - at).total_seconds() / 60.0


def scenarios(free_cash: int, open_sell_net: int, executable_sales: int) -> dict:
    """Efectivo al cierre en tres escenarios acumulativos, sin probabilidades."""
    return {"confirmed": free_cash, "with_published_sales": free_cash + open_sell_net,
            "with_executable_sales": free_cash + open_sell_net + executable_sales}


def state(clock: dict, cfg: PhaseConfig, scen: dict, at: Optional[datetime] = None) -> dict:
    m = minutes_to_close(clock, at)
    out = {"enabled": cfg.enabled, "minutes_to_close": None if m is None else round(m, 1), "phase": "A", "w": 0.0,
           "b_start": cfg.transition_min, "c_start": cfg.treasury_min, "accelerated_min": 0, "reason": "",
           "target_min": cfg.target_min, "target_stretch": cfg.target_stretch, "scenarios": scen,
           "deficit_min": max(0, cfg.target_min - scen["confirmed"]),
           "deficit_stretch": max(0, cfg.target_stretch - scen["confirmed"]),
           "reachable": scen["with_executable_sales"] >= cfg.target_min}
    if not cfg.enabled:
        out["reason"] = "fases desactivadas (--phases)"
        return out
    if m is None:
        out["reason"] = "el servidor no informa del cierre: se opera en fase A"
        return out
    short = max(0, cfg.target_min - scen["with_executable_sales"])
    if short > 0:
        frac = min(1.0, short / max(1, cfg.target_min))
        extra = int(math.ceil(frac * cfg.accelerate_max))
        out["accelerated_min"] = extra
        out["b_start"] = cfg.transition_min + extra
        out["c_start"] = cfg.treasury_min + extra // 2
        out["reason"] = (f"OBJETIVO DE {cfg.target_min} P NO ALCANZABLE con la liquidez observada (máximo "
                         f"{scen['with_executable_sales']} P): faltan {short} P; la transición se adelanta {extra} min y la "
                         f"tesorería {extra // 2} min para dar tiempo a vender sin liquidar con pérdida")
    b, c = out["b_start"], out["c_start"]
    if m > b:
        out["phase"], out["w"] = "A", 0.0
    elif m > c:
        out["phase"], out["w"] = "B", round(min(1.0, max(0.0, (b - m) / max(1, b - c))), 3)
    else:
        out["phase"], out["w"] = "C", 1.0
    return out


# ------------------------------------------------------------------ clasificación y efectos sobre las candidatas

def is_sale(c: dict) -> bool:
    if c["type"] == "accept":
        return (c.get("cash") or 0) > 0 and not c.get("receive")
    return c["type"] in ("list", "dealer_sell_open", "dealer_sell_counter", "dealer_sell_accept") and not c.get("receive")


def purchase_cost(c: dict) -> int:
    """Efectivo que la candidata inmoviliza o gasta si se cumple (0 si no es una compra con efectivo)."""
    t = c["type"]
    if t == "accept" and (c.get("cash") or 0) < 0:
        return -int(c["cash"])
    if t == "bid":
        return int(c.get("price") or -(c.get("cash") or 0) or 0)
    if t in ("dealer_open", "dealer_counter", "dealer_accept", "radio_buy"):
        return int(c.get("price") or c.get("ceiling") or 0)
    if t in ("team_open", "team_propose", "team_accept"):
        return int(c.get("price") or c.get("first_cash") or 0)
    return 0


def resale_backed(c: dict, states: dict, ticks_left: float, cfg: PhaseConfig, margin: float) -> tuple:
    """Una compra para reventa necesita una SALIDA real: una puja viva de otro equipo por esa carta, con tiempo para
    completar ambas operaciones y descontada por si el comprador desaparece. Sin salida respaldada: no está respaldada."""
    cost = purchase_cost(c)
    refs = list((c.get("receive") or {}).keys()) or ([c["ref"].removeprefix("card:")] if c.get("ref") else [])
    if not refs or cost <= 0:
        return False, "sin carta identificable"
    if ticks_left < 4:
        return False, "queda tiempo para una operación, no para dos"
    st = states.get(refs[0])
    bid = getattr(st, "best_bid", None)
    if not bid:
        return False, f"sin puja de otro equipo por {refs[0]}: precio de venta de un vendedor no es una salida"
    exit_net = bid.price * (1 - cfg.exit_haircut)
    ok = exit_net >= cost + margin
    return ok, (f"salida {bid.price} P descontada {cfg.exit_haircut:.0%} = {exit_net:.0f} P frente a coste {cost} P"
                + ("" if ok else " (insuficiente)"))


def apply(cands: list, ph: dict, cfg: PhaseConfig, *, free_cash: int, states: dict, tick_seconds: float,
          expiry_ratio: float, margin: float, open_cash_offers: list, args_extra: Optional[dict] = None) -> list:
    """Marca bloqueos, prioridad y caducidades según la fase. Devuelve notas legibles. No envía nada."""
    notes = []
    if not cfg.enabled or ph["phase"] == "A" and ph["minutes_to_close"] is None:
        return notes
    phase, w, m = ph["phase"], ph["w"], ph["minutes_to_close"]
    ticks_left = (m * 60.0 / tick_seconds) if (m is not None and tick_seconds) else 10 ** 6
    final = ticks_left <= cfg.final_ticks
    for c in cands:
        bl = c.setdefault("blockers", [])
        if final and c["type"] in ("list", "bid", "swap_list", "dealer_open", "team_open", "dealer_sell_open"):
            bl.append(f"últimos {cfg.final_ticks} ticks antes del cierre: no queda tiempo para liquidar")
            continue
        if phase == "A":
            continue
        cost = purchase_cost(c)
        if cost > 0 and c["type"] not in ("cancel",):
            if c.get("manual_order"):
                c.setdefault("notes", []).append("orden manual del operador: la fase no la bloquea")
            elif phase == "C":
                bl.append(f"TESORERÍA (faltan {m:.0f} min): sin compras ordinarias; meta {cfg.target_min} P libres")
            else:  # B
                after = free_cash - cost
                need = w * cfg.target_min
                backed, why = resale_backed(c, states, ticks_left, cfg, margin) if c["type"] != "dealer_open" else (False, "")
                campaign_ok = cfg.allow_campaign_in_transition and c.get("page_campaign")
                if after < need and not backed and not campaign_ok:
                    bl.append(f"TRANSICIÓN (w={w:.2f}, faltan {m:.0f} min): comprar {cost} P dejaría {after} P libres < "
                              f"{need:.0f} P de la meta de cierre"
                              + (f"; salida no respaldada ({why})" if why else "")
                              + ("; la campaña de página solo compite en B con --treasury-allow-campaign"
                                 if c.get("page_campaign") else ""))
                elif backed:
                    c.setdefault("notes", []).append(f"compra para reventa respaldada: {why}")
        if is_sale(c) and not bl:
            c["score"] = c.get("score", 0) + 1e5 * w  # prioridad de ORDENACIÓN (no son puntos ni dinero)
            c.setdefault("notes", []).append(f"fase {phase}: venta que libera liquidez (+{1e5 * w:.0f} de ordenación)")
        if c["type"] in ("list", "bid", "swap_list") and phase in ("B", "C"):
            remaining_eff = max(1.0, ticks_left * (1.0 - 0.5 * w) - cfg.final_ticks)
            cap = max(1, int(remaining_eff * max(1.0, expiry_ratio)))
            cur = c.get("expires_in")
            if cur is None or cur > cap:
                c["expires_in"] = cap
                notes.append(f"caducidad de {c.get('ref') or c.get('kind')} limitada a {cap} (solicitada) por la fase {phase}")
    if phase == "C":
        for o in open_cash_offers:
            cands.append({"type": "cancel", "module": "tesorería", "kind": "tesorería: retirar oferta que inmoviliza efectivo",
                          "offer": o["id"], "venue": o.get("venue"), "ref": o.get("ref"), "price": o.get("cash", 0),
                          "du": 0.0, "score": 3.2e5, "blockers": [], "notes": [], "uncertainty": "",
                          "reason": f"TESORERÍA: la oferta {o['id']} puede gastar {o.get('cash', 0)} P mañana; "
                                    "se confirma la cancelación en el servidor antes de liberar la reserva"})
    return notes


def open_cash_offers(my_offers: list, team: str) -> list:
    """Ofertas propias abiertas que entregan efectivo (pujas, contraofertas a vendedores) y que otro puede aceptar."""
    out = []
    for o in my_offers or []:
        g = o.get("give") or {}
        if o.get("maker") == team and o.get("status") == "open" and g.get("cash") and not g.get("assets"):
            w = o.get("want") or {}
            ref = next((t[5:] for t in list(w.get("types") or []) if str(t).startswith("card:")), None)
            out.append({"id": o["id"], "cash": int(g["cash"]), "venue": o.get("venue"), "ref": ref,
                        "thread": o.get("thread")})
    return out


def summary_lines(ph: dict, *, committed: int, pending_buys: int, pending_sells: int, closed: int, net_realized,
                  next_action: str, blockers: list) -> list:
    sc = ph["scenarios"]
    lines = [
        f"FASE {ph['phase']}{' (w=%.2f)' % ph['w'] if ph['phase'] == 'B' else ''} · "
        + (f"{ph['minutes_to_close']:.0f} min hasta el cierre" if ph["minutes_to_close"] is not None else "cierre desconocido")
        + f" · umbrales B≤{ph['b_start']} C≤{ph['c_start']} min"
        + (f" (adelantados {ph['accelerated_min']} min)" if ph["accelerated_min"] else ""),
        f"EFECTIVO libre {sc['confirmed']} P · comprometido {committed} P · déficit a {ph['target_min']} P: "
        f"{ph['deficit_min']} P · a {ph['target_stretch']} P: {ph['deficit_stretch']} P",
        f"PENDIENTE compras {pending_buys} · ventas {pending_sells} · cerradas {closed} · beneficio neto reconciliado "
        f"{net_realized} P",
        f"CIERRE (sin probabilidades): confirmado {sc['confirmed']} P · si se llenan las ventas publicadas "
        f"{sc['with_published_sales']} P · máximo observado con ventas ejecutables hoy {sc['with_executable_sales']} P"
        f" → meta {'ALCANZABLE en el máximo observado' if ph['reachable'] else 'NO alcanzable con lo observado'}",
        f"SIGUIENTE: {next_action}" + (f" · BLOQUEOS: {'; '.join(blockers[:3])}" if blockers else "")]
    if ph["reason"]:
        lines.append(f"AVISO: {ph['reason']}")
    return lines
