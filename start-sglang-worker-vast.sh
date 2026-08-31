#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HIGGS_SGLANG_APP_DIR:-/workspace/higgs-sglang-worker}"
VENV="$APP_DIR/.venv"
MODEL="${HIGGS_SGLANG_MODEL:-bosonai/higgs-audio-v3-tts-4b}"
BACKEND_PORT="${HIGGS_SGLANG_BACKEND_PORT:-8096}"
WORKER_HOST="${WORKER_HOST:-${HIGGS_WORKER_HOST:-0.0.0.0}}"
WORKER_PORT="${WORKER_PORT:-${HIGGS_WORKER_PORT:-8787}}"
WORKER_ID="${WORKER_ID:-gpu-1}"
WORKER_TTS_TOKEN="${WORKER_TTS_TOKEN:-${WORKER_API_TOKEN:-${HIGGS_WORKER_TOKEN:-}}}"
WORKER_ADMIN_TOKEN="${WORKER_ADMIN_TOKEN:-}"
MAX_BATCH_SIZE="${MAX_BATCH_SIZE:-32}"
MAX_RUNNING="${HIGGS_SGLANG_MAX_RUNNING:-4}"
CUDA_GRAPH_MAX_BS="${HIGGS_SGLANG_CUDA_GRAPH_MAX_BS:-4}"
export HF_HOME="${HF_HOME:-$APP_DIR/hf-cache}"
export HIGGS_SGLANG_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
export HIGGS_SGLANG_MODEL="$MODEL"
export HIGGS_WORKER_CACHE_DIR="${HIGGS_WORKER_CACHE_DIR:-$APP_DIR/voice-cache}"
export HIGGS_WORKER_TEMP_DIR="${HIGGS_WORKER_TEMP_DIR:-$APP_DIR/request-tmp}"
export HIGGS_SGLANG_MAX_RUNNING="$MAX_RUNNING"
export WORKER_ID WORKER_TTS_TOKEN WORKER_ADMIN_TOKEN WORKER_HOST WORKER_PORT
export MAX_BATCH_SIZE LOG_LEVEL WORKER_VERSION

if [ -z "$WORKER_TTS_TOKEN" ]; then
    echo "WORKER_TTS_TOKEN is required."
    exit 1
fi
if [ -z "$WORKER_ADMIN_TOKEN" ]; then
    echo "WORKER_ADMIN_TOKEN is required."
    exit 1
fi
if [ "$WORKER_TTS_TOKEN" = "$WORKER_ADMIN_TOKEN" ]; then
    echo "WORKER_TTS_TOKEN and WORKER_ADMIN_TOKEN must be different."
    exit 1
fi
if [ -z "$WORKER_ID" ]; then
    echo "WORKER_ID must not be empty."
    exit 1
fi
if ! [[ "$MAX_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]] || [ "$MAX_BATCH_SIZE" -gt 32 ]; then
    echo "MAX_BATCH_SIZE must be an integer from 1 to 32."
    exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
    echo "Worker is not installed. Run: bash $APP_DIR/setup-sglang-worker-vast.sh"
    exit 1
fi
for value in "$MAX_RUNNING" "$CUDA_GRAPH_MAX_BS"; do
    if ! [[ "$value" =~ ^[1-9][0-9]*$ ]] || [ "$value" -gt 32 ]; then
        echo "Concurrency and CUDA graph size must be integers from 1 to 32."
        exit 1
    fi
done
if [ "$CUDA_GRAPH_MAX_BS" -lt "$MAX_RUNNING" ]; then
    echo "HIGGS_SGLANG_CUDA_GRAPH_MAX_BS must be at least HIGGS_SGLANG_MAX_RUNNING."
    exit 1
fi

mkdir -p "$HIGGS_WORKER_CACHE_DIR" "$HIGGS_WORKER_TEMP_DIR"
cd "$APP_DIR"
backend_pid=""
cleanup() {
    if [ -n "$backend_pid" ] && kill -0 "$backend_pid" 2>/dev/null; then
        kill "$backend_pid" 2>/dev/null || true
        wait "$backend_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if ! curl --silent --fail "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
    echo "Starting SGLang-Omni backend. Logs: $APP_DIR/sglang-backend.log"
    echo "Server limit: max_running_requests=$MAX_RUNNING, cuda_graph_max_bs=$CUDA_GRAPH_MAX_BS"
    "$VENV/bin/sgl-omni" serve \
        --model-path "$MODEL" \
        --model-name "$MODEL" \
        --host 127.0.0.1 \
        --port "$BACKEND_PORT" \
        --tts_engine.engine.max_running_requests "$MAX_RUNNING" \
        --tts_engine.engine.cuda_graph_max_bs "$CUDA_GRAPH_MAX_BS" \
        > "$APP_DIR/sglang-backend.log" 2>&1 &
    backend_pid=$!
    echo "Waiting for the model and CUDA graphs to load..."
    for _ in $(seq 1 180); do
        if curl --silent --fail "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then break; fi
        if ! kill -0 "$backend_pid" 2>/dev/null; then
            echo "SGLang-Omni failed. Last log lines:"
            tail -n 120 "$APP_DIR/sglang-backend.log"
            exit 1
        fi
        sleep 10
    done
fi
if ! curl --silent --fail "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
    echo "Backend did not become ready. Last log lines:"
    tail -n 120 "$APP_DIR/sglang-backend.log"
    exit 1
fi

echo "Backend ready. Worker $WORKER_ID API: http://$WORKER_HOST:$WORKER_PORT"
"$VENV/bin/uvicorn" sglang_worker_api:app --host "$WORKER_HOST" --port "$WORKER_PORT"
