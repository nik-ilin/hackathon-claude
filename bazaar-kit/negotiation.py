"""Política de compra a un dealer, sin llamadas a la API, para poder probarla en local (sim.py, test_negotiation.py).

Lo que garantizan las reglas (RULES.md): el dealer solo se mueve si nos movemos; repetir precio no consigue nada; cada
conversación tiene un límite secreto; al agotarse su paciencia nombra una oferta final (final=true); un trato al precio
de apertura no cuenta para la escalera; solo la estructura de una oferta aceptada mueve algo, en el siguiente tick.
Todo lo demás de este módulo (zona de cierre, reciprocidad, ahorro esperado) es una ESTIMACIÓN heurística.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

QUOTA_REASONS = {"persona_quota", "sold_out", "cooloff"}  # cierres que no dicen nada del precio: no se mezclan


@dataclass
class Config:
    budget: int = 60                # presupuesto máximo configurado (P)
    reserve: int = 100              # efectivo que nunca se gasta
    min_margin: int = 2             # beneficio mínimo exigido: valor - precio
    open_frac: float = 0.70         # primera oferta = fracción de su precio de referencia (heurística, no un óptimo)
    start_room_frac: float = 0.0    # la primera oferta deja al menos este hueco bajo nuestro máximo
    step_frac: float = 0.50         # paso = fracción de la brecha mientras ella concede
    min_step: int = 1
    max_step: int = 4
    near_step: int = 1              # paso al acercarnos a la zona plausible de cierre
    near_gap: int = 3               # brecha a partir de la cual estamos "cerca"
    low_reciprocity: float = 0.35   # por debajo, ella apenas concede: no aceleramos nuestras concesiones
    stall_turns: int = 2            # respuestas seguidas sin concesión = se ha plantado
    turn_budget: int = 12           # control de riesgo PROPIO; no es la paciencia secreta del dealer, que no conocemos
    accept_gap: int = 2             # acepta si su precio está a esta distancia de nuestra última oferta
    min_saving: float = 1.5         # ahorro esperado mínimo para seguir regateando...
    risk_per_turn: float = 0.05     #...que sube con cada turno consumido (más riesgo de que se canse)
    reciprocity_window: int = 3
    reply_timeout_ticks: int = 3    # ticks sin respuesta tras los que el turno cuenta como "sin concesión"
    accept_opening_price: bool = False
    history_min_samples: int = 3
    prior_low: float = 0.55         # zona de cierre a priori, en fracción de la apertura (estimación débil)
    prior_high: float = 0.90
    pack_value_factor: float = 0.9  # valor de un sobre = valor esperado con tus valores privados * factor (estimación)
    prior_close_frac: float = 0.6   # sin historial, precio esperado de cierre = precio de lista * fracción (para elegir)


def price_ceiling(cfg: Config, cash: int, value: float) -> tuple[int, dict]:
    """precio_maximo = min(presupuesto, efectivo - reserva, valor - margen), y cada término para mostrarlo."""
    parts = {"presupuesto": cfg.budget, "efectivo - reserva": cash - cfg.reserve,
             "valor - margen": math.floor(value - cfg.min_margin)}
    return int(min(parts.values())), parts


# ------------------------------------------------------------------ estado de una conversación

@dataclass
class Ask:
    price: int
    offer_id: int
    final: bool
    live: bool
    tick: int
    expires: Optional[int] = None  # tick de caducidad de su oferta estructurada


@dataclass
class Round:
    ours: int                      # nuestra oferta
    before: Optional[int]          # su precio vigente cuando ofrecimos
    tick: int
    reply: Optional[int] = None    # su primer precio tras nuestra oferta


@dataclass
class NegState:
    item: Optional[str]
    opening: Optional[int] = None
    rounds: list = field(default_factory=list)
    current: Optional[Ask] = None  # su última oferta estructurada (viva o no)
    awaiting_reply: bool = False
    final_countered: bool = False  # ya contraofertamos después de una oferta final suya (escalera: una sola vez)

    @property
    def turns(self) -> int:
        return len(self.rounds)

    @property
    def last_ours(self) -> Optional[int]:
        return self.rounds[-1].ours if self.rounds else None

    @property
    def live(self) -> Optional[Ask]:
        return self.current if self.current and self.current.live else None

    @property
    def ref_ask(self) -> Optional[int]:
        return self.current.price if self.current else self.opening


def item_of(topic: Optional[dict]) -> Optional[str]:
    """Identifica el artículo de una conversación. Los topics de VENTA ({"sell": {"assets": [...]}}) devuelven
    "sell:<id>,<id>"; antes devolvían None y el hilo se descartaba entero, así que no había canal de venta a
    vendedores: justo el que llena la escalera."""
    t = topic or {}
    buy = t.get("buy") or {}
    if "card" in buy:
        return f"card:{buy['card']}"
    if "pack" in buy:
        return f"pack:{buy['pack']}"
    sell = t.get("sell") or {}
    assets = sell.get("assets") or []
    if assets:
        return "sell:" + ",".join(str(a) for a in assets)
    return None


def is_sell(item: Optional[str]) -> bool:
    return bool(item) and item.startswith("sell:")


def sell_assets(item: Optional[str]) -> list:
    return [int(x) for x in item[5:].split(",") if x] if is_sell(item) else []


def sell_assets_of(topic: Optional[dict]) -> list:
    """Activos que ofrecemos en una conversación de VENTA a un vendedor ({"sell": {"assets": [...]}})."""
    return list(((topic or {}).get("sell") or {}).get("assets") or [])


def _our_price(m: dict, side: str = "buy") -> Optional[int]:
    if m.get("price") is not None:
        return int(m["price"])
    cash = ((m.get("offer") or {}).get("give" if side == "buy" else "want") or {}).get("cash")
    return int(cash) if cash else None


def state_from_thread(thread: dict, dealer: str, now_tick: int, cfg: Config, side: str = "buy") -> NegState:
    """Reconstruye el estado desde los mensajes del hilo, así un reinicio retoma la negociación tal cual estaba.
    side="sell": el vendedor nos COMPRA (su precio está en give.cash); por defecto, compramos (want.cash)."""
    st = NegState(item=item_of(thread.get("topic")))
    for m in thread.get("messages") or []:
        o = m.get("offer")
        if m.get("sender") == dealer:
            if not o:
                continue
            exp = o.get("expires_tick")
            live = o.get("status") == "open" and (exp is None or int(exp) >= now_tick)  # caducada = no vigente
            price = o["want"]["cash"] if side == "buy" else o["give"]["cash"]
            a = Ask(int(price), o["id"], bool(o.get("final")), live, int(m.get("tick", 0)), exp)
            if st.opening is None:
                st.opening = a.price
            if st.rounds and st.rounds[-1].reply is None:
                st.rounds[-1].reply = a.price
            st.current = a
        else:
            p = _our_price(m, side)
            if p is not None:
                if st.current is not None and st.current.final:
                    st.final_countered = True
                st.rounds.append(Round(p, st.current.price if st.current else None, int(m.get("tick", 0))))
    last = st.rounds[-1] if st.rounds else None
    st.awaiting_reply = bool(last and last.reply is None and now_tick - last.tick < cfg.reply_timeout_ticks)
    return st


# ------------------------------------------------------------------ indicadores y estimación

@dataclass
class Metrics:
    gap: Optional[int]                 # su precio de referencia - nuestra última oferta
    her_drop: Optional[int]            # su último descenso
    our_raise: Optional[int]           # nuestra última subida
    reciprocity: Optional[float]       # sus descensos / nuestras subidas en la ventana reciente
    stalled: int                       # respuestas seguidas sin concesión (o sin respuesta)
    projection: Optional[float]        # precio de encuentro si la reciprocidad se mantuviera
    expected_saving: Optional[float]   # su precio - proyección


def metrics(st: NegState, cfg: Config) -> Metrics:
    ask, last = st.ref_ask, st.last_ours
    gap = ask - last if ask is not None and last is not None else None
    pairs = []  # (su descenso, nuestra subida) por ronda respondida
    for i, r in enumerate(st.rounds):
        if r.reply is not None and r.before is not None:
            pairs.append((r.before - r.reply, r.ours - st.rounds[i - 1].ours if i else None))
    win = [(d, s) for d, s in pairs if s and s > 0][-cfg.reciprocity_window:]
    rec = sum(d for d, _ in win) / sum(s for _, s in win) if win else None
    stalled = 0
    for i, r in enumerate(reversed(st.rounds)):
        if r.reply is None:
            if i == 0 and st.awaiting_reply:
                continue
            stalled += 1
        elif r.before is not None and r.reply >= r.before:
            stalled += 1
        else:
            break
    proj = saving = None
    if gap is not None and gap > 0 and rec:  # con reciprocidad 0 no se extrapola un precio mínimo fijo
        proj = last + gap / (1 + rec)
        saving = ask - proj
    return Metrics(gap, pairs[-1][0] if pairs else None,
                   st.rounds[-1].ours - st.rounds[-2].ours if len(st.rounds) >= 2 else None,
                   rec, stalled, proj, saving)


@dataclass
class Estimate:
    low: Optional[float]
    high: Optional[float]
    n: int                  # cierres comparables usados
    strength: str           # "sin datos" | "supuesto" | "limitada" | "razonable" (cualitativo, no estadístico)
    evidence: list


def estimate(outcomes: list, st: NegState, m: Metrics, cfg: Config) -> Estimate:
    """Zona heurística de cierre. `outcomes`: resultados previos del MISMO dealer y artículo, sin cierres por cuota."""
    if st.opening is None:
        return Estimate(None, None, 0, "sin datos", ["aún no hay precio de apertura"])
    deals = [o for o in outcomes if o.get("status") == "deal" and o.get("opening") and o.get("close_price")]
    ev = []
    if len(deals) >= cfg.history_min_samples:
        ratios = sorted(o["close_price"] / o["opening"] for o in deals)
        low, high = ratios[0] * st.opening, ratios[-1] * st.opening
        strength = "limitada" if len(deals) < 10 else "razonable"
        ev.append(f"{len(deals)} cierres comparables entre {ratios[0]:.0%} y {ratios[-1]:.0%} de la apertura")
    else:
        low, high = cfg.prior_low * st.opening, cfg.prior_high * st.opening
        strength = "supuesto"
        ev.append(f"SUPUESTO INICIAL, no aprendido: {cfg.prior_low:.0%}-{cfg.prior_high:.0%} de la apertura "
                  f"({len(deals)} cierres comparables, se necesitan {cfg.history_min_samples})")
    if len(outcomes) > len(deals):
        ev.append(f"{len(outcomes) - len(deals)} negociaciones comparables sin compra")
    replies = [r for r in st.rounds if r.reply is not None and r.before is not None]
    if replies:
        conceded = sum(1 for r in replies if r.reply < r.before)
        ev.append(f"observado: {conceded} concesiones en {len(replies)} respuestas")
        if not conceded:
            ev.append("sin concesiones: la hipótesis de descuento gradual pierde confianza "
                      "(pocas respuestas: no prueba un precio mínimo fijo)")
    if m.projection is not None:
        ev.append(f"reciprocidad {m.reciprocity:.2f} en esta conversación: encuentro proyectado ~{m.projection:.1f} P")
    if st.current:
        high = min(high, st.current.price)
    return Estimate(round(min(low, high), 1), round(high, 1), len(deals), strength, ev)


# ------------------------------------------------------------------ decisión

@dataclass
class Decision:
    action: str                     # counter | accept | abandon | wait
    reason: str
    price: Optional[int] = None
    offer_id: Optional[int] = None


def next_price(st: NegState, cfg: Config, ceiling: int, est: Estimate, m: Metrics) -> tuple[Optional[int], str]:
    """Nuestra siguiente oferta, siempre estrictamente por encima de la anterior y por debajo de su precio y del máximo."""
    ask = st.ref_ask
    hi = min(ceiling, ask - 1)  # ofrecer su precio o más no tiene sentido: entonces se acepta
    last = st.last_ours
    if last is None:
        p = max(1, min(int(cfg.open_frac * ask + 0.5), math.floor(ceiling * (1 - cfg.start_room_frac)), hi))
        if p > hi:
            return None, f"no hay primera oferta válida por debajo de {hi + 1} P"
        return p, f"apertura heurística: {cfg.open_frac:.0%} de {ask} P, acotada por el máximo de {ceiling} P"
    gap = ask - last
    step = min(cfg.max_step, max(cfg.min_step, int(gap * cfg.step_frac + 0.5)))
    why = f"paso moderado de {step} P (brecha {gap} P)"
    mid = (est.low + est.high) / 2 if est.low is not None else None
    if m.reciprocity and m.reciprocity < cfg.low_reciprocity:  # concede poco; si no concede nada, no se frena
        step, why = cfg.min_step, f"apenas concede (reciprocidad {m.reciprocity:.2f}): paso mínimo de {cfg.min_step} P"
    elif gap <= cfg.near_gap or (mid is not None and last + step > mid):
        step = min(step, cfg.near_step)
        why = f"cerca de la zona estimada ({est.low}-{est.high} P) o brecha corta: paso de {step} P"
    p = min(last + step, hi)
    if p <= last:
        return None, f"no queda una oferta nueva entre {last} P y {hi} P"
    return p, why


def decide(st: NegState, cfg: Config, ceiling: int, est: Estimate, m: Optional[Metrics] = None) -> Decision:
    m = m or metrics(st, cfg)
    live = st.live

    def worth(price: int) -> bool:  # económicamente válido y no el precio de apertura sin negociar
        return price <= ceiling and (cfg.accept_opening_price or st.opening is None or price < st.opening)

    if live and live.final:  # su última palabra: no se regatea más
        if worth(live.price):
            return Decision("accept", f"oferta final de {live.price} P dentro del máximo de {ceiling} P", live.price, live.offer_id)
        return Decision("abandon", f"oferta final de {live.price} P por encima del máximo ({ceiling} P) o igual a la apertura")
    if ceiling < 1:
        return Decision("abandon", f"sin margen económico (precio máximo {ceiling} P)")
    if st.current is None:
        return Decision("wait", "la abuela aún no ha puesto precio")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta a nuestra oferta")
    if live and worth(live.price):
        def take(why: str) -> Decision:
            return Decision("accept", why, live.price, live.offer_id)
        if st.last_ours is not None and live.price - st.last_ours <= cfg.accept_gap:
            return take(f"brecha de {live.price - st.last_ours} P: otro turno no compensa")
        if st.turns >= cfg.turn_budget:
            return take(f"presupuesto de {cfg.turn_budget} turnos agotado y {live.price} P está dentro del máximo")
        if m.stalled >= cfg.stall_turns:
            return take(f"{m.stalled} respuestas sin concesión: parece plantada en {live.price} P")
        need = cfg.min_saving + cfg.risk_per_turn * st.turns
        if m.expected_saving is not None and m.expected_saving < need:
            return take(f"ahorro esperado {m.expected_saving:.1f} P < {need:.1f} P exigidos por el riesgo de perder el trato")
    if st.turns >= cfg.turn_budget:
        return Decision("abandon", f"presupuesto de {cfg.turn_budget} turnos agotado y {st.ref_ask} P no es aceptable "
                                   f"(máximo {ceiling} P)")
    p, why = next_price(st, cfg, ceiling, est, m)
    if p is None:
        if live and worth(live.price):
            return Decision("accept", f"{why}; su precio de {live.price} P es aceptable", live.price, live.offer_id)
        return Decision("abandon", why)
    return Decision("counter", why, p)


MESSAGES = (
    "¡Buenas, Abuela Carmen! ¿Le parecería bien {p} P por {item}?",
    "Muchas gracias por atenderme. ¿Qué tal {p} P?",
    "Subo un poquito, con todo el cariño: {p} P.",
    "Me haría mucha ilusión. ¿Lo dejamos en {p} P?",
    "Hago un esfuerzo más: {p} P. ¿Trato hecho?",
    "Es usted un encanto. ¿{p} P le parece justo?",
    "Un pasito más por mi parte: {p} P.",
    "Gracias por su paciencia, Abuela. ¿{p} P?",
)


def message(turn: int, price: int, item_name: str) -> str:
    return MESSAGES[turn % len(MESSAGES)].format(p=price, item=item_name)


def dealer_message(dealer: str, turn: int, price: int, item_name: str) -> str:
    """Texto para una contraoferta: la plantilla de Abuela no debe dirigirse a Chato."""
    if dealer == "chato":
        return (f"Voy directo al precio, Chato: {price} P por {item_name}. ¿Cerramos?"
                if turn == 0 else f"Subo a {price} P por {item_name}. Es mi mejor oferta.")
    return message(turn, price, item_name)


# ------------------------------------------------------------------ modo primera compra (--first-purchase)

@dataclass
class FastConfig:
    """Negociación corta: rebaja moderada en dos contraofertas como mucho. Todos los valores son heurísticas ajustables."""
    open_frac: float = 0.85          # primera oferta = 85 % de su precio, siempre por debajo de él
    second_gap_frac: float = 0.75    # segunda oferta cierra el 75 % de la brecha restante (redondeo hacia arriba)
    max_counteroffers: int = 2
    max_negotiation_ticks: int = 6   # por conversación, contando desde que se abrió (también si se retoma)
    max_total_ticks: int = 12        # para todo el intento de primera compra, persistido entre reinicios
    accept_gap: int = 1              # con una brecha de 1 P y oferta válida, se acepta
    min_viable_frac: float = 0.85    # si nuestro máximo < 85 % de su precio, no insistimos: mejor otro artículo
    allow_opening_price: bool = True  # tras el intento breve; puede no contar como trato negociado para la escalera


OPENING_NOTE = "es su precio inicial: puede no contar como trato negociado para la progresión"


def decide_fast(st: NegState, f: FastConfig, ceiling: int, conv_ticks_left: int, total_ticks_left: int) -> Decision:
    """Primera oferta ~85 %; si rebaja dentro del máximo, aceptar; si mantiene, una segunda oferta que cierra ~75 % de la
    brecha; después aceptar una oferta válida o abandonar. Nunca más de `max_counteroffers` (cuenta las de hilos retomados)."""
    live, n = st.live, st.turns

    def take(why: str) -> Decision:
        note = f" ({OPENING_NOTE})" if st.opening is not None and live.price >= st.opening else ""
        return Decision("accept", why + note, live.price, live.offer_id)

    if live and live.final:  # siempre tiene prioridad
        return take(f"oferta final de {live.price} P dentro del máximo de {ceiling} P") if live.price <= ceiling else \
            Decision("abandon", f"oferta final de {live.price} P por encima del máximo de {ceiling} P")
    if ceiling < 1:
        return Decision("abandon", f"sin margen económico (máximo {ceiling} P)")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon",
                        "la abuela aún no ha puesto precio" + ("" if conv_ticks_left > 0 else " y se agotó el plazo"))
    if st.awaiting_reply:  # una respuesta pendiente no es un rechazo
        return Decision("wait", "esperando su respuesta a nuestra oferta")
    out_of_time = conv_ticks_left <= 0 or total_ticks_left <= 0
    if live and live.price <= ceiling:
        last = st.rounds[-1] if st.rounds else None
        if last and last.reply is not None and last.before is not None and last.reply < last.before:
            return take(f"ha rebajado de {last.before} P a {last.reply} P y cabe en el máximo de {ceiling} P")
        if st.last_ours is not None and live.price - st.last_ours <= f.accept_gap:
            return take(f"brecha de {live.price - st.last_ours} P con una oferta válida")
        if n >= f.max_counteroffers or out_of_time:
            if live.price >= (st.opening or 0) and not f.allow_opening_price:
                return Decision("abandon", "solo queda su precio inicial y este modo no lo acepta")
            why = "contraofertas agotadas" if n >= f.max_counteroffers else "plazo agotado"
            return take(f"{why}; {live.price} P cabe en el máximo de {ceiling} P")
    if out_of_time:
        return Decision("abandon", f"plazo agotado y no hay oferta vigente dentro del máximo de {ceiling} P")
    if n >= f.max_counteroffers:
        return Decision("abandon", f"{n} contraofertas hechas y su precio ({st.ref_ask} P) no es aceptable o ya no está vigente")
    ask = st.ref_ask
    hi = min(ceiling, ask - 1)
    if n == 0:
        if ceiling < ask * f.min_viable_frac:
            return Decision("abandon", f"nuestro máximo ({ceiling} P) está muy por debajo de su precio ({ask} P): "
                                       "mejor otro artículo")
        p = min(int(f.open_frac * ask + 0.5), hi)
        if p < 1:
            return Decision("abandon", "no hay primera oferta válida")
        return Decision("counter", f"primera oferta: {f.open_frac:.0%} de {ask} P, acotada por el máximo de {ceiling} P", p)
    p = min(st.last_ours + math.ceil(f.second_gap_frac * (ask - st.last_ours)), hi)
    if p <= st.last_ours:
        if live and live.price <= ceiling:
            return take(f"no queda una oferta nueva por encima de {st.last_ours} P; {live.price} P es válido")
        return Decision("abandon", f"no queda una oferta nueva entre {st.last_ours} P y el máximo de {ceiling} P")
    return Decision("counter", f"mantiene {ask} P: segunda oferta cierra el {f.second_gap_frac:.0%} de la brecha, "
                               f"acotada por {hi} P", p)


@dataclass
class DealerPolicy:
    """Negociación con un vendedor concreto. Valores heurísticos ajustables, no óptimos demostrados; los rasgos del
    vendedor (/api/dealers) orientan la elección, pero NO son una fórmula conocida de sus precios."""
    name: str
    open_frac: float                 # primera oferta = fracción de su precio vigente
    gap_frac: float                  # cada contraoferta cierra esta fracción de la brecha restante (redondeo arriba)
    max_counteroffers: int
    max_ticks: int                   # por conversación, contando desde que se abrió
    accept_on_concession: bool       # aceptar en cuanto rebaje (rápido) o seguir mientras quedan contraofertas
    allow_opening_price: bool        # False en modo score: el precio de apertura no cuenta para la escalera
    accept_gap: int = 1
    min_viable_frac: float = 0.8     # si nuestro máximo < 80 % de su precio, mejor otro artículo
    secure: bool = False             # SECURE: cerrar un trato negociado válido en cuanto exista (ver ladder_mode)
    sell_open_mult: float = 2.0      # VENTA: primera petición = múltiplo de su primera puja
    sell_gap_frac: float = 0.35      # VENTA: cada concesión nuestra cierra esta fracción de la brecha, bajando


# Rasgos leídos de /api/dealers el 2026-10-03 (patience / shrewdness / memory). La paciencia manda en cuántas
# rondas aguanta antes de romper el hilo; la memoria, en si romperlo es recuperable.
#   abuela  0.85 / 0.20 / 0.15   paciente y poco astuta  -> aguanta, se le puede abrir lejos
#   chato   0.35 / 0.85 / 0.30   impaciente y astuto     -> pocas rondas, pasos grandes
#   pilar   0.60 / 0.75 / 0.15   recíproca (cede 1:1 en 20 observaciones del feed)
#   picaros 0.40 / 0.70 / 0.30   impaciente; ofertas con truco -> validar siempre los assets
DEALER_TRAITS = {
    "abuela":  dict(open_frac=0.45, gap_frac=0.30, max_counteroffers=3, max_ticks=8,
                    sell_open_mult=2.4, sell_gap_frac=0.25),
    "chato":   dict(open_frac=0.55, gap_frac=0.40, max_counteroffers=2, max_ticks=5,
                    sell_open_mult=1.8, sell_gap_frac=0.40),
    "pilar":   dict(open_frac=0.50, gap_frac=0.35, max_counteroffers=3, max_ticks=6,
                    sell_open_mult=2.2, sell_gap_frac=0.30),
    "picaros": dict(open_frac=0.50, gap_frac=0.40, max_counteroffers=2, max_ticks=5,
                    sell_open_mult=2.0, sell_gap_frac=0.35),
}


def dealer_policy(dealer: str, mode: str = "score") -> DealerPolicy:
    """Una política por vendedor, derivada de sus rasgos publicados.

    La versión anterior tenía la asignación INVERTIDA: daba la política paciente a la abuela (que ya es la blanda)
    y metía a chato, pilar y picaros en un fallback de `open_frac=0.90` con UNA sola contraoferta. Abrir al 90 % de
    su precio captura ~10 % del rango, y la escalera paga precisamente cuota de rango capturada: de ahí los 0.356
    puntos de negociación por trato de t15 frente a los 0.939 de t01 (leaderboard tick 820).

    `accept_on_concession` se mantiene en False en modo score para todos: aceptar la primera rebaja cierra el trato
    en la parte baja del rango y gasta una de las 3 casillas que puntúan en ese nivel.
    """
    allow = mode != "score"
    t = DEALER_TRAITS.get(dealer)
    if t is None:  # vendedor nuevo: prudente, pero nunca a precio de apertura
        t = dict(open_frac=0.55, gap_frac=0.40, max_counteroffers=2, max_ticks=5,
                 sell_open_mult=1.9, sell_gap_frac=0.35)
    return DealerPolicy(dealer, t["open_frac"], t["gap_frac"], t["max_counteroffers"], t["max_ticks"],
                        accept_on_concession=mode != "score", allow_opening_price=allow,
                        sell_open_mult=t["sell_open_mult"], sell_gap_frac=t["sell_gap_frac"])


def price_floor(value_lost: float, margin: float = 0.0) -> int:
    """VENTA: precio mínimo aceptable = valor privado que entregamos + margen, redondeado arriba.

    `value_lost` sale de Valuation.delta(counts, add=0, remove={ref: 1}) en valor absoluto, así que ya incluye el
    bono de página que se rompería. Un suelo por encima del bono es lo que impide vender una carta de una página
    completa sin querer."""
    return int(math.ceil(abs(value_lost) + margin))


def decide_dealer_sell(st: NegState, pol: DealerPolicy, floor: int, conv_ticks_left: int,
                       total_ticks_left: int = 10 ** 9) -> Decision:
    """Lado VENDEDOR: su precio es una PUJA, así que queremos el máximo y concedemos bajando.

    Simétrico a decide_dealer: nunca por debajo del suelo, nunca repite ni sube una petición ya hecha, y en modo
    score nunca cierra a su puja de apertura (su apertura es su puja más BAJA: aceptarla captura rango ~0)."""
    live, n = st.live, st.turns

    def valid(price: int) -> bool:
        return price >= floor and (pol.allow_opening_price or st.opening is None or price > st.opening)

    def take(why: str) -> Decision:
        note = f" ({OPENING_NOTE})" if st.opening is not None and live.price <= st.opening else ""
        return Decision("accept", why + note, live.price, live.offer_id)

    if live and live.final:
        return take(f"puja final de {live.price} P aceptable (suelo {floor} P)") if valid(live.price) else \
            Decision("abandon", f"puja final de {live.price} P por debajo del suelo {floor} P "
                                f"(apertura {st.opening} P)")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha pujado")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    out_of_time = conv_ticks_left <= 0 or total_ticks_left <= 0
    last = st.rounds[-1] if st.rounds else None
    conceded = bool(last and last.reply is not None and last.before is not None and last.reply > last.before)
    if live and valid(live.price):
        if pol.accept_on_concession and conceded:
            return take(f"ha subido de {last.before} P a {last.reply} P")
        if st.last_ours is not None and st.last_ours - live.price <= pol.accept_gap:
            return take(f"brecha de {st.last_ours - live.price} P")
        if n >= pol.max_counteroffers or out_of_time:
            return take(("contraofertas agotadas" if n >= pol.max_counteroffers else "plazo agotado")
                        + f": {live.price} P por encima del suelo {floor} P")
    if out_of_time:
        return Decision("abandon", f"plazo agotado y su puja no supera el suelo de {floor} P")
    if n >= pol.max_counteroffers:
        return Decision("abandon", f"{n} contraofertas hechas y su puja ({st.ref_ask} P) sigue bajo el suelo "
                                   f"de {floor} P")
    bid = st.ref_ask
    lo = max(floor, bid + 1)  # pedir su puja o menos no tiene sentido: entonces se acepta
    if n == 0:
        p = max(int(pol.sell_open_mult * bid + 0.5), lo)
        return Decision("counter", f"primera petición: {pol.sell_open_mult:.1f}x su puja de {bid} P, "
                                   f"con suelo {floor} P", p)
    p = max(st.last_ours - math.ceil(pol.sell_gap_frac * (st.last_ours - bid)), lo)
    if p >= st.last_ours:
        p = st.last_ours - 1
    if p < lo:
        if live and valid(live.price):
            return take(f"no queda petición nueva por encima del suelo; {live.price} P es válido")
        return Decision("abandon", f"no queda petición entre {bid} P y nuestra última de {st.last_ours} P "
                                   f"sin bajar del suelo {floor} P")
    return Decision("counter", f"mantiene {bid} P: cerramos el {pol.sell_gap_frac:.0%} de la brecha bajando, "
                               f"con suelo {floor} P", p)


def decide_dealer(st: NegState, pol: DealerPolicy, ceiling: int, conv_ticks_left: int,
                  total_ticks_left: int = 10 ** 9) -> Decision:
    """final=true: aceptar dentro del máximo o abandonar. Nunca repite ni baja una oferta, nunca supera el máximo,
    cuenta las contraofertas de hilos retomados, y una respuesta pendiente no es un rechazo."""
    live, n = st.live, st.turns

    def valid(price: int) -> bool:
        return price <= ceiling and (pol.allow_opening_price or st.opening is None or price < st.opening)

    def take(why: str) -> Decision:
        note = f" ({OPENING_NOTE})" if st.opening is not None and live.price >= st.opening else ""
        return Decision("accept", why + note, live.price, live.offer_id)

    if live and live.final:
        return take(f"oferta final de {live.price} P aceptable (máximo {ceiling} P)") if valid(live.price) else \
            Decision("abandon", f"oferta final de {live.price} P no aceptable (máximo {ceiling} P, apertura {st.opening} P)")
    if ceiling < 1:
        return Decision("abandon", f"sin margen económico (máximo {ceiling} P)")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha puesto precio")
    if pol.secure and live and st.opening is not None and live.price < st.opening and valid(live.price):
        # SECURE: ya ha concedido (precio vigente < apertura) y está dentro del máximo -> cerrar YA el trato negociado;
        # no se arriesga un hueco de la escalera por ahorrar 1-3 P más.
        return take(f"SECURE: ha rebajado de {st.opening} P a {live.price} P (≤ máximo {ceiling} P); se cierra")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    out_of_time = conv_ticks_left <= 0 or total_ticks_left <= 0
    last = st.rounds[-1] if st.rounds else None
    conceded = bool(last and last.reply is not None and last.before is not None and last.reply < last.before)
    if live and valid(live.price):
        if pol.accept_on_concession and conceded:
            return take(f"ha rebajado de {last.before} P a {last.reply} P")
        if st.last_ours is not None and live.price - st.last_ours <= pol.accept_gap:
            return take(f"brecha de {live.price - st.last_ours} P")
        if n >= pol.max_counteroffers or out_of_time:
            return take(("contraofertas agotadas" if n >= pol.max_counteroffers else "plazo agotado") +
                        f"; {live.price} P es aceptable")
    if out_of_time or n >= pol.max_counteroffers:
        return Decision("abandon", f"{n} contraofertas / plazo agotado y {st.ref_ask} P no es aceptable o no está vigente")
    ask = st.ref_ask
    hi = min(ceiling, ask - 1)
    if n == 0:
        if ceiling < ask * pol.min_viable_frac:
            return Decision("abandon", f"máximo {ceiling} P muy por debajo de su precio {ask} P: mejor otro artículo")
        p = min(int(pol.open_frac * ask + 0.5), hi)
        if p < 1:
            return Decision("abandon", "no hay primera oferta válida")
        return Decision("counter", f"[{pol.name}] apertura {pol.open_frac:.0%} de {ask} P", p)
    p = min(st.last_ours + math.ceil(pol.gap_frac * (ask - st.last_ours)), hi)
    if p <= st.last_ours:
        if live and valid(live.price):
            return take(f"no queda oferta nueva por encima de {st.last_ours} P; {live.price} P es aceptable")
        return Decision("abandon", f"no queda oferta nueva entre {st.last_ours} P y {hi} P")
    return Decision("counter", f"[{pol.name}] cierra el {pol.gap_frac:.0%} de la brecha ({ask} P vigente)", p)


# ------------------------------------------------------------------ escalera de vendedores (mejores tres tratos)

LADDER_SLOTS = 3  # RULES.md: cuentan los tres mejores tratos negociados por nivel; uno que falta cuenta cero


def qualifying_deals(dealer: str, threads: list = (), outcomes: list = ()) -> list:
    """Tratos con `dealer` LIQUIDADOS y NEGOCIADOS (precio de cierre < su apertura; aceptar la apertura no cuenta).
    Determinista: hilos `deal` del servidor (precio realmente liquidado) + resultados del diario, sin duplicar hilos."""
    out = {}
    for t in threads or []:
        if t.get("with") != dealer or t.get("status") != "deal":
            continue
        st = state_from_thread(t, dealer, 10 ** 9, Config())
        paid = settled_price(t, dealer)
        if paid is not None and st.opening is not None and paid < st.opening:
            out[t.get("id")] = {"thread": t.get("id"), "item": st.item, "opening": st.opening, "close": paid,
                                "source": "servidor"}
    for r in outcomes or []:
        if r.get("dealer") != dealer or r.get("status") != "deal" or not r.get("settled"):
            continue
        op, cl = r.get("opening"), r.get("close_price")
        if op is not None and cl is not None and cl < op and r.get("thread") not in out:
            out[r.get("thread")] = {"thread": r.get("thread"), "item": r.get("item"), "opening": op, "close": cl,
                                    "source": "diario"}
    return sorted(out.values(), key=lambda x: str(x["thread"]))


def ladder_mode(n_qualifying: int) -> str:
    """SECURE hasta tener los tres tratos que puntúan con ese vendedor; después OPTIMIZE (mejorar los tres mejores)."""
    return "SECURE" if n_qualifying < LADDER_SLOTS else "OPTIMIZE"


def policy_for(dealer: str, mode: str, n_qualifying: int) -> DealerPolicy:
    """Política base del vendedor; en SECURE acepta en cuanto concede dentro del máximo y cierra la brecha más deprisa.
    Nunca relaja el máximo económico ni (en modo score) la regla de no aceptar la apertura."""
    base = dealer_policy(dealer, mode)
    if ladder_mode(n_qualifying) == "OPTIMIZE":
        return base
    return DealerPolicy(base.name, base.open_frac, max(base.gap_frac, 0.5), base.max_counteroffers, base.max_ticks,
                        accept_on_concession=True, allow_opening_price=base.allow_opening_price,
                        accept_gap=max(base.accept_gap, 3), min_viable_frac=base.min_viable_frac, secure=True)


def dealer_accept_priority(mode: str, n_qualifying: int, ticks_to_expiry: Optional[int]) -> int:
    """Prioridad ESTRATÉGICA (no un valor en primas inventado) de aceptar una oferta válida de un vendedor.
    Orden: seguridad (≥ 10⁶) > cierre de vendedor que caduca / tercer trato > otras aceptaciones inmediatas > resto."""
    score = 3 * 10 ** 5 if mode == "SECURE" else 10 ** 5 + 2 * 10 ** 3
    if mode == "SECURE" and n_qualifying == LADDER_SLOTS - 1:
        score += 10 ** 5  # el tercer trato llena el último hueco que puntúa
    if ticks_to_expiry is not None:
        score += 5 * 10 ** 4 if ticks_to_expiry <= 1 else 2 * 10 ** 4 if ticks_to_expiry <= 2 else 0
    return score


# ------------------------------------------------------------------ escalera de vendedores (--dealer-ladder, opt-in)
#
# Reglas OBSERVADAS en hilos reales (pocas muestras: son hipótesis de trabajo, no fórmulas del servidor):
# (t15-bazaar-bot/intel/AUDITORIA_LIDERES.md, tick ~280; pocas muestras: hipótesis de trabajo)
# - Chato copia el tamaño de nuestro paso (con +1 no se mueve), acepta nuestra oferta solo cuando estamos a 1-2 P de su
#   precio y, tras su oferta final, una contraoferta de final-1 funcionó 2/2; si no la acepta, se toma su final en el
#   tick siguiente. Pasos: +3 en poco común, +4 en rara; apertura de rara 0,70 (t18: 0,72 y +4). Aperturas razonables
#   (0,55-0,70): t13 abre a 0,2 y no cierra.
# - Abuela cede ~1 P por ronda: pasos de 1 y paciencia (final de sobre 19-21 frente a 22-24 con pasos de 2). t18 abre una
#   común de 12 a 7 (~0,6). Vende poco comunes a 21-22 P (Chato 27-31): esas compras se enrutan a ella. Compra comunes a
#   5 con final 6 abras como abras (t02 pide 10 y baja de 1 en 1).
# - Vendedores nuevos (nivel 3, p. ej. Pilar): sin observaciones, política prudente con parámetros de línea de órdenes.

@dataclass
class LadderProfile:
    """Política de escalera para un vendedor. Pasos FIJOS por rareza (`steps`) o, si no hay, una fracción de la brecha."""
    name: str
    open_frac: dict                  # rareza -> fracción de su precio en la primera oferta ("*" = resto)
    steps: dict                      # rareza -> paso fijo en P ("*" = resto); vacío = usar gap_frac
    accept_gap: int                  # aceptar su precio vigente si está a esta distancia de nuestra última oferta
    max_counteroffers: int
    max_ticks: int
    counter_final_once: bool = False  # tras su oferta final: una contraoferta de final-1, después aceptar el final
    gap_frac: float = 0.5
    max_open_frac: float = 1.0       # nunca pagar más de esta fracción de su apertura (prudencia con desconocidos)
    min_viable_frac: float = 0.8
    allow_opening_price: bool = False
    final_wait_ticks: int = 1        # tras la contraoferta final-1: ticks de espera antes de tomar su final

    def _by_rarity(self, table: dict, rarity: Optional[str], default):
        return table.get(rarity or "*", table.get("*", default))

    def opening(self, rarity: Optional[str]) -> float:
        return float(self._by_rarity(self.open_frac, rarity, 0.8))

    def step(self, rarity: Optional[str], gap: int) -> int:
        fixed = self._by_rarity(self.steps, rarity, None)
        return max(1, int(fixed)) if fixed else max(1, math.ceil(self.gap_frac * gap))


@dataclass
class LadderConfig:
    """Parámetros de la escalera. Los valores por defecto salen de las reglas observadas (ver arriba)."""
    chato_open: dict = field(default_factory=lambda: {"rare": 0.70, "uncommon": 0.70, "*": 0.70})
    chato_steps: dict = field(default_factory=lambda: {"rare": 4, "uncommon": 3, "*": 2})
    chato_counters: int = 5
    chato_ticks: int = 12
    abuela_open: dict = field(default_factory=lambda: {"*": 0.60})
    abuela_counters: int = 15
    abuela_ticks: int = 30
    new_open: float = 0.80           # vendedores nuevos (nivel 3): apertura prudente
    new_gap_frac: float = 0.35
    new_counters: int = 3
    new_ticks: int = 10
    new_max_frac: float = 0.95       # nunca más del 95 % de su apertura con un vendedor sin observar
    new_accept_gap: int = 1
    route: dict = field(default_factory=lambda: {"uncommon": "abuela"})  # rareza -> vendedor preferido para comprar
    sell_route: dict = field(default_factory=lambda: {"common": "abuela"})  # rareza -> vendedor que la compra
    sell_expected: dict = field(default_factory=lambda: {"abuela": {"common": 6}})  # final observado al venderle
    sell_open: int = 10              # nuestra primera petición al vender una común (t02: pide 10)
    sell_counters: int = 6
    sell_ticks: int = 12
    sell_margin: float = 1.0         # excedente mínimo al vender: precio >= valor perdido + margen


def ladder_profile(dealer: str, lc: Optional[LadderConfig] = None, mode: str = "score") -> LadderProfile:
    lc = lc or LadderConfig()
    allow = mode != "score"
    if dealer == "chato":
        return LadderProfile("chato", dict(lc.chato_open), dict(lc.chato_steps), accept_gap=1,
                             max_counteroffers=lc.chato_counters, max_ticks=lc.chato_ticks, counter_final_once=True,
                             allow_opening_price=allow)
    if dealer == "abuela":
        return LadderProfile("abuela", dict(lc.abuela_open), {"*": 1}, accept_gap=1,
                             max_counteroffers=lc.abuela_counters, max_ticks=lc.abuela_ticks, allow_opening_price=allow)
    return LadderProfile(dealer, {"*": lc.new_open}, {}, accept_gap=lc.new_accept_gap,
                         max_counteroffers=lc.new_counters, max_ticks=lc.new_ticks, gap_frac=lc.new_gap_frac,
                         max_open_frac=lc.new_max_frac, min_viable_frac=lc.new_open, allow_opening_price=allow)


def decide_ladder(st: NegState, prof: LadderProfile, ceiling: int, conv_ticks_left: int,
                  rarity: Optional[str] = None, now_tick: Optional[int] = None) -> Decision:
    """Compra con escalera. Garantías: nunca supera el máximo económico, nunca repite ni baja una oferta, nunca paga su
    apertura en modo score, y como mucho UNA contraoferta tras su oferta final."""
    live, n = st.live, st.turns
    cap = ceiling
    if st.opening is not None and prof.max_open_frac < 1.0:
        cap = min(cap, math.floor(prof.max_open_frac * st.opening))

    def valid(price: int) -> bool:
        return price <= cap and (prof.allow_opening_price or st.opening is None or price < st.opening)

    def take(why: str) -> Decision:
        return Decision("accept", f"[{prof.name}] {why}", live.price, live.offer_id)

    if live and live.final:
        last = st.rounds[-1] if st.rounds else None
        if st.final_countered and last and last.reply is None and (
                now_tick - last.tick < prof.final_wait_ticks if now_tick is not None else st.awaiting_reply):
            return Decision("wait", f"[{prof.name}] esperando respuesta a nuestra contraoferta final-1")
        p = live.price - 1
        if prof.counter_final_once and not st.final_countered and p <= cap and p > (st.last_ours or 0) \
                and conv_ticks_left > 0:
            return Decision("counter", f"[{prof.name}] oferta final de {live.price} P: contraoferta final-1 "
                                       f"una sola vez", p)
        if valid(live.price):
            return take(f"oferta final de {live.price} P aceptable (máximo {cap} P)")
        return Decision("abandon", f"[{prof.name}] oferta final de {live.price} P no aceptable (máximo {cap} P, "
                                   f"apertura {st.opening} P)")
    if cap < 1:
        return Decision("abandon", f"sin margen económico (máximo {cap} P)")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha puesto precio")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    out_of_time = conv_ticks_left <= 0
    if live and valid(live.price):
        if st.last_ours is not None and live.price - st.last_ours <= prof.accept_gap:
            return take(f"brecha de {live.price - st.last_ours} P")
        if n >= prof.max_counteroffers or out_of_time:
            return take(("contraofertas agotadas" if n >= prof.max_counteroffers else "plazo agotado") +
                        f"; {live.price} P es aceptable")
    if out_of_time or n >= prof.max_counteroffers:
        return Decision("abandon", f"[{prof.name}] {n} contraofertas / plazo agotado y {st.ref_ask} P no es aceptable")
    ask = st.ref_ask
    hi = min(cap, ask - 1)
    if n == 0:
        if cap < ask * prof.min_viable_frac:
            return Decision("abandon", f"máximo {cap} P muy por debajo de su precio {ask} P: mejor otro artículo")
        frac = prof.opening(rarity)
        p = min(int(frac * ask + 0.5), hi)
        if p < 1:
            return Decision("abandon", "no hay primera oferta válida")
        return Decision("counter", f"[{prof.name}] apertura {frac:.0%} de {ask} P", p)
    step = prof.step(rarity, ask - st.last_ours)
    p = min(st.last_ours + step, hi)
    if p <= st.last_ours:
        if live and valid(live.price):
            return take(f"no queda oferta nueva por encima de {st.last_ours} P; {live.price} P es aceptable")
        return Decision("abandon", f"no queda oferta nueva entre {st.last_ours} P y {hi} P")
    return Decision("counter", f"[{prof.name}] paso de {step} P ({ask} P vigente, rareza {rarity or '?'})", p)


def decide_ladder_sell(st: NegState, lc: LadderConfig, floor: int, conv_ticks_left: int,
                       mode: str = "score") -> Decision:
    """VENTA a un vendedor (state_from_thread con side="sell": ref_ask = su puja, last_ours = nuestra petición).
    Pedimos `sell_open` y bajamos de 1 en 1; nunca por debajo de `floor` (valor perdido + margen) ni, en modo score, a su
    precio de apertura. Su oferta final se acepta si llega al suelo."""
    live, n = st.live, st.turns
    lo = floor
    if mode == "score" and st.opening is not None:
        lo = max(lo, st.opening + 1)  # un trato al precio de apertura no cuenta para la escalera

    def take(why: str) -> Decision:
        return Decision("accept", why, live.price, live.offer_id)

    if live and live.final:
        return take(f"oferta final de {live.price} P >= suelo {lo} P") if live.price >= lo else \
            Decision("abandon", f"oferta final de {live.price} P bajo el suelo de {lo} P")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha puesto precio")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    out_of_time = conv_ticks_left <= 0
    if live and live.price >= lo:
        if st.last_ours is not None and st.last_ours - live.price <= 1:
            return take(f"su puja de {live.price} P está a {st.last_ours - live.price} P de nuestra petición")
        if n >= lc.sell_counters or out_of_time:
            return take(f"contraofertas o plazo agotados; {live.price} P >= suelo {lo} P")
    if out_of_time or n >= lc.sell_counters:
        return Decision("abandon", f"{n} peticiones / plazo agotado y su puja ({st.ref_ask} P) no llega a {lo} P")
    bid = st.ref_ask
    p = max(lc.sell_open, bid + 1, lo) if n == 0 else st.last_ours - 1
    p = max(p, bid + 1, lo)
    if st.last_ours is not None and p >= st.last_ours:
        if live and live.price >= lo:
            return take(f"no queda una petición nueva por debajo de {st.last_ours} P; {live.price} P es aceptable")
        return Decision("abandon", f"no queda una petición nueva entre {lo} P y {st.last_ours} P")
    return Decision("counter", f"[venta] pedimos {p} P (su puja {bid} P, suelo {lo} P)", p)


def ladder_message(dealer: str, turn: int, price: int, item_name: str, side: str = "buy") -> str:
    """Textos de la escalera: no se dirige a un vendedor nuevo con la plantilla de la Abuela."""
    if side == "sell":
        return (f"¡Buenas, Abuela! ¿Me compraría esta carta repetida por {price} P?" if dealer == "abuela"
                else f"Le vendo esta carta por {price} P.")
    if dealer in ("abuela", "chato"):
        return dealer_message(dealer, turn, price, item_name)
    return f"Le ofrezco {price} P por {item_name}." if turn == 0 else f"Puedo subir a {price} P por {item_name}."


def settled_sell_price(thread: dict, dealer: str) -> Optional[int]:
    """Precio cobrado en una VENTA a un vendedor, según la oferta liquidada del hilo."""
    for m in reversed(thread.get("messages") or []):
        o = m.get("offer") or {}
        if o.get("status") == "settled":
            side = "give" if o.get("maker") == dealer else "want"
            return (o.get(side) or {}).get("cash")
    return None


def sell_offer_problems(offer: dict, *, dealer: str, asset_id: int, floor: int) -> list:
    """Problemas de la oferta de compra del vendedor (lista vacía = se puede aceptar): solo efectivo por esa copia."""
    problems = []
    give, want = offer.get("give") or {}, offer.get("want") or {}
    if offer.get("maker") != dealer:
        problems.append(f"la hace {offer.get('maker')!r}, no {dealer!r}")
    if offer.get("status") != "open":
        problems.append(f"no está abierta ({offer.get('status')!r})")
    if give.get("assets") or give.get("types"):
        problems.append("además del dinero entrega cartas")
    if not isinstance(give.get("cash"), int) or give["cash"] < floor:
        problems.append(f"paga {give.get('cash')!r} P, bajo el suelo de {floor} P")
    ids = [a["id"] if isinstance(a, dict) else a for a in want.get("assets") or []]
    if ids != [asset_id] or want.get("cash") or want.get("types"):
        problems.append(f"no pide exactamente el activo {asset_id} (pide {want})")
    return problems


def conversation_ticks_used(thread: dict, now_tick: int) -> int:
    start = thread.get("created_tick")
    if start is None:
        ticks = [m.get("tick") for m in thread.get("messages") or [] if m.get("tick") is not None]
        start = min(ticks) if ticks else now_tick
    return max(0, now_tick - int(start))


@dataclass
class Candidate:
    item: str
    name: str
    value: float
    list_price: Optional[int]
    ceiling: int
    owned: int
    tier: int          # 0 = viable al precio publicado, 1 = necesita una rebaja moderada, 2 = descartada
    why: str


def rank_cards(cards: list, f: FastConfig) -> list:
    """cards: dicts {item, name, value, list_price, ceiling, owned}. Ordena TODOS los candidatos: primero las cartas que
    no tenemos y ya son viables al precio publicado (no dependen de un descuento incierto), luego las que necesitan una
    rebaja moderada; dentro de cada grupo, mayor beneficio al precio publicado."""
    out = []
    for c in cards:
        lp, ceiling = c.get("list_price"), c["ceiling"]
        if lp is None:
            tier, why = 2, "la abuela no la vende"
        elif ceiling >= lp:
            tier, why = 0, f"viable al precio publicado ({lp} P <= máximo {ceiling} P)"
        elif ceiling >= lp * f.min_viable_frac:
            tier, why = 1, f"necesita rebaja: publicado {lp} P > máximo {ceiling} P"
        else:
            tier, why = 2, f"máximo {ceiling} P muy por debajo de {lp} P"
        out.append(Candidate(c["item"], c["name"], c["value"], lp, ceiling, c.get("owned", 0), tier, why))
    return sorted(out, key=lambda c: (c.tier, c.owned > 0, -(c.value - (c.list_price or 0)), c.item))


# ------------------------------------------------------------------ validación y liquidación

def find_offer(thread: dict, offer_id: Optional[int]) -> Optional[dict]:
    for m in thread.get("messages") or []:
        o = m.get("offer")
        if o and o.get("id") == offer_id:
            return o
    for o in thread.get("standing_offers") or []:
        if o.get("id") == offer_id:
            return o
    return None


def validate_offer(offer: dict, *, dealer: str, team: Optional[str], thread_id: int, item: str, ceiling: int,
                   cash: int, now_tick: int, resolve_asset: Optional[Callable[[int], Optional[str]]] = None) -> list:
    """Problemas de la estructura de la oferta (lista vacía = se puede aceptar). El texto del hilo no cuenta."""
    problems = []
    give, want = offer.get("give") or {}, offer.get("want") or {}
    if offer.get("maker") != dealer:
        problems.append(f"la hace {offer.get('maker')!r}, no {dealer!r}")
    if team and offer.get("to") != team:
        problems.append(f"va dirigida a {offer.get('to')!r}, no a {team!r}")
    if offer.get("thread") != thread_id:
        problems.append(f"pertenece al hilo {offer.get('thread')!r}, no al {thread_id}")
    if offer.get("status") != "open":
        problems.append(f"no está abierta ({offer.get('status')!r})")
    if offer.get("expires_tick") is not None and offer["expires_tick"] < now_tick:
        problems.append(f"caducada en el tick {offer['expires_tick']}")
    price = want.get("cash")
    if not isinstance(price, int) or price < 1:
        problems.append(f"precio no válido: {price!r}")
    else:
        if price > ceiling:
            problems.append(f"{price} P supera nuestro máximo de {ceiling} P")
        if price > cash:
            problems.append(f"{price} P supera nuestro efectivo ({cash} P)")
    if want.get("assets") or want.get("types"):
        problems.append("además del dinero nos pide cartas o activos")
    if give.get("cash"):
        problems.append("incluye dinero en sentido inesperado")
    types, assets = list(give.get("types") or []), list(give.get("assets") or [])
    if not ((types == [item] and not assets) or
            (not types and len(assets) == 1 and resolve_asset is not None and resolve_asset(assets[0]) == item)):
        problems.append(f"no entrega exactamente {item} (types={types}, assets={assets})")
    return problems


def settled_price(thread: dict, dealer: str) -> Optional[int]:
    """Precio realmente pagado según el servidor: la oferta del hilo con estado "settled" (no nuestra última oferta)."""
    for m in reversed(thread.get("messages") or []):
        o = m.get("offer") or {}
        if o.get("status") == "settled":
            return (o.get("want") or {}).get("cash") if o.get("maker") == dealer else (o.get("give") or {}).get("cash")
    return None


def holdings(assets: list, item: str) -> int:
    return sum(1 for a in assets if f"{a.get('kind')}:{a.get('ref')}" == item)


def settlement_status(pending: dict, thread: dict, holdings_now: int, cash_now: int) -> tuple[str, str]:
    """settled | waiting | failed: una compra solo es definitiva cuando el artículo está en nuestras manos."""
    status = thread.get("status")
    if status == "deal":
        settled = next((m.get("offer") for m in reversed(thread.get("messages") or [])
                        if (m.get("offer") or {}).get("status") == "settled"), None)
        if settled is not None:  # evidencia del servidor; un sobre ya abierto no vuelve a aparecer en el inventario
            return "settled", f"oferta {settled.get('id')} liquidada en el servidor"
        if holdings_now > pending["holdings_before"]:
            paid_ok = pending.get("price") is None or cash_now <= pending["cash_before"] - pending["price"]
            return "settled", "" if paid_ok else "el efectivo no cuadra con el precio (¿otros movimientos?)"
        return "waiting", "trato cerrado, esperando la entrega"
    offer = find_offer(thread, pending.get("offer_id"))
    if status == "open" and offer and offer.get("status") in ("open", "accepted", "queued", "pending"):
        return "waiting", f"oferta {offer.get('status')}: se liquida en el siguiente tick"
    return "failed", f"conversación {status}, oferta {offer.get('status') if offer else 'no encontrada'}"


def outcome_record(st: NegState, *, dealer: str, thread_id: int, status: str, closed_reason: Optional[str],
                   close_price: Optional[int], ceiling: int, est: Optional[Estimate], settled: bool, note: str = "") -> dict:
    offers = [r.ours for r in st.rounds]
    return {
        "dealer": dealer, "item": st.item, "thread": thread_id, "status": status, "closed_reason": closed_reason,
        "context": "quota" if closed_reason in QUOTA_REASONS else "normal",
        "opening": st.opening, "close_price": close_price, "ceiling": ceiling,
        "offers": offers, "replies": [r.reply for r in st.rounds],
        "our_concessions": [b - a for a, b in zip(offers, offers[1:])],
        "her_concessions": [r.before - r.reply for r in st.rounds if r.before is not None and r.reply is not None],
        "turns": st.turns, "estimate": asdict(est) if est else None,
        "saving_vs_opening": st.opening - close_price if st.opening and close_price else None,
        "settled": settled, "note": note,
    }


class Journal:
    """Registro local en JSON Lines, sin credenciales: decisiones, resultados y la aceptación pendiente."""

    def __init__(self, folder: str):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.log = os.path.join(folder, "negotiations.jsonl")
        self.pending_path = os.path.join(folder, "pending.json")

    def append(self, kind: str, rec: dict) -> None:
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "ts": round(time.time()), **rec}, ensure_ascii=False) + "\n")

    def records(self, kind: Optional[str] = None) -> list:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def outcomes(self, dealer: str, item: str) -> list:
        """Comparables: mismo dealer, mismo artículo, sin cierres por cuota o enfriamiento."""
        return [r for r in self.records("outcome")
                if r.get("dealer") == dealer and r.get("item") == item and r.get("context") == "normal"]

    def purchased(self, dealer: str, item: str) -> bool:
        return any(r.get("status") == "deal" and r.get("settled") for r in self.records("outcome")
                   if r.get("dealer") == dealer and r.get("item") == item)

    def known_threads(self) -> set:
        return {r.get("thread") for r in self.records("outcome")}

    def pending(self) -> Optional[dict]:
        if not os.path.exists(self.pending_path):
            return None
        with open(self.pending_path, encoding="utf-8") as f:
            return json.load(f)

    def set_pending(self, pending: dict) -> None:
        tmp = self.pending_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pending, f)
        os.replace(tmp, self.pending_path)

    def clear_pending(self) -> None:
        if os.path.exists(self.pending_path):
            os.remove(self.pending_path)

    def load(self, name: str) -> Optional[dict]:
        path = os.path.join(self.folder, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, name: str, data: dict) -> None:
        path = os.path.join(self.folder, name)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class InstanceLock:
    """Una sola instancia a la vez: data/agent.lock guarda pid y versión; el bloqueo de un proceso muerto se recupera."""

    def __init__(self, path: str, info: dict):
        self.path, self.info = path, {**info, "pid": os.getpid(), "since": round(time.time())}

    def acquire(self) -> Optional[dict]:
        """None si lo hemos obtenido; si no, los datos de la instancia que lo tiene."""
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        holder = json.load(f)
                except (OSError, ValueError):
                    holder = {}
                if holder.get("pid") and pid_alive(int(holder["pid"])):
                    return holder
                os.remove(self.path)  # bloqueo huérfano
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.info, f)
            return None
        return {"pid": "?"}

    def release(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(self.path)
        except (OSError, ValueError):
            pass
