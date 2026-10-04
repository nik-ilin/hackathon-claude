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

4 oct 11:43: duel_runner.py comparte data/agent.lock con el coordinador (ejecución exclusiva), así que
hasta ahora esta ventana dedicaba el día ENTERO a negociar con vendedores y solo jugaba duelos una vez,
al final. Pero duel_runner corre en bucle infinito por tick, no como un lote de una vez — está pensado
para ir recogiendo duelos vivos sobre la marcha. Se vieron 3 duelos vivos con deadline a 60-165 s
(tick 2035) sin nadie respondiendo: cualquier duelo que aparezca DURANTE la negociación se pierde gratis
hasta el relevo final. Ahora los bloques de coordinador se acortan a ~3 min y, entre bloque y bloque, se
abre un hueco corto para duel_runner (acotado con timeout; InstanceLock se autorrecupera si el proceso
muere a mitad, así que el timeout nunca deja el bloqueo huérfano). Además el relevo final usaba
`--execute --days --reconcile --verify-accept`, sin `--ladder`/`--profiles` — el propio DUELS.md del
equipo mide `--ladder` en 53 % de tratos (25 % sin él) y `--ladder --profiles` en 47-48 % frente al 43 %
de solo `--ladder`; se añaden ambos (y `--learn`, acotado y solo-mejora por diseño) a toda invocación de
duelos, no solo al relevo final.
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
COORD_BLOCK_SECONDS = 180  # bloque corto: deja hueco regular para que duel_runner recoja duelos vivos
DUEL_BURST_SECONDS = 30    # duel_runner es un bucle infinito por tick; se acota para no robarle el turno a la negociación
DUEL_CMD = ["./run.sh", "duels", "--execute", "--days", "--ladder", "--profiles",
            "--reconcile", "--verify-accept", "--learn"]

# market.db del collector del equipo, para calibrar la fiabilidad de las fuentes de --news-sell (solo
# lectura; opcional). Por defecto None en ejecuciones donde esa ruta no exista en el disco.
_NEWS_DB = os.environ.get("NEWS_DB", "")
# 4 oct: delega en el preset `t15` de run.sh (mantenido por el equipo) en vez de reimplementar sus
# propios flags a mano. Esta lista privada se había quedado desincronizada del preset real: corría con
# --per-card por defecto (60 P, no los 95 P calibrados), una lista de --deny-teams obsoleta, y sin
# --allow-last-copy SAL-07/--pilar-last-copy/--deny-margin que el preset ya tenía. Solo se añaden aquí
# los flags propios de la ejecución desatendida (--execute, --max-spend, --allow-concurrent, etc.) que
# no tiene sentido meter en el preset compartido.
BASE = ["./run.sh", "t15", "--execute", "--max-spend", "200", "--allow-concurrent",
        "--page-campaign", "none", "--news-margin", "2", "--fever-wait", "1",
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


FINALE_SAFETY_MARGIN = dt.timedelta(minutes=5)  # los dealers se desactivan en el instante exacto del evento


def _finale_target(now: dt.datetime, close_at: dt.datetime, clock: dict, schedule: dict) -> dt.datetime | None:
    """Los dealers pueden desactivarse (evento `persona` "stalls close") o darse el último duelo
    (`duels` cuyo nombre incluya "Final") ANTES del cierre general del día — hasta 1h antes, visto
    el 4 oct. Calibra at_hours -> hora real con el propio `closes` como ancla (self.closes es el único
    punto fijo fiable; `t_hours` no es 1:1 con horas reales entre días, varía con tick_seconds)."""
    t_hours_now = clock.get("t_hours")
    upcoming = schedule.get("upcoming") or []
    close_ev = next((e for e in upcoming if e.get("action") == "day_closes"), None)
    if t_hours_now is None or close_ev is None:
        return None
    t_hours_close = close_ev.get("at_hours")
    real_seconds_to_close = (close_at - now).total_seconds()
    t_hours_remaining = t_hours_close - t_hours_now
    if not t_hours_remaining or t_hours_remaining <= 0:
        return None
    ratio = real_seconds_to_close / t_hours_remaining  # segundos reales por unidad t_hours
    finale_ats = [e["at_hours"] for e in upcoming
                  if (e.get("action") == "persona" and "stalls close" in (e.get("note") or "").lower())
                  or (e.get("action") == "duels" and "final" in (e.get("params", {}).get("name") or "").lower())]
    if not finale_ats:
        return None
    soonest = min(finale_ats)
    return now + dt.timedelta(seconds=(soonest - t_hours_now) * ratio)


def _live_target(default_target: dt.datetime) -> tuple[dt.datetime, float]:
    """Lee /api/clock() y /api/schedule() para fijar el relevo al evento real que corte antes:
    el cierre del día o la desactivación de dealers/duelo final (visto el 4 oct: ~1h antes del
    cierre general). Si algo falla, cae de vuelta a las 20:35."""
    try:
        import bazaar_sdk
        env = {**_read_env(HERE / ".env"), **os.environ}
        url, key = env.get("BAZAAR_URL", ""), env.get("BAZAAR_KEY", "")
        if not url or not key:
            raise RuntimeError("falta BAZAAR_URL/BAZAAR_KEY")
        sdk = bazaar_sdk.Bazaar(url, key, timeout=10, retries=2)
        clock = sdk.clock()
        closes = clock.get("closes")
        tick_seconds = float(clock.get("tick_seconds") or 30.0)
        if not closes:
            raise RuntimeError("clock() sin 'closes'")
        close_at = dt.datetime.fromisoformat(closes)
        target = close_at - CLOSE_SAFETY_MARGIN
        now = dt.datetime.now(TZ)
        try:
            schedule = sdk.schedule()
            finale_at = _finale_target(now, close_at, clock, schedule)
            if finale_at is not None:
                finale_target = finale_at - FINALE_SAFETY_MARGIN
                if finale_target < target:
                    print(f"AVISO: evento de cierre de dealers/duelo final en {finale_at.isoformat()}, "
                          f"antes que el cierre general; adelanto el relevo.", flush=True)
                    target = finale_target
        except Exception as exc:
            print(f"AVISO: no se pudo leer /api/schedule ({exc}); uso solo el cierre general.", flush=True)
        return target, tick_seconds
    except Exception as exc:
        print(f"AVISO: no se pudo leer /api/clock en vivo ({exc}); uso relevo por defecto 20:35.", flush=True)
        return default_target, 30.0


def _duel_burst() -> None:
    """Hueco corto entre bloques de coordinador para que duel_runner recoja duelos vivos. Se acota con timeout
    porque duel_runner corre en bucle infinito (uno por tick); si no hay duelos vivos simplemente espera y se
    corta sin haber hecho nada. data/agent.lock se autorrecupera (InstanceLock.acquire comprueba que el pid
    siga vivo) si el proceso muere a mitad de un tick, así que el timeout nunca deja el bloqueo huérfano
    bloqueando el siguiente bloque de coordinador."""
    print(f"{dt.datetime.now(TZ).isoformat()} hueco de {DUEL_BURST_SECONDS}s para duel_runner", flush=True)
    try:
        subprocess.run(DUEL_CMD, cwd=HERE, timeout=DUEL_BURST_SECONDS)
    except subprocess.TimeoutExpired:
        pass


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
        block_ticks = max(1, math.ceil(COORD_BLOCK_SECONDS / tick_seconds))
        ticks = min(120, block_ticks, max(1, math.ceil(seconds / tick_seconds)))
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
        _duel_burst()
    # Enfriamiento y relevo sin solapar los bloqueos de coordinador/duelos.
    time.sleep(1)
    print(f"{dt.datetime.now(TZ).isoformat()} relevo final a duel_runner (sin límite de tiempo)", flush=True)
    return subprocess.run(DUEL_CMD, cwd=HERE).returncode


if __name__ == "__main__":
    raise SystemExit(main())
