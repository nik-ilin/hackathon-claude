"""Ejecutor de duelos: SOLO duelos (duel_say / duel_accept). No toca vendedores, mercado ni campañas.

    python3 duel_runner.py                 # análisis (por defecto): lee duelos y muestra lo que haría, no envía nada
    python3 duel_runner.py --execute       # juega los duelos vivos con la política de duels.py

Convivencia con el coordinador: este proceso solo actúa cuando hay duelos vivos y lo hace al principio de cada tick.
Solo hay una aceptación por tick y equipo; si el coordinador ya la usó, el servidor responde `wait_for_tick` (no
cuesta nada) y se reintenta en el tick siguiente. Durante una oleada de duelos conviene que el coordinador no acepte
(los duelos vencen todos a la vez).

Automejora: al terminar cada oleada repite todos los duelos terminados de las dos últimas sesiones con una rejilla de
parámetros y adopta la mejor combinación solo si mejora ≥ 3 % la captura (data/duel_params.json). Registro de cada
decisión en data/duels_log.jsonl.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path

import duels as dl
from bazaar_sdk import Bazaar, BazaarError

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOG = DATA / "duels_log.jsonl"
PARAMS_FILE = DATA / "duel_params.json"
GRID = {"GOOD_SHARE": [0.4, 0.5, 0.6, 0.7, 0.8], "STALL_TICKS": [2, 3, 4], "SAFE_TICKS": [1, 2]}


def log(rec: dict) -> None:
    DATA.mkdir(exist_ok=True)
    rec = {"ts": round(time.time(), 1), **rec}
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_params() -> None:
    if PARAMS_FILE.exists():
        dl.PARAMS.update(json.loads(PARAMS_FILE.read_text()))


def retune(b: Bazaar) -> None:
    done = [d for d in b.duels(done=True).get("duels", []) if d.get("status") != "live"]
    sessions = sorted({d.get("session") for d in done if d.get("session") is not None})
    recent = [d for d in done if d.get("session") in sessions[-2:]]
    if sum(1 for d in recent if d.get("messages")) < 6:
        return
    current = {k: dl.PARAMS[k] for k in GRID}
    base = dl.replay(recent, current)["captured"]
    best, best_v = current, base
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(GRID, combo))
        r = dl.replay(recent, p)
        if r["outside_limit"] == 0 and r["captured"] > best_v:
            best, best_v = p, r["captured"]
    if best != current and best_v >= base * 1.03 + 1:
        dl.PARAMS.update(best)
        DATA.mkdir(exist_ok=True)
        PARAMS_FILE.write_text(json.dumps({k: dl.PARAMS[k] for k in GRID}))
        log({"retune": best, "from": current, "replay": [round(base), round(best_v)], "duels": len(recent)})
    else:
        log({"retune": "sin cambios", "params": current, "replay": round(base), "duels": len(recent)})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--execute", action="store_true", help="enviar (por defecto solo análisis)")
    a = ap.parse_args()
    key = os.environ.get("BAZAAR_KEY", "")
    if not key or key == "tk-xxxx-xxxx":
        raise SystemExit("Falta BAZAAR_KEY (usa ./run.sh o exporta la variable)")
    b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key, wait_on_tick=False)
    load_params()
    last_tick, had_live = None, False
    log({"start": True, "execute": a.execute, "params": dl.PARAMS})
    while True:
        try:
            c = b.clock()
            if c.get("paused") or c["tick"] == last_tick:
                time.sleep(1.0 if not c.get("paused") else 10)
                continue
            last_tick = c["tick"]
            live = [d for d in b.duels().get("duels", []) if d.get("status") == "live"]
            if not live:
                if had_live:                 # la oleada acaba de terminar: aprender de ella
                    retune(b)
                had_live = False
                time.sleep(2)
                continue
            had_live = True
            accepted = False
            for cand in dl.duel_candidates(live, c["tick"]):
                if cand["type"] == "duel_accept" and accepted:
                    continue                 # una aceptación por tick y equipo
                rec = {"tick": c["tick"], **{k: v for k, v in cand.items() if k != "score"}}
                if not a.execute:
                    log({**rec, "sent": False})
                    continue
                try:
                    if cand["type"] == "duel_accept":
                        b.duel_accept(cand["duel"])
                        accepted = True
                    else:
                        b.duel_say(cand["duel"], text=cand["text"], price=cand["price"], days=cand.get("days"))
                    log({**rec, "sent": True})
                except BazaarError as e:
                    log({**rec, "sent": False, "error": e.code, "message": e.message})
                    if cand["type"] == "duel_accept" and e.code == "wait_for_tick":
                        accepted = True
        except BazaarError as e:
            log({"error": e.code, "message": e.message})
            time.sleep(3)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
