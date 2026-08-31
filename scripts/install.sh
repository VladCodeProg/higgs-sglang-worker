#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_ROOT="${WORKER_INSTALL_ROOT:-/opt/higgs-worker}"
LOG_DIR="${WORKER_LOG_DIR:-/var/log/higgs-worker}"
MODEL_CACHE="${MODEL_CACHE_DIR:-/workspace/model-cache}"
SGLANG_REV="${HIGGS_SGLANG_REV:-d0aec3118ea1f609223c555347ef685469f4e1db}"
VENV="$INSTALL_ROOT/.venv"
SGLANG_SRC="$INSTALL_ROOT/sglang-omni-src"

case "$INSTALL_ROOT" in
    /*) ;;
    *) echo "WORKER_INSTALL_ROOT must be an absolute path." >&2; exit 2 ;;
esac
case "$(realpath -m "$INSTALL_ROOT")" in
    /|/opt|/workspace|/root|/usr|/var|/tmp)
        echo "Refusing unsafe WORKER_INSTALL_ROOT: $INSTALL_ROOT" >&2
        exit 2
        ;;
esac

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "nvidia-smi is unavailable; an NVIDIA GPU/driver is required." >&2
    exit 3
fi
if ! nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -n 1; then
    echo "NVIDIA driver is present but no usable GPU was detected." >&2
    exit 3
fi

export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends \
        ca-certificates curl git libsndfile1 unzip
else
    echo "apt-get is required by this Vast deployment bundle." >&2
    exit 4
fi

mkdir -p "$INSTALL_ROOT" "$LOG_DIR" "$MODEL_CACHE" \
    "$INSTALL_ROOT/runtime" "$INSTALL_ROOT/voice-cache" "$INSTALL_ROOT/request-tmp"

if [ "$SOURCE_ROOT" != "$INSTALL_ROOT" ]; then
    cp -f "$SOURCE_ROOT/sglang_worker_api.py" "$INSTALL_ROOT/"
    cp -f "$SOURCE_ROOT/requirements-sglang-worker.txt" "$INSTALL_ROOT/"
    cp -f "$SOURCE_ROOT/start-sglang-worker-vast.sh" "$INSTALL_ROOT/"
    cp -f "$SOURCE_ROOT/.env.worker.example" "$INSTALL_ROOT/"
    cp -f "$SOURCE_ROOT/DEPLOYMENT.md" "$INSTALL_ROOT/"
    cp -f "$SOURCE_ROOT/BOT_HANDOFF.md" "$INSTALL_ROOT/"
    mkdir -p "$INSTALL_ROOT/scripts"
    cp -f "$SOURCE_ROOT/scripts/"*.sh "$INSTALL_ROOT/scripts/"
fi
chmod +x "$INSTALL_ROOT/start-sglang-worker-vast.sh" "$INSTALL_ROOT/scripts/"*.sh

export UV_INSTALL_DIR="$INSTALL_ROOT/bin"
export UV_CACHE_DIR="$INSTALL_ROOT/uv-cache"
export UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python"
export HF_HOME="$MODEL_CACHE/huggingface"
mkdir -p "$UV_INSTALL_DIR" "$UV_CACHE_DIR" "$HF_HOME"
curl -LsSf https://astral.sh/uv/install.sh | sh
UV="$UV_INSTALL_DIR/uv"
if [ ! -x "$UV" ]; then UV="$(command -v uv)"; fi
"$UV" python install 3.12
if [ ! -x "$VENV/bin/python" ]; then "$UV" venv "$VENV" --python 3.12 --seed; fi

if [ ! -d "$SGLANG_SRC/.git" ]; then
    git clone https://github.com/sgl-project/sglang-omni.git "$SGLANG_SRC"
fi
git -C "$SGLANG_SRC" fetch --depth 1 origin "$SGLANG_REV"
git -C "$SGLANG_SRC" checkout --detach FETCH_HEAD

"$UV" pip install --python "$VENV/bin/python" -e "$SGLANG_SRC" --torch-backend=cu130
"$UV" pip install --python "$VENV/bin/python" \
    -r "$INSTALL_ROOT/requirements-sglang-worker.txt" --torch-backend=cu130

"$VENV/bin/python" -c \
    "import torch, torchaudio, sglang_omni; assert torch.cuda.is_available(); print('GPU:', torch.cuda.get_device_name(0), 'CUDA:', torch.version.cuda, 'TorchAudio:', torchaudio.__version__)"

if [ "${HIGGS_SGLANG_PRELOAD_MODEL:-1}" = "1" ]; then
    "$VENV/bin/hf" download bosonai/higgs-audio-v3-tts-4b
fi

echo "Worker installation complete at $INSTALL_ROOT"
echo "Create $INSTALL_ROOT/.env from .env.worker.example, then run scripts/start.sh."
