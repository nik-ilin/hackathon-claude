"""LABORATORIO OFFLINE de tres niveles sobre la memoria existente (agent_memory.sqlite3). No envía nada al servidor.

    python3 lab.py replay     # A · replay histórico SIN fuga: estimaciones con solo lo conocible en cada tick
    python3 lab.py eval       # A · evaluación honesta por tiempo (entrenamiento / validación / prueba) de estimadores de precio
    python3 lab.py sim        # B · simulación de compradores (firmes, concesivos, lentos, mudos, cambiantes) con supuestos
    python3 lab.py shadow     # C · política candidata frente a la activa sobre el ÚLTIMO estado vivo (data/), sin enviar
    python3 lab.py propose    # registra una versión CANDIDATA en data/policy_versions.json (no la activa)
    python3 lab.py all

Límites: el replay evalúa decisiones y restricciones, pero NO demuestra qué habría respondido un rival a una oferta que
nunca recibió. La simulación declara sus supuestos y sus cifras dependen de ellos. No hay fórmula de score inventada.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

import fast_sales as fs
import learning as lrn

DATA = Path(__file__).parent / "data"
MEMORY = DATA / "agent_memory.sqlite3"


def card_groups(catalog: dict) -> dict:
    return {c["id"]: (s["id"], c.get("rarity")) for s in (catalog or {}).get("sets", []) for c in s.get("cards", [])}


def catalog_from_events(events: list) -> dict:
    """Barrio y rareza de cada carta según las liquidaciones públicas (no hace falta red)."""
    g = {}
    for e in events:
        for it in (e.get("payload") or {}).get("items") or []:
            if it.get("ref") and it.get("set"):
                g[it["ref"]] = (it["set"], it.get("rarity"))
    return g


# ------------------------------------------------------------------ A · replay y evaluación por tiempo

def predictors(half_life: float):
    """Estimadores comparados. Todos ven SOLO filas con tick < el de la liquidación que se predice."""
    def last_price(rows, ref, t, g):
        xs = [r for r in rows if r["type"] == "settlement" and r["ref"] == ref and r["tick"] < t and not r.get("persona")]
        return xs[-1]["price"] if xs else None

    def current_rule(rows, ref, t, g):   # regla activa de ventas rápidas: mediana de ≥2 cierres en 300 ticks
        xs = sorted(r["price"] for r in rows if r["type"] == "settlement" and r["ref"] == ref and t - 300 <= r["tick"] < t
                    and not r.get("persona"))
        return xs[len(xs) // 2] if len(xs) >= 2 else None

    def learned(rows, ref, t, g):
        prior = [r for r in rows if r["tick"] < t]
        return lrn.price_estimate(prior, ref, t - 1, g, half_life)["value"]
    return {"último precio": last_price, "regla actual": current_rule, f"memoria (vida media {half_life:g})": learned}


def evaluate(rows: list, groups: dict, split=(0.6, 0.8), half_lives=(60, 240, 960)) -> dict:
    """Divide las liquidaciones entre equipos por TIEMPO. La vida media se elige en VALIDACIÓN; el informe final usa solo
    PRUEBA (nunca se ajusta y evalúa sobre las mismas operaciones). Métrica: error absoluto frente al precio liquidado
    (un precio liquidado no es el «valor real»; es lo que dos partes aceptaron), con cobertura e IC por bootstrap."""
    sets = sorted((r for r in rows if r["type"] == "settlement" and not r.get("persona") and r["price"] > 0),
                  key=lambda r: r["tick"])
    if len(sets) < 30:
        return {"error": f"solo {len(sets)} liquidaciones: muestra insuficiente para evaluar"}
    a, b = sets[int(len(sets) * split[0])]["tick"], sets[int(len(sets) * split[1])]["tick"]
    val = [r for r in sets if a <= r["tick"] < b]
    test = [r for r in sets if r["tick"] >= b]

    def score(fn, part):
        errs, cover = [], 0
        for r in part:
            p = fn(rows, r["ref"], r["tick"], groups)
            if p is None:
                continue
            cover += 1
            errs.append(abs(p - r["price"]))
        return errs, cover

    best_hl, best = None, None
    for hl in half_lives:
        fn = list(predictors(hl).values())[-1]
        errs, cov = score(fn, val)
        m = statistics.mean(errs) if errs else float("inf")
        if best is None or m < best:
            best_hl, best = hl, m
    out = {"train_until": a, "validation": [a, b], "test_from": b, "n_validation": len(val), "n_test": len(test),
           "chosen_half_life": best_hl, "models": {}}
    rng = random.Random(7)
    for name, fn in predictors(best_hl).items():
        errs, cov = score(fn, test)
        if not errs:
            out["models"][name] = {"coverage": 0.0}
            continue
        boots = sorted(statistics.mean(rng.choice(errs) for _ in errs) for _ in range(300))
        out["models"][name] = {"mae": round(statistics.mean(errs), 2), "ci90": [round(boots[15], 2), round(boots[284], 2)],
                               "coverage": round(cov / len(test), 2), "n": len(errs)}
    return out


def replay(events: list, ticks: list, refs: list, groups: dict) -> list:
    """Estimaciones en cada tick con SOLO lo conocible entonces (learning.as_of). Útil para auditar decisiones pasadas;
    no simula respuestas de rivales."""
    out = []
    for t in ticks:
        rows = lrn.experiences(lrn.as_of(events, t))
        for ref in refs:
            e = lrn.price_estimate(rows, ref, t, groups)
            out.append({"tick": t, "ref": ref, "value": e["value"], "n_eff": e["n_eff"], "confidence": e["confidence"]})
    return out


# ------------------------------------------------------------------ B · simulación declarada

ASSUMPTIONS = [
    "cada comprador tiene una reserva fija (o cambiante) desconocida para nosotros; compra si precio + comisión ≤ reserva",
    "firme: reserva fija; concesivo: su reserva sube 1 P por ventana si seguimos ahí; lento: solo mira cada 3 ticks;",
    "mudo: nunca compra; cambiante: su reserva cae a la mitad a mitad de partida",
    "competencia: un vendedor rival publica la misma carta 1 P por debajo del objetivo con probabilidad 0,3 por ventana",
    "comisión del comprador 5 % + 1 P (El Rastro); nuestra publicación caduca al final de cada ventana; liquidación al tick",
    "las reservas se sortean alrededor del precio comparable (±40 %); esto es un SUPUESTO, no una medición",
]


def simulate(params: dict, n: int = 400, seed: int = 11, horizon: int = 60, minimum: int = 9, objective: int = 18) -> dict:
    """Política de ventanas de ventas rápidas (fast_sales.revised_price) con los parámetros dados, contra una población
    de compradores sintéticos. Métricas: beneficio neto por liquidación, tasa de cierre (vendidos / activos), ticks hasta
    cerrar, efectivo al cierre. No modela páginas (se pierden 0 por construcción)."""
    rng = random.Random(seed)
    cfg = fs.Config(refs=("X",), ticks=params.get("ticks", 6), dry_windows=params.get("dry_windows", 2),
                    min_step=params.get("min_step", 2))
    sold, ttc, cash = 0, [], 0
    for _ in range(n):
        kind = rng.choice(["firme", "concesivo", "lento", "mudo", "cambiante"])
        reserve = objective * rng.uniform(0.6, 1.4)
        t, price, dry, last = 0, int(objective * (1 + params.get("target_adj", 0.0))), 0, None
        done = False
        while t < horizon and not done:
            st = {"dry_windows": dry, "last_window_price": last}
            price, _ = fs.revised_price(max(price, minimum), minimum, st, cfg, False)
            rival = rng.random() < 0.3
            for k in range(cfg.ticks):
                tt = t + k
                r = reserve * (0.5 if kind == "cambiante" and tt > horizon / 2 else 1.0)
                if kind == "concesivo":
                    r += dry
                looks = kind != "mudo" and (kind != "lento" or tt % 3 == 0)
                if looks and not (rival and price > objective - 1) and price + 0.05 * price + 1 <= r:
                    sold += 1
                    ttc.append(tt)
                    cash += price
                    done = True
                    break
            t += cfg.ticks
            if not done:
                dry = dry + 1 if last in (None, price) else 1
                last = price
    return {"params": params, "close_rate": round(sold / n, 3), "denominator": f"{n} activos simulados",
            "median_ticks_to_close": statistics.median(ttc) if ttc else None,
            "net_per_settlement": round(cash / sold - minimum + 2, 2) if sold else None,
            "cash": cash, "assumptions": ASSUMPTIONS}


# ------------------------------------------------------------------ C · sombra sobre el último estado vivo

def shadow(active: dict, candidate: dict, data: Path = DATA) -> list:
    """Próxima propuesta por activo de la campaña con la política activa y con la candidata, sobre el informe y el
    registro que escribe el agente (solo lectura)."""
    rep = json.loads((data / "coordinator_report.json").read_text())
    led = json.loads((data / "coordinator_ledger.json").read_text())
    fsr = (rep.get("plan") or {}).get("fast_sales_report") or {}
    tick = rep.get("tick") or 0
    out = []
    for r in fsr.get("refs", []):
        if not r.get("authorized"):
            continue
        p = r["prices"]
        for a in r.get("free", []) + [int(k) for k in (r.get("locked") or {})]:
            row = {"ref": r["ref"], "asset": a}
            for name, par in (("activa", active), ("candidata", candidate)):
                cfg = fs.Config(refs=(r["ref"],), ticks=par["ticks"], dry_windows=par["dry_windows"], min_step=par["min_step"])
                st = fs.stage(led, a, tick, cfg)
                obj = max(p["minimum"], int(p["objective"] * (1 + par.get("target_adj", 0.0))))
                row[name] = fs.revised_price(obj, p["minimum"], st, cfg, bool(fsr.get("urgent")))[0]
            row["differs"] = row["activa"] != row["candidata"]
            out.append(row)
    return out


def propose(sim_results: list, ev: dict) -> dict:
    """Candidata = mejor tasa de cierre simulada sin bajar el beneficio por liquidación más de 1 P frente a la actual;
    precio aprendido solo si la memoria gana en PRUEBA a la regla actual con IC sin solaparse."""
    base = sim_results[0]
    ok = [r for r in sim_results if r["net_per_settlement"] is not None and base["net_per_settlement"] is not None
          and r["net_per_settlement"] >= base["net_per_settlement"] - 1]
    best = max(ok, key=lambda r: (r["close_rate"], -(r["median_ticks_to_close"] or 99)))
    params = dict(best["params"])
    m = ev.get("models") or {}
    learned = next((v for k, v in m.items() if k.startswith("memoria")), None)
    cur = m.get("regla actual")
    params["use_learned_price"] = bool(learned and cur and learned.get("ci90") and cur.get("ci90")
                                       and learned["ci90"][1] < cur["ci90"][0])
    return {"params": params, "why": {"sim": {k: best[k] for k in ("close_rate", "median_ticks_to_close", "net_per_settlement")},
                                      "baseline_sim": {k: base[k] for k in ("close_rate", "median_ticks_to_close", "net_per_settlement")},
                                      "pricing_test": {"memoria": learned, "regla actual": cur}}}


GRID = [dict(lrn.DEFAULTS), dict(lrn.DEFAULTS, dry_windows=1), dict(lrn.DEFAULTS, ticks=8), dict(lrn.DEFAULTS, min_step=3),
        dict(lrn.DEFAULTS, dry_windows=1, ticks=8), dict(lrn.DEFAULTS, target_adj=-0.05)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["replay", "eval", "sim", "shadow", "propose", "all"])
    ap.add_argument("--memory", default=str(MEMORY))
    a = ap.parse_args(argv)
    events = lrn.load_events(a.memory)
    groups = catalog_from_events(events)
    rows = lrn.experiences(events)
    if a.cmd in ("replay", "all"):
        cov = lrn.coverage(events)
        print(f"COBERTURA: {cov}")
        ticks = sorted({e['tick'] for e in events})
        sample = ticks[::max(1, len(ticks) // 5)][-5:]
        for r in replay(events, sample, ["MAL-07", "SAL-04", "LAT-07"], groups):
            print(f"  t{r['tick']} {r['ref']}: {r['value']} P · n_eff {r['n_eff']} · {r['confidence']}")
    ev = {}
    if a.cmd in ("eval", "all", "propose"):
        ev = evaluate(rows, groups)
        print("EVALUACIÓN POR TIEMPO (error absoluto frente al precio liquidado, solo PRUEBA):")
        print(json.dumps(ev, ensure_ascii=False, indent=1))
    sims = []
    if a.cmd in ("sim", "all", "propose"):
        print("SIMULACIÓN (supuestos declarados):")
        for s in ASSUMPTIONS:
            print("  ·", s)
        for p in GRID:
            r = simulate(p)
            sims.append(r)
            print(f"  {p} → cierre {r['close_rate']} ({r['denominator']}) · mediana {r['median_ticks_to_close']} ticks · "
                  f"neto/liquidación {r['net_per_settlement']}")
        prof = lrn.counterparty_profiles(lrn.lifecycles(rows), max((r["tick"] or 0) for r in rows))
        teams = [v for v in prof.values() if v["kind"] == "team" and v["n"] >= 5][:6]
        print("PERFILES (equipos, ≥5 ofertas; tasa con IC90 y etiqueta):")
        for v in teams:
            print(f"  {v['maker']} {v['side']}: llenado {v['fill']['rate']} IC90 {v['fill']['ci90']} ({v['fill']['label']}, "
                  f"n={v['n']}) · cancela {v['cancel_rate']} · concesión {v['concessions']}")
    if a.cmd in ("propose", "all"):
        cand = propose(sims, ev)
        store = lrn.load_store(DATA)
        vid = lrn.add_version(store, cand["params"], json.dumps(cand["why"], ensure_ascii=False, default=str))
        lrn.save_store(DATA, store)
        print(f"CANDIDATA registrada: {vid} (NO activada) · {store['versions'][vid]['params']}")
    if a.cmd in ("shadow", "all"):
        store = lrn.load_store(DATA)
        cand_id = sorted(store["versions"], key=lambda v: int(v[1:]))[-1]
        act, _ = lrn.params_for(DATA, store.get("active"))
        cand, _ = lrn.params_for(DATA, cand_id)
        print(f"SOMBRA sobre el último estado vivo: activa {store.get('active')} frente a candidata {cand_id}")
        try:
            for r in shadow(act, cand):
                print(f"  {r['ref']} #{r['asset']}: activa {r['activa']} P · candidata {r['candidata']} P{' ≠' if r['differs'] else ''}")
        except (OSError, ValueError) as e:
            print(f"  sin estado vivo legible: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
