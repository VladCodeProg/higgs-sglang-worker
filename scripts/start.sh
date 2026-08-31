#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${WORKER_INSTALL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${1:-${WORKER_ENV_FILE:-$INSTALL_ROOT/.env}}"
RUNTIME_DIR="$INSTALL_ROOT/runtime"

if [ ! -r "$ENV_FILE" ]; then
    echo "Worker environment file is not readable: $ENV_FILE" >&2
    exit 2
fi
case "$INSTALL_ROOT:$ENV_FILE" in
    *$'\n'*|*$'\r'*|*' '*) echo "Worker paths must not contain whitespace." >&2; exit 2 ;;
esac

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
LOG_DIR="${WORKER_LOG_DIR:-/var/log/higgs-worker}"
PORT="${WORKER_PORT:-8787}"
START_TIMEOUT="${WORKER_START_TIMEOUT:-1800}"
SUPERVISOR_CONF="$RUNTIME_DIR/supervisord.conf"
SUPERVISORCTL="$INSTALL_ROOT/.venv/bin/supervisorctl"
SUPERVISORD="$INSTALL_ROOT/.venv/bin/supervisord"

if [ ! -x "$SUPERVISORD" ]; then
    echo "Supervisor is not installed. Run scripts/install.sh first." >&2
    exit 3
fi
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

cat > "$SUPERVISOR_CONF" <<EOF
[unix_http_server]
file=$RUNTIME_DIR/supervisor.sock
chmod=0700

[supervisord]
logfile=$LOG_DIR/supervisord.log
pidfile=$RUNTIME_DIR/supervisord.pid
childlogdir=$LOG_DIR

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://$RUNTIME_DIR/supervisor.sock

[program:higgs-worker]
command=/bin/bash $INSTALL_ROOT/scripts/run.sh $ENV_FILE
directory=$INSTALL_ROOT
autostart=true
autorestart=unexpected
startsecs=5
startretries=3
stopsignal=TERM
stopwaitsecs=${WORKER_STOP_TIMEOUT:-180}
stopasgroup=true
killasgroup=true
stdout_logfile=$LOG_DIR/worker.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3
redirect_stderr=true
EOF

if [ -f "$RUNTIME_DIR/supervisord.pid" ] \
    && kill -0 "$(cat "$RUNTIME_DIR/supervisord.pid")" 2>/dev/null; then
    if "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" status higgs-worker \
        | grep -q 'RUNNING'; then
        echo "Worker is already running."
    else
        "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" reread >/dev/null
        "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" update >/dev/null
        "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" start higgs-worker
    fi
else
    "$SUPERVISORD" -c "$SUPERVISOR_CONF"
fi

echo "Waiting for worker readiness on 127.0.0.1:$PORT ..."
for _ in $(seq 1 "$START_TIMEOUT"); do
    HEALTH="$(curl -sS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
    if printf '%s' "$HEALTH" | grep -Eq '"ready"[[:space:]]*:[[:space:]]*true'; then
        echo "$HEALTH"
        exit 0
    fi
    if printf '%s' "$HEALTH" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"error"'; then
        echo "Worker entered error state: $HEALTH" >&2
        tail -n 100 "$LOG_DIR/worker.log" >&2 || true
        exit 5
    fi
    if ! "$SUPERVISORCTL" -c "$SUPERVISOR_CONF" status higgs-worker \
        | grep -Eq 'RUNNING|STARTING'; then
        echo "Worker process stopped during startup." >&2
        tail -n 100 "$LOG_DIR/worker.log" >&2 || true
        exit 5
    fi
    sleep 1
done

echo "Timed out waiting for worker readiness after ${START_TIMEOUT}s." >&2
tail -n 100 "$LOG_DIR/worker.log" >&2 || true
exit 6
