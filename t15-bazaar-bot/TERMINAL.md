# Operar desde un terminal (sin launchd)

El bot de negociación se ejecuta **siempre desde un único terminal** (el de quien opera). Nunca dos procesos que escriban con la misma clave a la vez: solo hay una aceptación por tick y equipo.

```bash
git clone <repo> && cd <repo>/t15-bazaar-bot
mkdir -p state && printf 'BAZAAR_KEY=tk-...\n' > state/.env && chmod 600 state/.env   # la clave nunca va al código

python3 tests/test_guard.py          # comprobaciones offline
ops/run.sh agent                     # dry-run: muestra lo que haría, no envía nada
AGENT_ARGS="" ops/run.sh agent       # en vivo (Ctrl-C para parar)
touch state/STOP                     # parada total · state/STOP_TRADING: solo duelos
```

Inteligencia de mercado (solo lectura, puede correr en cualquier máquina, no escribe nunca):

```bash
ops/run.sh collector                 # intel/market.db
ops/run.sh profiler                  # intel/REPORT.md + intel/recommendations.json
```

Los `ops/*.plist` son para launchd en macOS y llevan rutas de la máquina original: no hacen falta para operar desde terminal.

| Momento (sábado) | Qué |
|---|---|
| ~11:50, cada 2 h | Market Test: puesto gratuito (no hace falta nada) |
| ~12:00 | Duelos I: el agente juega los duelos primero y no acepta dealers/P2P durante la oleada |
| ~18:30 | Duelos II (precio + días) |

Análisis de la jornada: [`intel/ANALISIS_SABADO.md`](intel/ANALISIS_SABADO.md).
