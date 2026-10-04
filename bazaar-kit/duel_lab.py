"""LABORATORIO REPRODUCIBLE de duelos: compara la política base (duels.py) con la que aprende (duel_learning.py) sobre historiales
confirmados por el servidor y sobre escenarios simulados. No envía nada al servidor y no toca el lock.

    python3 duel_lab.py audit     # qué registra el servidor, qué falta, decisiones que fallaron (hechos / hipótesis)
    python3 duel_lab.py summary   # resumen por situación con n e intervalos
    python3 duel_lab.py replay    # A · replay histórico con separación TEMPORAL (entrena / valida / prueba)
    python3 duel_lab.py waves     # A · replay por OLEADAS con una aceptación por tick: política anterior frente a la agresiva
    python3 duel_lab.py sim       # B · simulación (supuestos declarados) con semillas de entrenamiento y de prueba DISJUNTAS
    python3 duel_lab.py all
    python3 duel_lab.py fetch     # (solo lectura) descarga los duelos terminados a data/duels_history.json

Límites (leer antes de creerse una cifra): el replay solo evalúa la decisión de ACEPTAR/ESPERAR sobre la trayectoria que el rival
publicó; no sabe qué habría respondido a ofertas que nunca le enviamos. La simulación depende de sus supuestos. Con pocas decenas de
duelos, los intervalos son anchos: se muestran siempre.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from pathlib import Path

import duel_learning as L
import duel_sim
import duels as dl

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
HISTORY = DATA / "duels_history.json"
LOG = DATA / "duels_log.jsonl"
PRODUCTION = {"PLAY_DAYS": True, "PROFILES": True, "LADDER": (0.20, 0.12, 0.06)}


def log_rows() -> list:
    rows = []
    try:
        for line in open(LOG, encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


# ------------------------------------------------------------------ replay histórico

def live_state(d: dict, t: int) -> dict:
    """El duelo tal como se veía en el tick t: solo mensajes con tick ≤ t, la oferta rival vigente y nuestras ofertas hasta t."""
    seen = [m for m in d.get("messages") or [] if m["tick"] <= t]
    rv = [m for m in seen if m["from"] != "you" and m.get("price") is not None]
    ours = [m for m in seen if m["from"] == "you" and m.get("price") is not None]
    return dict(d, status="live", messages=[dict(m, **{"from": ("R" if m["from"] != "you" else "us")}) for m in seen], rival="R",
                rival_offer={"price": rv[-1]["price"], "days": rv[-1].get("days"), "tick": rv[-1]["tick"]} if rv else None,
                your_offer={"price": ours[-1]["price"], "days": ours[-1].get("days")} if ours else None, rounds=0,
                result=None, price=None, days=None)


def play_duel(d: dict, model=None) -> dict:
    """Repite UN duelo tick a tick con la política (base o con aprendizaje). Devuelve {accept_tick, capture, best}.
    capture = excedente total al aceptar (rondas = 0), 0 si no acepta; best = el mejor total que el rival publicó (techo)."""
    saved, hooks = dict(dl.PARAMS), dict(dl.HOOKS)
    dl.PARAMS.update(PRODUCTION)
    try:
        if model is not None:
            dl.PARAMS["LEARN"] = True
            dl.HOOKS["refine"] = lambda dd, f, a, r: L.refine(dd, f, a, r, model)
            dl.HOOKS["open_share"] = lambda dd, left, base: L.best_open_share(dd, left, model, base)
        else:
            dl.PARAMS["LEARN"] = False
            dl.HOOKS["refine"] = dl.HOOKS["open_share"] = None
        msgs = [m for m in d.get("messages") or [] if m["from"] != "you" and m.get("price") is not None]
        if not msgs or d.get("deadline_tick") is None:
            return {"played": False}
        best = max((dl.margin(dict(d, status="live"), m["price"], m.get("days") if "days" in (d.get("issues") or []) else None)
                    for m in msgs), default=0.0)
        info = {"played": True, "accept_tick": None, "capture": 0.0, "best": max(0.0, best)}
        for t in range(min(m["tick"] for m in msgs), d["deadline_tick"]):
            st = live_state(d, t)
            if st["rival_offer"] is None:
                continue
            acc = [c for c in dl.duel_candidates([st], t) if c["type"] == "duel_accept"]
            note = (dl.NOTES.get(d["duel"]) or {}).get("learned") or {}
            info["learned"] = info.get("learned", False) or bool(note.get("learned"))       # el aprendizaje cambió alguna decisión
            if acc:
                ro = st["rival_offer"]
                info.update(accept_tick=t, capture=max(0.0, dl.margin(st, ro["price"], ro.get("days") if "days" in st["issues"] else None) or 0.0))
                break
        return info
    finally:
        dl.PARAMS.clear()
        dl.PARAMS.update(saved)
        dl.HOOKS.update(hooks)


def wave_replay(raw: list, sessions=(3, 4, 5), extra: dict | None = None) -> dict:
    """Replay por OLEADAS: todos los duelos de una sesión a la vez, tick a tick, con UNA aceptación por tick como en el
    servidor (las que no caben se difieren). Las ofertas rivales siguen su trayectoria real; no simula respuestas a nuestras
    ofertas. Captura = excedente TOTAL al aceptar (rondas = 0). Devuelve también las aceptaciones diferidas y perdidas."""
    saved, hooks = dict(dl.PARAMS), dict(dl.HOOKS)
    dl.PARAMS.update(PRODUCTION)
    dl.PARAMS.update(extra or {})
    dl.PARAMS["LEARN"] = False
    out = {"captured": 0.0, "closed": 0, "duels": 0, "lost_to_queue": [], "deferred": 0, "ticks_before_deadline": [],
           "violations": 0, "best": 0.0}
    try:
        for ses in sessions:
            pool = [d for d in raw if d.get("session") == ses and d.get("status") in ("deal", "no_deal") and d.get("deadline_tick")
                    and any(m["from"] != "you" and m.get("price") is not None for m in d.get("messages") or [])]
            if not pool:
                continue
            out["duels"] += len(pool)
            for d in pool:
                ms = [m for m in d["messages"] if m["from"] != "you" and m.get("price") is not None]
                out["best"] += max(0.0, max(dl.margin(dict(d, status="live"), m["price"], m.get("days")
                                                      if "days" in (d.get("issues") or []) else None) or 0.0 for m in ms))
            t0 = min(min(m["tick"] for m in d["messages"] if m["from"] != "you") for d in pool)
            t1 = max(d["deadline_tick"] for d in pool)
            done, wanted = {}, {}
            for t in range(t0, t1):
                states = []
                for d in pool:
                    if d["duel"] in done or t >= d["deadline_tick"]:
                        continue
                    st = live_state(d, t)
                    st["duel"] = d["duel"]
                    if st["rival_offer"] is not None:
                        states.append(st)
                if not states:
                    continue
                accs = [c for c in dl.duel_candidates(states, t) if c["type"] == "duel_accept"]
                for c in accs:
                    wanted.setdefault(c["duel"], t)
                if accs:
                    c = accs[0]                                   # el orden del candidato YA es la prioridad explícita
                    st = next(x for x in states if x["duel"] == c["duel"])
                    ro = st["rival_offer"]
                    days = ro.get("days") if "days" in (st.get("issues") or []) else None
                    tot, pm = dl.margin(st, ro["price"], days), dl.price_margin(st, ro["price"])
                    if tot is None or tot <= 0 or pm is None or pm < 0:
                        out["violations"] += 1
                    done[c["duel"]] = t
                    out["captured"] += max(0.0, tot or 0.0)
                    out["closed"] += 1
                    out["ticks_before_deadline"].append(st["deadline_tick"] - t)
                    out["deferred"] += len(accs) - 1
            out["lost_to_queue"] += [k for k in wanted if k not in done]
    finally:
        dl.PARAMS.clear()
        dl.PARAMS.update(saved)
        dl.HOOKS.update(hooks)
    tb = out.pop("ticks_before_deadline")
    out["mean_ticks_before_deadline"] = round(statistics.mean(tb), 2) if tb else None
    out["captured"], out["best"] = round(out["captured"], 1), round(out["best"], 1)
    return out


def split_by_time(raw: list, played_only: bool = True, fr=(0.6, 0.8)) -> tuple:
    done = [d for d in raw if d.get("status") in ("deal", "no_deal") and d.get("session") != 1]       # sesión 1 = práctica sin jugar
    if played_only:
        done = [d for d in done if any(m["from"] != "you" and m.get("price") is not None for m in d.get("messages") or [])]
    done.sort(key=lambda d: (d.get("deadline_tick") or 0, d["duel"]))
    a, b = int(len(done) * fr[0]), int(len(done) * fr[1])
    return done[:a], done[a:b], done[b:]


def bootstrap_diff(xs: list, ys: list, n: int = 400, seed: int = 5) -> tuple:
    rng = random.Random(seed)
    diffs = [a - b for a, b in zip(xs, ys)]
    if not diffs:
        return None, None
    ms = sorted(statistics.mean(rng.choice(diffs) for _ in diffs) for _ in range(n))
    return round(ms[int(0.05 * n)], 2), round(ms[int(0.95 * n)], 2)


def replay_report(raw: list) -> dict:
    train, val, test = split_by_time(raw)
    records_train = L.normalize(train)
    model = L.Model().fit(records_train)
    out = {"n": {"train": len(train), "validation": len(val), "test": len(test)},
           "split_ticks": {"train_until": max((d["deadline_tick"] for d in train), default=None),
                           "test_from": min((d["deadline_tick"] for d in test), default=None)}}
    for name, part in (("validation", val), ("test", test)):
        base = [play_duel(d, None) for d in part]
        new = [play_duel(d, model) for d in part]
        bc, nc = [b["capture"] for b in base if b["played"]], [n["capture"] for n in new if n["played"]]
        if not bc:
            out[name] = {"n": 0}
            continue
        reg = [(d["duel"], round(b["capture"], 1), round(n["capture"], 1)) for d, b, n in zip(part, base, new)
               if b["played"] and n["capture"] < b["capture"] - 0.5]
        imp = [(d["duel"], round(b["capture"], 1), round(n["capture"], 1)) for d, b, n in zip(part, base, new)
               if b["played"] and n["capture"] > b["capture"] + 0.5]
        actual = [d.get("result") or 0.0 for d in part]
        lo, hi = bootstrap_diff(nc, bc)
        out[name] = {"n": len(bc), "base": {"captured": round(sum(bc), 1), "mean": round(statistics.mean(bc), 2),
                                           "closed": sum(1 for b in base if b["played"] and b["accept_tick"] is not None)},
                     "learned": {"captured": round(sum(nc), 1), "mean": round(statistics.mean(nc), 2),
                                 "closed": sum(1 for n in new if n["played"] and n["accept_tick"] is not None),
                                 "decisions_changed": sum(1 for n in new if n.get("learned"))},
                     "ceiling_best_offer": round(sum(b["best"] for b in base if b["played"]), 1),
                     "server_actual_result_sum": round(sum(actual), 1),
                     "diff_mean_ci90": [lo, hi], "regressions": reg, "improvements": imp,
                     "note": "capture = excedente total al aceptar (rondas=0); el actual del servidor incluye nuestras ofertas y "
                             "decay, no es comparable 1:1"}
    out["model"] = model.summary()
    return out


# ------------------------------------------------------------------ simulación con semillas disjuntas

def sim_report(n_train: int = 150, n_test: int = 120, days: bool = True) -> dict:
    def waves(seed0, n, model):
        res, hist = [], []
        saved, hooks = dict(dl.PARAMS), dict(dl.HOOKS)
        try:
            dl.PARAMS.update(PRODUCTION | {"PLAY_DAYS": days, "PROFILES": True, "LADDER": ()})
            if model is not None:
                dl.PARAMS["LEARN"] = True
                dl.HOOKS["refine"] = lambda dd, f, a, r: L.refine(dd, f, a, r, model)
                dl.HOOKS["open_share"] = lambda dd, left, base: L.best_open_share(dd, left, model, base)
            else:
                dl.PARAMS["LEARN"] = False
                dl.HOOKS["refine"] = dl.HOOKS["open_share"] = None
            for i in range(n):
                rng = random.Random(seed0 + i)
                kinds = [rng.choice(list(duel_sim.RIVALS)) for _ in range(duel_sim.WAVE)]
                res += duel_sim.play_wave(kinds, random.Random(seed0 * 7 + i), None, days, False, collect=hist, base_duel=i * 10)
        finally:
            dl.PARAMS.clear()
            dl.PARAMS.update(saved)
            dl.HOOKS.update(hooks)
        return res, hist
    _, hist_train = waves(1000, n_train, None)                      # historia de entrenamiento: política base, semillas 1000+
    model = L.Model().fit(L.normalize(hist_train))
    base, _ = waves(50_000, n_test, None)                            # prueba: semillas 50 000+ (disjuntas)
    new, _ = waves(50_000, n_test, model)
    f = lambda rs: {"capture": round(statistics.mean(r["capture"] for r in rs), 4), "deal_rate": round(sum(r["deal"] for r in rs) / len(rs), 3),
                    "outside_limit": sum(r["outside"] for r in rs), "n": len(rs)}
    diffs = [b["capture"] - a["capture"] for a, b in zip(base, new)]
    ms = sorted(statistics.mean(random.Random(i).choice(diffs) for _ in diffs) for i in range(200))
    return {"train_waves": n_train, "test_waves": n_test, "train_duels": len(hist_train), "base": f(base), "learned": f(new),
            "diff_capture_ci90": [round(ms[10], 4), round(ms[189], 4)],
            "assumptions": ["rivales sintéticos (cedente, firme, mudo, espejo, impaciente) con reservas sorteadas",
                            "el modelo se entrena con la historia de la política BASE y se prueba con semillas disjuntas",
                            "los resultados dependen de ese modelo de rivales: no son un rendimiento real en el servidor"]}


# ------------------------------------------------------------------ resumen por situación

def situations(records: list) -> list:
    rows = []
    by = {}
    for r in records:
        if r["session"] == 1:
            continue
        key = (r["role"], "días" if r["days"] else "precio", r["rival"])
        by.setdefault(key, []).append(r)
    allres = [r["result"] for r in records if r["status"] == "deal" and r.get("result") is not None and r["session"] != 1]
    prior = statistics.mean(allres) if allres else 0.0
    for key, rs in sorted(by.items()):
        deals = [r for r in rs if r["status"] == "deal"]
        res = [r["result"] for r in deals if r.get("result") is not None]
        lo, hi = L.wilson(len(deals), len(rs))
        w = len(res) / (len(res) + L.POOL_K) if res else 0.0
        mean = w * statistics.mean(res) + (1 - w) * prior if res else prior
        rows.append({"role": key[0], "tema": key[1], "rival": key[2], "n": len(rs), "deal_rate": round(len(deals) / len(rs), 2),
                     "deal_ci90": [lo, hi], "mean_result_shrunk": round(mean, 1), "n_results": len(res),
                     "nosotros_aceptamos": sum(1 for r in deals if r["inferred"]["closer"] == "nosotros_aceptamos_su_oferta"),
                     "rival_acepto_la_nuestra": sum(1 for r in deals if r["inferred"]["closer"] == "el_rival_acepto_la_nuestra"),
                     "negativos": sum(1 for r in deals if (r.get("result") or 0) < 0),
                     "etiqueta": "propia" if len(rs) >= L.MIN_N else "muestra pequeña (media encogida hacia el global)"})
    return rows


def public_context(path=DATA / "agent_memory.sqlite3") -> dict:
    """duel.closed del feed público: estado agregado por sesión (no hay equipos ni ofertas: no se infiere nada de rivales concretos)."""
    import sqlite3
    out = {}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for (body,) in db.execute("SELECT body FROM events WHERE body LIKE '%duel.closed%'"):
            e = json.loads(body)
            p = e.get("payload") or {}
            k = p.get("session")
            out.setdefault(k, {"deal": 0, "no_deal": 0})[p.get("status") if p.get("status") in ("deal", "no_deal") else "no_deal"] += 1
        db.close()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {str(k): dict(v, rate=round(v["deal"] / max(1, v["deal"] + v["no_deal"]), 2)) for k, v in sorted(out.items(), key=lambda kv: str(kv[0]))}


def fetch() -> int:
    from bazaar_sdk import Bazaar
    key = os.environ.get("BAZAAR_KEY")
    if not key:
        raise SystemExit("falta BAZAAR_KEY (./run.sh o carga .env)")
    b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key)
    d = b.duels(done=True).get("duels", [])
    HISTORY.write_text(json.dumps(d, ensure_ascii=False))
    print(f"{len(d)} duelos guardados en {HISTORY} (solo lectura)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["audit", "summary", "replay", "waves", "sim", "all", "fetch"])
    ap.add_argument("--history", default=str(HISTORY))
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return fetch()
    raw = L.load_history(a.history)
    rows = log_rows()
    rec = L.normalize(raw, rows)
    if a.cmd in ("audit", "all"):
        print("AUDITORÍA (resultado confirmado por el servidor)")
        print(json.dumps(L.audit(rec, raw, rows), ensure_ascii=False, indent=1))
        print("DECISIONES QUE FALLARON")
        print(json.dumps(L.failures(rec), ensure_ascii=False, indent=1))
        print("CONTEXTO PÚBLICO (duel.closed del feed)", json.dumps(public_context(), ensure_ascii=False))
    if a.cmd in ("summary", "all"):
        print("RESUMEN POR SITUACIÓN (rol · tema · rival)")
        for r in situations(rec):
            print(f"  {r['role']:6} {r['tema']:6} {r['rival']:9} n={r['n']:3} cierre {r['deal_rate']:.0%} IC90 {r['deal_ci90']} "
                  f"resultado medio {r['mean_result_shrunk']:>5} (n={r['n_results']}) nosotros {r['nosotros_aceptamos']} / aceptó el "
                  f"rival {r['rival_acepto_la_nuestra']} / negativos {r['negativos']} · {r['etiqueta']}")
    if a.cmd in ("replay", "all"):
        print("REPLAY HISTÓRICO (separación temporal)")
        print(json.dumps(replay_report(raw), ensure_ascii=False, indent=1))
    if a.cmd in ("waves", "all"):
        print("REPLAY POR OLEADAS (una aceptación por tick; anterior frente a agresivo)")
        for name, ex in (("anterior", {"AGGRESSIVE": False}), ("agresivo", {"AGGRESSIVE": True})):
            print(f"  {name:9}", json.dumps(wave_replay(raw, extra=ex), ensure_ascii=False))
    if a.cmd in ("sim", "all"):
        print("SIMULACIÓN (semillas de entrenamiento y de prueba disjuntas)")
        print(json.dumps(sim_report(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
