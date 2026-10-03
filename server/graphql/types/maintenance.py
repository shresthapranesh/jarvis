"""Types for the maintenance surface — the TTS voice.

Mirrors the `main.py download-voice` subcommand, which could previously only
be run from a terminal on the machine.
"""

from __future__ import annotations

import strawberry


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
