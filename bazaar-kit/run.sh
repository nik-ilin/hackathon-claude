#!/usr/bin/env bash
# Lanza el agente cargando la clave desde .env
#   ./run.sh                          -> starter_agent.py (sobre sobre_barrio)
#   ./run.sh --card LAV-03 --dry-run  -> starter_agent.py con argumentos (ver --help)
#   ./run.sh broker                   -> starter_broker.py (necesita BROKER_KEY en .env)
#   ./run.sh memory                   -> coordinador con memoria persistente (análisis por defecto)
#   ./run.sh market-broker            -> market_broker.py: Market Test con suelo del puesto y vigilante
#   ./run.sh coord                    -> coordinador único: análisis (solo lectura); --execute para operar
#   ./run.sh market                   -> análisis de colección y mercado (solo lectura)
#   ./run.sh market --execute --cycles 8 -> operación entre equipos, máximo una acción por ciclo
#   ./run.sh celestina [--loop]       -> casamentera de v15: imprime el anuncio (dry run); --execute para publicarlo
#   ./run.sh t15 [--execute --ticks 120] -> coordinador con la configuración decidida por t15 (ver T15_PLAN.md)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Falta el archivo .env (copia la clave de tu equipo en BAZAAR_KEY)" >&2
  exit 1
fi

set -a
source .env
set +a

if [ "${BAZAAR_KEY:-}" = "tk-xxxx-xxxx" ] || [ -z "${BAZAAR_KEY:-}" ]; then
  echo "Edita .env y pon tu BAZAAR_KEY real" >&2
  exit 1
fi

case "${1:-}" in
  memory) shift; exec python3 memory_coordinator.py "$@" ;;
  coord)  shift; exec python3 coordinator.py "$@" ;;
  market) shift; exec python3 market_agent.py "$@" ;;
  celestina) shift; exec python3 celestina.py "$@" ;;
  t15)    shift; exec python3 coordinator.py --no-rival-venues --duende-venue rastro \
            --deny-teams t14,t12,t10,t18 --deny-margin 15 --ladder-fill --ladder-calibrated \
            --pilar-sell SAL,LAV:1.25 --pilar-last-copy --allow-last-copy SAL-07 \
            --reserve 5 --per-card 95 "$@" ;;
  broker) exec python3 starter_broker.py ;;
  market-broker) shift; exec python3 market_broker.py "$@" ;;
  agent)  shift; exec python3 starter_agent.py "$@" ;;
  *)      exec python3 starter_agent.py "$@" ;;
esac
