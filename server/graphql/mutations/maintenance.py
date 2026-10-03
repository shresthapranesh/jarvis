"""Maintenance actions: download the Piper TTS voice (mirrors `main.py`)."""

from __future__ import annotations

import asyncio

import strawberry

from core.config import get_config
from core.voice import download_voice, voice_status

from ..types.maintenance import VoiceStatus


@strawberry.type
class MaintenanceMutation:
    @strawberry.mutation
    async def download_voice(self, force: bool = False) -> VoiceStatus:
        """Fetch the configured Piper voice model (~60 MB) if it isn't present.

        Blocking HTTP + file IO, so it runs on a worker thread; parking the
        event loop for the length of the transfer would stall every live
        subscription in the app.
        """
        cfg = get_config()
        current = voice_status(cfg.piper_voice, cfg.work_dir)
        if current.error:
            raise ValueError(current.error)
        if current.ready and not force:
            return VoiceStatus.from_status(current)
        result = await asyncio.to_thread(
            download_voice, cfg.piper_voice, cfg.work_dir, force=force
        )
        return VoiceStatus.from_status(result)
