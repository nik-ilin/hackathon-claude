"""Mantiene la estrategia vendedora y releva al ejecutor de duelos a las 20:35 Europe/Madrid.

Horario recalibrado el 3 oct a tick 832 (t_hours 8.2583, ratio ~100.75 ticks/hora de juego) contra
/api/schedule en vivo: fiebre de Salamanca ~18:09-20:09 (antes se asumía 18:03-20:03; el tick de
corte 668-838 ya había quedado obsoleto y cerraba la ventana de Pilar ANTES de que empezara la
fiebre real) y Duelos II ~20:39 (antes se asumía una hora de relevo fija a las 17:40, muy anterior
a cualquier evento del calendario real).
"""
from __future__ import annotations

import datetime as dt
import math
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = ["./run.sh", "coord", "--execute", "--max-spend", "0", "--reserve", "5",
        "--duende-venue", "rastro", "--no-rival-venues", "--ladder-fill", "--dealer-sell-dups",
        "--dedupe-bids", "--deny-teams", "t05,t12,t13,t14", "--page-campaign", "none",
        "--pilar-sell", "SAL:1.25", "--pilar-from-tick", "905", "--pilar-until-tick", "1135"]


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
