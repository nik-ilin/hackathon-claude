"""Mejoras OPT-IN del coordinador para la escalera de vendedores y la selección de contrapartes (sin llamadas a la API).

Se apoyan en la escalera de #8 (negotiation.LadderConfig / decide_ladder, ventas `dealer_sell_*` del coordinador) en
vez de duplicarla. Cada pieza va detrás de su flag; sin ellos el coordinador se comporta exactamente igual que antes.

  --deny-teams t12,t13,t14 [--deny-margin N]
        No publicar ofertas dirigidas a, ni aceptar ofertas de, ni negociar campañas con esos equipos. Con
        --deny-margin N se permite SOLO si nuestro excedente a valores privados (ΔU) es ≥ N P.
        Motivo (ESTRATEGIA_TOP3 §1): la principal contraparte P2P de t12 somos nosotros (4 de 8 tratos).
        Límite honesto: una publicación PÚBLICA (sin `to`) la puede aceptar cualquiera, también un equipo denegado.

  --pilar-sell SET[:MULT]   (p. ej. SAL:1.25, SAL,MAL:1.1 o *)
        Vender a Doña Pilar (nivel 3; compra poco comunes y raras). Observado: abre ~16 y sube ~1 cada 1-2 rondas;
        final mediano 17-18 y hasta 25 con la «Salamanca fever» (+25 % sobre libro en SAL). t10 lo sacó pidiendo 33 y
        bajando de 2 en 2; t04 pidiendo 40 y bajando de 1 en 1; t14 19-20 bajando de 29 de 2 en 2. Por defecto: pedir 33
        (--pilar-open) y bajar de 2 en 2 (--pilar-step); aceptar su final si ≥ nuestro valor privado; un hilo por carta;
        nunca una copia que mantiene una página completa (page_guard); objetivo 3 tratos NEGOCIADOS (cierre > su
        apertura). La última copia de un barrio del modo se vende solo con --pilar-last-copy o --allow-last-copy SET.
        Ventana opcional --pilar-from-tick / --pilar-until-tick (la fiebre: ≈14:55 → 17:13).

  --ladder-fill
        Completar 3 tratos negociados por vendedor desbloqueado (abuela, chato, pilar) antes que pujas pasivas: activa la
        escalera de #8 (--dealer-ladder) con las aperturas de ESTRATEGIA_TOP3 (Abuela comunes 5-7 con paso 1; Chato raras
        ~0,74 de su apertura con paso 3; Chato poco comunes baja prioridad: su final es 31 casi siempre), aparta caja
        para el siguiente trato de cada escalera incompleta y, si no se pasó --pilar-sell, vende a Pilar a precio mediano.

Las cifras de los vendedores son OBSERVACIONES de rondas anteriores, no reglas del servidor: el valor privado manda.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

import negotiation as neg
from negotiation import Decision, NegState, LADDER_SLOTS

PILAR = "pilar"
PILAR_RARITIES = ("uncommon", "rare")    # lo que compra según su anuncio; el menú `buys` manda si existe
PILAR_MEDIAN_FINAL = 17.5                # final mediano observado (17-18) sin fiebre
PILAR_OPEN = 33                          # t10: pidió 33 y bajó de 2 en 2 hasta 25 en la fiebre
PILAR_STEP = 2
PILAR_MAX_COUNTERS = 10
PILAR_MAX_TICKS = 20
LADDER_DEALERS = ("abuela", "chato", PILAR)


# ------------------------------------------------------------------ equipos denegados

def norm_team(t) -> Optional[str]:
    """'T012' / 't12' / ' t12 ' -> 't12'. Lo que no parece un id de equipo se devuelve en minúsculas."""
    if t is None:
        return None
    s = str(t).strip().lower()
    m = re.fullmatch(r"t0*(\d+)", s)
    return f"t{int(m.group(1))}" if m else s


def parse_deny_teams(spec) -> frozenset:
    return frozenset(norm_team(x) for x in str(spec or "").split(",") if x.strip())


SKIP_DENY = {"cancel", "team_cancel", "team_close", "info", "dealer_open", "dealer_counter", "dealer_accept",
             "dealer_close", "dealer_sell_open", "dealer_sell_counter", "dealer_sell_accept"}


def counterparty_of(c: dict) -> Optional[str]:
    """Equipo al otro lado de una candidata: destinatario (`to`), autor de la oferta que aceptamos (`maker`) o equipo
    de la campaña (`team` / `opp.team`)."""
    for k in ("to", "maker", "team", "buyer"):
        if c.get(k):
            return norm_team(c[k])
    opp = c.get("opp") or {}
    return norm_team(opp.get("team")) if opp.get("team") else None


def surplus_of(c: dict) -> Optional[float]:
    for k in ("du", "expected_du", "du_est"):
        if isinstance(c.get(k), (int, float)):
            return float(c[k])
    opp = c.get("opp") or {}
    return float(opp["du_est"]) if isinstance(opp.get("du_est"), (int, float)) else None


def deny_blockers(c: dict, deny: frozenset, margin: Optional[float] = None) -> list:
    """[] si la candidata puede seguir; si no, el motivo. Cancelar y cerrar nunca se bloquea."""
    if not deny or c.get("type") in SKIP_DENY:
        return []
    team = counterparty_of(c)
    if team not in deny:
        return []
    du = surplus_of(c)
    if margin is not None and du is not None and du >= margin:
        c.setdefault("notes", []).append(f"--deny-teams: {team} permitido por excedente {du} P ≥ {margin} P")
        return []
    return [f"--deny-teams: {team} denegado" + (f" (excedente {du} P < --deny-margin {margin} P)"
                                                if margin is not None else "")]


def apply_deny(cands: list, deny: frozenset, margin: Optional[float] = None) -> int:
    n = 0
    for c in cands:
        b = deny_blockers(c, deny, margin)
        if b:
            c["blockers"] = list(c.get("blockers") or []) + b
            n += 1
    return n


# ------------------------------------------------------------------ Doña Pilar: venta

@dataclass
class PilarConfig:
    sets: frozenset                  # vacío = todos los barrios
    mult: float = 1.0                # fiebre SAL: 1.25 sobre su precio habitual
    from_tick: Optional[int] = None
    until_tick: Optional[int] = None
    dealer: str = PILAR
    open_ask: int = PILAR_OPEN
    step: int = PILAR_STEP
    max_counters: int = PILAR_MAX_COUNTERS
    max_ticks: int = PILAR_MAX_TICKS
    last_copy: bool = False          # --pilar-last-copy: vender también la última copia de los barrios del modo
    target_deals: int = LADDER_SLOTS

    def in_window(self, tick: int) -> bool:
        return (self.from_tick is None or tick >= self.from_tick) and (self.until_tick is None or tick <= self.until_tick)

    def covers(self, set_id: str) -> bool:
        return not self.sets or set_id in self.sets


def parse_pilar_sell(spec) -> Optional[PilarConfig]:
    """'SAL:1.25' -> sets {SAL}, mult 1.25 · 'SAL,MAL' -> mult 1.0 · '*' o 'ALL' -> todos los barrios."""
    if spec in (None, ""):
        return None
    left, _, mult = str(spec).partition(":")
    try:
        m = float(mult) if mult.strip() else 1.0
    except ValueError:
        raise ValueError(f"--pilar-sell mal formado: {spec!r} (formato SET[:MULT], p. ej. SAL:1.25)")
    if not 0.5 <= m <= 3.0:
        raise ValueError(f"--pilar-sell: MULT {m} fuera de [0.5, 3]")
    sets = frozenset(x.strip().upper() for x in left.split(",") if x.strip())
    if sets & {"*", "ALL", "TODOS"}:
        sets = frozenset()
    return PilarConfig(sets=sets, mult=m)


def is_sell_topic(topic) -> bool:
    return isinstance(topic, dict) and isinstance(topic.get("sell"), dict)


def last_copy_allowed(ref: str, set_id: str, allow: set, cfg: PilarConfig) -> bool:
    """--allow-last-copy acepta refs (SAL-06) o barrios enteros (SAL); --pilar-last-copy, los barrios del modo."""
    return ref in allow or set_id in allow or (cfg.last_copy and bool(cfg.sets) and set_id in cfg.sets)


def qualifying_sell_deals(dealer: str, threads: list = (), outcomes: list = ()) -> list:
    """Ventas a `dealer` liquidadas y NEGOCIADAS (cobramos MÁS que su primera puja)."""
    out = {}
    for t in threads or []:
        if t.get("with") != dealer or t.get("status") != "deal" or not is_sell_topic(t.get("topic")):
            continue
        st = neg.state_from_thread(t, dealer, 10 ** 9, neg.Config(), side="sell")
        got = neg.settled_sell_price(t, dealer)
        if got is not None and st.opening is not None and got > st.opening:
            out[t.get("id")] = {"thread": t.get("id"), "side": "sell", "opening": st.opening, "close": got,
                                "source": "servidor"}
    for r in outcomes or []:
        if r.get("dealer") != dealer or r.get("side") != "sell" or r.get("status") != "deal" or not r.get("settled"):
            continue
        op, cl = r.get("opening"), r.get("close_price")
        if op is not None and cl is not None and cl > op and r.get("thread") not in out:
            out[r.get("thread")] = {"thread": r.get("thread"), "side": "sell", "opening": op, "close": cl,
                                    "source": "diario"}
    return sorted(out.values(), key=lambda x: str(x["thread"]))


def expected_price(card: dict, cfg: PilarConfig, buys_row: Optional[dict] = None) -> int:
    """Precio esperado de Pilar (para decidir si merece la pena abrir y para ordenar): `list_price` de su fila `buys`
    si existe; si no, el final mediano observado (17-18), × MULT en los barrios de la fiebre (hasta ~22-25)."""
    base = (buys_row or {}).get("list_price") or PILAR_MEDIAN_FINAL
    return int(math.ceil(base * (cfg.mult if cfg.covers(card.get("set", "")) else 1.0)))


def first_ask(floor: int, cfg: PilarConfig) -> int:
    return max(floor, cfg.open_ask)


def decide_sell(st: NegState, floor: int, first: int, cfg: PilarConfig, conv_ticks_left: int) -> Decision:
    """Venta a un vendedor (st de neg.state_from_thread(side="sell"): ref_ask = su puja, last_ours = nuestra petición).
    Pedimos `first` y bajamos de `cfg.step` en `cfg.step`, nunca por debajo de `floor` (valor privado que perdemos) ni
    de su puja vigente + 1. Final: aceptar si ≥ floor. Su puja de APERTURA solo se toma si es su final o se agotan las
    rondas (vende ≥ valor, pero no puntúa en la escalera)."""
    live, n = st.live, st.turns

    def take(why: str) -> Decision:
        note = " (es su precio de apertura: no cuenta para la escalera)" if st.opening is not None and \
            live.price <= st.opening else ""
        return Decision("accept", why + note, live.price, live.offer_id)

    if live and live.final:
        return take(f"oferta final de {live.price} P ≥ nuestro valor privado {floor} P") if live.price >= floor else \
            Decision("abandon", f"oferta final de {live.price} P por debajo de nuestro valor privado ({floor} P)")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha puesto precio")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    negotiated = live is not None and st.opening is not None and live.price > st.opening
    out_of_time = conv_ticks_left <= 0 or n >= cfg.max_counters
    if live and live.price >= floor:
        if st.last_ours is not None and st.last_ours - live.price <= cfg.step and negotiated:
            return take(f"brecha de {st.last_ours - live.price} P ≤ paso {cfg.step}: cerramos en {live.price} P")
        if out_of_time:
            return take(("rondas agotadas" if n >= cfg.max_counters else "plazo agotado") +
                        f"; {live.price} P ≥ valor privado {floor} P")
    if out_of_time:
        return Decision("abandon", f"{n} peticiones / plazo agotado y su puja ({st.ref_ask} P) no cubre {floor} P")
    bid = st.ref_ask or 0
    lo = max(floor, bid + 1)
    p = max(first if st.last_ours is None else st.last_ours - cfg.step, lo)
    if st.last_ours is not None and p >= st.last_ours:
        if live and live.price >= floor:
            return take(f"no queda una petición nueva por debajo de {st.last_ours} P; {live.price} P es aceptable")
        return Decision("abandon", f"no queda petición nueva entre {lo} P y {st.last_ours} P")
    return Decision("counter", ("apertura alta" if st.last_ours is None else f"bajamos {cfg.step} P") +
                    f": pedimos {p} P (su puja {bid} P, nuestro mínimo {floor} P)", p)


# ------------------------------------------------------------------ --ladder-fill sobre la escalera de #8

FILL_PRIORITY = {("abuela", "common"): 10 ** 4, ("chato", "rare"): 10 ** 4, ("chato", "uncommon"): -500}


def top3_ladder_config(lc: "neg.LadderConfig") -> "neg.LadderConfig":
    """Aperturas y pasos de ESTRATEGIA_TOP3 sobre la LadderConfig de #8 (solo lo que difiere):
    Abuela comunes abrir a ~0,5 de su 12 (5-7, t18/t14) con paso 1 (ya es el de #8);
    Chato raras abrir a 0,74 de su apertura (t17: 72 frente a 97) con paso 3 (#8 usa 0,70 y +4)."""
    lc.abuela_open = dict(lc.abuela_open, common=0.5)
    lc.chato_open = dict(lc.chato_open, rare=0.74)
    lc.chato_steps = dict(lc.chato_steps, rare=3)
    return lc


def fill_priority(dealer: str, rarity: Optional[str]) -> int:
    """Prioridad de apertura con --ladder-fill (no es valor en primas): Abuela comunes y Chato raras primero; Chato
    poco comunes al final (su final es 31 casi siempre: poco rango que capturar)."""
    return FILL_PRIORITY.get((dealer, rarity), 0)
