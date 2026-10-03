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
- Puntuación: excedente realizado entre límites reales / excedente máximo posible del run (todos sus operadores).

Mecanismos: puesto gratuito/auto (cruce ordenado por tick), starter_broker (su bench_plan: el mismo cruce), el broker
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

import starter_broker as sb
from market_broker import BenchBroker, Match, best_matching

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
        card = {"cash": 0, "assets": [], "types": ["card:SIM-01"]}
        cash = {"cash": q, "assets": [], "types": []}
        return {"id": self.id, "give": cash if self.buyer else card, "want": card if self.buyer else cash}


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
    """El puesto gratuito / auto y starter_broker: el bench_plan de starter_broker en cada tick."""

    def __init__(self):
        self.mode = "stall"

    def plan(self, book, tick):
        return [Match(s, b, p, "stall") for s, b, p in sb.bench_plan(book)]

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


def play(runs: list, mech, ticks: int, server: str = "quote") -> dict:
    """Juega una sesión tick a tick y devuelve excedente real, óptimo y contadores."""
    by_id = {t.id: t for run in runs for t in run}
    done, gain, n, refused = set(), 0.0, 0, 0
    for t in range(ticks):
        live = [x for x in by_id.values() if x.present(t) and x.id not in done]
        book = {"bench_offers": [x.offer(t) for x in live], "offers": []}
        for m in mech.plan(book, t):
            s, b = by_id.get(m.sell), by_id.get(m.buy)
            ok = (s and b and s in live and b in live and m.sell not in done and m.buy not in done
                  and s.id.split("-")[0] == b.id.split("-")[0] and not s.buyer and b.buyer)
            if ok and server == "quote":
                ok = s.quote(t) <= m.price <= b.quote(t)
            elif ok and server == "limit":
                ok = s.limit <= m.price <= b.limit
            mech.feedback(m, bool(ok))
            if ok:
                done |= {m.sell, m.buy}
                gain += b.limit - s.limit
                n += 1
            else:
                refused += 1
    dog = getattr(mech, "dog", None)
    tripped, probe_off = bool(dog) and dog.mode == "stall", bool(dog) and mech.probe > 0 and not dog.probing
    mech.plan({"bench_offers": []}, ticks)  # fin de sesión
    best = sum(optimum(run) for run in runs)
    return {"gain": gain, "best": best, "eff": gain / best if best else 1.0, "matches": n, "refused": refused,
            "tripped": tripped, "probe_off": probe_off}


MECHANISMS = {  # cada fábrica recibe el libro de la sesión y el servidor (solo el oráculo los usa)
    "puesto/auto":          lambda runs, srv: Stall(),
    "starter_broker":       lambda runs, srv: Stall(),  # bench_plan es exactamente el cruce del puesto
    "nuevo (recomendado)":  lambda runs, srv: BenchBroker("smart", "cover", probe=3),
    "nuevo sin sondeo":     lambda runs, srv: BenchBroker("smart", "cover"),
    "nuevo sin vigilante":  lambda runs, srv: BenchBroker("smart", "cover", probe=3, watchdog=False),
    "nuevo suelo=nº pares": lambda runs, srv: BenchBroker("smart", "count", probe=3, watchdog=False),
    "nuevo sin suelo":      lambda runs, srv: BenchBroker("smart", "none", probe=3, watchdog=False),
    "oráculo miope (ref.)":      lambda runs, srv: Oracle(runs, srv),
}


def bench(scenarios, sessions: int, seed: int, server: str = "quote", mechanisms=None) -> dict:
    out = {}
    for name in scenarios:
        sc, rows = SCENARIOS[name], {}
        books = [session(seed * 100003 + i, sc) for i in range(sessions)]
        base = [play(b, Stall(), sc["ticks"], server)["eff"] for b in books]
        for mech, make in (mechanisms or MECHANISMS).items():
            res = [play(b, make(b, server), sc["ticks"], server) for b in books]
            eff = [r["eff"] for r in res]
            delta = [e - s for e, s in zip(eff, base)]
            rows[mech] = {"eff": statistics.mean(eff), "ratio": statistics.mean(eff) / statistics.mean(base),
                          "worse": sum(d < -0.005 for d in delta) / sessions, "worst": min(delta),
                          "p10": sorted(delta)[sessions // 10], "tripped": sum(r["tripped"] for r in res) / sessions,
                          "probe_off": sum(r["probe_off"] for r in res) / sessions,
                          "matches": statistics.mean(r["matches"] for r in res)}
        out[name] = rows
    return out


def report(out: dict, server: str, stall_points: float) -> str:
    what = {"quote": "cotización (SDK)", "limit": "límites reales (hipótesis)", "libre": "nada (peor caso)"}[server]
    lines = [f"Servidor: valida por {what}",
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
    a = ap.parse_args()
    print(report(bench(a.scenario or list(SCENARIOS), a.sessions, a.seed, a.server), a.server, a.stall_points))
