"""Read-only maintenance status: TTS voice presence."""

from __future__ import annotations

import strawberry

from core.config import get_config
from core.voice import voice_status

from ..types.maintenance import VoiceStatus


@strawberry.type
class MaintenanceQuery:
    @strawberry.field
    async def voice_status(self) -> VoiceStatus:
        """Is the configured Piper voice downloaded? `POST /tts` 404s until it is."""
        cfg = get_config()
        return VoiceStatus.from_status(voice_status(cfg.piper_voice, cfg.work_dir))
