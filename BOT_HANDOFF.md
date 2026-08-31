# Bot-admin handoff contract

## Purpose and ownership

Each worker represents one GPU. The Telegram bot/backend owns PostgreSQL FIFO
ordering, reserves one worker for a complete job, and sends that job's fragment
batches sequentially. The worker has no persistent queue and performs no Vast.ai
API operations.

## Connection and authentication

- Default worker port: `8787`.
- `GET /health` is public.
- `POST /v1/tts/batch` requires `Authorization: Bearer <WORKER_TTS_TOKEN>`.
- Every `/admin/*` endpoint requires
  `Authorization: Bearer <WORKER_ADMIN_TOKEN>`.
- Tokens must be present and different. A TTS token cannot call admin endpoints,
  and an admin token cannot generate speech.
- Use HTTPS through the platform edge or an SSH tunnel across untrusted networks.

## Worker states

| State | Meaning | Generation behavior |
| --- | --- | --- |
| `starting` | Model has not passed health yet | HTTP 503 |
| `ready` | Model loaded and admission enabled | Accept one batch |
| `busy` | One batch is active | HTTP 409 for another batch |
| `draining` | Admission disabled; active batch may finish | HTTP 503 |
| `unavailable` | Loaded backend is now unreachable | HTTP 503 |
| `error` | Initialization or fatal worker error | HTTP 503 |
| `stopping` | Graceful process shutdown requested | HTTP 503 |

## Public health

`GET /health`:

```json
{
  "status": "ready",
  "ready": true,
  "busy": false,
  "draining": false,
  "worker_id": "gpu-1",
  "model": "higgs-audio",
  "max_batch_size": 32,
  "uptime_seconds": 1234,
  "active_job_id": null,
  "active_batch_size": 0,
  "version": "3.0.0"
}
```

Assign a new job only when `status == "ready"` and `ready == true`. Do not infer
readiness from HTTP 200 alone.

## Admin status and telemetry

`GET /admin/status` with the admin token returns every health field plus:

```json
{
  "gpu": {
    "name": "NVIDIA GeForce RTX 3090",
    "uuid": "GPU-...",
    "utilization_percent": 73,
    "memory_used_mb": 18000,
    "memory_total_mb": 24576,
    "temperature_c": 67,
    "power_usage_watts": 210.0
  },
  "cuda_version": "13.0",
  "driver_version": "580.1",
  "model_loaded": true,
  "last_error": null,
  "telemetry_error": null
}
```

If NVML fails, HTTP 200 is retained, GPU/CUDA/driver fields are null, and
`telemetry_error` explains the failure. Inference continues.

## Generation

`POST /v1/tts/batch`, multipart form-data:

| Field | Required | Meaning |
| --- | --- | --- |
| `reference_audio` | yes | Non-empty cloned-voice audio |
| `reference_text` | yes | Exact transcript of reference audio |
| `items` | yes | JSON array, 1–32 unique string IDs and non-empty text |
| `job_id` | no | UUID used only in state/logs/errors |
| `max_new_tokens` | no | 1–8192, default 768 |
| `temperature` | no | >0–2, default 1.0 |
| `top_p` | no | >0–1, default 0.95 |
| `top_k` | no | 0–1000, default 50 |
| `repetition_penalty` | no | 0.1–2, default 1.0 |
| `seed` | no | -1 or uint32; -1 randomizes each item |

Example `items`:

```json
[
  {"id": "fragment-101", "text": "First fragment."},
  {"id": "fragment-102", "text": "Second fragment."}
]
```

Success is HTTP 200 `application/zip`, containing numeric WAV names and:

```json
{
  "worker_id": "gpu-1",
  "job_id": "9a934250-8d69-4e10-a138-1a18087e7842",
  "items": [
    {"id": "fragment-101", "filename": "001.wav"},
    {"id": "fragment-102", "filename": "002.wav"}
  ]
}
```

Validate that every requested ID appears exactly once and every filename exists
in the ZIP. IDs are preserved exactly and must never be treated as paths.

## Administrative controls

- `POST /admin/drain`: reject new generation immediately, let active work finish,
  and return `status: "draining"`.
- `POST /admin/resume`: restore admission only when healthy; otherwise HTTP 503.
- `POST /admin/shutdown`: return `status: "stopping"`, finish active work in the
  background, then terminate only the worker process. It never stops/deletes the
  Vast instance. Restart through `scripts/start.sh` externally.

## Error contract

```json
{
  "detail": "Worker is busy",
  "worker_id": "gpu-1",
  "job_id": "optional-job-uuid"
}
```

- `401`: missing/wrong token or wrong token class.
- `409`: another generation batch owns the worker.
- `422`: malformed multipart input, JSON, IDs, UUID, audio, or parameters.
- `500`: model/generation failure.
- `503`: starting, draining, unavailable, stopping, or error.

Examples:

```json
{"detail":"Worker is busy","worker_id":"gpu-1"}
```

```json
{"detail":"Worker is draining","worker_id":"gpu-1","job_id":"9a934250-8d69-4e10-a138-1a18087e7842"}
```

## Required environment

```dotenv
WORKER_ID=gpu-1
WORKER_TTS_TOKEN=<generation-only secret>
WORKER_ADMIN_TOKEN=<different admin-only secret>
WORKER_HOST=0.0.0.0
WORKER_PORT=8787
MAX_BATCH_SIZE=32
LOG_LEVEL=INFO
WORKER_VERSION=3.0.0
WORKER_INSTALL_ROOT=/opt/higgs-worker
WORKER_ENV_FILE=/opt/higgs-worker/.env
WORKER_LOG_DIR=/var/log/higgs-worker
MODEL_CACHE_DIR=/workspace/model-cache
```

## SSH prerequisites and fixed commands

The admin backend needs root SSH key access, mapped SSH host/port, SCP/SFTP,
outbound internet, an NVIDIA GPU/driver, CUDA 13-compatible hardware, `apt-get`,
and sufficient disk. Upload secrets in a mode-0600 environment file rather than
interpolating them into shell commands.

```bash
mkdir -p /opt/higgs-worker
unzip -o /tmp/higgs-worker.zip -d /opt/higgs-worker
chmod 700 /opt/higgs-worker/scripts/*.sh
WORKER_INSTALL_ROOT=/opt/higgs-worker WORKER_LOG_DIR=/var/log/higgs-worker MODEL_CACHE_DIR=/workspace/model-cache bash /opt/higgs-worker/scripts/install.sh
install -m 600 /tmp/higgs-worker.env /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/start.sh /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/status.sh /opt/higgs-worker/.env
tail -n 200 /var/log/higgs-worker/worker.log
bash /opt/higgs-worker/scripts/stop.sh /opt/higgs-worker/.env
bash /opt/higgs-worker/scripts/uninstall.sh
```

## Version compatibility

This contract is worker major version `3`. Read `/health.version` after install:

- Exact supported version: normal operation.
- Same major but different minor/patch: allow only by explicit compatibility
  policy.
- Different major or missing version: mark incompatible, drain, and reinstall
  the pinned bundle before assigning another job.
