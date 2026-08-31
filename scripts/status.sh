#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${WORKER_INSTALL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${1:-${WORKER_ENV_FILE:-$INSTALL_ROOT/.env}}"
RUNTIME_DIR="$INSTALL_ROOT/runtime"
SUPERVISOR_CONF="$RUNTIME_DIR/supervisord.conf"

if [ ! -r "$ENV_FILE" ]; then
    echo "Worker environment file is not readable: $ENV_FILE" >&2
    exit 2
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
PORT="${WORKER_PORT:-8787}"
SUPERVISORCTL="$INSTALL_ROOT/.venv/bin/supervisorctl"

if [ -x "$SUPERVISORCTL" ] && [ -S "$RUNTIME_DIR/supervisor.sock" ]; then
    "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" status higgs-worker || true
else
    echo "Supervisor: not running"
fi

if HEALTH="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health")"; then
    echo "Health: $HEALTH"
    exit 0
fi
echo "Health: unavailable"
exit 1
