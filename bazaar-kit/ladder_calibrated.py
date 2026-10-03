"""Escalera de vendedores CALIBRADA con los hilos reales (opt-in: --ladder-calibrated [--ladder-profile FICHERO.json]).

La escalera puntúa la fracción del rango de precio de cada vendedor que capturamos (los tres mejores tratos por nivel; un
hueco cuenta 0; los niveles altos pesan más). Nuestra captura era ~0,14 porque aceptábamos cerca de su apertura; los
líderes capturan ~0,75 con la misma receta: apertura extrema, paso corto y paciencia hasta su oferta `final: true`.

Medido en intel/market.db (585 hilos de 17 equipos con los tres vendedores; `ladder_measure.py` lo reproduce y
`dealer_sim.py --replay` valida el modelo contra ellos con un error medio de 0,6-1,2 P por hilo):

  vendedor · modo · rareza     apertura  final:true   mejor  cómo se consiguió el mejor
  abuela · compra · común          12       9 (8-10)     8    t03: abre 4, paso 1, final 8 en la ronda 4
  abuela · compra · poco común     29      24 (20-25)   20    t07: paso 1, 7 rondas (t04/t09/t03: 21-22)
  chato  · compra · rara           97      89 (84-91)   81    t04: abre 55, paso 4, 7 rondas; t09 84 (60, paso 4)
  chato  · compra · poco común     33      27           26    t13: abre 9, paso 3; tras su final 27 contraoferta 26 (2/2)
  abuela · venta  · común           5       6            6    pedir 10 y bajar de 1 en 1: siempre 6
  chato  · venta  · poco común     13      14-15        15    t02: pide 21, baja de 1 en 1, 6 rondas
  pilar  · venta  · poco común     16      17-19        21    t09: pide 30, baja de 1-2, 7 rondas (fuera de SAL/RET)
  pilar  · venta  · poco común SAL/RET 22  23-25        25    t04: pide 40, baja de 1; t10 33; t12 35 → 25 final

Cómo ceden (modelo de dealer_sim.py): Chato concede según un calendario que acelera (1,1,2,3,4,4…) pero nunca más que
nuestro paso («small steps earn small steps»); la Abuela concede 3-4 al principio y luego 1, haga lo que hagamos; Pilar
sube su puja ~1 por ronda tras una o dos rondas planas. La paciencia (rondas hasta `final: true`) varía por conversación.
Pilar FAROLEA: dice «my final courtesy» sin `final: true` y sube en la ronda siguiente (hilos 482, 846, 901, 754). Por
eso aquí SOLO cuenta la marca estructurada `final` de la oferta; el texto nunca cierra ni detiene el regateo.

Reglas de decisión (compra; la venta es simétrica):
  * Primera oferta = `first` × su apertura; después `step` P por ronda; al llegar al objetivo, pasos de 1. Con el
    simulador (rejilla de aperturas y pasos) se ajustaron Chato raras (0,50 y +5, como t04 pero un poco más abajo),
    Abuela poco comunes (objetivo 20, t07) y Pilar (pasos de 2-3).
  * Una oferta NO final se acepta solo si alcanza el objetivo (`target` × su apertura, el mejor trato observado) o si se
    acaban las rondas/ticks propios. Nunca por encima del máximo económico (compra) ni por debajo de nuestro valor
    privado (venta), y nunca a su precio de apertura (no puntúa).
  * Oferta `final: true`: Abuela y Chato aceptaron final-1 en 3 de 3 intentos observados → una sola contraoferta
    final∓1 y, si no contesta en `final_wait_ticks`, se toma su final. Con Pilar no hay muestras: se acepta su final si
    cubre el valor privado.

Las cifras son OBSERVACIONES de rondas anteriores, no reglas del servidor: el valor privado manda siempre.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, fields, replace
from typing import Optional

from negotiation import Decision, NegState

LADDER_DEALERS = ("abuela", "chato", "pilar")
LEVEL_WEIGHT = {"abuela": 1, "chato": 2, "pilar": 3}  # «higher levels weigh more»: el peso exacto no es público
PILAR_FAV_SETS = frozenset({"SAL", "RET"})            # abre a 22 (no a 16) por SAL/RET; su menú `buys` los nombra
BLUFF_RE = re.compile(r"\bfinal\b|last word|última palabra|not a step more|not one more", re.I)


def _floor(x: float) -> int:
    return math.floor(x + 1e-9)


def _ceil(x: float) -> int:
    return math.ceil(x - 1e-9)


@dataclass(frozen=True)
class Profile:
    """Parámetros de un vendedor × modo × rareza, en fracciones de SU precio de apertura en el hilo."""
    first: float                 # compra: nuestra primera oferta / su apertura · venta: nuestra petición / su puja
    step: int                    # paso fijo por ronda (P) hasta el objetivo
    target: float                # aceptar una oferta NO final que llegue aquí (mejor trato observado)
    limit: float                 # extremo estimado del rango (para estimar la captura)
    open_obs: float              # su apertura típica en P (para planificar antes de abrir el hilo)
    counter_final: bool = False  # tras `final: true`: una contraoferta final∓1
    near_step: int = 1           # paso una vez alcanzado el objetivo
    max_rounds: int = 16         # contraofertas propias antes de rendirse (no es la paciencia del vendedor)
    max_ticks: int = 40          # ticks de conversación propios
    final_wait_ticks: int = 1
    obs: str = ""

    def target_price(self, opening: int, mode: str) -> int:
        return _floor(self.target * opening) if mode == "buy" else _ceil(self.target * opening)

    def capture(self, opening: Optional[int], price: Optional[int], mode: str) -> float:
        """Fracción estimada del rango [apertura, extremo] capturada (0 sin trato o al precio de apertura)."""
        if opening is None or price is None:
            return 0.0
        edge = self.limit * opening
        span = (opening - edge) if mode == "buy" else (edge - opening)
        got = (opening - price) if mode == "buy" else (price - opening)
        return 0.0 if span <= 0 or got <= 0 else round(min(1.0, got / span), 3)

    def expected_price(self, mode: str, opening: Optional[float] = None) -> int:
        o = opening if opening is not None else self.open_obs
        return _floor(self.target * o) if mode == "buy" else _ceil(self.target * o)


P = Profile
PROFILES = {
    "abuela|buy|common": P(0.34, 1, 0.67, 0.67, 12, True, obs="58 hilos: apertura 12, final 9, mejor 8 (t03 4/+1)"),
    "abuela|buy|uncommon": P(0.24, 1, 0.70, 0.69, 29, True, obs="89 hilos: apertura 29, final 24, mejor 20 (t07 +1)"),
    "abuela|buy|sobre_barrio": P(0.33, 1, 0.65, 0.63, 30, True, obs="43 hilos: apertura 30, final 21, mejor 19 (t13)"),
    "abuela|buy|*": P(0.34, 1, 0.70, 0.67, 20, True, obs="sin muestras propias: como común"),
    "chato|buy|rare": P(0.50, 5, 0.846, 0.82, 97, True, obs="46 hilos: apertura 97, final 89, mejor 81 (t04 55/+4)"),
    "chato|buy|uncommon": P(0.30, 2, 0.79, 0.76, 33, True, obs="86 hilos: apertura 33, final 27→26 (t13 9/+3)"),
    "chato|buy|sobre_plata": P(0.27, 5, 0.86, 0.85, 188, True, obs="2 hilos: apertura 188, mejor 162 (t08 50/+5)"),
    "chato|buy|*": P(0.56, 3, 0.85, 0.82, 60, True, obs="sin muestras propias"),
    "abuela|sell|common": P(2.0, 1, 1.2, 1.2, 5, obs="89 hilos: apertura 5, final 6 siempre (pedir 10, -1)"),
    "abuela|sell|uncommon": P(2.0, 1, 1.33, 1.42, 12, obs="8 hilos: apertura 12, mejor 17 (t02 24, -1/-3)"),
    "chato|sell|uncommon": P(1.6, 1, 1.15, 1.16, 13, obs="31 hilos: apertura 13, final 14-15, mejor 15 (t02 21/-1)"),
    "chato|sell|rare": P(2.0, 3, 1.15, 1.2, 39, obs="1 hilo (apertura 39): sin datos, prudente"),
    "pilar|sell|uncommon": P(1.9, 2, 1.31, 1.31, 16, obs="53 hilos: apertura 16, final 17-19, mejor 21 (t09 30/-1)"),
    "pilar|sell|uncommon|fav": P(1.6, 3, 1.13, 1.18, 22, obs="SAL/RET: apertura 22, mejor 25 (t04 40/-1, t10 33)"),
    "pilar|sell|rare": P(1.8, 2, 1.15, 1.2, 47, obs="2 hilos: 47→50 (t02 64/-3); 61→71 final (t01 110/-2)"),
    "pilar|sell|rare|fav": P(1.8, 2, 1.15, 1.2, 61, obs="t01: 61→71 final pidiendo 110 y bajando de 2"),
    "pilar|sell|epic": P(1.35, 4, 1.147, 1.15, 122, obs="1 hilo: 122→140 (t08 165/-8)"),
    "*|buy|*": P(0.70, 2, 0.85, 0.80, 30, obs="vendedor sin observar: prudente"),
    "*|sell|*": P(1.6, 1, 1.15, 1.2, 15, obs="vendedor sin observar: prudente"),
}
del P


def load_profiles(path: Optional[str] = None) -> dict:
    """Perfiles medidos + (opcional) un JSON {"clave": {campo: valor}} que sobrescribe campos o añade claves.
    Campos desconocidos -> ValueError (un error tipográfico no debe pasar en silencio)."""
    out = dict(PROFILES)
    if not path:
        return out
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    names = {f.name for f in fields(Profile)}
    for key, over in (raw.get("profiles", raw) if isinstance(raw, dict) else {}).items():
        if not isinstance(over, dict):
            continue
        bad = set(over) - names
        if bad:
            raise ValueError(f"--ladder-profile: campos desconocidos en {key!r}: {sorted(bad)}")
        base = out.get(key) or out["*|buy|*" if "|buy|" in key else "*|sell|*"]
        out[key] = replace(base, **over)
    return out


def profile_for(profiles: dict, dealer: str, mode: str, rarity: Optional[str], fav: bool = False) -> tuple:
    """(clave, perfil): primero la variante «fav» (Pilar SAL/RET), después la rareza, el comodín del vendedor y el
    genérico prudente."""
    r = rarity or "*"
    keys = ([f"{dealer}|{mode}|{r}|fav"] if fav else []) + [f"{dealer}|{mode}|{r}", f"{dealer}|{mode}|*",
                                                           f"*|{mode}|*"]
    for k in keys:
        if k in profiles:
            return k, profiles[k]
    raise KeyError(f"sin perfil para {dealer}|{mode}|{r}")


def fav_sets(dealer_row: Optional[dict]) -> frozenset:
    """Barrios favoritos de un coleccionista: las filas `buys` con lista explícita de barrios; si no hay, SAL/RET."""
    sets = set()
    for row in ((dealer_row or {}).get("menu") or {}).get("buys", []):
        if isinstance(row.get("sets"), list):
            sets |= {str(x).upper() for x in row["sets"]}
    return frozenset(sets) or PILAR_FAV_SETS


def is_fav(dealer: str, set_id: Optional[str], dealer_row: Optional[dict] = None) -> bool:
    return dealer == "pilar" and bool(set_id) and set_id.upper() in fav_sets(dealer_row)


def bluff_final(thread: dict, dealer: str) -> bool:
    """El ÚLTIMO mensaje del vendedor habla de «final» pero su oferta no lleva `final: true` (farol de Pilar)."""
    for m in reversed(thread.get("messages") or []):
        if m.get("sender") != dealer:
            continue
        o = m.get("offer") or {}
        return bool(BLUFF_RE.search(m.get("text") or "")) and not o.get("final")
    return False


# ------------------------------------------------------------------ decisiones

def decide_buy(st: NegState, prof: Profile, ceiling: int, conv_ticks_left: int, now_tick: Optional[int] = None,
               name: str = "calibrada") -> Decision:
    """Compra calibrada. Garantías: nunca supera `ceiling`, nunca paga su apertura, nunca repite ni baja una oferta,
    acepta una oferta NO final solo al llegar al objetivo (o al agotar rondas/ticks propios), una sola contraoferta
    tras su `final: true`."""
    live, n, opening = st.live, st.turns, st.opening

    def valid(price: int) -> bool:
        return price <= ceiling and (opening is None or price < opening)

    def take(why: str) -> Decision:
        return Decision("accept", f"[{name}] {why}", live.price, live.offer_id)

    if live and live.final:
        last = st.rounds[-1] if st.rounds else None
        if st.final_countered and last and last.reply is None and (
                now_tick - last.tick < prof.final_wait_ticks if now_tick is not None else st.awaiting_reply):
            return Decision("wait", f"[{name}] esperando respuesta a nuestra contraoferta final-1")
        p = live.price - 1
        if prof.counter_final and not st.final_countered and valid(p) and p > (st.last_ours or 0) \
                and conv_ticks_left > 0:
            return Decision("counter", f"[{name}] final:true de {live.price} P: contraoferta final-1 una vez", p)
        if valid(live.price):
            return take(f"final:true de {live.price} P dentro del máximo ({ceiling} P)")
        return Decision("abandon", f"[{name}] final:true de {live.price} P no aceptable (máximo {ceiling} P, "
                                   f"apertura {opening} P)")
    if ceiling < 1:
        return Decision("abandon", f"sin margen económico (máximo {ceiling} P)")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha puesto precio")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    target = prof.target_price(opening, "buy")
    if live and valid(live.price) and live.price <= target:
        return take(f"{live.price} P alcanza el objetivo {target} P (mejor trato observado)")
    if conv_ticks_left <= 0 or n >= prof.max_rounds:
        if live and valid(live.price):
            return take(f"rondas/ticks propios agotados; {live.price} P dentro del máximo")
        return Decision("abandon", f"[{name}] {n} contraofertas / plazo agotado y {st.ref_ask} P no es aceptable")
    hi = min(ceiling, st.ref_ask - 1)
    if n == 0:
        p = min(max(1, int(prof.first * opening + 0.5)), hi)
        if p < 1:
            return Decision("abandon", "no hay primera oferta válida")
        return Decision("counter", f"[{name}] apertura extrema {p} P ({prof.first:.0%} de {opening} P; objetivo "
                                   f"{target} P)", p)
    step = prof.step if st.last_ours + prof.step <= target else prof.near_step
    p = min(st.last_ours + step, hi)
    if p <= st.last_ours:
        if live and valid(live.price):
            return take(f"no queda oferta nueva por encima de {st.last_ours} P; {live.price} P es aceptable")
        return Decision("abandon", f"no queda oferta nueva entre {st.last_ours} P y {hi} P")
    return Decision("counter", f"[{name}] paso de {step} P ({st.ref_ask} P vigente, objetivo {target} P; "
                               "paciencia hasta final:true)", p)


def decide_sell(st: NegState, prof: Profile, floor: int, conv_ticks_left: int, now_tick: Optional[int] = None,
                name: str = "calibrada") -> Decision:
    """Venta calibrada (st con side="sell": ref_ask = su puja, last_ours = nuestra petición). Garantías: nunca por
    debajo de `floor` (valor privado de la copia), nunca a su puja de apertura, una puja NO final solo si alcanza el
    objetivo, su `final: true` se acepta si cubre el suelo."""
    live, n, opening = st.live, st.turns, st.opening
    lo = max(floor, (opening + 1) if opening is not None else floor)

    def take(why: str) -> Decision:
        return Decision("accept", f"[{name}] {why}", live.price, live.offer_id)

    if live and live.final:
        last = st.rounds[-1] if st.rounds else None
        if st.final_countered and last and last.reply is None and (
                now_tick - last.tick < prof.final_wait_ticks if now_tick is not None else st.awaiting_reply):
            return Decision("wait", f"[{name}] esperando respuesta a nuestra contraoferta final+1")
        p = live.price + 1
        if prof.counter_final and not st.final_countered and live.price >= lo and \
                (st.last_ours is None or p < st.last_ours) and conv_ticks_left > 0:
            return Decision("counter", f"[{name}] final:true de {live.price} P: contraoferta final+1 una vez", p)
        if live.price >= lo:
            return take(f"final:true de {live.price} P ≥ suelo {lo} P")
        return Decision("abandon", f"[{name}] final:true de {live.price} P bajo el suelo de {lo} P")
    if st.current is None:
        return Decision("wait" if conv_ticks_left > 0 else "abandon", "aún no ha puesto precio")
    if st.awaiting_reply:
        return Decision("wait", "esperando su respuesta")
    target = prof.target_price(opening, "sell")
    if live and live.price >= max(lo, target):
        return take(f"{live.price} P alcanza el objetivo {target} P (mejor trato observado)")
    if conv_ticks_left <= 0 or n >= prof.max_rounds:
        if live and live.price >= lo:
            return take(f"rondas/ticks propios agotados; {live.price} P ≥ suelo {lo} P")
        return Decision("abandon", f"[{name}] {n} peticiones / plazo agotado y su puja ({st.ref_ask} P) no llega a "
                                   f"{lo} P")
    bottom = max(lo, st.ref_ask + 1)
    if n == 0:
        p = max(int(prof.first * opening + 0.5), bottom)
        return Decision("counter", f"[{name}] petición extrema {p} P ({prof.first:.2f}× su puja {opening} P; "
                                   f"objetivo {target} P, suelo {lo} P)", p)
    step = prof.step if st.last_ours - prof.step >= target else prof.near_step
    p = max(st.last_ours - step, bottom)
    if p >= st.last_ours:
        if live and live.price >= lo:
            return take(f"no queda petición nueva por debajo de {st.last_ours} P; {live.price} P ≥ suelo {lo} P")
        return Decision("abandon", f"no queda petición nueva entre {bottom} P y {st.last_ours} P")
    return Decision("counter", f"[{name}] bajamos {st.last_ours - p} P: pedimos {p} P (su puja {st.ref_ask} P, "
                               f"objetivo {target} P; paciencia hasta final:true)", p)


# ------------------------------------------------------------------ plan: 3 tratos por vendedor, puntos por P

@dataclass
class Option:
    dealer: str
    mode: str                 # "buy" | "sell"
    ref: str
    asset: Optional[int]      # venta: copia concreta
    price: int                # precio esperado (objetivo del perfil)
    cash: int                 # caja que consume (compra) o 0 (venta)
    du: float                 # excedente a valores privados (precio - valor perdido · valor ganado - precio)
    capture: float            # captura esperada al precio objetivo
    key: str                  # clave del perfil

    @property
    def gain(self) -> float:
        return LEVEL_WEIGHT.get(self.dealer, 1) * self.capture


def plan(options: list, filled: dict, budget: int, slots: int = 3) -> dict:
    """Asignación óptima (programación dinámica): maximiza Σ peso_nivel × captura de los huecos que faltan por
    vendedor (como mucho `slots` - tratos que ya puntúan), con el excedente ΔU menos la caja inmovilizada como desempate
    (puntos por P: una venta de duplicado no gasta caja) y la caja de las compras ≤ `budget`. Cada carta/copia se usa una vez. Devuelve {"picks": [Option], "gain": x, "cash": y}."""
    groups = {}
    for o in options:
        if o.capture <= 0 or o.du < 0:
            continue
        groups.setdefault((o.mode, o.asset if o.mode == "sell" else o.ref), []).append(o)
    free = {d: max(0, slots - filled.get(d, 0)) for d in LADDER_DEALERS}
    budget = max(0, int(budget))
    # estado: (huecos usados abuela, chato, pilar, caja) -> (valor, picks)
    states = {(0, 0, 0, 0): (0.0, ())}
    idx = {d: i for i, d in enumerate(LADDER_DEALERS)}
    for _, opts in sorted(groups.items(), key=lambda kv: str(kv[0])):
        nxt = dict(states)
        for st, (val, picks) in states.items():
            for o in opts:
                i = idx.get(o.dealer)
                if i is None or st[i] >= free[o.dealer] or st[3] + o.cash > budget:
                    continue
                ns = list(st)
                ns[i] += 1
                ns[3] += o.cash
                ns = tuple(ns)
                v = val + o.gain * 1000 + o.du - o.cash  # a igual captura, la que menos caja inmoviliza (puntos/P)
                if ns not in nxt or v > nxt[ns][0]:
                    nxt[ns] = (v, picks + (o,))
        states = nxt
    best = max(states.values(), key=lambda x: x[0])
    picks = sorted(best[1], key=lambda o: (-LEVEL_WEIGHT.get(o.dealer, 1), -o.gain, o.cash))
    return {"picks": picks, "gain": round(sum(o.gain for o in picks), 3), "cash": sum(o.cash for o in picks),
            "free": free}


def describe(o: Option) -> str:
    verb = "vender" if o.mode == "sell" else "comprar"
    return (f"{o.dealer}: {verb} {o.ref} a ~{o.price} P (captura ~{o.capture:.2f} × peso {LEVEL_WEIGHT.get(o.dealer, 1)}"
            f", ΔU {o.du:+.1f} P{', caja ' + str(o.cash) + ' P' if o.cash else ''})")


def as_dict(p: Profile) -> dict:
    return asdict(p)
