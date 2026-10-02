#!/usr/bin/env bash
# Lanza el agente cargando la clave desde .env
#   ./run.sh                          -> starter_agent.py (sobre sobre_barrio)
#   ./run.sh --card LAV-03 --dry-run  -> starter_agent.py con argumentos (ver --help)
#   ./run.sh broker                   -> starter_broker.py (necesita BROKER_KEY en .env)
#   ./run.sh coord                    -> coordinador único: análisis (solo lectura); --execute para operar
#   ./run.sh market                   -> análisis de colección y mercado (solo lectura)
#   ./run.sh market --execute --cycles 8 -> operación entre equipos, máximo una acción por ciclo
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
  coord)  shift; exec python3 coordinator.py "$@" ;;
  market) shift; exec python3 market_agent.py "$@" ;;
  broker) exec python3 starter_broker.py ;;
  agent)  shift; exec python3 starter_agent.py "$@" ;;
  *)      exec python3 starter_agent.py "$@" ;;
esac
