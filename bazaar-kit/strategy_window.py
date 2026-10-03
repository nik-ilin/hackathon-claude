"""Mantiene la estrategia vendedora y releva al ejecutor de duelos a las 20:35 Europe/Madrid.

Horario recalibrado el 3 oct a tick 832 (t_hours 8.2583, ratio ~100.75 ticks/hora de juego) contra
/api/schedule en vivo: fiebre de Salamanca ~18:09-20:09 (antes se asumía 18:03-20:03; el tick de
corte 668-838 ya había quedado obsoleto y cerraba la ventana de Pilar ANTES de que empezara la
fiebre real) y Duelos II ~20:39 (antes se asumía una hora de relevo fija a las 17:40, muy anterior
a cualquier evento del calendario real).
Además, actúa de forma proactiva ante radio/eventos sin esperar confirmación: --news-sell (abre venta
en cuanto hay demanda viva de una fuente fiable o fila en el menú del vendedor), --fever-priority
(prioriza/espera ventas durante una fiebre de /api/schedule, p. ej. Pilar+25% en SAL), --ladder-calibrated
y --chato-mirror on (nunca se queda quieto si Chato refleja nuestra concesión). --allow-concurrent porque
esta ventana puede arrancar con hilos ya abiertos por una ejecución anterior (propia, no de un rival
confirmado) y no debe bloquearse por eso.
"""
from __future__ import annotations

import os
import datetime as dt
import math
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
# market.db del collector del equipo, para calibrar la fiabilidad de las fuentes de --news-sell (solo
# lectura; opcional). Por defecto None en ejecuciones donde esa ruta no exista en el disco.
_NEWS_DB = os.environ.get("NEWS_DB", "")
BASE = ["./run.sh", "coord", "--execute", "--max-spend", "0", "--reserve", "5",
        "--duende-venue", "rastro", "--no-rival-venues", "--allow-concurrent",
        "--ladder-fill", "--ladder-calibrated", "--chato-mirror", "on", "--dealer-sell-dups",
        "--dedupe-bids", "--deny-teams", "t05,t12,t13,t14", "--page-campaign", "none",
        "--pilar-sell", "SAL:1.25", "--pilar-from-tick", "905", "--pilar-until-tick", "1135",
        "--news-sell", "--news-margin", "2", "--fever-priority", "--fever-wait", "1",
        "--v10-commission", "--max-posts", "3"]
if _NEWS_DB and Path(_NEWS_DB).is_file():
    BASE += ["--news-db", _NEWS_DB]


def main() -> int:
    tz = ZoneInfo("Europe/Madrid")
    now = dt.datetime.now(tz)
    target = now.replace(hour=20, minute=35, second=0, microsecond=0)
    if now >= target:
        print("20:35 Europe/Madrid ya pasó; no inicio una ventana tardía.", flush=True)
        return 2
    while True:
        now = dt.datetime.now(tz)
        seconds = (target - now).total_seconds()
        if seconds <= 1:
            break
        # Los ticks actuales duran 30 s. Un bloque acaba en el tick inmediatamente anterior al relevo.
        ticks = min(120, max(1, math.ceil(seconds / 30)))
        cmd = BASE + ["--ticks", str(ticks)]
        print(f"{now.isoformat()} iniciando bloque de {ticks} ticks", flush=True)
        result = subprocess.run(cmd, cwd=HERE)
        if result.returncode:
            print(f"Coordinador terminó con código {result.returncode}; no se relanza automáticamente.", flush=True)
            return result.returncode
        if ticks == 1:
            time.sleep(seconds)
            break
    # Enfriamiento y relevo sin solapar los bloqueos de coordinador/duelos.
    time.sleep(1)
    print(f"{dt.datetime.now(tz).isoformat()} relevo a duel_runner --execute --days", flush=True)
    return subprocess.run(["./run.sh", "duels", "--execute", "--days", "--reconcile", "--verify-accept"],
                          cwd=HERE).returncode


if __name__ == "__main__":
    raise SystemExit(main())
