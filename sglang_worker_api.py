from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
import uuid
import wave
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import imageio_ffmpeg
from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response

DEFAULT_VERSION = "3.0.0"
logger = logging.getLogger("higgs-sglang-worker")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    tts_token: str
    admin_token: str
    host: str = "0.0.0.0"
    port: int = 8787
    max_batch_size: int = 32
    log_level: str = "INFO"
    version: str = DEFAULT_VERSION
    backend_url: str = "http://127.0.0.1:8096"
    backend_model: str = "bosonai/higgs-audio-v3-tts-4b"
    model_label: str = "higgs-audio"
    cache_dir: Path = Path("/workspace/higgs-sglang-worker/voice-cache")
    temporary_root: Path = Path("/workspace/higgs-sglang-worker/request-tmp")
    max_audio_bytes: int = 50 * 1024 * 1024
    inference_concurrency: int = 4
    shutdown_timeout_seconds: float = 120.0
    backend_poll_seconds: float = 2.0

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        tts_token = (
            os.getenv("WORKER_TTS_TOKEN")
            or os.getenv("WORKER_API_TOKEN")
            or os.getenv("HIGGS_WORKER_TOKEN")
            or ""
        )
        return cls(
            worker_id=os.getenv("WORKER_ID", "gpu-1"),
            tts_token=tts_token,
            admin_token=os.getenv("WORKER_ADMIN_TOKEN", ""),
            host=os.getenv("WORKER_HOST", "0.0.0.0"),
            port=int(os.getenv("WORKER_PORT", "8787")),
            max_batch_size=int(os.getenv("MAX_BATCH_SIZE", "32")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            version=os.getenv("WORKER_VERSION", DEFAULT_VERSION),
            backend_url=os.getenv(
                "HIGGS_SGLANG_BACKEND_URL", "http://127.0.0.1:8096"
            ).rstrip("/"),
            backend_model=os.getenv(
                "HIGGS_SGLANG_MODEL", "bosonai/higgs-audio-v3-tts-4b"
            ),
            model_label=os.getenv("WORKER_MODEL_LABEL", "higgs-audio"),
            cache_dir=Path(
                os.getenv(
                    "HIGGS_WORKER_CACHE_DIR",
                    "/workspace/higgs-sglang-worker/voice-cache",
                )
            ),
            temporary_root=Path(
                os.getenv(
                    "HIGGS_WORKER_TEMP_DIR",
                    "/workspace/higgs-sglang-worker/request-tmp",
                )
            ),
            max_audio_bytes=max(1, int(os.getenv("HIGGS_WORKER_MAX_AUDIO_MB", "50")))
            * 1024
            * 1024,
            inference_concurrency=int(os.getenv("HIGGS_SGLANG_MAX_RUNNING", "4")),
            shutdown_timeout_seconds=max(
                1.0, float(os.getenv("HIGGS_WORKER_SHUTDOWN_TIMEOUT", "120"))
            ),
            backend_poll_seconds=max(
                0.1, float(os.getenv("HIGGS_WORKER_BACKEND_POLL_SECONDS", "2"))
            ),
        )

    def validate(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("WORKER_ID must not be empty")
        if not self.tts_token:
            raise ValueError("WORKER_TTS_TOKEN must be configured")
        if not self.admin_token:
            raise ValueError("WORKER_ADMIN_TOKEN must be configured")
        if hmac.compare_digest(self.tts_token, self.admin_token):
            raise ValueError("WORKER_TTS_TOKEN and WORKER_ADMIN_TOKEN must be different")
        if not self.host.strip():
            raise ValueError("WORKER_HOST must not be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("WORKER_PORT must be from 1 to 65535")
        if not 1 <= self.max_batch_size <= 32:
            raise ValueError("MAX_BATCH_SIZE must be from 1 to 32")
        if not 1 <= self.inference_concurrency <= 32:
            raise ValueError("HIGGS_SGLANG_MAX_RUNNING must be from 1 to 32")
        if not self.version.strip():
            raise ValueError("WORKER_VERSION must not be empty")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")


@dataclass(slots=True)
class WorkerRuntime:
    settings: WorkerSettings
    client: httpx.AsyncClient
    owns_client: bool
    state_lock: asyncio.Lock
    active_done: asyncio.Event
    inference_slots: asyncio.Semaphore
    started_at: float
    accepting: bool = True
    busy: bool = False
    draining: bool = False
    stopping: bool = False
    model_loaded: bool = False
    backend_available: bool = False
    active_job_id: str | None = None
    active_batch_size: int = 0
    initialization_error: str | None = None
    fatal_error: str | None = None
    last_error: str | None = None
    monitor_task: asyncio.Task[None] | None = None
    shutdown_task: asyncio.Task[None] | None = None


class WorkerAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        worker_id: str,
        job_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.worker_id = worker_id
        self.job_id = job_id


def _error_response(_: Request, exc: Exception) -> JSONResponse:
    worker_error = cast(WorkerAPIError, exc)
    payload: dict[str, Any] = {
        "detail": worker_error.detail,
        "worker_id": worker_error.worker_id,
    }
    if worker_error.job_id is not None:
        payload["job_id"] = worker_error.job_id
    return JSONResponse(status_code=worker_error.status_code, content=payload)


def _worker_status(runtime: WorkerRuntime) -> str:
    if runtime.stopping:
        return "stopping"
    if runtime.initialization_error or runtime.fatal_error:
        return "error"
    if runtime.draining:
        return "draining"
    if runtime.busy:
        return "busy"
    if runtime.backend_available and runtime.model_loaded and runtime.accepting:
        return "ready"
    if runtime.model_loaded:
        return "unavailable"
    return "starting"


async def _health_payload(runtime: WorkerRuntime) -> dict[str, Any]:
    async with runtime.state_lock:
        status = _worker_status(runtime)
        return {
            "status": status,
            "ready": status == "ready",
            "busy": runtime.busy,
            "draining": runtime.draining,
            "worker_id": runtime.settings.worker_id,
            "model": runtime.settings.model_label,
            "max_batch_size": runtime.settings.max_batch_size,
            "uptime_seconds": max(0, int(time.monotonic() - runtime.started_at)),
            "active_job_id": runtime.active_job_id,
            "active_batch_size": runtime.active_batch_size,
            "version": runtime.settings.version,
        }


async def _refresh_backend(runtime: WorkerRuntime) -> None:
    available = False
    error: str | None = None
    try:
        response = await runtime.client.get(
            f"{runtime.settings.backend_url}/health", timeout=2.0
        )
        available = response.is_success
        if not available:
            error = f"SGLang health returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        error = f"SGLang health check failed: {type(exc).__name__}"
    async with runtime.state_lock:
        runtime.backend_available = available
        if available:
            runtime.model_loaded = True
        elif error is not None:
            runtime.last_error = error


async def _backend_monitor(runtime: WorkerRuntime) -> None:
    while True:
        await asyncio.sleep(runtime.settings.backend_poll_seconds)
        await _refresh_backend(runtime)


def _validate_wav(data: bytes) -> bool:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            return (
                audio.getnchannels() == 1
                and audio.getframerate() == 24_000
                and audio.getsampwidth() == 2
                and audio.getnframes() > 0
            )
    except (EOFError, wave.Error):
        return False


def _validate_wav_file(path: Path) -> bool:
    try:
        return _validate_wav(path.read_bytes())
    except OSError:
        return False


def _convert_reference(source: Path, source_bytes: bytes, cache_dir: Path) -> Path:
    digest = hashlib.sha256(source_bytes).hexdigest()
    target = cache_dir / f"{digest}.wav"
    temporary = cache_dir / f"{digest}.part.wav"
    if target.exists() and _validate_wav_file(target):
        return target
    target.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode or not _validate_wav_file(temporary):
            logger.error("Reference conversion failed: %s", result.stderr.strip())
            raise ValueError("Reference audio is corrupt or uses an unsupported format")
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


async def _save_reference(
    upload: UploadFile,
    request_dir: Path,
    settings: WorkerSettings,
) -> Path:
    data = await upload.read(settings.max_audio_bytes + 1)
    if not data:
        raise WorkerAPIError(422, "Reference audio is empty", settings.worker_id)
    if len(data) > settings.max_audio_bytes:
        raise WorkerAPIError(
            422,
            f"Reference audio exceeds {settings.max_audio_bytes // 1024 // 1024} MB",
            settings.worker_id,
        )
    source = request_dir / "reference.input"
    await asyncio.to_thread(source.write_bytes, data)
    try:
        return await asyncio.to_thread(
            _convert_reference, source, data, settings.cache_dir
        )
    except ValueError as exc:
        raise WorkerAPIError(422, str(exc), settings.worker_id) from exc


def _parse_items(raw: str, settings: WorkerSettings) -> list[dict[str, str]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerAPIError(
            422, f"items is not valid JSON: {exc.msg}", settings.worker_id
        ) from exc
    if not isinstance(payload, list):
        raise WorkerAPIError(422, "items must be a JSON array", settings.worker_id)
    if not 1 <= len(payload) <= settings.max_batch_size:
        raise WorkerAPIError(
            422,
            f"Batch must contain from 1 to {settings.max_batch_size} items",
            settings.worker_id,
        )
    parsed: list[dict[str, str]] = []
    ids: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise WorkerAPIError(422, f"Item {index} must be an object", settings.worker_id)
        item_id = item.get("id")
        text = item.get("text")
        if not isinstance(item_id, str) or not item_id.strip():
            raise WorkerAPIError(
                422,
                f"Item {index} must contain a non-empty string id",
                settings.worker_id,
            )
        if item_id in ids:
            raise WorkerAPIError(422, f"Duplicate item id: {item_id}", settings.worker_id)
        if not isinstance(text, str) or not text.strip():
            raise WorkerAPIError(
                422,
                f"Item {index} must contain non-empty string text",
                settings.worker_id,
            )
        ids.add(item_id)
        parsed.append({"id": item_id, "text": text.strip()})
    return parsed


def _validate_parameters(
    settings: WorkerSettings,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    repetition_penalty: float,
    seed: int,
) -> None:
    checks = [
        (1 <= max_new_tokens <= 8192, "max_new_tokens must be from 1 to 8192"),
        (0 < temperature <= 2, "temperature must be greater than 0 and at most 2"),
        (0 < top_p <= 1, "top_p must be greater than 0 and at most 1"),
        (0 <= top_k <= 1000, "top_k must be from 0 to 1000"),
        (0.1 <= repetition_penalty <= 2, "repetition_penalty must be from 0.1 to 2"),
        (-1 <= seed <= 4_294_967_295, "seed must be -1 or a 32-bit unsigned integer"),
    ]
    for valid, message in checks:
        if not valid:
            raise WorkerAPIError(422, message, settings.worker_id)


def _parse_job_id(value: str | None, settings: WorkerSettings) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise WorkerAPIError(422, "job_id must be a valid UUID", settings.worker_id) from exc


async def _reserve(runtime: WorkerRuntime, job_id: str | None, batch_size: int) -> None:
    async with runtime.state_lock:
        status = _worker_status(runtime)
        if status == "busy":
            raise WorkerAPIError(409, "Worker is busy", runtime.settings.worker_id, job_id)
        if status != "ready":
            raise WorkerAPIError(
                503,
                f"Worker is {status}",
                runtime.settings.worker_id,
                job_id,
            )
        runtime.busy = True
        runtime.active_job_id = job_id
        runtime.active_batch_size = batch_size
        runtime.active_done.clear()


async def _release(runtime: WorkerRuntime) -> None:
    async with runtime.state_lock:
        runtime.busy = False
        runtime.active_job_id = None
        runtime.active_batch_size = 0
        runtime.active_done.set()


async def _generate_one(
    runtime: WorkerRuntime,
    index: int,
    item: dict[str, str],
    reference_data_url: str,
    reference_text: str,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    repetition_penalty: float,
    seed: int,
) -> tuple[int, bytes]:
    payload = {
        "model": runtime.settings.backend_model,
        "input": item["text"],
        "voice": "default",
        "response_format": "wav",
        "max_new_tokens": max_new_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "repetition_penalty": repetition_penalty,
        "seed": seed + index if seed >= 0 else int.from_bytes(os.urandom(4), "big"),
        "references": [{"audio_path": reference_data_url, "text": reference_text}],
    }
    async with runtime.inference_slots:
        response = await runtime.client.post(
            f"{runtime.settings.backend_url}/v1/audio/speech", json=payload
        )
    if response.status_code != 200:
        raise RuntimeError(f"SGLang generation returned HTTP {response.status_code}")
    if not _validate_wav(response.content):
        raise RuntimeError("SGLang returned an invalid WAV file")
    return index, response.content


def _write_atomic_wav(request_dir: Path, index: int, data: bytes) -> str:
    filename = f"{index:03d}.wav"
    final = request_dir / filename
    temporary = request_dir / f"{index:03d}.part.wav"
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if not _validate_wav_file(temporary):
            raise RuntimeError("Generated audio failed WAV validation")
        temporary.replace(final)
        return filename
    finally:
        temporary.unlink(missing_ok=True)


def _build_archive(
    request_dir: Path,
    generated: list[tuple[int, bytes]],
    items: list[dict[str, str]],
    worker_id: str,
    job_id: str | None,
) -> bytes:
    by_index = {index: audio for index, audio in generated}
    manifest_items: list[dict[str, str]] = []
    filenames: list[str] = []
    for index, item in enumerate(items, start=1):
        audio = by_index.get(index)
        if audio is None:
            raise RuntimeError("Generation omitted one or more requested WAV files")
        filename = _write_atomic_wav(request_dir, index, audio)
        final = request_dir / filename
        if not final.is_file():
            raise RuntimeError("Generated WAV file is missing before packaging")
        filenames.append(filename)
        manifest_items.append({"id": item["id"], "filename": filename})
    manifest = {"worker_id": worker_id, "job_id": job_id, "items": manifest_items}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for filename in filenames:
            output.write(request_dir / filename, arcname=filename)
        output.writestr("manifest.json", json.dumps(manifest, indent=2))
    return archive.getvalue()


def _decode_nvml(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _cuda_version(value: int) -> str:
    return f"{value // 1000}.{(value % 1000) // 10}"


def _read_gpu_telemetry() -> dict[str, Any]:
    import pynvml  # type: ignore[import-not-found]

    pynvml.nvmlInit()
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
        cuda_raw = pynvml.nvmlSystemGetCudaDriverVersion_v2()
        return {
            "gpu": {
                "name": _decode_nvml(pynvml.nvmlDeviceGetName(handle)),
                "uuid": _decode_nvml(pynvml.nvmlDeviceGetUUID(handle)),
                "utilization_percent": int(utilization.gpu),
                "memory_used_mb": int(memory.used // (1024 * 1024)),
                "memory_total_mb": int(memory.total // (1024 * 1024)),
                "temperature_c": int(
                    pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                ),
                "power_usage_watts": round(
                    pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1
                ),
            },
            "cuda_version": _cuda_version(int(cuda_raw)),
            "driver_version": _decode_nvml(pynvml.nvmlSystemGetDriverVersion()),
        }
    finally:
        pynvml.nvmlShutdown()


def _empty_telemetry() -> dict[str, Any]:
    return {
        "gpu": {
            "name": None,
            "uuid": None,
            "utilization_percent": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "temperature_c": None,
            "power_usage_watts": None,
        },
        "cuda_version": None,
        "driver_version": None,
    }


async def _default_shutdown() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


async def _shutdown_when_idle(
    runtime: WorkerRuntime,
    callback: Callable[[], None | Awaitable[None]],
) -> None:
    await runtime.active_done.wait()
    await asyncio.sleep(0.25)
    result = callback()
    if inspect.isawaitable(result):
        await result


def create_app(
    settings: WorkerSettings | None = None,
    backend_client: httpx.AsyncClient | None = None,
    telemetry_provider: Callable[[], dict[str, Any]] | None = None,
    shutdown_callback: Callable[[], None | Awaitable[None]] | None = None,
) -> FastAPI:
    configured = settings or WorkerSettings.from_environment()
    read_telemetry = telemetry_provider or _read_gpu_telemetry
    request_shutdown = shutdown_callback or _default_shutdown

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=getattr(logging, configured.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        client = backend_client or httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=20.0),
            limits=httpx.Limits(
                max_connections=max(8, configured.inference_concurrency * 2)
            ),
        )
        active_done = asyncio.Event()
        active_done.set()
        runtime = WorkerRuntime(
            settings=configured,
            client=client,
            owns_client=backend_client is None,
            state_lock=asyncio.Lock(),
            active_done=active_done,
            inference_slots=asyncio.Semaphore(configured.inference_concurrency),
            started_at=time.monotonic(),
        )
        application.state.runtime = runtime
        try:
            configured.validate()
            await asyncio.to_thread(configured.cache_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                configured.temporary_root.mkdir, parents=True, exist_ok=True
            )
            await _refresh_backend(runtime)
            runtime.monitor_task = asyncio.create_task(
                _backend_monitor(runtime), name="sglang-health-monitor"
            )
        except Exception as exc:
            runtime.initialization_error = str(exc)
            runtime.last_error = str(exc)
            logger.exception("Worker initialization failed")
        try:
            yield
        finally:
            async with runtime.state_lock:
                runtime.accepting = False
                runtime.draining = True
                runtime.stopping = True
            if runtime.monitor_task is not None:
                runtime.monitor_task.cancel()
                await asyncio.gather(runtime.monitor_task, return_exceptions=True)
            try:
                await asyncio.wait_for(
                    runtime.active_done.wait(), timeout=configured.shutdown_timeout_seconds
                )
            except TimeoutError:
                logger.warning("Timed out waiting for the active generation to finish")
            if runtime.owns_client:
                await runtime.client.aclose()

    application = FastAPI(
        title="Higgs SGLang Vast Worker", version=configured.version, lifespan=lifespan
    )
    application.add_exception_handler(WorkerAPIError, _error_response)

    async def require_tts_token(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        runtime: WorkerRuntime = request.app.state.runtime
        if runtime.initialization_error:
            raise WorkerAPIError(
                503, "Worker configuration is invalid", runtime.settings.worker_id
            )
        expected = f"Bearer {runtime.settings.tts_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise WorkerAPIError(401, "Missing or invalid TTS token", runtime.settings.worker_id)

    async def require_admin_token(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        runtime: WorkerRuntime = request.app.state.runtime
        if runtime.initialization_error:
            raise WorkerAPIError(
                503, "Worker configuration is invalid", runtime.settings.worker_id
            )
        expected = f"Bearer {runtime.settings.admin_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise WorkerAPIError(
                401, "Missing or invalid admin token", runtime.settings.worker_id
            )

    @application.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return await _health_payload(request.app.state.runtime)

    @application.get("/admin/status", dependencies=[Depends(require_admin_token)])
    async def admin_status(request: Request) -> dict[str, Any]:
        runtime: WorkerRuntime = request.app.state.runtime
        payload = await _health_payload(runtime)
        telemetry_error: str | None = None
        try:
            telemetry = await asyncio.to_thread(read_telemetry)
        except Exception as exc:
            logger.exception("GPU telemetry collection failed")
            telemetry = _empty_telemetry()
            telemetry_error = f"{type(exc).__name__}: {exc}"
        payload.update(telemetry)
        payload["model_loaded"] = runtime.model_loaded
        payload["last_error"] = runtime.last_error
        payload["telemetry_error"] = telemetry_error
        return payload

    @application.post("/admin/drain", dependencies=[Depends(require_admin_token)])
    async def admin_drain(request: Request) -> dict[str, Any]:
        runtime: WorkerRuntime = request.app.state.runtime
        async with runtime.state_lock:
            if not runtime.stopping:
                runtime.draining = True
                runtime.accepting = False
        logger.info("Worker drain requested worker_id=%s", runtime.settings.worker_id)
        return await _health_payload(runtime)

    @application.post("/admin/resume", dependencies=[Depends(require_admin_token)])
    async def admin_resume(request: Request) -> dict[str, Any]:
        runtime: WorkerRuntime = request.app.state.runtime
        async with runtime.state_lock:
            if runtime.stopping:
                raise WorkerAPIError(503, "Worker is stopping", runtime.settings.worker_id)
            if runtime.initialization_error or runtime.fatal_error:
                raise WorkerAPIError(503, "Worker is in error state", runtime.settings.worker_id)
            if not runtime.model_loaded or not runtime.backend_available:
                raise WorkerAPIError(503, "Model is unavailable", runtime.settings.worker_id)
            runtime.draining = False
            runtime.accepting = True
        logger.info("Worker resumed worker_id=%s", runtime.settings.worker_id)
        return await _health_payload(runtime)

    @application.post("/admin/shutdown", dependencies=[Depends(require_admin_token)])
    async def admin_shutdown(request: Request) -> dict[str, Any]:
        runtime: WorkerRuntime = request.app.state.runtime
        async with runtime.state_lock:
            runtime.accepting = False
            runtime.draining = True
            runtime.stopping = True
            if runtime.shutdown_task is None:
                runtime.shutdown_task = asyncio.create_task(
                    _shutdown_when_idle(runtime, request_shutdown),
                    name="worker-graceful-shutdown",
                )
        logger.info("Worker shutdown requested worker_id=%s", runtime.settings.worker_id)
        return await _health_payload(runtime)

    @application.post("/v1/tts/batch", dependencies=[Depends(require_tts_token)])
    async def generate_batch(
        request: Request,
        reference_audio: Annotated[UploadFile, File()],
        reference_text: Annotated[str, Form()],
        items: Annotated[str, Form()],
        max_new_tokens: Annotated[int, Form()] = 768,
        temperature: Annotated[float, Form()] = 1.0,
        top_p: Annotated[float, Form()] = 0.95,
        top_k: Annotated[int, Form()] = 50,
        repetition_penalty: Annotated[float, Form()] = 1.0,
        seed: Annotated[int, Form()] = -1,
        job_id: Annotated[str | None, Form()] = None,
    ) -> Response:
        runtime: WorkerRuntime = request.app.state.runtime
        parsed_job_id = _parse_job_id(job_id, runtime.settings)
        parsed_items = _parse_items(items, runtime.settings)
        if not reference_text.strip():
            raise WorkerAPIError(
                422,
                "reference_text must not be empty",
                runtime.settings.worker_id,
                parsed_job_id,
            )
        _validate_parameters(
            runtime.settings,
            max_new_tokens,
            temperature,
            top_p,
            top_k,
            repetition_penalty,
            seed,
        )
        reserved = False
        try:
            await _reserve(runtime, parsed_job_id, len(parsed_items))
            reserved = True
            logger.info(
                "Batch started worker_id=%s job_id=%s items=%d",
                runtime.settings.worker_id,
                parsed_job_id,
                len(parsed_items),
            )
            with tempfile.TemporaryDirectory(
                prefix="request-", dir=runtime.settings.temporary_root
            ) as temporary:
                request_dir = Path(temporary)
                reference_path = await _save_reference(
                    reference_audio, request_dir, runtime.settings
                )
                reference_bytes = await asyncio.to_thread(reference_path.read_bytes)
                encoded = base64.b64encode(reference_bytes).decode("ascii")
                reference_data_url = f"data:audio/wav;base64,{encoded}"
                tasks = [
                    _generate_one(
                        runtime,
                        index,
                        item,
                        reference_data_url,
                        reference_text.strip(),
                        max_new_tokens,
                        temperature,
                        top_p,
                        top_k,
                        repetition_penalty,
                        seed,
                    )
                    for index, item in enumerate(parsed_items, start=1)
                ]
                generated = await asyncio.gather(*tasks)
                archive = await asyncio.to_thread(
                    _build_archive,
                    request_dir,
                    generated,
                    parsed_items,
                    runtime.settings.worker_id,
                    parsed_job_id,
                )
            logger.info(
                "Batch completed worker_id=%s job_id=%s items=%d",
                runtime.settings.worker_id,
                parsed_job_id,
                len(parsed_items),
            )
            return Response(
                archive,
                media_type="application/zip",
                headers={
                    "Content-Disposition": 'attachment; filename="higgs_batch.zip"'
                },
            )
        except WorkerAPIError:
            raise
        except Exception as exc:
            runtime.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Batch failed worker_id=%s job_id=%s",
                runtime.settings.worker_id,
                parsed_job_id,
            )
            raise WorkerAPIError(
                500, "Model generation failed", runtime.settings.worker_id, parsed_job_id
            ) from exc
        finally:
            if reserved:
                await _release(runtime)

    return application


app = create_app()
