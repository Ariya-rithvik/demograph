#!/usr/bin/env bash
# Runs FalkorDB inside WSL without Docker, using the redis-server + falkordb.so
# that ship in the falkordblite wheel. Use docker-compose.yml when Docker exists.
set -euo pipefail
VENV="${FDB_VENV:-$HOME/fdb-venv}"
DATA="${FDB_DATA:-$HOME/falkordb-data}"
PORT="${FDB_PORT:-6379}"
if [ ! -x "$VENV/bin/redis-server" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q falkordblite
fi
MODULE="$(find "$VENV" -name falkordb.so | head -1)"
mkdir -p "$DATA"
exec "$VENV/bin/redis-server" --port "$PORT" --bind 127.0.0.1 \
  --dir "$DATA" --appendonly yes --save 60 1 \
  --loadmodule "$MODULE"
