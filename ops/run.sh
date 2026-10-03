#!/bin/bash
# Usage: ops/run.sh agent|broker|collector   — launched by launchd (KeepAlive)
cd "$(dirname "$0")/.." || exit 1
set -a; . state/.env; set +a
PY=/usr/bin/python3
mkdir -p logs
case "$1" in
  agent)     exec $PY -u -m bazaar.agent ${AGENT_ARGS---dry-run} ;;   # live only with AGENT_ARGS=""
  broker)    [ -s state/broker_key.txt ] || { sleep 30; exit 0; }
             MODE=$(cat state/BROKER_MODE 2>/dev/null || echo v2); FLAGS=$(cat state/BROKER_FLAGS 2>/dev/null)
             exec $PY -u -m bazaar.broker --mode "$MODE" $FLAGS ;;
  profiler)  exec $PY -u intel/profiler.py loop ;;
  collector) exec $PY -u intel/collector.py loop ;;   # read-only: GETs only (me/duels need the key)
esac
