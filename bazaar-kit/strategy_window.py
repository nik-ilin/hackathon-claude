"""Mantiene la estrategia vendedora y releva al ejecutor de duelos antes del cierre del día de juego.

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

4 oct: el domingo cierra a las 15:00 Europe/Madrid (`/api/clock().closes`), no a las 20:35 de ayer —
cada día de juego puede cerrar a una hora distinta y el calendario completo vive en el servidor, así
que el relevo ahora se calcula leyendo `closes` en vivo (con margen de seguridad) en vez de usar una
hora fija; si la lectura falla al arrancar, cae de vuelta a las 20:35 como ventana conservadora.
También se añadió reintento acotado ante fallos de red transitorios: un corte de DNS de ~15 min el 3
oct dejó el proceso parado 14 h sin que nadie lo notara (`no se relanza automáticamente` + KeepAlive
desactivado en el plist), perdiendo el relevo de Duelos II de esa noche.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Madrid")
CLOSE_SAFETY_MARGIN = dt.timedelta(minutes=12)  # tiempo para que el relevo a duelos termine antes del cierre real
MAX_CONSECUTIVE_FAILURES = 20  # ~absorbe horas de cortes de red transitorios antes de rendirse de verdad
FAILURE_BACKOFF_SECONDS = 20

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


def _read_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _live_target(default_target: dt.datetime) -> tuple[dt.datetime, float]:
    """Lee /api/clock().closes para fijar el relevo al cierre real del día; 20:35 si falla."""
    try:
        import bazaar_sdk
        env = {**_read_env(HERE / ".env"), **os.environ}
        url, key = env.get("BAZAAR_URL", ""), env.get("BAZAAR_KEY", "")
        if not url or not key:
            raise RuntimeError("falta BAZAAR_URL/BAZAAR_KEY")
        clock = bazaar_sdk.Bazaar(url, key, timeout=10, retries=2).clock()
        closes = clock.get("closes")
        tick_seconds = float(clock.get("tick_seconds") or 30.0)
        if not closes:
            raise RuntimeError("clock() sin 'closes'")
        close_at = dt.datetime.fromisoformat(closes)
        return close_at - CLOSE_SAFETY_MARGIN, tick_seconds
    except Exception as exc:
        print(f"AVISO: no se pudo leer /api/clock en vivo ({exc}); uso relevo por defecto 20:35.", flush=True)
        return default_target, 30.0


def main() -> int:
    now = dt.datetime.now(TZ)
    default_target = now.replace(hour=20, minute=35, second=0, microsecond=0)
    target, tick_seconds = _live_target(default_target)
    print(f"Relevo fijado a {target.isoformat()} (tick_seconds={tick_seconds})", flush=True)
    if now >= target:
        print(f"{target.isoformat()} ya pasó; no inicio una ventana tardía.", flush=True)
        return 2
    consecutive_failures = 0
    while True:
        now = dt.datetime.now(TZ)
        seconds = (target - now).total_seconds()
        if seconds <= 1:
            break
        ticks = min(120, max(1, math.ceil(seconds / tick_seconds)))
        cmd = BASE + ["--ticks", str(ticks)]
        print(f"{now.isoformat()} iniciando bloque de {ticks} ticks", flush=True)
        result = subprocess.run(cmd, cwd=HERE)
        if result.returncode:
            consecutive_failures += 1
            print(f"Coordinador terminó con código {result.returncode} "
                  f"(fallo consecutivo {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}).", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print("Demasiados fallos seguidos; me rindo de verdad.", flush=True)
                return result.returncode
            time.sleep(FAILURE_BACKOFF_SECONDS)
            continue
        consecutive_failures = 0
        if ticks == 1:
            time.sleep(seconds)
            break
    # Enfriamiento y relevo sin solapar los bloqueos de coordinador/duelos.
    time.sleep(1)
    print(f"{dt.datetime.now(TZ).isoformat()} relevo a duel_runner --execute --days", flush=True)
    return subprocess.run(["./run.sh", "duels", "--execute", "--days", "--reconcile", "--verify-accept"],
                          cwd=HERE).returncode


if __name__ == "__main__":
    raise SystemExit(main())
