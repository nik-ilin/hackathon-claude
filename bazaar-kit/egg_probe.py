"""Prueba única y acotada con Abuela Carmen (RULES.md permite tratar con los dealers; no puntúa ni cambia precios).

Hipótesis (una): «Sharp ear» premia responder a un detalle personal que Abuela ya contó (pregunta natural, NO inyección).
Un mensaje, sin precios ni ofertas. Por defecto es SIMULACIÓN: comprueba las precondiciones y no escribe nada en el servidor.

    python3 egg_probe.py              # simulación (solo lecturas)
    python3 egg_probe.py --send       # envía el único mensaje; exige agente parado y sin hilo abierto con Abuela
    python3 egg_probe.py --alongside-agent --send   # con el agente operando, solo si no hay hilo abierto con Abuela

Nunca acepta ni contraoferta, no adquiere nada por una promesa de texto, no pide claves ni datos de otros equipos. Se detiene
ante advertencia, cooldown, cuota o error. Todo queda en data/egg_probe.jsonl; lo que el personaje «revele» es hipótesis.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DATA = Path(__file__).parent / "data"
LOG = "egg_probe.jsonl"
DEALER = "abuela"
MESSAGE = "Buenas tardes, Abuela Carmen. ¿Cómo está su nieto? ¿Sigue pegando cartas en su álbum?"
WATCH_TICKS = 4                      # ticks que se observa tras el mensaje; luego se cierra la prueba
STOP_WORDS = re.compile(r"\b(warn|warning|stop|enough|basta|no more|cooldown|goodbye|adiós|adios|not talk|no hablo)\b", re.I)


def preconditions(me: dict, threads: list, dealers: dict, procs: list, clock: dict, alongside: bool = False) -> list:
    """Motivos por los que NO se puede enviar (lista vacía = viable)."""
    out = []
    d = (dealers or {}).get(DEALER)
    if d is None:
        out.append("Abuela no figura en /api/dealers")
    elif DEALER not in (me.get("unlocked") or []) and not d.get("open_to_all"):
        out.append("sin acceso a Abuela")
    elif d.get("enabled") is False or d.get("status", "active") != "active":
        out.append("Abuela no está activa")
    if procs and not alongside:
        out.append(f"hay un ejecutor en marcha ({len(procs)}): páralo o usa --alongside-agent (solo si no hay hilo con Abuela)")
    if any(t.get("with") == DEALER and t.get("status") == "open" for t in threads):
        out.append("ya hay un hilo ABIERTO con Abuela (comercial): no se mezcla")
    lim = (clock.get("limits") or {}).get("max_open_threads_per_team", 6)
    if sum(1 for t in threads if t.get("status") == "open") >= lim:
        out.append("sin hilos libres")
    return out


def find_egg(events: list, team: str, since_tick: int) -> dict:
    """Huevo / insignia PÚBLICOS de nuestro equipo desde `since_tick` (hecho del servidor, no del texto del personaje)."""
    egg = [e for e in events if e.get("type") in ("egg.found", "egg.given", "badge.awarded") and e.get("tick", 0) >= since_tick
           and (e.get("payload") or {}).get("team") == team]
    return {"found": any(e["type"] == "egg.found" for e in egg), "events": egg}


def stop_reason(thread: dict, dealer: str = DEALER) -> str | None:
    if thread.get("status") not in (None, "open"):
        return f"el hilo pasó a «{thread.get('status')}»"
    for m in thread.get("messages") or []:
        if m.get("sender") == dealer and m.get("text") and STOP_WORDS.search(m["text"]):
            return "advertencia o despedida en su respuesta"
    return None


def log(rec: dict) -> None:
    DATA.mkdir(exist_ok=True)
    with open(DATA / LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(rec, ts=round(time.time())), ensure_ascii=False, default=str) + "\n")


def run(api, send: bool, procs: list, sleep=time.sleep, alongside: bool = False) -> dict:
    me, clock = api.me(), api.clock()
    threads = (api.my_threads() or {}).get("threads", [])
    dealers = {d["id"]: d for d in (lambda r: r if isinstance(r, list) else r.get("personas") or r.get("dealers") or [])(api.dealers())}
    tick, team = clock.get("tick"), me["id"]
    problems = preconditions(me, threads, dealers, procs, clock, alongside)
    before = {"cash": me.get("cash"), "assets": len(me.get("assets") or []), "score": me.get("score")}
    log({"event": "plan", "send": send, "tick": tick, "message": MESSAGE, "problems": problems, "before": before})
    if problems or not send:
        return {"sent": False, "problems": problems, "message": MESSAGE, "dry_run": not send}
    th = api.open_thread(DEALER)                           # sin tema comercial
    tid = th.get("id")
    log({"event": "thread_opened", "tick": tick, "thread": tid, "response": th})
    resp = api.say(tid, MESSAGE)                           # UN mensaje, sin precio ni oferta
    log({"event": "sent", "thread": tid, "text": MESSAGE, "response": resp})
    seen, outcome = set(), "sin respuesta útil"
    for _ in range(WATCH_TICKS):
        sleep(float(clock.get("tick_seconds") or 30))
        t = api.thread(tid)
        for m in t.get("messages") or []:
            if m.get("id") not in seen and m.get("sender") == DEALER:
                seen.add(m.get("id"))
                log({"event": "reply", "thread": tid, "tick": m.get("tick"), "text": m.get("text"), "offer": m.get("offer")})
        egg = find_egg((api.feed(400) or {}).get("events", []), team, tick)
        if egg["found"]:
            outcome = "HUEVO: egg.found de nuestro equipo en el feed público"
            log({"event": "egg", "events": egg["events"]})
            break
        why = stop_reason(t)
        if why:
            outcome = "detenida: " + why
            break
        if seen:
            outcome = "respondió sin disparar huevo"
    try:
        api.close_thread(tid)                              # no se acepta ni contraoferta nada
    except Exception as e:  # noqa: BLE001
        log({"event": "close_failed", "error": str(e)})
    after = api.me()
    log({"event": "end", "outcome": outcome, "after": {"cash": after.get("cash"), "assets": len(after.get("assets") or []),
                                                          "score": after.get("score")}})
    return {"sent": True, "thread": tid, "outcome": outcome, "dry_run": False}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="enviar el único mensaje (por defecto: simulación)")
    ap.add_argument("--alongside-agent", action="store_true",
                    help="permitir que el agente siga operando: la prueba no acepta, no contraoferta y no compra; exige que "
                         "NO haya un hilo abierto con Abuela (el agente verá a Abuela ocupada mientras dure, ~2 min)")
    a = ap.parse_args(argv)
    from bazaar_sdk import Bazaar
    import coordinator
    key = os.environ.get("BAZAAR_KEY")
    if not key:
        ap.error("falta BAZAAR_KEY (usa ./run.sh o carga .env)")
    api = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key)
    r = run(api, a.send, coordinator.other_processes(), alongside=a.alongside_agent)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if not r.get("problems") else 2


if __name__ == "__main__":
    sys.exit(main())
