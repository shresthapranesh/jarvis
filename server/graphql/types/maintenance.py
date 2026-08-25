"""Types for the maintenance surface — checkpoint retention and the TTS voice.

Both mirror `main.py` subcommands that could previously only be run from a
terminal on the machine: `maintenance prune-checkpoints` and `download-voice`.
"""

from __future__ import annotations

import strawberry


@strawberry.type
class CheckpointStats:
    """Shape of checkpoints.db, plus what a sweep would actually remove."""

    db_path: str
    exists: bool
    size_bytes: int
    threads: int
    checkpoints: int
    subgraph_checkpoints: int
    # From a dry-run sweep — i.e. after both guards (active threads, minimum
    # age) have been applied. A low number against a large count is the sweep
    # being careful, not a bug.
    prunable_root: int
    prunable_subgraph: int
    reclaimable_bytes: int
    # Threads left alone because a run is currently using them.
    threads_skipped_active: int
    active_threads: int

    @property
    def prunable(self) -> int:
        return self.prunable_root + self.prunable_subgraph

    @classmethod
    def from_stats(cls, s: dict) -> "CheckpointStats":
        return cls(
            db_path=s["db_path"],
            exists=s["exists"],
            size_bytes=s["size_bytes"],
            threads=s["threads"],
            checkpoints=s["checkpoints"],
            subgraph_checkpoints=s["subgraph_checkpoints"],
            prunable_root=s.get("prunable_root", 0),
            prunable_subgraph=s.get("prunable_subgraph", 0),
            reclaimable_bytes=s.get("reclaimable_bytes", 0),
            threads_skipped_active=s.get("threads_skipped_active", 0),
            active_threads=s.get("active_threads", 0),
        )


@strawberry.type
class CheckpointPruneResult:
    root_pruned: int
    subgraph_pruned: int
    bytes_freed: int
    threads_skipped_active: int
    dry_run: bool
    # Freed pages go on SQLite's freelist and are reused by later checkpoint
    # writes, so the file plateaus rather than shrinking. Handing pages back to
    # the filesystem needs a VACUUM, which needs an exclusive lock — that stays
    # the offline CLI's job, and this line says so in the UI.
    note: str

    @classmethod
    def from_stats(cls, s: dict) -> "CheckpointPruneResult":
        pruned = s["root_pruned"] + s["subgraph_pruned"]
        if s["dry_run"]:
            note = "Nothing was deleted."
        elif pruned:
            note = (
                "Freed pages return to SQLite's freelist and are reused by later writes, "
                "so the file plateaus rather than shrinking. Run `main.py maintenance "
                "prune-checkpoints` with the server stopped to VACUUM."
            )
        else:
            note = "Nothing to prune."
        return cls(
            root_pruned=s["root_pruned"],
            subgraph_pruned=s["subgraph_pruned"],
            bytes_freed=s["bytes_freed"],
            threads_skipped_active=s["threads_skipped_active"],
            dry_run=s["dry_run"],
            note=note,
        )


@strawberry.type
class VoiceFile:
    name: str
    path: str
    url: str
    exists: bool
    size_bytes: int
    downloaded: bool


@strawberry.type
class VoiceStatus:
    """The configured Piper voice, and whether POST /tts can actually use it."""

    voice: str
    directory: str
    ready: bool
    files: list[VoiceFile]
    error: str

    @classmethod
    def from_status(cls, s) -> "VoiceStatus":
        return cls(
            voice=s.voice,
            directory=s.directory,
            ready=s.ready,
            error=s.error,
            files=[
                VoiceFile(
                    name=f.name,
                    path=f.path,
                    url=f.url,
                    exists=f.exists,
                    size_bytes=f.size_bytes,
                    downloaded=f.downloaded,
                )
                for f in s.files
            ],
        )
