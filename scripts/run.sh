#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${WORKER_INSTALL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${1:-${WORKER_ENV_FILE:-$INSTALL_ROOT/.env}}"

if [ ! -r "$ENV_FILE" ]; then
    echo "Worker environment file is not readable: $ENV_FILE" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export HIGGS_SGLANG_APP_DIR="$INSTALL_ROOT"
export HF_HOME="${HF_HOME:-${MODEL_CACHE_DIR:-/workspace/model-cache}/huggingface}"
export HIGGS_WORKER_CACHE_DIR="${HIGGS_WORKER_CACHE_DIR:-$INSTALL_ROOT/voice-cache}"
export HIGGS_WORKER_TEMP_DIR="${HIGGS_WORKER_TEMP_DIR:-$INSTALL_ROOT/request-tmp}"

exec bash "$INSTALL_ROOT/start-sglang-worker-vast.sh"
