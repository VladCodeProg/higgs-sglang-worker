#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${WORKER_INSTALL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${1:-${WORKER_ENV_FILE:-$INSTALL_ROOT/.env}}"
RUNTIME_DIR="$INSTALL_ROOT/runtime"
SUPERVISOR_CONF="$RUNTIME_DIR/supervisord.conf"
SUPERVISORCTL="$INSTALL_ROOT/.venv/bin/supervisorctl"

if [ ! -r "$ENV_FILE" ]; then
    echo "Worker environment file is not readable: $ENV_FILE" >&2
    exit 2
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
PORT="${WORKER_PORT:-8787}"
TIMEOUT="${WORKER_STOP_TIMEOUT:-180}"

if [ ! -S "$RUNTIME_DIR/supervisor.sock" ]; then
    echo "Worker supervisor is not running."
    exit 0
fi

AUTH="Authorization: Bearer ${WORKER_ADMIN_TOKEN:-}"
curl -sS --max-time 5 -X POST -H "$AUTH" \
    "http://127.0.0.1:$PORT/admin/drain" >/dev/null 2>&1 || true

for _ in $(seq 1 "$TIMEOUT"); do
    HEALTH="$(curl -sS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
    if ! printf '%s' "$HEALTH" | grep -Eq '"busy"[[:space:]]*:[[:space:]]*true'; then
        break
    fi
    sleep 1
done

curl -sS --max-time 5 -X POST -H "$AUTH" \
    "http://127.0.0.1:$PORT/admin/shutdown" >/dev/null 2>&1 || true

for _ in $(seq 1 15); do
    if ! "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" status higgs-worker 2>/dev/null \
        | grep -q 'RUNNING'; then
        echo "Worker stopped gracefully."
        exit 0
    fi
    sleep 1
done

echo "Graceful API shutdown timed out; asking Supervisor to stop the process." >&2
"$SUPERVISORCTL" -c "$SUPERVISOR_CONF" stop higgs-worker >/dev/null
echo "Worker stopped by Supervisor."
