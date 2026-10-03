"""MODO SOMBRA: compara la política actual con la propuesta sobre el MISMO estado, sin enviar nada ni tocar el servidor.

    python3 shadow.py                      # rejilla de escenarios de cierre-ahora-frente-a-esperar + último informe real
    python3 shadow.py --report data/coordinator_report.json   # solo el selector sobre las candidatas del último ciclo

1. CERRAR vs ESPERAR: la regla anterior (`legacy_close`) frente a `decision.compare` (rangos, confianza, evidencia ≥3).
2. SELECTOR: `select(..., "legacy")` frente a `select(..., "economic")` sobre las candidatas de un ciclo real guardado.
Cada desacuerdo se explica; ninguna de las dos políticas ejecuta nada aquí.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import decision as dec
import fast_sales as fs


def legacy_close(best_net, minimum, objective, closes, urgent=False, deadline=False):
    """La regla de la primera versión de ventas rápidas: esperar si había ≥2 cierres y el objetivo superaba la puja en >1 P."""
    if best_net is None or best_net < minimum:
        return "no_accept"
    if best_net >= objective or urgent or deadline:
        return "accept"
    return "wait" if (len(closes) >= 2 and objective > best_net + 1) else "accept"


def proposed_close(best_net, minimum, objective, closes, urgent=False, deadline=False, loss=5.0, expires_in=6, live_bids=()):
    ev = {"bids": [{"offer": i + 2, "team": f"t{i}", "net": n, "expires": 99} for i, n in enumerate(live_bids)],
          "closes": [{"price": p, "tick": 0} for p in closes], "recent_buyers": []}
    best = {"net": best_net, "expires_in": expires_in} if best_net is not None and best_net >= minimum else None
    r = dec.compare(loss=loss, minimum=minimum, objective=objective, best=best, alts=dec.alternatives(ev, None, 0),
                    ticks_left=4, w=1.0 if urgent else 0.0, deadline=deadline)
    return {"accept": "accept", "wait_list": "wait", "list": "no_accept", "hold": "no_accept"}[r["action"]], r


SCENARIOS = [
    ("puja = objetivo", dict(best_net=14, minimum=9, objective=14, closes=[])),
    ("puja entre mínimo y objetivo, sin evidencia", dict(best_net=11, minimum=9, objective=14, closes=[])),
    ("puja baja, 2 cierres altos (poca evidencia)", dict(best_net=11, minimum=9, objective=14, closes=[14, 15])),
    ("puja baja, 3 cierres altos (evidencia suficiente)", dict(best_net=11, minimum=9, objective=14, closes=[14, 15, 16])),
    ("puja 1 P bajo el objetivo, 3 cierres", dict(best_net=13, minimum=9, objective=14, closes=[14, 14, 14])),
    ("puja baja con urgencia de caja", dict(best_net=11, minimum=9, objective=14, closes=[14, 15, 16], urgent=True)),
    ("puja baja, presupuesto de ticks agotado", dict(best_net=11, minimum=9, objective=14, closes=[14, 15, 16], deadline=True)),
    ("puja bajo el mínimo", dict(best_net=7, minimum=9, objective=14, closes=[14, 15, 16])),
    ("puja baja y otra puja vigente peor", dict(best_net=11, minimum=9, objective=14, closes=[], live_bids=(10,))),
]


def close_table() -> list:
    rows = []
    for name, kw in SCENARIOS:
        old = legacy_close(**{k: v for k, v in kw.items() if k in ("best_net", "minimum", "objective", "closes", "urgent", "deadline")})
        new, r = proposed_close(**kw)
        rows.append({"scenario": name, "legacy": old, "proposed": new, "agree": old == new, "why": r["reason"],
                     "confidence": r["confidence"]})
    return rows


def selector_diff(cands: list, tick: int = 0, max_posts: int = 3) -> dict:
    import coordinator as co
    led = {"class_tick": {}, "class_count": {}, "blocked": {}}
    a = co.select([dict(c) for c in cands], led, tick, max_posts, "legacy")
    b = co.select([dict(c) for c in cands], led, tick, max_posts, "economic")
    name = lambda c: f"{c['type']}:{c.get('ref') or c.get('offer') or c.get('thread')}"
    only_a = [name(c) for c in a if name(c) not in {name(x) for x in b}]
    only_b = [name(c) for c in b if name(c) not in {name(x) for x in a}]
    return {"legacy": [name(c) for c in a], "economic": [name(c) for c in b], "only_legacy": only_a, "only_economic": only_b,
            "tiers": {name(c): co.priority_tier(c) for c in b}}


def windows_diff(report: dict, led: dict) -> list:
    """Ventas rápidas sobre el registro REAL: precio de la próxima propuesta con la política anterior (mismo objetivo en cada
    ventana) frente a la revisión por ventanas secas. Solo lectura."""
    fsr = (report.get("plan") or {}).get("fast_sales_report") or {}
    tick = report.get("tick") or 0
    cfg = fs.Config(refs=tuple(r["ref"] for r in fsr.get("refs", [])))
    out = []
    for r in fsr.get("refs", []):
        if not r.get("authorized"):
            continue
        p = r["prices"]
        for a in r.get("free", []) + [int(k) for k in (r.get("locked") or {})]:
            st = fs.stage(led, a, tick, cfg)
            new, why = fs.revised_price(max(p["objective"], p["minimum"]), p["minimum"], st, cfg, bool(fsr.get("urgent")))
            out.append({"ref": r["ref"], "asset": a, "windows": st["windows"], "dry": st["dry_windows"],
                        "legacy_next": max(p["objective"], p["minimum"]), "proposed_next": new, "why": why or "sin cambio"})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", default=None, help="coordinator_report.json (candidatas de un ciclo real)")
    a = ap.parse_args(argv)
    if not a.report:
        print("CERRAR AHORA frente a ESPERAR (política anterior → propuesta)")
        for r in close_table():
            print(f"  {'=' if r['agree'] else '≠'} {r['scenario']:52} {r['legacy']:9} → {r['proposed']:9} ({r['confidence']}) {'' if r['agree'] else r['why']}")
    path = Path(a.report or Path(__file__).parent / "data" / "coordinator_report.json")
    if path.exists():
        rep = json.loads(path.read_text())
        d = selector_diff(rep.get("candidates", []), rep.get("tick", 0))
        print(f"\nSELECTOR sobre el último ciclo real (tick {rep.get('tick')}, sin enviar nada)")
        print("  legacy  :", d["legacy"])
        print("  economic:", d["economic"])
        print("  solo legacy:", d["only_legacy"], "· solo economic:", d["only_economic"])
        ledger = path.parent / "coordinator_ledger.json"
        if ledger.exists():
            print("\nVENTANAS de ventas rápidas sobre el registro real (anterior → propuesta)")
            for w in windows_diff(rep, json.loads(ledger.read_text())):
                print(f"  {w['ref']} #{w['asset']}: {w['windows']} ventanas, {w['dry']} secas seguidas · próxima "
                      f"{w['legacy_next']} P → {w['proposed_next']} P · {w['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
