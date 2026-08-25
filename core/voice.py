"""Piper TTS voice model download — shared by the CLI and the GraphQL API.

The voice is two files (`<name>.onnx` + `<name>.onnx.json`) fetched from the
`rhasspy/piper-voices` HuggingFace repo, whose layout is derived from the voice
*name*: `{lang_region}-{speaker}-{quality}.onnx` →
`{lang}/{lang_region}/{speaker}/{quality}/`. `POST /tts` (server/routes_media.py)
404s until both exist, so this is a setup step rather than a runtime one —
which is exactly why it needs to be reachable from the UI and not only from a
terminal on the box.

Downloading is blocking IO; async callers must run `download_voice` on a worker
thread (`asyncio.to_thread`) so it cannot park the event loop for the length of
a ~60 MB transfer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

# Progress callback: (filename, bytes_so_far, total_or_None).
ProgressCb = Callable[[str, int, int | None], None]


@dataclass
class VoiceFile:
    name: str
    path: str
    url: str
    exists: bool
    size_bytes: int = 0
    downloaded: bool = False


@dataclass
class VoiceStatus:
    """Where the voice lives and whether it is usable right now."""

    voice: str          # the configured voice name, e.g. en_US-hfc_female-medium
    directory: str
    ready: bool         # both files present → /tts will work
    files: list[VoiceFile] = field(default_factory=list)
    error: str = ""     # set when the name can't be parsed into a repo path


def resolve_voice_path(piper_voice: str, work_dir: Path) -> Path:
    """Absolute path of the configured voice; relative names sit under work_dir."""
    path = Path(piper_voice)
    return path if path.is_absolute() else work_dir / path


def _repo_prefix(stem: str) -> str:
    """`en_US-hfc_female-medium` → `en/en_US/hfc_female/medium` (raises on junk)."""
    parts = stem.split("-")
    if len(parts) < 3:
        raise ValueError(
            f"cannot parse voice name {stem!r} (expected lang_region-speaker-quality)"
        )
    lang_region, speaker, quality = parts[0], parts[1], parts[2]
    lang = lang_region.split("_")[0]
    return f"{VOICES_BASE}/{lang}/{lang_region}/{speaker}/{quality}"


def _plan(voice_path: Path) -> list[VoiceFile]:
    prefix = _repo_prefix(voice_path.stem)
    targets = [voice_path, voice_path.parent / f"{voice_path.name}.json"]
    return [
        VoiceFile(
            name=p.name,
            path=str(p),
            url=f"{prefix}/{p.name}",
            exists=p.exists(),
            size_bytes=p.stat().st_size if p.exists() else 0,
        )
        for p in targets
    ]


def voice_status(piper_voice: str, work_dir: Path) -> VoiceStatus:
    """Read-only: is the configured voice downloaded? Never touches the network."""
    voice_path = resolve_voice_path(piper_voice, work_dir)
    try:
        files = _plan(voice_path)
    except ValueError as exc:
        return VoiceStatus(
            voice=voice_path.stem,
            directory=str(voice_path.parent),
            ready=False,
            files=[],
            error=str(exc),
        )
    return VoiceStatus(
        voice=voice_path.stem,
        directory=str(voice_path.parent),
        ready=all(f.exists for f in files),
        files=files,
    )


def download_voice(
    piper_voice: str,
    work_dir: Path,
    *,
    force: bool = False,
    progress: ProgressCb | None = None,
    timeout: float = 30.0,
) -> VoiceStatus:
    """Fetch any missing voice file. Blocking — run it on a worker thread.

    Already-present files are skipped unless `force`, so this is safe to
    re-run; a partial file from an interrupted transfer is the one case that
    needs `force`, because "exists" is all we can cheaply check.
    """
    import httpx

    voice_path = resolve_voice_path(piper_voice, work_dir)
    files = _plan(voice_path)  # raises ValueError on an unparseable name
    voice_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        for f in files:
            dest = Path(f.path)
            if dest.exists() and not force:
                continue
            logger.info("downloading piper voice file %s", f.name)
            # Write to a sibling temp file and rename, so an interrupted
            # transfer never leaves a truncated .onnx that `exists` calls done.
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                with client.stream("GET", f.url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0)) or None
                    seen = 0
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_bytes(chunk_size=65536):
                            fh.write(chunk)
                            seen += len(chunk)
                            if progress:
                                progress(f.name, seen, total)
                tmp.replace(dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            f.exists = True
            f.downloaded = True
            f.size_bytes = dest.stat().st_size

    return VoiceStatus(
        voice=voice_path.stem,
        directory=str(voice_path.parent),
        ready=all(Path(f.path).exists() for f in files),
        files=files,
    )
