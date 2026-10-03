"""De lo observado a una política de regateo, y su backtest.

Capa sobre `feed_oracle`: lo importa en sólo-lectura y no modifica nada suyo.
Lógica pura, biblioteca estándar, sin red.

El oráculo responde «qué se ha visto». Esto responde «con qué precio abro,
con qué paso cedo, cuántas rondas aguanto y cuándo me levanto», y —lo que
importa— mide esa política contra los regateos que de verdad pasaron en el
feed, en vez de presentarla como buena porque suena razonable.

Qué se puede medir y qué no (leer antes de creerse una cifra):

- El dealer real respondió a las concesiones del *otro* equipo, no a las
  nuestras. Reproducir su escalera contra nuestro plan es una aproximación,
  no una contrafactual válida. `simulate` lo dice en su docstring y el
  backtest lo marca en cada fila.
- El backtest es **dentro de muestra**: el plan se deriva del mismo historial
  que contiene el baile que se evalúa, así que el suelo ya «sabe» cómo acabó.
  Es una cota optimista del plan, no una predicción.
- Un suelo visto es una **cota**, no una promesa: el verdadero límite del
  dealer puede estar más abajo, y cada conversación tiene el suyo, secreto.
  De ahí que el campo se llame `floor_seen` y no `floor`.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from feed_oracle import Dance, Oracle, Settled, is_team, read_settlements

#: Orden de fiabilidad de `DealerLine.confidence` / `advise()["confidence"]`.
#: Mayor es mejor; sirve para ordenar el playbook, no para ponderar precios.
CONFIDENCE_RANK = {
    "SETTLED": 5, "FINAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1,
    "RARITY": 1, "NONE": 0,
}

#: Por debajo de esto una media es una anécdota. No cambia ningún cálculo:
#: sólo hace que el informe lo diga en vez de dejarlo en la letra pequeña.
THIN_SAMPLE = 8


# ----------------------------------------------------------------- historial

def read_history(path: str | Path) -> Iterator[dict]:
    """Eventos de `data/feed_history.jsonl`, una línea JSON cada uno.

    Es lectura de disco, no de red: el recolector es `feed_watch.py`. Las
    líneas corruptas se saltan en silencio porque el archivo se escribe en
    caliente y la última puede estar a medias.
    """
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ------------------------------------------------------- el cierre observado

@dataclass(frozen=True)
class Close:
    """El precio al que un baile acabó de verdad, según una liquidación.

    Hace falta porque la escalera del dealer **no** basta para saber cómo
    acabó: en el feed real abuela cotiza 10 y acepta la puja de 9 del equipo,
    así que su cotización más baja es una cota superior del precio pagado, no
    el precio. La liquidación sí es evidencia dura.
    """
    thread: int
    price: int
    tick: int


def match_closes(oracle: Oracle, events: Iterable[dict]) -> dict[int, Close]:
    """Empareja cada baile con la liquidación dealer↔equipo que lo cerró.

    Clave: misma carta, mismo dealer, mismo equipo y un tick dentro del
    tramo de la conversación (con holgura, porque una operación se asienta en
    el tick siguiente). Un baile sin liquidación queda fuera: no sabemos si
    acabó en trato, y suponerlo inflaría la muestra.
    """
    #: Los traspasos entre equipos no dicen nada del suelo de un dealer.
    dealer_settles: list[Settled] = [
        s for s in read_settlements(events)
        if not (is_team(s.seller) and is_team(s.buyer))
    ]
    out: dict[int, Close] = {}
    for thread, d in oracle.dances.items():
        if not d.dealer or not d.team or not d.moves:
            continue
        first, last = d.moves[0].tick, d.moves[-1].tick
        for s in dealer_settles:
            if s.ref != d.ref:
                continue
            parties = (s.seller, s.buyer)
            if d.dealer not in parties or d.team not in parties:
                continue
            if first <= s.tick <= last + CLOSE_TICK_SLACK:
                out[thread] = Close(thread=thread, price=s.price, tick=s.tick)
                break
    return out


#: Una operación aceptada se asienta en el tick siguiente, y el último mensaje
#: del hilo puede haberse recogido antes. Tres ticks de holgura cubren eso sin
#: llegar a capturar una conversación posterior por la misma carta.
CLOSE_TICK_SLACK = 3


def dance_side(dance: Dance) -> Optional[str]:
    """El sentido del dealer en este baile: `ask` nos vende, `bid` nos compra.

    Se toma del primer movimiento del dealer y no del equipo, porque es el
    dealer quien fija el sentido del precio que vamos a leer.
    """
    for q in dance.moves:
        if q.is_dealer:
            return q.side
    return None


# ----------------------------------------------------------------- el plan

@dataclass(frozen=True)
class Plan:
    """Una política de regateo concreta para (dealer, carta, sentido).

    Todo campo numérico sale de evidencia del feed. `basis` dice de dónde:
    `observed` si hay línea propia de esa carta, `rarity` si se generalizó
    por rareza. Sin ninguna de las dos no hay Plan (`plan_for` → None).
    """
    dealer: str
    ref: str
    side: str                       # "ask": nos la vende | "bid": nos la compra
    floor_seen: int                 #: mejor precio VISTO. Cota, no promesa.
    open_at: int                    #: con qué abrimos
    step: int                       #: tamaño de cada concesión nuestra
    walk_away: int                  #: peor precio que aún aceptamos
    never_accept: Optional[int]     #: su apertura: cerrar ahí no puntúa
    max_rounds: Optional[int]       #: rondas a aguantar; None = sin dato
    confidence: str
    observations: int
    basis: str                      # "observed" | "rarity"

    @property
    def spread(self) -> Optional[int]:
        """Primas en juego: de su apertura a su suelo visto.

        Es lo que se gana por no aceptar lo primero que dice. Sin apertura
        observada no hay cifra.
        """
        if self.never_accept is None:
            return None
        return abs(self.never_accept - self.floor_seen)

    def quote(self, round_index: int) -> int:
        """Nuestro precio en la ronda `round_index` (0 = la apertura).

        Pasos iguales y topados en `walk_away`: «small steps earn small
        steps», y pasarse del abandono convierte el plan en una subasta
        contra uno mismo.
        """
        drift = self.step * max(0, round_index)
        if self.side == "ask":                      # compramos: subimos
            return min(self.open_at + drift, self.walk_away)
        return max(self.open_at - drift, self.walk_away)   # vendemos: bajamos

    def acceptable(self, price: int) -> bool:
        """¿Cerraríamos a este precio sin cruzar el abandono?"""
        return price <= self.walk_away if self.side == "ask" else price >= self.walk_away


def plan_for(oracle: Oracle, dealer: str, ref: str, *, side: str = "ask",
             tolerance: int = 0, rounds_cap: Optional[int] = None) -> Optional[Plan]:
    """Deriva el plan de `Oracle.advise()` más los pares de concesión.

    - `open_at` viene del oráculo: un paso por debajo del suelo visto, para
      tener algo que ceder sin cruzarlo. Se fuerza además a ser estrictamente
      mejor que su apertura, porque **un trato al precio de apertura de un
      dealer no cuenta para la escalera** (RULES.md, «Dealers»).
    - `step` es el paso nuestro que más concesión ajena compró por prima
      cedida (`Response.best_step`), no un porcentaje inventado.
    - `max_rounds` es la mediana de rondas que ese dealer aguantó antes de
      nombrar su oferta final; si nunca se le ha visto llegar al final, se usa
      la conversación más larga observada, que es una cota inferior.
    - `tolerance` ensancha el abandono por encima del suelo visto. Por defecto
      0: no pagar más de lo que otro ya consiguió. Subirlo cierra más tratos y
      paga de más; el backtest mide exactamente ese canje.

    Sin suelo ni paso observados devuelve None. Preferimos no opinar a dar una
    cifra plausible.
    """
    a = oracle.advise(dealer, ref, side=side)
    floor, step = a["floor"], a["step"]
    if floor is None or step is None or a["open_at"] is None:
        return None

    line = oracle.dealer_floor(dealer, ref, side)
    basis = "observed" if line and line.n else "rarity"

    never = a["never_accept"]
    open_at = a["open_at"]
    if never is not None:
        # Nunca abrir en su apertura ni peor: ese cierre no puntúa.
        open_at = min(open_at, never - 1) if side == "ask" else max(open_at, never + 1)
    open_at = max(1, open_at)

    walk = floor + tolerance if side == "ask" else max(1, floor - tolerance)

    typical = a["rounds_before_final"]
    pa = oracle.patience.get(dealer)
    if typical is not None:
        max_rounds = int(math.ceil(typical))
    elif pa and pa.seen_longer:
        max_rounds = pa.seen_longer
    else:
        max_rounds = None               # sin dato: no se inventa un número
    if rounds_cap is not None:
        max_rounds = rounds_cap if max_rounds is None else min(max_rounds, rounds_cap)

    return Plan(
        dealer=dealer, ref=ref, side=side, floor_seen=floor, open_at=open_at,
        step=step, walk_away=walk, never_accept=never, max_rounds=max_rounds,
        confidence=a["confidence"], observations=a["observations"], basis=basis,
    )


# -------------------------------------------------------------- simulación

@dataclass(frozen=True)
class SimResult:
    """Lo que el plan habría hecho en un baile histórico.

    `price=None` significa sin trato: o se agotaron las rondas (`no_deal`) o
    el dealer nombró su final por encima del abandono (`walked`). No cerrar es
    un resultado, no un fallo del simulador.
    """
    thread: int
    price: Optional[int]
    rounds: int
    channel: str        # "took_quote" | "dealer_accepted" | "took_final" | "walked" | "no_deal"

    @property
    def closed(self) -> bool:
        return self.price is not None


def simulate(plan: Plan, dance: Dance, close: Optional[Close] = None) -> Optional[SimResult]:
    """Aplica `plan` contra la escalera REAL del dealer en ese hilo.

    SUPUESTO Y LÍMITE, explícito: el dealer de ese hilo respondió a las
    concesiones del **otro** equipo, no a las nuestras. Las reglas dicen que
    un dealer sólo se mueve cuando te mueves y que cada conversación tiene su
    límite secreto, así que con nuestras concesiones su escalera habría sido
    *otra*. Reproducirla es una aproximación: sirve para saber en qué ronda el
    plan habría alcanzado un precio que el dealer **demostró** aceptar en esa
    conversación, no para afirmar qué habría contestado.

    Un cierre se da por posible sólo con evidencia de ese mismo hilo:

    1. `took_quote`: el dealer cotizó un precio que nuestro precio ya cubría.
       Se cierra a su precio, nunca peor. Se excluye la ronda 1 a su precio de
       apertura: cerrar ahí no cuenta para la escalera.
    2. `dealer_accepted`: hay liquidación en ese hilo a un precio que nuestro
       precio alcanzaba. Es el canal que falta en las cotizaciones (abuela
       cotiza 10 y acepta 9); sin `close` no se puede usar y el resultado es
       entonces una **cota superior** del precio del plan.
    3. `took_final`: el dealer nombró su oferta final. Se toma si cae dentro
       del abandono; si no, se abandona: «take it or it walks».

    Devuelve None si el dealer no cotizó nunca: no hay escalera que replicar.
    """
    ladder = dance.ladder(dealer=True)
    if not ladder:
        return None
    finals = [q.final for q in dance.moves if q.is_dealer]
    opening = ladder[0]
    budget = len(ladder) if plan.max_rounds is None else min(plan.max_rounds, len(ladder))

    for i in range(budget):
        theirs, mine = ladder[i], plan.quote(i)
        covered = theirs <= mine if plan.side == "ask" else theirs >= mine

        # 1. su cotización ya nos vale. La apertura en la ronda 1 no puntúa.
        if covered and plan.acceptable(theirs) and not (i == 0 and theirs == opening):
            return SimResult(dance.thread, theirs, i + 1, "took_quote")

        # 2. el precio que ese dealer aceptó de hecho en este hilo
        if close is not None and close.price != opening and plan.acceptable(close.price):
            reached = mine >= close.price if plan.side == "ask" else mine <= close.price
            if reached:
                return SimResult(dance.thread, close.price, i + 1, "dealer_accepted")

        # 3. su última palabra: dentro del abandono se toma, fuera se deja.
        if finals[i]:
            if plan.acceptable(theirs):
                return SimResult(dance.thread, theirs, i + 1, "took_final")
            return SimResult(dance.thread, None, i + 1, "walked")

    return SimResult(dance.thread, None, budget, "no_deal")


# ---------------------------------------------------------------- backtest

@dataclass(frozen=True)
class Comparison:
    """Un baile: lo que consiguió el equipo real contra lo que haría el plan."""
    thread: int
    dealer: str
    ref: str
    side: str
    real_price: int
    real_rounds: int
    sim: SimResult
    plan: Plan

    @property
    def primas_saved(self) -> Optional[int]:
        """Positivo = el plan habría pagado menos (o cobrado más)."""
        if self.sim.price is None:
            return None
        if self.side == "ask":
            return self.real_price - self.sim.price
        return self.sim.price - self.real_price

    @property
    def rounds_saved(self) -> Optional[int]:
        if self.sim.price is None:
            return None
        return self.real_rounds - self.sim.rounds


def _median_mean(xs: list[int]) -> dict:
    if not xs:
        return {"median": None, "mean": None, "min": None, "max": None}
    return {"median": statistics.median(xs), "mean": round(statistics.mean(xs), 2),
            "min": min(xs), "max": max(xs)}


def backtest(oracle: Oracle, *, closes: Optional[dict[int, Close]] = None,
             tolerance: int = 0, events: Optional[Iterable[dict]] = None) -> dict:
    """Corre el plan sobre todos los bailes con cierre observado y compara.

    Sólo entran bailes con liquidación emparejada (`match_closes`): sin ella
    no sabemos qué consiguió el equipo real y el «ahorro» sería imaginario.
    Se puede pasar `closes` ya calculado o `events` para calcularlo aquí.

    Lo que el número NO demuestra, y va en el propio resultado:

    - `in_sample: True`. El plan se deriva del historial que incluye el baile
      evaluado, así que el suelo ya conoce el desenlace. Cota optimista.
    - `credited_by_observed_close`: cuántos cierres se acreditan por el canal
      `dealer_accepted`, es decir al precio observado. En esos bailes el ahorro
      en primas es ~0 **por construcción**: lo que se mide de verdad ahí son
      las rondas, no el precio.
    - `thin_sample`: la muestra es pequeña para presentar una media.
    """
    if closes is None:
        closes = match_closes(oracle, events) if events is not None else {}

    rows: list[Comparison] = []
    skipped_no_close = skipped_no_plan = 0
    for thread, d in sorted(oracle.dances.items()):
        side = dance_side(d)
        if not d.dealer or side is None or not d.rounds:
            continue
        close = closes.get(thread)
        if close is None:
            skipped_no_close += 1
            continue
        plan = plan_for(oracle, d.dealer, d.ref, side=side, tolerance=tolerance)
        if plan is None:
            skipped_no_plan += 1
            continue
        sim = simulate(plan, d, close)
        if sim is None:
            continue
        rows.append(Comparison(thread, d.dealer, d.ref, side, close.price,
                               d.rounds, sim, plan))

    closed = [r for r in rows if r.sim.closed]
    primas = [r.primas_saved for r in closed if r.primas_saved is not None]
    rounds = [r.rounds_saved for r in closed if r.rounds_saved is not None]
    credited = sum(1 for r in closed if r.sim.channel == "dealer_accepted")

    return {
        "dances_sampled": len(rows),
        "dances_closed": len(closed),
        "dances_no_deal": len(rows) - len(closed),
        "skipped_no_observed_close": skipped_no_close,
        "skipped_no_plan": skipped_no_plan,
        "tolerance": tolerance,
        "primas_saved": _median_mean(primas),
        "rounds_saved": _median_mean(rounds),
        "primas_saved_total": sum(primas) if primas else 0,
        "credited_by_observed_close": credited,
        "in_sample": True,
        "thin_sample": len(rows) < THIN_SAMPLE,
        "caveats": [
            "El dealer respondió a las concesiones de otro equipo: la réplica"
            " es una aproximación, no una contrafactual válida.",
            "Dentro de muestra: el plan se deriva del historial que contiene"
            " el baile evaluado. Cota optimista.",
            f"{credited} de {len(closed)} cierres se acreditan al precio"
            " observado, donde el ahorro en primas es ~0 por construcción.",
        ] + ([f"Muestra de {len(rows)} bailes: demasiado pequeña para una media."]
             if len(rows) < THIN_SAMPLE else []),
        "rows": [
            {"thread": r.thread, "dealer": r.dealer, "ref": r.ref, "side": r.side,
             "real_price": r.real_price, "real_rounds": r.real_rounds,
             "plan_price": r.sim.price, "plan_rounds": r.sim.rounds,
             "channel": r.sim.channel, "primas_saved": r.primas_saved,
             "rounds_saved": r.rounds_saved, "confidence": r.plan.confidence}
            for r in rows
        ],
    }


# ---------------------------------------------------------------- playbook

def playbook(oracle: Oracle, *, tolerance: int = 0,
             sides: tuple[str, ...] = ("ask", "bid")) -> list[dict]:
    """Tabla final por dealer y carta, ordenada por confianza y primas en juego.

    Primero lo que está respaldado por una liquidación, y dentro de eso lo que
    más primas mueve: ahí es donde una ronda bien jugada paga. Las cartas sin
    suelo ni paso observados no salen en la tabla en vez de salir con huecos.
    """
    rows = []
    for (dealer, ref, side) in sorted(oracle.dealers):
        if side not in sides:
            continue
        plan = plan_for(oracle, dealer, ref, side=side, tolerance=tolerance)
        if plan is None:
            continue
        cm = oracle.cards.get(ref)
        rows.append({
            "dealer": dealer, "ref": ref, "side": side,
            "rarity": cm.rarity if cm else None,
            "book": cm.book if cm else None,
            "floor_seen": plan.floor_seen,       # cota, no promesa
            "never_accept": plan.never_accept,
            "open_at": plan.open_at,
            "step": plan.step,
            "walk_away": plan.walk_away,
            "max_rounds": plan.max_rounds,
            "spread": plan.spread,
            "confidence": plan.confidence,
            "observations": plan.observations,
            "basis": plan.basis,
        })
    rows.sort(key=lambda r: (-CONFIDENCE_RANK.get(r["confidence"], 0),
                             -(r["spread"] or 0), r["dealer"], r["ref"]))
    return rows


def report(oracle: Oracle, *, events: Optional[Iterable[dict]] = None,
           tolerance: int = 0, limit: int = 15) -> str:
    """Informe de texto: el backtest con su tamaño de muestra, y la tabla.

    El tamaño de muestra va antes de cualquier media, a propósito: una media
    de tres bailes leída sin su n es peor que no tener media.
    """
    bt = backtest(oracle, tolerance=tolerance, events=events)
    out = [
        "=== BACKTEST (dentro de muestra) ===",
        f"bailes con cierre observado: {bt['dances_sampled']}"
        f"  (cerrados {bt['dances_closed']}, sin trato {bt['dances_no_deal']})",
        f"descartados: {bt['skipped_no_observed_close']} sin liquidación,"
        f" {bt['skipped_no_plan']} sin plan derivable",
        f"primas ahorradas: mediana {bt['primas_saved']['median']},"
        f" media {bt['primas_saved']['mean']}, total {bt['primas_saved_total']}",
        f"rondas ahorradas: mediana {bt['rounds_saved']['median']},"
        f" media {bt['rounds_saved']['mean']}",
    ]
    out += [f"  aviso: {c}" for c in bt["caveats"]]
    out.append("")
    out.append("=== PLAYBOOK (floor_seen es una COTA, no un suelo garantizado) ===")
    out.append(f"{'dealer':8} {'carta':8} {'s':3} {'cota':>5} {'abrir':>5} "
               f"{'paso':>4} {'dejar':>5} {'rond':>4} {'juego':>5} conf")
    for r in playbook(oracle, tolerance=tolerance)[:limit]:
        out.append(f"{r['dealer']:8} {r['ref']:8} {r['side']:3} "
                   f"{r['floor_seen']:>5} {r['open_at']:>5} {r['step']:>4} "
                   f"{r['walk_away']:>5} {str(r['max_rounds']):>4} "
                   f"{str(r['spread']):>5} {r['confidence']}")
    return "\n".join(out)


if __name__ == "__main__":  # pragma: no cover - utilidad de consola
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "data/feed_history.jsonl"
    events = list(read_history(path))
    oracle = Oracle()
    oracle.ingest(events)
    print(report(oracle, events=events))
