"""Banco de pruebas offline del Market Test (sin red): el mismo libro sintético para cada mecanismo, como en el juego.

    python3 sim_bench.py                 # todos los escenarios, 300 sesiones cada uno (semilla fija)
    python3 sim_bench.py --sessions 50 --scenario dificil

Modelo (lo que dicen RULES.md y starter_broker.py, no el código del servidor):
- Cada sesión tiene varios runs (`b3` en `b3-7`): compradores con un valor oculto y vendedores con un coste oculto.
- Cotizan alejándose del límite un sombreado propio. Los que se relajan acercan la cotización al límite a medida que se
  acaba su paciencia (más deprisa al final). Los firmes no la mueven nunca. Al agotarse la paciencia se van.
- El servidor acepta un emparejamiento si ask <= precio <= bid por cotización (`--server quote`, lo que dice el SDK); en
  la variante hipotética `--server limit`, si coste <= precio <= valor por límites ocultos; y en `--server libre` (el
  peor caso para el sondeo) a cualquier precio, aunque el par destruya excedente.
- Comisión del venue (`--fee-bps`): el comprador paga precio + ⌈bps·precio/10000⌉, así que un cruce solo es legal si eso
  cabe en su puja. Con `--leak`, la comisión además se descuenta del excedente puntuado. La referencia es el puesto
  gratuito con `--stall-fee` (0 por defecto).
- Puntuación: excedente realizado entre límites reales / excedente máximo posible del run (todos sus operadores).

Mecanismos: puesto gratuito/auto (cruce ordenado por tick, con precio legal), starter_broker (su bench_plan tal cual:
punto medio sin descontar la comisión), broker_engine.plan (el broker de broker_run.py, también con su tope de 10 cruces
por tick y con `defer`), el broker
nuevo (market_broker.BenchBroker) con sus variantes y, como referencia, un oráculo miope que conoce los límites reales y
cruza cada tick el máximo excedente real que el servidor aceptaría (no se puede construir en el juego y no es un techo:
no mira al futuro). Los resultados dependen de este modelo: son una comparación
relativa con supuestos explícitos, no una predicción de la nota.
"""
from __future__ import annotations

import argparse
import math
import random
import statistics

import broker_engine as be
import starter_broker as sb
from market_broker import BenchBroker, Match, best_matching, fee_fn, stall_plan

# escenario: runs, operadores por lado, ticks, llegadas escalonadas, % impacientes, paciencia, % firmes, sombreado
SCENARIOS = {
    "normal":    dict(runs=(4, 7), side=(5, 12), ticks=24, stagger=0.5, impatient=0.3, slow=(8, 20), fast=(1, 4),
                      firm=0.25, shade=(0.08, 0.35)),
    "dificil":   dict(runs=(4, 7), side=(5, 12), ticks=24, stagger=0.5, impatient=0.7, slow=(6, 14), fast=(1, 3),
                      firm=0.75, shade=(0.10, 0.35)),
    "paciente":  dict(runs=(4, 7), side=(5, 12), ticks=24, stagger=0.3, impatient=0.1, slow=(12, 24), fast=(2, 5),
                      firm=0.10, shade=(0.08, 0.35)),
    "denso":     dict(runs=(3, 5), side=(15, 25), ticks=20, stagger=0.0, impatient=0.3, slow=(8, 20), fast=(1, 4),
                      firm=0.25, shade=(0.08, 0.35)),
    "sin_sombra": dict(runs=(4, 7), side=(5, 12), ticks=24, stagger=0.5, impatient=0.3, slow=(8, 20), fast=(1, 4),
                       firm=0.25, shade=(0.0, 0.03)),
    "ruidoso":   dict(runs=(4, 7), side=(5, 12), ticks=24, stagger=0.5, impatient=0.3, slow=(8, 20), fast=(1, 4),
                      firm=0.25, shade=(0.0, 0.50)),
}


class Trader:
    def __init__(self, oid, buyer, limit, arrive, patience, firm, shade, relax):
        self.id, self.buyer, self.limit, self.arrive, self.patience = oid, buyer, limit, arrive, patience
        self.firm, self.shade, self.relax = firm, shade, relax

    def quote(self, t: int) -> int:
        frac = 0.0 if self.firm else min((t - self.arrive) / max(self.patience - 1, 1), 1.0) ** 2 * self.relax
        s = self.shade * (1 - frac)
        return max(1, math.floor(self.limit * (1 - s))) if self.buyer else math.ceil(self.limit * (1 + s))

    def present(self, t: int) -> bool:
        return self.arrive <= t < self.arrive + self.patience

    def offer(self, t: int) -> dict:
        q = self.quote(t)
        cash = {"cash": q, "assets": [], "types": []}
        if self.buyer:  # como el tablón real: el vendedor entrega la carta, el comprador pide su tipo
            return {"id": self.id, "give": cash, "want": {"cash": 0, "assets": [], "types": ["card:SIM-01"]}}
        return {"id": self.id, "give": {"cash": 0, "assets": [{"kind": "card", "ref": "SIM-01"}], "types": []},
                "want": cash}


def session(seed: int, sc: dict) -> list:
    """Lista de runs; cada run es una lista de Trader. Mismo libro para todos los mecanismos (misma semilla)."""
    r, runs = random.Random(seed), []
    for k in range(r.randint(*sc["runs"])):
        base, traders, n = r.uniform(10, 60), [], 0
        for buyer in (True, False):
            for _ in range(r.randint(*sc["side"])):
                n += 1
                fast = r.random() < sc["impatient"]
                traders.append(Trader(f"b{k + 1}-{n}", buyer, base * r.uniform(0.6, 1.4),
                                      r.randint(0, int(sc["ticks"] * sc["stagger"])),
                                      r.randint(*(sc["fast"] if fast else sc["slow"])), r.random() < sc["firm"],
                                      r.uniform(*sc["shade"]), r.uniform(0.7, 1.0)))
        runs.append(traders)
    return runs


def optimum(traders: list) -> float:
    v = sorted((t.limit for t in traders if t.buyer), reverse=True)
    c = sorted(t.limit for t in traders if not t.buyer)
    return sum(max(a - b, 0) for a, b in zip(v, c))


class Stall:
    """El puesto gratuito / un venue auto: cruce ordenado de bench_plan con precio legal para la comisión del venue.
    Con raw=True es starter_broker tal cual: el punto medio sin descontar la comisión (con fee_bps > 0 puede ser
    rechazado)."""

    def __init__(self, raw: bool = False):
        self.mode, self.raw = "stall", raw

    def plan(self, book, tick):
        return [Match(s, b, p, "stall") for s, b, p in (sb.bench_plan(book) if self.raw else stall_plan(book))]

    def feedback(self, m, ok):
        pass


class Oracle:
    """Referencia miope: conoce los límites reales y cruza cada tick el emparejamiento de máximo excedente real
    entre los pares que el servidor aceptaría (por cotización, o cualquier par con excedente si valida por límites)."""

    def __init__(self, runs: list, server: str):
        self.by, self.server, self.mode = {t.id: t for run in runs for t in run}, server, "smart"

    def plan(self, book, tick):
        runs, out = {}, []
        for o in book["bench_offers"]:
            t = self.by[o["id"]]
            runs.setdefault(t.id.split("-")[0], ([], []))[t.buyer].append(t)
        for asks, bids in runs.values():
            if self.server == "quote":
                edge = lambda i, j: bids[i].quote(tick) >= asks[j].quote(tick)  # noqa: E731
            else:
                edge = lambda i, j: bids[i].limit >= asks[j].limit  # noqa: E731
            for i, j in best_matching(bids, asks, lambda i, j: bids[i].limit - asks[j].limit, edge):
                b, a = bids[i], asks[j]
                price = (a.quote(tick) + b.quote(tick)) // 2 if self.server == "quote" else round((a.limit + b.limit) / 2)
                out.append(Match(a.id, b.id, price, "oracle"))
        return out

    def feedback(self, m, ok):
        pass


class Engine:
    """broker_engine.plan (el otro broker del repo, el que lanza broker_run.py) con su BrokerConfig."""

    def __init__(self, ticks: int, **cfg):
        self.cfg, self.ticks, self.mode = be.BrokerConfig(**cfg), ticks, "smart"

    def plan(self, book, tick):
        left = max(1, self.ticks - tick) if self.cfg.defer else None
        return [Match(m.sell, m.buy, m.price, "engine") for m in be.plan(be.parse_book(book), self.cfg, ticks_left=left)]

    def feedback(self, m, ok):
        pass


def play(runs: list, mech, ticks: int, server: str = "quote", fee_bps: int = 0, leak: bool = False) -> dict:
    """Juega una sesión tick a tick y devuelve excedente real, óptimo y contadores. fee_bps: comisión del venue (el
    comprador paga precio + comisión <= su puja); leak: la comisión se descuenta del excedente puntuado."""
    by_id = {t.id: t for run in runs for t in run}
    done, gain, n, refused = set(), 0.0, 0, 0
    fee = fee_fn({"fee_bps": fee_bps})
    for t in range(ticks):
        live = [x for x in by_id.values() if x.present(t) and x.id not in done]
        book = {"tick": t, "fee_bps": fee_bps, "fee_per_card": 0, "bench_offers": [x.offer(t) for x in live],
                "offers": []}
        for m in mech.plan(book, t):
            s, b = by_id.get(m.sell), by_id.get(m.buy)
            ok = (s and b and s in live and b in live and m.sell not in done and m.buy not in done
                  and s.id.split("-")[0] == b.id.split("-")[0] and not s.buyer and b.buyer)
            if ok and server == "quote":
                ok = s.quote(t) <= m.price and m.price + fee(m.price) <= b.quote(t)
            elif ok and server == "limit":
                ok = s.limit <= m.price and m.price + fee(m.price) <= b.limit
            mech.feedback(m, bool(ok))
            if ok:
                done |= {m.sell, m.buy}
                gain += b.limit - s.limit - (fee(m.price) if leak else 0)
                n += 1
            else:
                refused += 1
    dog = getattr(mech, "dog", None)
    tripped, probe_off = bool(dog) and dog.mode == "stall", bool(dog) and mech.probe > 0 and not dog.probing
    mech.plan({"bench_offers": []}, ticks)  # fin de sesión
    best = sum(optimum(run) for run in runs)
    return {"gain": gain, "best": best, "eff": gain / best if best else 1.0, "matches": n, "refused": refused,
            "tripped": tripped, "probe_off": probe_off}


MECHANISMS = {  # cada fábrica recibe el libro de la sesión, el servidor y los ticks (solo el oráculo y el motor los usan)
    "puesto/auto":          lambda runs, srv, T: Stall(),
    "starter_broker":       lambda runs, srv, T: Stall(raw=True),  # bench_plan tal cual: punto medio sin comisión
    "nuevo (recomendado)":  lambda runs, srv, T: BenchBroker("smart", "cover", probe=3),
    "nuevo sin sondeo":     lambda runs, srv, T: BenchBroker("smart", "cover"),
    "nuevo sin vigilante":  lambda runs, srv, T: BenchBroker("smart", "cover", probe=3, watchdog=False),
    "nuevo suelo=nº pares": lambda runs, srv, T: BenchBroker("smart", "count", probe=3, watchdog=False),
    "nuevo sin suelo":      lambda runs, srv, T: BenchBroker("smart", "none", probe=3, watchdog=False),
    "broker_engine":        lambda runs, srv, T: Engine(T),
    "broker_engine (run)":  lambda runs, srv, T: Engine(T, max_matches=10),  # broker_run.py: 10 cruces por tick
    "broker_engine defer":  lambda runs, srv, T: Engine(T, defer=True),
    "oráculo miope (ref.)": lambda runs, srv, T: Oracle(runs, srv),
}


def bench(scenarios, sessions: int, seed: int, server: str = "quote", mechanisms=None, fee_bps: int = 0,
          leak: bool = False, stall_fee: int = 0) -> dict:
    """La referencia es siempre el puesto gratuito con comisión stall_fee; los mecanismos juegan con fee_bps."""
    out = {}
    for name in scenarios:
        sc, rows = SCENARIOS[name], {}
        books = [session(seed * 100003 + i, sc) for i in range(sessions)]
        base = [play(b, Stall(), sc["ticks"], server, stall_fee, leak)["eff"] for b in books]
        for mech, make in (mechanisms or MECHANISMS).items():
            res = [play(b, make(b, server, sc["ticks"]), sc["ticks"], server, fee_bps, leak) for b in books]
            eff = [r["eff"] for r in res]
            delta = [e - s for e, s in zip(eff, base)]
            rows[mech] = {"eff": statistics.mean(eff), "ratio": statistics.mean(eff) / statistics.mean(base),
                          "worse": sum(d < -0.005 for d in delta) / sessions, "worst": min(delta),
                          "p10": sorted(delta)[sessions // 10], "tripped": sum(r["tripped"] for r in res) / sessions,
                          "probe_off": sum(r["probe_off"] for r in res) / sessions,
                          "matches": statistics.mean(r["matches"] for r in res)}
        out[name] = rows
    return out


def report(out: dict, server: str, stall_points: float, fee_bps: int = 0, leak: bool = False, stall_fee: int = 0) -> str:
    what = {"quote": "cotización (SDK)", "limit": "límites reales (hipótesis)", "libre": "nada (peor caso)"}[server]
    lines = [f"Servidor: valida por {what} · comisión del venue {fee_bps} bps (puesto de referencia: {stall_fee} bps)"
             f" · comisión {'descontada' if leak else 'no descontada'} del excedente",
             f"Puntos orientativos = {stall_points} × eficiencia / eficiencia del puesto (supuesto lineal, no documentado)"]
    for name, rows in out.items():
        lines += ["", f"## {name}",
                  f"{'mecanismo':<22} {'eficiencia':>10} {'× puesto':>9} {'pts':>6} {'pares':>6} {'peor que':>9} "
                  f"{'Δ peor':>7} {'Δ p10':>7} {'vigil.':>7} {'sondeo off':>10}"]
        for mech, r in rows.items():
            lines.append(f"{mech:<22} {r['eff']:>9.1%} {r['ratio']:>9.3f} {stall_points * r['ratio']:>6.2f} "
                         f"{r['matches']:>6.1f} {r['worse']:>8.0%} {r['worst']:>+7.1%} {r['p10']:>+7.1%} "
                         f"{r['tripped']:>6.0%} {r['probe_off']:>10.0%}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scenario", choices=list(SCENARIOS), action="append")
    ap.add_argument("--server", choices=["quote", "limit", "libre"], default="quote")
    ap.add_argument("--stall-points", type=float, default=6.73)
    ap.add_argument("--fee-bps", type=int, default=0, help="comisión del venue que se prueba")
    ap.add_argument("--stall-fee", type=int, default=0, help="comisión del puesto de referencia")
    ap.add_argument("--leak", action="store_true", help="la comisión se descuenta del excedente puntuado")
    a = ap.parse_args()
    out = bench(a.scenario or list(SCENARIOS), a.sessions, a.seed, a.server, fee_bps=a.fee_bps, leak=a.leak,
                stall_fee=a.stall_fee)
    print(report(out, a.server, a.stall_points, a.fee_bps, a.leak, a.stall_fee))
