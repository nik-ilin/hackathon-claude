"""Ejecutor de duelos: SOLO duelos (duel_say / duel_accept). No toca vendedores, mercado ni campañas.

    python3 duel_runner.py                 # análisis (por defecto): lee duelos y muestra lo que haría, no envía nada
    python3 duel_runner.py --execute       # juega los duelos vivos con la política de duels.py
    python3 duel_runner.py --execute --days --ladder --profiles --reconcile --verify-accept   # recomendado Duelos II
    # Registro (data/duels_log.jsonl): `accept_requested`/`offer_sent` con sent:true NO son tratos puntuados; el estado final
    # (deal/no_deal, precio, días, rol, deadline, predicción y error) lo escribe `reconciled` en el tick siguiente, y
    # `score_snapshot` guarda duel_points/negotiating de /api/me antes y después (variación observada, no atribuida).

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
from typing import Optional

import duels as dl
import duel_learning
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
    ap.add_argument("--learn", action="store_true",
                    help="opt-in: aprendizaje en línea con los duelos TERMINADOS (resultado del servidor): refina accept/wait y el ancla "
                         "de apertura solo con evidencia suficiente; sin ella, política base. Registra la razón de cada decisión")
    ap.add_argument("--day3", action="store_true",
                    help="preset Duelos III: --days --ladder --profiles --reconcile --verify-accept; "
                         "--learn y --logroll siguen opt-in")
    a = ap.parse_args()
    if a.day3:
        a.days = a.ladder = a.profiles = a.reconcile = a.verify_accept = True
    dl.PARAMS["PLAY_DAYS"] = a.days
    if a.ladder:
        dl.PARAMS["LADDER"] = LADDER
    if a.probe:
        dl.PARAMS["PROBE"] = True
    dl.PARAMS["PROFILES"] = a.profiles
    dl.PARAMS["LOGROLL"] = a.logroll
    learner = duel_learning.Learner(LOG) if a.learn else None
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
        run(b, a.execute, a.feed_file, reconcile=a.reconcile, verify_accept=a.verify_accept, track=True, learner=learner)
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
    m = dl.margin(fresh, ro.get("price"), days)               # excedente TOTAL: precio + utilidad firmada de los días
    pm = dl.price_margin(fresh, ro.get("price"))              # límite de precio del servidor, restricción independiente
    return m is not None and pm is not None and pm >= 0 and m > 0 and m >= cand["du"]


def score_snapshot(b, tick: int, prev: dict | None) -> dict | None:
    """Lee duel_points y negotiating de /api/me. La variación es OBSERVADA entre dos lecturas: no se atribuye a un duelo
    concreto (el leaderboard público se refresca con retraso y `/api/me` mezcla todo lo que pasó entre lecturas)."""
    try:
        sc = (b.me() or {}).get("score") or {}
        snap = {"tick": tick, "duel_points": sc.get("duel_points"), "negotiating": sc.get("negotiating"),
                "score": sc.get("score"), "rank": sc.get("rank")}
        if not isinstance(snap["duel_points"], (int, float)) and not isinstance(snap["negotiating"], (int, float)):
            return None
    except Exception:  # noqa: BLE001 — la medición nunca debe frenar el duelo
        return None
    if prev:
        snap["delta"] = {k: round(snap[k] - prev[k], 2) for k in ("duel_points", "negotiating", "score")
                         if isinstance(snap.get(k), (int, float)) and isinstance(prev.get(k), (int, float))}
        snap["since_tick"] = prev["tick"]
    snap["note"] = "variación observada entre lecturas de /api/me; no atribuida a un duelo (el leaderboard público va con retraso)"
    return snap


def _learn(learner, done: list, tick: int) -> None:
    """Reajusta el modelo cuando hay un duelo resuelto nuevo (y lo instala la primera vez). Registra qué cambió."""
    if learner is None or not done:
        return
    try:
        first = learner.model is None
        if learner.update(done):
            if first:
                learner.install()
            snap = learner.snapshot()
            log({"event": "learn_update", "tick": tick, **snap})
            try:
                (DATA / "duel_learning.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1))
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 — el aprendizaje nunca frena el duelo
        log({"event": "learn_error", "tick": tick, "error": f"{type(e).__name__}: {e}"})


def merge_done(history: list, newly_done: list) -> list:
    """Retain the full confirmed cohort after a settlement; the latest server row wins."""
    by_id = {d["duel"]: d for d in (history or []) + (newly_done or [])
             if d.get("duel") is not None and d.get("status") in ("deal", "no_deal")}
    return list(by_id.values())


def refresh_history(b, history: list, history_tick: int | None, tick: int) -> tuple[list, int | None, bool]:
    """Refresh confirmed evidence; a failed GET must be retried next tick."""
    if history_tick is not None and tick - history_tick < 20:
        return history, history_tick, False
    try:
        done = b.duels(done=True).get("duels", [])
    except BazaarError:
        return history, history_tick, False
    return merge_done(history, done), tick, True


def reconcile_accepted(b, awaiting: dict, tick: int, stats: dict) -> Optional[list]:
    """Tras aceptar, en el tick siguiente se relee el duelo y se registra su estado FINAL (deal/no_deal, precio, días, rol,
    deadline) con la predicción hecha antes de aceptar. `sent: true` solo prueba que se envió la acción."""
    due = {k: v for k, v in awaiting.items() if tick > v["tick"]}
    if not due:
        return None
    done_list = b.duels(done=True).get("duels", [])
    done = {d.get("duel"): d for d in done_list}
    for duel_id, w in due.items():
        d = done.get(duel_id)
        if d is None or d.get("status") == "live":
            if tick - w["tick"] >= 3:
                log({"event": "reconcile_pending", "tick": tick, "duel": duel_id, "accepted_at": w["tick"],
                     "note": "sigue sin liquidarse tras 3 ticks: revisar en el servidor"})
                awaiting.pop(duel_id, None)
            continue
        pred = w["prediction"] or {}
        real = d.get("result")
        stats["deals" if d.get("status") == "deal" else "no_deals"] += 1
        log({"event": "reconciled", "tick": tick, "duel": duel_id, "status": d.get("status"), "role": d.get("role"),
             "price": d.get("price"), "days": d.get("days"), "deadline_tick": d.get("deadline_tick"),
             "result": real, "rounds": d.get("rounds"), "predicted_result": pred.get("expected_result"),
             "predicted_surplus": pred.get("surplus_total"), "predicted_price_margin": pred.get("price_margin"),
             "predicted_days_utility": pred.get("days_utility"), "decision_reason": w.get("why"),
             "prediction_error": (round(real - pred["expected_result"], 2)
                                  if isinstance(real, (int, float)) and isinstance(pred.get("expected_result"), (int, float)) else None),
             "public_events": [e for e in w.get("events", [])][:3], "learned": w.get("learned")})
        awaiting.pop(duel_id, None)
    return done_list


def _timeout_for(c: dict) -> float:
    """Que una petición colgada no se coma el tick: como mucho media duración de tick (3–15 s)."""
    ts = c.get("tick_seconds")
    return 15.0 if not isinstance(ts, (int, float)) or ts <= 0 else max(3.0, min(15.0, 0.5 * ts))


def run(b, execute, feed_file: Path | None = None, reconcile=False, verify_accept=False, track=False, learner=None):
    """`track=True` (lo activa main): relee duelos terminados para reconciliar aceptaciones, medir duel_points/negotiating
    en /api/me y alimentar el historial de anclas. Desactivado, el bucle solo hace las lecturas/escrituras de siempre."""
    last_tick = None
    stats = {"offers_sent": 0, "accepts_requested": 0, "deals": 0, "no_deals": 0}
    awaiting: dict = {}          # duelo aceptado → {tick, prediction, why}: se reconcilia en el tick siguiente
    history: list = []
    history_tick = None
    prev_score = None
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
            if track and execute:
                if prev_score is None:
                    prev_score = score_snapshot(b, tick, None)
                    if prev_score:
                        log({"event": "score_snapshot", "label": "inicio", **prev_score})
                history, history_tick, refreshed = refresh_history(b, history, history_tick, tick)
                if refreshed:
                    _learn(learner, history, tick)
                if awaiting:
                    done_now = reconcile_accepted(b, awaiting, tick, stats)
                    if done_now is not None:
                        # El modelo representa TODOS los duelos terminados. Ajustarlo sólo con
                        # los recién reconciliados borraría temporalmente la evidencia anterior.
                        history = merge_done(history, done_now)
                        _learn(learner, history, tick)
                    snap = score_snapshot(b, tick, prev_score)
                    if snap:
                        log({"event": "score_snapshot", "label": "tras reconciliar", **snap,
                             "resumen": {**stats, "duelos_activos": len(live)}})
                        prev_score = snap
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
                             "days_meaning": d.get("days_meaning"), "role": d.get("role"),
                             "days_sign": dl.days_sign(d)[0], "days_format": dl.days_format(d),
                             "days_table": dl.days_table(d), "days_utility": dl.days_utility_table(d)})
            pending = {k: v for k, v in pending.items() if tick <= v + PENDING_TICKS}
            playable = [d for d in live if d.get("duel") not in pending]
            by_id = {d.get("duel"): d for d in playable}
            events = load_feed_events(feed_file)
            accepted = False
            for step in duel_tree.plan(playable, tick, events,
                                       load_feed_events(LOG), history):
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
                        stats["accepts_requested"] += 1
                        awaiting[cand["duel"]] = {"tick": tick, "prediction": cand.get("prediction"), "why": cand.get("why"),
                                                  "learned": cand.get("learned")}
                    else:
                        b.duel_say(cand["duel"], text=cand["text"], price=cand["price"], days=cand.get("days"))
                        stats["offers_sent"] += 1
                    # `sent: true` = la acción se envió. NO es un trato puntuado: eso solo lo confirma «reconciled».
                    log({**rec, "sent": True, "event": "accept_requested" if cand["type"] == "duel_accept" else "offer_sent",
                         "scored": False})
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
