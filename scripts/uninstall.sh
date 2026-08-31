#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${WORKER_INSTALL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${WORKER_ENV_FILE:-$INSTALL_ROOT/.env}"
LOG_DIR="${WORKER_LOG_DIR:-/var/log/higgs-worker}"
MODEL_CACHE="${MODEL_CACHE_DIR:-/workspace/model-cache}"

if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    LOG_DIR="${WORKER_LOG_DIR:-$LOG_DIR}"
    MODEL_CACHE="${MODEL_CACHE_DIR:-$MODEL_CACHE}"
fi

ROOT_RESOLVED="$(realpath -m "$INSTALL_ROOT")"
case "$ROOT_RESOLVED" in
    /|/opt|/workspace|/root|/usr|/var|/tmp)
        echo "Refusing unsafe install root: $ROOT_RESOLVED" >&2
        exit 2
        ;;
esac

if [ -x "$INSTALL_ROOT/scripts/stop.sh" ] && [ -r "$ENV_FILE" ]; then
    bash "$INSTALL_ROOT/scripts/stop.sh" "$ENV_FILE" || true
fi
if [ -x "$INSTALL_ROOT/.venv/bin/supervisorctl" ] \
    && [ -f "$INSTALL_ROOT/runtime/supervisord.conf" ]; then
    "$INSTALL_ROOT/.venv/bin/supervisorctl" \
        -c "$INSTALL_ROOT/runtime/supervisord.conf" shutdown >/dev/null 2>&1 || true
fi

rm -rf -- "$ROOT_RESOLVED"
LOG_RESOLVED="$(realpath -m "$LOG_DIR")"
case "$LOG_RESOLVED" in
    /|/opt|/workspace|/root|/usr|/var|/var/log|/tmp)
        echo "Refusing unsafe log directory: $LOG_RESOLVED" >&2
        exit 2
        ;;
    *) [ -d "$LOG_RESOLVED" ] && rm -rf -- "$LOG_RESOLVED" ;;
esac
if [ "${REMOVE_MODEL_CACHE:-0}" = "1" ]; then
    CACHE_RESOLVED="$(realpath -m "$MODEL_CACHE")"
    case "$CACHE_RESOLVED" in
        /|/opt|/workspace|/root|/usr|/var|/tmp)
            echo "Refusing unsafe model cache path: $CACHE_RESOLVED" >&2
            exit 2
            ;;
        *) rm -rf -- "$CACHE_RESOLVED" ;;
    esac
else
    echo "Preserved model cache: $MODEL_CACHE"
fi
echo "Higgs worker bundle removed."
