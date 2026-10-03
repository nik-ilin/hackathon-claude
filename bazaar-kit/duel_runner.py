"""Ejecutor de duelos: SOLO duelos (duel_say / duel_accept). No toca vendedores, mercado ni campañas.

    python3 duel_runner.py                 # análisis (por defecto): lee duelos y muestra lo que haría, no envía nada
    python3 duel_runner.py --execute       # juega los duelos vivos con la política de duels.py

Ejecución exclusiva: comparte data/agent.lock con los agentes existentes. Detener el coordinador antes de
usar --execute. El modo análisis no envía operaciones ni reajusta parámetros. El replay histórico es exploratorio:
no reproduce la competencia de varios duelos por una aceptación por tick, por lo que no se usa para autoajustar.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path

import duels as dl
import duel_tree
from bazaar_sdk import Bazaar, BazaarError
from negotiation import InstanceLock

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


def load_feed_events(path: Path | None) -> list[dict]:
    """El feed es contexto opcional; una línea parcial no frena el duelo."""
    if not path:
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            events = []
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            return events
    except OSError:
        return []


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


class PacedBazaar(Bazaar):
    """Espaciar todas las peticiones; no reintentar escrituras ambiguas."""
    def _call(self, *args, **kwargs):
        time.sleep(max(0.0, 0.3 - (time.monotonic() - getattr(self, "_last_request", 0))))
        self._last_request = time.monotonic()
        return super()._call(*args, **kwargs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--execute", action="store_true", help="enviar (por defecto solo análisis)")
    ap.add_argument("--feed-file", type=Path, default=DATA / "feed_history.jsonl",
                    help="JSONL del feed público para contexto (por defecto data/feed_history.jsonl)")
    a = ap.parse_args()
    key = os.environ.get("BAZAAR_KEY", "")
    if not key or key == "tk-xxxx-xxxx":
        raise SystemExit("Falta BAZAAR_KEY (carga .env o exporta la variable)")
    b = PacedBazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key, wait_on_tick=False, retries=0)
    log({"start": True, "execute": a.execute, "params": dl.PARAMS})
    lock = InstanceLock(str(DATA / "agent.lock"), {"version": "duels-1.0"})
    if a.execute:
        holder = lock.acquire()
        if holder:
            raise SystemExit(f"Otro agente está activo (pid {holder.get('pid')}); detén ese agente antes de ejecutar duelos")
    try:
        run(b, a.execute, a.feed_file)
    finally:
        if a.execute:
            lock.release()


def run(b, execute, feed_file: Path | None = None):
    last_tick = None
    while True:
        try:
            c = b.clock()
            if c.get("paused") or c["tick"] == last_tick:
                time.sleep(1.0 if not c.get("paused") else 10)
                continue
            last_tick = c["tick"]
            live = [d for d in b.duels().get("duels", []) if d.get("status") == "live"]
            if not live:
                time.sleep(2)
                continue
            for d in live:
                if "days" in (d.get("issues") or []):
                    log({"tick": c["tick"], "duel": d["duel"], "skipped": "days utility not verified"})
            events = load_feed_events(feed_file)
            accepted = False
            for step in duel_tree.plan(live, c["tick"], events):
                cand = step["candidate"]
                if step["action"] in {"wait", "defer"}:
                    log({"tick": c["tick"], "duel": step["duel"],
                         "action": step["action"], "path": step["path"],
                         "reason": step["reason"], "feed": step["feed"], "sent": False})
                    continue
                if cand["type"] == "duel_accept" and accepted:
                    continue
                rec = {"tick": c["tick"], **{k: v for k, v in cand.items() if k != "score"}}
                rec.update(action=step["action"], path=step["path"], feed=step["feed"])
                if not execute:
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
                    if e.code in {"network", "bad_response"}:
                        raise SystemExit("Escritura de duelo ambigua: revisar estado del servidor antes de reiniciar")
                    if cand["type"] == "duel_accept" and e.code == "wait_for_tick":
                        accepted = True
        except BazaarError as e:
            log({"error": e.code, "message": e.message})
            time.sleep(3)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
