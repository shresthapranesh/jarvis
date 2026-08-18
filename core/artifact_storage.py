"""Centralized on-disk path helpers for artifacts — shared by the write_artifact
tool, GraphQL mutations, and the GraphQL type's content resolver, so the file
extension is derived in exactly one place instead of being hardcoded to
".md" at each call site.
"""

from __future__ import annotations

from pathlib import Path

from core.config import get_config

_KIND_BY_MIME_PREFIX = {
    "audio": "audio",
    "video": "video",
    "image": "image",
}


def artifact_path(artifact_id: str, ext: str) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}{ext}"


def version_path(artifact_id: str, version: int, ext: str) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}_v{version}{ext}"


def infer_kind(mime_type: str | None, ext: str) -> str:
    """Coarse artifact category from a mime type (preferred) or file extension.

    Markdown artifacts never go through this — write_artifact always passes
    kind="markdown" explicitly. This is only for the file path.
    """
    if mime_type:
        prefix = mime_type.split("/", 1)[0]
        if prefix in _KIND_BY_MIME_PREFIX:
            return _KIND_BY_MIME_PREFIX[prefix]
    ext = ext.lower().lstrip(".")
    if ext in ("mp3", "wav", "ogg", "m4a", "flac", "aac"):
        return "audio"
    if ext in ("mp4", "webm", "mov", "mkv", "avi"):
        return "video"
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"):
        return "image"
    return "binary"
