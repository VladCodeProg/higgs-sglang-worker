# Higgs SGLang worker deployment

The bundle installs one headless Higgs Audio worker per GPU. PostgreSQL, FIFO
ordering, job assignment, and Vast instance creation/deletion stay outside this
repository.

## Requirements and defaults

- Fresh Vast.ai CUDA 13 Linux instance with one NVIDIA GPU and working root SSH.
- RTX 3090/4090-class GPU with 24 GB VRAM is the intended baseline.
- At least 35 GB disk; 50 GB is recommended for wheels and model cache.
- Outbound HTTPS and `apt-get`.

```text
install root: /opt/higgs-worker
environment:  /opt/higgs-worker/.env
logs:         /var/log/higgs-worker
model cache:  /workspace/model-cache
worker port:  8787
```

The bundle owns a private Supervisor process. It does not require systemd and
does not modify Vast's platform Supervisor configuration.

## Manual archive installation

From the admin machine:

```bash
scp -P SSH_PORT higgs-sglang-worker-vast.zip root@SSH_HOST:/tmp/higgs-worker.zip
scp -P SSH_PORT worker-gpu-1.env root@SSH_HOST:/tmp/higgs-worker.env
ssh -p SSH_PORT root@SSH_HOST
```

On the instance:

```bash
mkdir -p /opt/higgs-worker
unzip -o /tmp/higgs-worker.zip -d /opt/higgs-worker
chmod 700 /opt/higgs-worker/scripts/*.sh
WORKER_INSTALL_ROOT=/opt/higgs-worker \
WORKER_LOG_DIR=/var/log/higgs-worker \
MODEL_CACHE_DIR=/workspace/model-cache \
bash /opt/higgs-worker/scripts/install.sh
install -m 600 /tmp/higgs-worker.env /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/start.sh /opt/higgs-worker/.env
```

Upload the environment as a file; do not put secrets in SSH command arguments or
logs. The TTS and admin secrets must be different.

## Repository/ref installation

When the worker is hosted in Git, configure `WORKER_REPO_URL` and `WORKER_REF` as
trusted admin-backend settings, never as Telegram/user input:

```bash
git clone --filter=blob:none "$WORKER_REPO_URL" /tmp/higgs-worker-source
git -C /tmp/higgs-worker-source fetch --depth 1 origin "$WORKER_REF"
git -C /tmp/higgs-worker-source checkout --detach FETCH_HEAD
WORKER_INSTALL_ROOT=/opt/higgs-worker \
WORKER_LOG_DIR=/var/log/higgs-worker \
MODEL_CACHE_DIR=/workspace/model-cache \
bash /tmp/higgs-worker-source/scripts/install.sh
```

This pins application code to `WORKER_REF`; `install.sh` also pins the known
working SGLang-Omni commit and Python package versions.

## Non-interactive admin-backend contract

After uploading the ZIP and environment file, use this fixed sequence:

```bash
mkdir -p /opt/higgs-worker
unzip -o /tmp/higgs-worker.zip -d /opt/higgs-worker
chmod 700 /opt/higgs-worker/scripts/*.sh
WORKER_INSTALL_ROOT=/opt/higgs-worker WORKER_LOG_DIR=/var/log/higgs-worker MODEL_CACHE_DIR=/workspace/model-cache bash /opt/higgs-worker/scripts/install.sh
install -m 600 /tmp/higgs-worker.env /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/start.sh /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/status.sh /opt/higgs-worker/.env
```

Use fixed paths and separately uploaded files. Do not interpolate user-controlled
shell text. Dynamic SSH values must come from the trusted Vast instance record.

## Environment

Start with `.env.worker.example`. Keep CUDA graph size at least as large as
SGLang concurrency. Higher values use more VRAM and are independent of the
worker's one-request admission rule.

## Operations

```bash
bash /opt/higgs-worker/scripts/start.sh /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/status.sh /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/stop.sh /opt/higgs-worker/.env
tail -n 200 /var/log/higgs-worker/worker.log
tail -n 200 /opt/higgs-worker/sglang-backend.log
tail -f /var/log/higgs-worker/worker.log
bash /opt/higgs-worker/scripts/uninstall.sh
REMOVE_MODEL_CACHE=1 bash /opt/higgs-worker/scripts/uninstall.sh
```

`start.sh` avoids duplicate processes. `stop.sh` drains first and escalates to
Supervisor only after graceful API shutdown fails. Restart is deliberately
external: run `stop.sh`, then `start.sh`.

## Networking

For private SSH tunnels, bind the worker to `127.0.0.1` and map distinct local
ports:

```bash
ssh -N -p GPU1_SSH_PORT root@GPU1_HOST -L 8787:127.0.0.1:8787
ssh -N -p GPU2_SSH_PORT root@GPU2_HOST -L 8788:127.0.0.1:8787
ssh -N -p GPU3_SSH_PORT root@GPU3_HOST -L 8789:127.0.0.1:8787
```

When binding `0.0.0.0`, expose the Vast port only through the intended network
layer. Generation and admin APIs are token protected; `/health` is public.
