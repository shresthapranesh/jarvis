"""Media endpoints — health, model catalog, TTS, and transcription."""

from __future__ import annotations

import asyncio
import gc
import io
import os
import pathlib
import platform as _platform
import re
import tempfile
import threading
import wave as _wave
from functools import lru_cache

from fastapi import APIRouter, Response, UploadFile
from fastapi.responses import JSONResponse

from core.agents import AVAILABLE_MODELS, DEFAULT_MODEL
from core.config import get_config
from core.schemas import TTSRequest

router = APIRouter()

_PIPER_VOICE_PATH = get_config().piper_voice
_WHISPER_MODEL_SIZE = get_config().whisper_model
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MiB cap for /transcribe uploads


_USE_MLX = _platform.system() == "Darwin" and _platform.machine() == "arm64"
_MLX_REPO: dict[str, str] = {
    "tiny":     "mlx-community/whisper-tiny",
    "base":     "mlx-community/whisper-base",
    "small":    "mlx-community/whisper-small",
    "medium":   "mlx-community/whisper-medium",
    "large":    "mlx-community/whisper-large-v3",
    "large-v2": "mlx-community/whisper-large-v2",
    "large-v3": "mlx-community/whisper-large-v3",
}

_WHISPER_IDLE_TTL = 300  # seconds of inactivity before the model is evicted (CPU path only)

_whisper_lock = threading.Lock()
_whisper_model = None
_whisper_evict_timer: threading.Timer | None = None


def _evict_whisper() -> None:
    global _whisper_model, _whisper_evict_timer
    with _whisper_lock:
        _whisper_model = None
        _whisper_evict_timer = None
    gc.collect()


def _get_whisper_model(size: str):
    global _whisper_model, _whisper_evict_timer
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415
            _whisper_model = WhisperModel(size, device="cpu", compute_type="int8")
        if _whisper_evict_timer is not None:
            _whisper_evict_timer.cancel()
        _whisper_evict_timer = threading.Timer(_WHISPER_IDLE_TTL, _evict_whisper)
        _whisper_evict_timer.daemon = True
        _whisper_evict_timer.start()
        return _whisper_model


@lru_cache(maxsize=4)
def _get_piper_voice(path: str):
    from piper import PiperVoice  # noqa: PLC0415
    return PiperVoice.load(path)


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Model catalog ────────────────────────────────────────────────────────────

@router.get("/models")
async def list_models() -> JSONResponse:
    """Return the set of models the UI may offer."""
    return JSONResponse({
        "default": DEFAULT_MODEL,
        "available": [
            {"id": m.id, "label": m.label, "provider": m.provider}
            for m in AVAILABLE_MODELS
        ],
    })


# ── TTS ──────────────────────────────────────────────────────────────────────

@router.post("/tts")
async def tts_endpoint(req: TTSRequest) -> Response:
    if not os.path.exists(_PIPER_VOICE_PATH):
        return JSONResponse(
            {"error": "voice model not found — set PIPER_VOICE env var"},
            status_code=503,
        )
    clean = re.sub(r"[*_`#\[\]()>~]", "", req.text).strip()
    if not clean:
        return JSONResponse({"error": "empty"}, status_code=400)

    loop = asyncio.get_running_loop()

    def synthesize() -> bytes:
        voice = _get_piper_voice(_PIPER_VOICE_PATH)
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            voice.synthesize_wav(clean, wf)
        return buf.getvalue()

    wav_bytes = await loop.run_in_executor(None, synthesize)
    return Response(content=wav_bytes, media_type="audio/wav")


# ── Transcribe ───────────────────────────────────────────────────────────────

async def transcribe_bytes(data: bytes, suffix: str = ".ogg") -> str:
    """Transcribe raw audio bytes using the local Whisper model."""
    loop = asyncio.get_running_loop()

    def _run() -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            fname = f.name
        try:
            if _USE_MLX:
                import mlx_whisper  # noqa: PLC0415
                repo = _MLX_REPO.get(_WHISPER_MODEL_SIZE, f"mlx-community/whisper-{_WHISPER_MODEL_SIZE}")
                result = mlx_whisper.transcribe(fname, path_or_hf_repo=repo, language="en")
                return str(result.get("text") or "").strip()
            else:
                model = _get_whisper_model(_WHISPER_MODEL_SIZE)
                segments, _ = model.transcribe(fname, beam_size=5, language="en")
                return " ".join(seg.text for seg in segments).strip()
        finally:
            try:
                os.unlink(fname)
            except OSError:
                pass

    return await loop.run_in_executor(None, _run)


@router.post("/transcribe")
async def transcribe_endpoint(audio: UploadFile) -> JSONResponse:
    data = await audio.read(_MAX_AUDIO_BYTES + 1)
    if len(data) > _MAX_AUDIO_BYTES:
        return JSONResponse(
            {"error": f"audio exceeds {_MAX_AUDIO_BYTES // (1024 * 1024)} MiB limit"},
            status_code=413,
        )
    suffix = pathlib.Path(audio.filename or "audio.webm").suffix or ".webm"
    text = await transcribe_bytes(data, suffix)
    return JSONResponse({"text": text})
