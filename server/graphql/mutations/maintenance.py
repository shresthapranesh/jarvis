"""Maintenance actions: prune checkpoints, download the Piper TTS voice.

Both mirror `main.py` subcommands. The checkpoint prune here is the **online**
sweep (`core/checkpoint_retention.py`), not the CLI's offline one — it can run
against a live server because it skips threads with an in-flight run and never
touches a checkpoint younger than an hour. The CLI keeps the aggressive
variant, which is more thorough and VACUUMs but needs the server stopped; the
result's `note` says so rather than leaving the difference implicit.
"""

from __future__ import annotations

import asyncio

import strawberry

from core.checkpoint_retention import prune_checkpoints
from core.config import get_config
from core.voice import download_voice, voice_status

from ..types.maintenance import CheckpointPruneResult, VoiceStatus


@strawberry.type
class MaintenanceMutation:
    @strawberry.mutation
    async def prune_checkpoints(self, dry_run: bool = False) -> CheckpointPruneResult:
        """Drop superseded checkpoints from the live DB."""
        return CheckpointPruneResult.from_stats(await prune_checkpoints(dry_run=dry_run))

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
