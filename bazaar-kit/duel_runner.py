"""Ejecutor de duelos: SOLO duelos (duel_say / duel_accept). No toca vendedores, mercado ni campañas.

    python3 duel_runner.py                 # análisis (por defecto): lee duelos y muestra lo que haría, no envía nada
    python3 duel_runner.py --execute       # juega los duelos vivos con la política de duels.py
    python3 duel_runner.py --execute --days --ladder --profiles --reconcile --verify-accept   # recomendado Duelos II

Opt-in (por defecto desactivados, medidos con duel_sim.py):
    --days           jugar duelos de precio + días (sin él se omiten y puntúan 0)
    --ladder         frente a rivales mudos, escalera de ofertas en vez de una sola
    --probe          frente a un rival plantado, una contraoferta antes de aceptar (neutro en duel_sim.py)
    --reconcile      una escritura ambigua (timeout) NO detiene el ejecutor: se reconcilia leyendo el estado
    --verify-accept  releer el duelo justo antes de aceptar y no aceptar si la oferta rival cambió a peor
    --profiles       reglas por bot de la casa (Rojo/Noche: aceptar pronto; Oro/Luna: tras el salto; Plata/Verde:
                     esperar; mudos: ofertas propias pronto y escalonadas)
    --logroll        con --days, conceder días que nos cuestan poco a cambio de precio (neutro en duel_sim.py)

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
LADDER = (0.20, 0.12, 0.06)
PENDING_TICKS = 1      # un duelo aceptado se excluye hasta tick + PENDING_TICKS (liquida en el tick siguiente)
GRID = {"MID_ACCEPT_RATIO": [0.1, 0.15, 0.2], "STALL_TICKS": [2, 3, 4], "SAFE_TICKS": [1, 2]}


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
    ap.add_argument("--days", action="store_true",
                    help="jugar duelos con días (Duelos II): precio dentro de límite y cada oferta con days; "
                         "sin el flag se saltan y puntúan 0")
    ap.add_argument("--ladder", action="store_true", help=f"opt-in: escalera {LADDER} frente a rivales mudos")
    ap.add_argument("--probe", action="store_true", help="opt-in: una contraoferta a rivales plantados")
    ap.add_argument("--reconcile", action="store_true", help="opt-in: no detenerse ante una escritura ambigua")
    ap.add_argument("--verify-accept", action="store_true", help="opt-in: releer el duelo antes de aceptar")
    ap.add_argument("--profiles", action="store_true", help="opt-in: reglas por perfil del bot de la casa")
    ap.add_argument("--logroll", action="store_true", help="opt-in (con --days): días baratos a cambio de precio")
    a = ap.parse_args()
    dl.PARAMS["PLAY_DAYS"] = a.days
    if a.ladder:
        dl.PARAMS["LADDER"] = LADDER
    if a.probe:
        dl.PARAMS["PROBE"] = True
    dl.PARAMS["PROFILES"] = a.profiles
    dl.PARAMS["LOGROLL"] = a.logroll
    key = os.environ.get("BAZAAR_KEY", "")
    if not key or key == "tk-xxxx-xxxx":
        raise SystemExit("Falta BAZAAR_KEY (carga .env o exporta la variable)")
    # retries: un GET fallido (rate_limited, red) ya no hace perder el tick; las escrituras con fallo de red nunca se
    # repiten (bazaar_sdk las propaga al instante) y rate_limited es un rechazo seguro de reintentar.
    b = PacedBazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key, wait_on_tick=False, retries=2)
    log({"start": True, "execute": a.execute, "params": dl.PARAMS, "deprecated": dl.DEPRECATED,
         "reconcile": a.reconcile, "verify_accept": a.verify_accept,
         "note": "safe_ticks efectivo = SAFE_TICKS + max(duelos con el mismo deadline, cola de aceptables) − 1; "
                 "ver facts por decisión"})
    lock = InstanceLock(str(DATA / "agent.lock"), {"version": "duels-1.1"})
    if a.execute:
        holder = lock.acquire()
        if holder:
            raise SystemExit(f"Otro agente está activo (pid {holder.get('pid')}); detén ese agente antes de ejecutar duelos")
    try:
        run(b, a.execute, a.feed_file, reconcile=a.reconcile, verify_accept=a.verify_accept)
    finally:
        if a.execute:
            lock.release()


def _offer_key(d: dict):
    ro = d.get("rival_offer") or {}
    return ro.get("id"), ro.get("price"), ro.get("days")


def _still_acceptable(b, cand: dict, snapshot: dict) -> bool:
    """Relee el duelo: aceptar solo si la oferta rival sigue siendo la del snapshot (o una igual de aceptable)."""
    fresh = {d.get("duel"): d for d in b.duels().get("duels", [])}.get(cand["duel"])
    if not fresh or fresh.get("status") != "live":
        return False
    if _offer_key(fresh) == _offer_key(snapshot):
        return True
    ro = fresh.get("rival_offer") or {}
    days = ro.get("days") if "days" in (fresh.get("issues") or []) else None
    m = dl.margin(fresh, ro.get("price"), days)
    pm = dl.margin(dict(fresh, your_days_weight=None), ro.get("price"))
    return m is not None and pm is not None and pm >= 0 and m >= cand["du"]


def _timeout_for(c: dict) -> float:
    """Que una petición colgada no se coma el tick: como mucho media duración de tick (3–15 s)."""
    ts = c.get("tick_seconds")
    return 15.0 if not isinstance(ts, (int, float)) or ts <= 0 else max(3.0, min(15.0, 0.5 * ts))


def run(b, execute, feed_file: Path | None = None, reconcile=False, verify_accept=False):
    last_tick = None
    seen_weights: set = set()
    pending: dict = {}           # duelo aceptado → tick del envío (no volver a gastar la aceptación del tick en él)
    while True:
        try:
            c = b.clock()
            if c.get("paused") or c["tick"] == last_tick:
                nxt = c.get("next_tick_in")
                wait = 10 if c.get("paused") else min(1.0, max(0.2, float(nxt))) if isinstance(nxt, (int, float)) else 1.0
                time.sleep(wait)
                continue
            if hasattr(b, "timeout"):
                b.timeout = _timeout_for(c)
            tick = c["tick"]
            live = [d for d in b.duels().get("duels", []) if d.get("status") == "live"]
            last_tick = tick                 # solo tras leer los duelos: un fallo de lectura no hace perder el tick
            if not live:
                time.sleep(2)
                continue
            for d in live:
                if "days" in (d.get("issues") or []):
                    if not dl.PARAMS["PLAY_DAYS"]:
                        log({"tick": tick, "duel": d.get("duel"), "skipped": "days utility not verified"})
                    elif d.get("duel") not in seen_weights:   # formato real de your_days_weight, una vez por duelo
                        seen_weights.add(d.get("duel"))
                        log({"tick": tick, "duel": d.get("duel"), "your_days_weight": d.get("your_days_weight"),
                             "days_table": dl.days_table(d)})
            pending = {k: v for k, v in pending.items() if tick <= v + PENDING_TICKS}
            playable = [d for d in live if d.get("duel") not in pending]
            by_id = {d.get("duel"): d for d in playable}
            events = load_feed_events(feed_file)
            accepted = False
            for step in duel_tree.plan(playable, tick, events,
                                       load_feed_events(LOG)):
                cand = step["candidate"]
                if step["action"] in {"wait", "defer", "already_sent"}:
                    log({"tick": tick, "duel": step["duel"],
                         "action": step["action"], "path": step["path"],
                         "trigger": step["trigger"], "facts": step["facts"],
                         "reason": step["reason"], "feed": step["feed"], "sent": False})
                    continue
                if cand["type"] == "duel_accept" and accepted:
                    continue                 # una aceptación por tick y equipo
                rec = {"tick": tick, **{k: v for k, v in cand.items() if k != "score"}}
                rec.update(action=step["action"], path=step["path"],
                           trigger=step["trigger"], facts=step["facts"], feed=step["feed"])
                if not execute:
                    log({**rec, "sent": False})
                    continue
                try:
                    if cand["type"] == "duel_accept":
                        if verify_accept and not _still_acceptable(b, cand, by_id.get(cand["duel"], {})):
                            log({**rec, "sent": False, "skipped": "la oferta rival cambió antes de aceptar"})
                            continue
                        b.duel_accept(cand["duel"])
                        accepted = True
                        pending[cand["duel"]] = tick
                    else:
                        b.duel_say(cand["duel"], text=cand["text"], price=cand["price"], days=cand.get("days"))
                    log({**rec, "sent": True})
                except BazaarError as e:
                    log({**rec, "sent": False, "error": e.code, "message": e.message})
                    if e.code in {"network", "bad_response"}:
                        if not reconcile:
                            raise SystemExit("Escritura de duelo ambigua: revisar estado del servidor antes de reiniciar")
                        if cand["type"] == "duel_accept":   # pudo llegar: gastada la del tick; se reconcilia leyendo
                            accepted = True
                            pending[cand["duel"]] = tick
                    if cand["type"] == "duel_accept" and e.code == "wait_for_tick":
                        accepted = True
        except BazaarError as e:
            log({"error": e.code, "message": e.message})
            time.sleep(1 if e.code in {"rate_limited", "network"} else 3)
        except KeyboardInterrupt:
            break
        except Exception as e:  # un dato inesperado no puede tumbar el ejecutor en mitad de la sesión
            log({"error": "unexpected", "message": f"{type(e).__name__}: {e}"})
            time.sleep(2)


if __name__ == "__main__":
    main()
