"""Read-only maintenance status: checkpoint DB shape, TTS voice presence."""

from __future__ import annotations

import strawberry

from core.checkpoint_retention import checkpoint_stats
from core.config import get_config
from core.voice import voice_status

from ..types.maintenance import CheckpointStats, VoiceStatus


@strawberry.type
class MaintenanceQuery:
    @strawberry.field
    async def checkpoint_stats(self) -> CheckpointStats:
        """Size of checkpoints.db and what a prune would remove from it.

        Includes a dry-run sweep, so the numbers are post-guard: threads with a
        live run and checkpoints younger than the minimum age are already
        excluded.
        """
        return CheckpointStats.from_stats(await checkpoint_stats())

    @strawberry.field
    async def voice_status(self) -> VoiceStatus:
        """Is the configured Piper voice downloaded? `POST /tts` 404s until it is."""
        cfg = get_config()
        return VoiceStatus.from_status(voice_status(cfg.piper_voice, cfg.work_dir))
