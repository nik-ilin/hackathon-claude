"""Mantiene la estrategia vendedora y releva al ejecutor de duelos a las 17:40 Europe/Madrid."""
from __future__ import annotations

import datetime as dt
import math
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BASE = ["./run.sh", "coord", "--execute", "--max-spend", "0", "--reserve", "118",
        "--duende-venue", "rastro", "--no-rival-venues", "--ladder-fill", "--dealer-sell-dups",
        "--dedupe-bids", "--deny-teams", "t12,t13,t14", "--page-campaign", "none",
        "--pilar-sell", "SAL:1.25", "--pilar-from-tick", "668", "--pilar-until-tick", "838"]


def main() -> int:
    tz = ZoneInfo("Europe/Madrid")
    now = dt.datetime.now(tz)
    target = now.replace(hour=17, minute=40, second=0, microsecond=0)
    if now >= target:
        print("17:40 Europe/Madrid ya pasó; no inicio una ventana tardía.", flush=True)
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
