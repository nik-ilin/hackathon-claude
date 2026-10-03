"""Broker para el Market Test que nunca cruza menos que el puesto gratuito y, además, cruza los pares que el puesto
desaprovecha. Se lanza igual que starter_broker.py, sobre un venue `board`:

    BROKER_KEY=bk_... python3 market_broker.py              # modo smart con vigilante y sondeo (por defecto)
    BROKER_KEY=bk_... python3 market_broker.py --mode stall # idéntico al puesto (bench_plan de starter_broker)

Cada tick y en cada run del libro sintético (`b12` en el id `b12-7`):

1. **Suelo.** Calcula el plan del puesto (`starter_broker.bench_plan`: la mejor puja contra el ask más bajo mientras
   cruzan). Toda oferta que el puesto cruzaría queda cruzada también por este broker, aunque quizá con otro socio.
2. **Límites estimados.** Las cotizaciones se alejan de un límite oculto y se relajan al acabarse la paciencia. Se estima
   el límite desde la primera cotización vista (prior de sombreado `shade`), corregido por la relajación observada.
3. **Excedente máximo.** Entre los emparejamientos que cumplen el suelo y cruzan por cotización (el servidor exige
   ask <= precio <= bid), elige el de mayor excedente estimado (Σ límite comprador − Σ límite vendedor) con el algoritmo
   húngaro. Así aprovecha pares que el emparejamiento ordenado del puesto deja sin cruzar (pujas 10 y 8 contra asks 7 y
   9: el puesto cruza 10×7 y para; aquí 10×9 y 8×7) solo cuando el excedente estimado del par extra es positivo.
4. **Precio justo:** punto medio de las cotizaciones, como el puesto. El precio no cambia el excedente total.
5. **Orden de envío:** primero las ofertas con más prisa (las que se relajan deprisa o caducan antes).

El **vigilante** vuelve al plan del puesto durante el resto de la sesión si el planificador falla, si un plan cruza menos
ofertas que el puesto, si el servidor rechaza demasiados emparejamientos propios o si el sombreado aprendido de la sesión
es tan pequeño que los pares extra dejan de compensar.

El **sondeo** (`--probe N`, 3 por tick por defecto; `--probe 0` lo desactiva) intenta además pares sobrantes que no
cruzan por cotización pero sí por límites estimados. Solo sirve si el servidor valida contra límites reales, lo que no
está documentado (el SDK dice ask <= precio <= bid). Un rechazo no cuesta nada, y se apaga solo tras PROBE_STRIKES
rechazos sin ningún acierto.

Las funciones son puras (sin red) y las usa tal cual el banco de pruebas `sim_bench.py`.
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field

from starter_broker import bench_plan, public_plan

SHADE = 0.20          # prior de sombreado: bid ≈ límite × (1 − s), ask ≈ límite × (1 + s)
URGENCY = 0.0         # bonificación por prisa (fracción de la escala del run); 0 = solo ordena el envío
PROBE_MAX = 3         # sondeos por tick como máximo
PROBE_STRIKES = 3     # sondeos rechazados, sin ningún acierto, que apagan el sondeo en la sesión
REFUSAL_LIMIT = 3     # rechazos de emparejamientos propios que activan el vigilante...
REFUSAL_RATE = 0.25   # ...si además superan esta proporción de los enviados en la sesión
PRIOR_WEIGHT = 2      # peso del prior de sombreado frente a cada oferta relajada observada
RELAX_DONE = 0.85     # fracción media del sombreado que ha recorrido una oferta relajada al irse
MIN_SHADE = 0.05      # con al menos MIN_MOVES ofertas relajadas observadas y menos sombreado, vuelve al puesto
MIN_MOVES = 4


# ---------------------------------------------------------------------------------------------------- lectura del libro
def bench_runs(book: dict) -> dict:
    """run -> (asks, bids), cada uno [{id, quote, expires}] en el orden del libro (como lo lee bench_plan)."""
    runs = {}
    for o in book.get("bench_offers") or []:
        asks, bids = runs.setdefault(o["id"].split("-")[0], ([], []))
        side, quote = (asks, o["want"]["cash"]) if o["want"]["cash"] else (bids, o["give"]["cash"])
        side.append({"id": o["id"], "quote": quote, "expires": o.get("expires_tick")})
    return runs


@dataclass
class Seen:
    first_tick: int
    first_quote: int
    quote: int
    tick: int


class Tracker:
    """Historial de cotizaciones por id: de él salen el límite estimado y la prisa de cada oferta del banco.

    El sombreado de la sesión se aprende: una oferta que se relajó y se fue sin cruzar acabó cerca de su límite, así que
    su movimiento total relativo mide el sombreado (prior `shade` con peso PRIOR_WEIGHT mientras haya pocas)."""

    def __init__(self, shade: float = SHADE):
        self.prior, self.seen, self.moves, self.closed = shade, {}, [], set()

    @property
    def shade(self) -> float:
        return (sum(self.moves) / RELAX_DONE + PRIOR_WEIGHT * self.prior) / (len(self.moves) + PRIOR_WEIGHT)

    def update(self, book: dict, tick: int, matched=frozenset()) -> None:
        live = set()
        for o in book.get("bench_offers") or []:
            q = o["want"]["cash"] or o["give"]["cash"]
            live.add(o["id"])
            s = self.seen.get(o["id"])
            if s is None:
                self.seen[o["id"]] = Seen(tick, q, q, tick)
            elif q != s.quote or tick != s.tick:
                s.quote, s.tick = q, tick
        for oid in self.seen.keys() - live - self.closed:  # se fue: si no la cruzamos, su relajación es información
            self.closed.add(oid)
            s = self.seen[oid]
            if oid not in matched and s.quote != s.first_quote:
                self.moves.append(abs(s.quote - s.first_quote) / max(s.quote, s.first_quote))

    def limit(self, oid: str, buyer: bool, quote: int) -> float:
        """Límite estimado, nunca peor que la cotización (un comprador paga al menos su bid, un vendedor acepta su ask)."""
        s = self.seen.get(oid) or Seen(0, quote, quote, 0)
        age, moved = max(s.tick - s.first_tick, 1), quote - s.first_quote
        if buyer:
            est = s.first_quote / (1 - self.shade)
            if moved > 0:  # se ha relajado: le queda al menos otro paso como los que ya ha dado
                est = max(est, quote + moved / age)
            return max(est, quote)
        est = s.first_quote / (1 + self.shade)
        if moved < 0:
            est = min(est, quote + moved / age)
        return min(est, quote)

    def ticks_left(self, oid: str, buyer: bool, quote: int, expires, tick: int) -> float:
        """Ticks que le quedan, estimados: caducidad si el libro la da; si no, ritmo de relajación; si no, desconocido."""
        if expires is not None:
            return max(expires - tick, 0)
        s = self.seen.get(oid)
        if s is None or s.quote == s.first_quote:
            return 8.0  # sin señal: firme o recién llegado
        total = abs(self.limit(oid, buyer, quote) - s.first_quote) or 1
        done = min(abs(quote - s.first_quote) / total, 0.99)
        return max(s.tick - s.first_tick, 1) * (1 - done) / done


# ---------------------------------------------------------------------------------------------------- emparejamiento
def assign_max(w: list) -> list:
    """Asignación de peso máximo (húngaro, O(n²m)). w: n×m con n <= m. Devuelve la columna de cada fila."""
    n, m, inf = len(w), len(w[0]), float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0, minv, used = i, 0, [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0], i0, delta, j1 = True, p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = -w[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    res = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            res[p[j] - 1] = j - 1
    return res


def best_matching(bids: list, asks: list, gain, edge) -> list:
    """Emparejamiento de peso máximo: [(i bid, j ask)] solo con aristas válidas (edge) de peso gain(i, j) > 0."""
    if not bids or not asks:
        return []
    w = [[gain(i, j) if edge(i, j) else 0.0 for j in range(len(asks))] for i in range(len(bids))]
    w = [[x if x > 0 else 0.0 for x in row] for row in w]
    if len(bids) <= len(asks):
        pairs = [(i, j) for i, j in enumerate(assign_max(w))]
    else:
        wt = [list(col) for col in zip(*w)]
        pairs = [(i, j) for j, i in enumerate(assign_max(wt))]
    return [(i, j) for i, j in pairs if j >= 0 and edge(i, j) and w[i][j] > 0]


@dataclass
class Match:
    sell: str
    buy: str
    price: int
    kind: str = "smart"     # smart (cruza por cotización), probe (solo por límites estimados) o stall
    urgency: float = 0.0
    surplus: float = 0.0    # excedente estimado entre límites


def smart_plan(book: dict, tracker: Tracker, tick: int, *, floor: str = "cover", urgency: float = URGENCY,
               probe: int = 0, exclude=frozenset()) -> list:
    """Plan del banco. floor: 'cover' (cruza toda oferta que el puesto cruzaría), 'count' (al menos tantos pares como el
    puesto por run) o 'none' (solo excedente estimado). probe: sondeos fuera de cotización como máximo."""
    stall_ids = {x for s, b, _ in bench_plan(book) for x in (s, b)}
    plan, probes = [], []
    for run, (asks, bids) in sorted(bench_runs(book).items()):
        asks = [a for a in asks if a["id"] not in exclude]
        bids = [b for b in bids if b["id"] not in exclude]
        if not asks or not bids:
            continue
        lb = [tracker.limit(b["id"], True, b["quote"]) for b in bids]
        la = [tracker.limit(a["id"], False, a["quote"]) for a in asks]
        ub = [1 / (1 + tracker.ticks_left(b["id"], True, b["quote"], b["expires"], tick)) for b in bids]
        ua = [1 / (1 + tracker.ticks_left(a["id"], False, a["quote"], a["expires"], tick)) for a in asks]
        quotes = sorted(x["quote"] for x in asks + bids)
        scale = quotes[len(quotes) // 2] or 1
        est = [[lb[i] - la[j] + urgency * scale * (ub[i] + ua[j]) for j in range(len(asks))] for i in range(len(bids))]
        big = 1 + sum(max(x, 0) for row in est for x in row) + scale * len(bids) * len(asks)
        cross = lambda i, j: bids[i]["quote"] >= asks[j]["quote"]  # noqa: E731
        if floor == "cover":
            gain = lambda i, j: est[i][j] + big * ((bids[i]["id"] in stall_ids) + (asks[j]["id"] in stall_ids))  # noqa: E731
        elif floor == "count":
            k = sum(1 for s, b, _ in bench_plan({"bench_offers": [o for o in book["bench_offers"]
                                                                  if o["id"].split("-")[0] == run]}))
            gain = (lambda i, j: est[i][j] + big) if k else (lambda i, j: est[i][j])  # noqa: E731
        else:
            gain = lambda i, j: est[i][j]  # noqa: E731
        pairs = best_matching(bids, asks, gain, cross)
        if floor == "count" and k and len(pairs) < k:  # imposible con peso big por par, pero el suelo manda
            pairs = []
        used_b, used_a = set(), set()
        for i, j in pairs:
            bid, ask = bids[i]["quote"], asks[j]["quote"]
            plan.append(Match(asks[j]["id"], bids[i]["id"], (ask + bid) // 2, "smart", max(ub[i], ua[j]), lb[i] - la[j]))
            used_b.add(i)
            used_a.add(j)
        if probe:
            rb = [i for i in range(len(bids)) if i not in used_b]
            ra = [j for j in range(len(asks)) if j not in used_a]
            margin = 0.05 * scale
            more = best_matching([bids[i] for i in rb], [asks[j] for j in ra],
                                 lambda x, y: lb[rb[x]] - la[ra[y]] - margin,
                                 lambda x, y: bids[rb[x]]["quote"] < asks[ra[y]]["quote"])
            for x, y in more:
                i, j = rb[x], ra[y]
                price = round((lb[i] + la[j]) / 2)
                if la[j] <= price <= lb[i]:
                    probes.append(Match(asks[j]["id"], bids[i]["id"], price, "probe", max(ub[i], ua[j]), lb[i] - la[j]))
    plan.sort(key=lambda m: (-m.urgency, -m.surplus))
    probes.sort(key=lambda m: -m.surplus)
    return plan + probes[:probe]


# ---------------------------------------------------------------------------------------------------- vigilante
@dataclass
class Watchdog:
    """Vuelve al plan del puesto durante el resto de la sesión ante cualquier señal de que el broker rinde menos."""
    mode: str = "smart"
    reason: str = ""
    sent: int = 0
    refused: int = 0
    probe_ok: int = 0
    probe_refused: int = 0
    errors: int = 0
    log: list = field(default_factory=list)

    def trip(self, why: str) -> None:
        if self.mode != "stall":
            self.mode, self.reason = "stall", why
            self.log.append(why)

    def check_plan(self, book: dict, plan: list, floor: str) -> None:
        """El plan cruza al menos tantas ofertas como el puesto y, con suelo 'cover', todas las suyas."""
        stall = bench_plan(book)
        ours = {x for m in plan if m.kind == "smart" for x in (m.sell, m.buy)}
        theirs = {x for s, b, _ in stall for x in (s, b)}
        if len(ours) < len(theirs) or (floor == "cover" and not theirs <= ours):
            self.trip(f"plan con menos cruces que el puesto ({len(ours) // 2} < {len(theirs) // 2})")

    def check_shade(self, tracker: Tracker) -> None:
        """Los pares extra son una apuesta a que el sombreado supera el hueco entre cotizaciones: sin sombreado, no."""
        seen = sum(tracker.moves) / RELAX_DONE / max(len(tracker.moves), 1)  # solo la evidencia, sin el prior
        if len(tracker.moves) >= MIN_MOVES and seen < MIN_SHADE:
            self.trip(f"sombreado observado {seen:.1%} < {MIN_SHADE:.0%}: los pares extra no compensan")

    def feedback(self, m: Match, ok: bool) -> None:
        if m.kind == "probe":
            self.probe_ok += ok
            self.probe_refused += not ok
            return
        self.sent += 1
        self.refused += not ok
        if self.refused >= REFUSAL_LIMIT and self.refused > REFUSAL_RATE * self.sent:
            self.trip(f"{self.refused} de {self.sent} emparejamientos rechazados")

    @property
    def probing(self) -> bool:
        return self.probe_ok > 0 or self.probe_refused < PROBE_STRIKES


class BenchBroker:
    """Estado de una sesión de Market Test: historial, vigilante y emparejamientos enviados pendientes de liquidar."""

    def __init__(self, mode: str = "smart", floor: str = "cover", probe: int = 0, urgency: float = URGENCY,
                 shade: float = SHADE, watchdog: bool = True):
        self.mode, self.floor, self.probe, self.urgency, self.shade, self.use_watchdog = \
            mode, floor, probe, urgency, shade, watchdog
        self.new_session()

    def new_session(self) -> None:
        self.tracker, self.dog, self.pending, self.matched = Tracker(self.shade), Watchdog(), set(), set()

    def plan(self, book: dict, tick: int) -> list:
        """[Match] para este estado del libro. Tras una sesión (libro del banco vacío) empieza otra desde cero."""
        live = {o["id"] for o in book.get("bench_offers") or []}
        if not live:
            if self.tracker.seen:
                self.new_session()
            return []
        self.pending &= live  # lo enviado que sigue en el libro aún no se ha liquidado: no se reenvía
        view = {**book, "bench_offers": [o for o in book["bench_offers"] if o["id"] not in self.pending]}
        self.tracker.update(book, tick, self.matched)
        if self.use_watchdog:
            self.dog.check_shade(self.tracker)
        if self.mode == "stall" or (self.use_watchdog and self.dog.mode == "stall"):
            return [Match(s, b, p, "stall") for s, b, p in bench_plan(view)]
        try:
            probe = self.probe if self.dog.probing else 0
            plan = smart_plan(view, self.tracker, tick, floor=self.floor, urgency=self.urgency, probe=probe)
        except Exception as e:  # noqa: BLE001 — cualquier fallo del planificador: el puesto, nunca nada
            self.dog.errors += 1
            self.dog.trip(f"error del planificador: {e!r}")
            return [Match(s, b, p, "stall") for s, b, p in bench_plan(view)]
        if self.use_watchdog:
            self.dog.check_plan(view, plan, self.floor)
            if self.dog.mode == "stall":
                return [Match(s, b, p, "stall") for s, b, p in bench_plan(view)]
        return plan

    def feedback(self, m: Match, ok: bool) -> None:
        if ok:
            self.pending |= {m.sell, m.buy}
            self.matched |= {m.sell, m.buy}
        if m.kind != "stall":
            self.dog.feedback(m, ok)


# ---------------------------------------------------------------------------------------------------- bucle real
def main() -> None:
    from bazaar_sdk import BazaarError, Broker
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=["smart", "stall"], default="smart")
    ap.add_argument("--floor", choices=["cover", "count", "none"], default="cover")
    ap.add_argument("--probe", type=int, default=PROBE_MAX, help="sondeos por tick fuera de cotización (0 = ninguno)")
    args = ap.parse_args()
    bb = BenchBroker(args.mode, args.floor, min(args.probe, PROBE_MAX))
    broker, seen = Broker(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BROKER_KEY"]), None
    while True:  # dos lecturas por segundo, como starter_broker
        try:
            tick, book = broker.clock()["tick"], broker.book()
            now = (tick, [o["id"] for o in (book.get("bench_offers") or []) + (book.get("offers") or [])])
            if now != seen:
                seen = now
                mode = bb.dog.mode
                for m in bb.plan(book, tick):
                    try:
                        broker.match(m.sell, m.buy, m.price)
                        bb.feedback(m, True)
                    except BazaarError as e:
                        bb.feedback(m, False)
                        print(f"tick {tick}: {m.kind} {m.sell} x {m.buy} a {m.price} rechazado ({e})")
                if bb.dog.mode != mode:
                    print(f"tick {tick}: VIGILANTE → plan del puesto ({bb.dog.reason})")
                for sell, buy, price in public_plan(book):
                    try:
                        broker.match(sell, buy, price)
                    except BazaarError as e:
                        print(f"tick {tick}: {sell} x {buy} a {price} rechazado ({e})")
        except BazaarError as e:
            print(f"no se puede leer el libro ({e}), reintento")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
