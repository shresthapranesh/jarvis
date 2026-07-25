"""Piper TTS synthesis — shared by the /tts REST endpoint and the
jarvis.text_to_speech() SDK helper so both call the same voice-loading and
synthesis code.
"""

from __future__ import annotations

import io
import wave as _wave
from functools import lru_cache

from core.config import get_config

_PIPER_VOICE_PATH = get_config().piper_voice


@lru_cache(maxsize=4)
def _get_piper_voice(path: str):
    from piper import PiperVoice  # noqa: PLC0415
    return PiperVoice.load(path)


def synthesize_wav_bytes(text: str, voice_path: str | None = None) -> bytes:
    """Synthesize `text` to WAV bytes using the local Piper voice model.

    Raises ImportError if piper-tts isn't installed in this build, and
    FileNotFoundError if the configured voice model file is missing.
    """
    path = voice_path or _PIPER_VOICE_PATH
    import os
    if not os.path.exists(path):
        raise FileNotFoundError(f"voice model not found: {path} — set PIPER_VOICE env var")
    voice = _get_piper_voice(path)
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)
    return buf.getvalue()
