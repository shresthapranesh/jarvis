"""Artifact tools — create, read, and list user-visible deliverables.

Artifacts are markdown documents the agent produces as deliverables (reports,
drafts, resumes, etc.) — distinct from `write_file`, which is for scratch work.
The frontend renders them in a side panel; the agent should call
`write_artifact` whenever the user asked for a document-shaped result.
Binary deliverables (audio/video/image) go through the same tool via `file_path`.

Files are stored on disk under ``AppConfig.artifacts_dir`` as ``{uuid}.md``;
the DB row in ``artifacts`` tracks metadata (title, conversation, timestamps).
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

from langchain_core.tools import tool

from core.artifact_storage import artifact_path, infer_kind, version_path
from db import async_session
from db.ops import (
    create_artifact,
    create_artifact_version,
    get_artifact,
    get_latest_artifact_version_number,
    list_artifacts as db_list_artifacts,
    update_artifact,
)
from tools.context import current_ctx

logger = logging.getLogger(__name__)

MAX_ARTIFACT_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB — artifacts persist indefinitely and duplicate per version


def _artifact_path(artifact_id: str) -> Path:
    return artifact_path(artifact_id, ".md")


def _version_path(artifact_id: str, version: int) -> Path:
    return version_path(artifact_id, version, ".md")


@tool
async def write_artifact(
    title: str,
    content: str | None = None,
    file_path: str | None = None,
    artifact_id: str | None = None,
) -> str:
    """Save a deliverable the user keeps. Shown in a side panel; auto-versioned.

    Pass `content` for a markdown document (report, draft, resume), or
    `file_path` for an already-written audio/video/image file. Not for scratch
    work — use write_file for that.

    Args:
        title: Short human-readable title.
        content: Markdown body. Mutually exclusive with file_path.
        file_path: Path to an existing file to register instead of content.
        artifact_id: Update this artifact in place; omit to create a new one.

    Returns the artifact id and title as JSON.
    """
    if (content is None) == (file_path is None):
        return json.dumps(
            {"error": "pass exactly one of content= (markdown) or file_path= (a file on disk)"}
        )
    if file_path is not None:
        return await _write_file_artifact(title, file_path, artifact_id)
    assert content is not None
    return await _write_markdown_artifact(title, content, artifact_id)


async def _write_markdown_artifact(
    title: str,
    content: str,
    artifact_id: str | None = None,
) -> str:
    """Write a markdown artifact. Reached via `write_artifact(content=...)`."""
    ctx = current_ctx()
    conversation_id = ctx.conversation_id

    async with async_session() as session:
        if artifact_id:
            existing = await get_artifact(session, artifact_id)
            if existing is None:
                return json.dumps({"error": f"artifact {artifact_id} not found"})
            # --- Versioning: preserve old content if no history (migration) ---
            live_path = _artifact_path(artifact_id)
            latest_version = await get_latest_artifact_version_number(session, artifact_id)
            if latest_version == 0 and live_path.exists():
                try:
                    old_content = live_path.read_text(encoding="utf-8")
                    v1_path = _version_path(artifact_id, 1)
                    v1_path.write_text(old_content, encoding="utf-8")
                    await create_artifact_version(
                        session, artifact_id, existing.title, str(v1_path), 1
                    )
                    latest_version = 1
                    logger.info("artifact %s migration: saved v1 from existing file", artifact_id)
                except Exception as exc:
                    logger.warning("artifact version migration failed for %s: %s", artifact_id, exc)

            # Update title first
            await update_artifact(session, artifact_id, title=title)
            art = existing
            action = "updated"
            # Write new live content
            live_path.write_text(content, encoding="utf-8")
            # Create new version snapshot for this update
            new_version = latest_version + 1
            ver_path = _version_path(artifact_id, new_version)
            try:
                ver_path.write_text(content, encoding="utf-8")
                await create_artifact_version(
                    session, artifact_id, title, str(ver_path), new_version
                )
                logger.info("artifact %s version %d saved", artifact_id, new_version)
            except Exception as exc:
                logger.warning("artifact version save failed for %s v%d: %s", artifact_id, new_version, exc)
        else:
            art = await create_artifact(
                session,
                title=title,
                filename="",  # set after we know the id
                kind="markdown",
                conversation_id=conversation_id,
            )
            live_path = _artifact_path(art.id)
            await update_artifact(session, art.id, filename=str(live_path))
            action = "created"
            # Write live file
            live_path.write_text(content, encoding="utf-8")
            # Version 1
            try:
                v1_path = _version_path(art.id, 1)
                v1_path.write_text(content, encoding="utf-8")
                await create_artifact_version(
                    session, art.id, title, str(v1_path), 1
                )
            except Exception as exc:
                logger.warning("artifact v1 save failed for %s: %s", art.id, exc)

    ctx.emit(
        "artifact",
        action=action,
        id=art.id,
        title=title,
        preview=content[:300],
        conversation_id=conversation_id,
    )
    return json.dumps({"id": art.id, "title": title, "action": action})


async def _write_file_artifact(
    title: str,
    file_path: str,
    artifact_id: str | None = None,
    kind: str | None = None,
    mime_type: str | None = None,
) -> str:
    """Register an on-disk file as a binary artifact.

    Reached via `write_artifact(file_path=...)`. ``kind``/``mime_type`` are not
    on the tool surface (they cost schema tokens on every LLM call and are
    inferable from the extension); they stay here for internal callers.
    """
    ctx = current_ctx()
    conversation_id = ctx.conversation_id

    src = Path(file_path)
    if not src.exists() or not src.is_file():
        return json.dumps({"error": f"file not found: {file_path}"})
    size = src.stat().st_size
    if size > MAX_ARTIFACT_FILE_BYTES:
        return json.dumps({
            "error": f"file is {size} bytes, exceeds the {MAX_ARTIFACT_FILE_BYTES // (1024 * 1024)} MiB artifact cap"
        })

    ext = src.suffix or (mimetypes.guess_extension(mime_type) if mime_type else None) or ".bin"
    if not mime_type:
        mime_type = mimetypes.guess_type(src.name)[0]
    if not kind:
        kind = infer_kind(mime_type, ext)
    data = src.read_bytes()

    async with async_session() as session:
        if artifact_id:
            existing = await get_artifact(session, artifact_id)
            if existing is None:
                return json.dumps({"error": f"artifact {artifact_id} not found"})
            live_path = artifact_path(artifact_id, ext)
            latest_version = await get_latest_artifact_version_number(session, artifact_id)
            await update_artifact(session, artifact_id, title=title, kind=kind, mime_type=mime_type)
            art = existing
            action = "updated"
            live_path.write_bytes(data)
            new_version = latest_version + 1
            ver_path = version_path(artifact_id, new_version, ext)
            try:
                ver_path.write_bytes(data)
                await create_artifact_version(session, artifact_id, title, str(ver_path), new_version)
                logger.info("artifact %s version %d saved", artifact_id, new_version)
            except Exception as exc:
                logger.warning("artifact version save failed for %s v%d: %s", artifact_id, new_version, exc)
        else:
            art = await create_artifact(
                session,
                title=title,
                filename="",
                kind=kind,
                mime_type=mime_type,
                conversation_id=conversation_id,
            )
            live_path = artifact_path(art.id, ext)
            await update_artifact(session, art.id, filename=str(live_path))
            action = "created"
            live_path.write_bytes(data)
            try:
                v1_path = version_path(art.id, 1, ext)
                v1_path.write_bytes(data)
                await create_artifact_version(session, art.id, title, str(v1_path), 1)
            except Exception as exc:
                logger.warning("artifact v1 save failed for %s: %s", art.id, exc)

    ctx.emit(
        "artifact",
        action=action,
        id=art.id,
        title=title,
        preview=f"[{kind} · {mime_type or 'unknown'} · {size} bytes]",
        conversation_id=conversation_id,
    )
    return json.dumps({"id": art.id, "title": title, "action": action, "kind": kind, "mime_type": mime_type, "size": size})


@tool
async def read_artifact(artifact_id: str, version: int | None = None) -> str:
    """Read the markdown content of a previously saved artifact.

    Binary artifacts (audio/video/image/binary kind) can't be read as text —
    this returns a short descriptor instead; use the raw download endpoint to
    fetch the actual bytes.

    Args:
        artifact_id: The artifact id.
        version: Optional version number to read. If omitted, reads the latest
            live version. Versions are numbered from 1 (first save).
    """
    async with async_session() as session:
        art = await get_artifact(session, artifact_id)
        if art is None:
            return f"Artifact not found: {artifact_id}"
        if art.kind != "markdown":
            size = Path(art.filename).stat().st_size if Path(art.filename).exists() else 0
            return (
                f"Artifact {artifact_id} is a {art.kind} file "
                f"({art.mime_type or 'unknown mime type'}, {size} bytes) — "
                "binary, not text-readable; download it via the raw artifact endpoint."
            )
        if version is not None:
            from db.ops import get_artifact_version

            ver = await get_artifact_version(session, artifact_id, version)
            if ver is None:
                return f"Artifact version {version} not found for {artifact_id}"
            vpath = Path(ver.filename)
            if not vpath.exists():
                # Fallback to versioned path convention
                vpath = _version_path(artifact_id, version)
            if not vpath.exists():
                return f"Artifact version file missing: {artifact_id} v{version}"
            return vpath.read_text(encoding="utf-8")

    path = _artifact_path(artifact_id)
    if not path.exists():
        return f"Artifact file missing on disk: {artifact_id}"
    return path.read_text(encoding="utf-8")


@tool
async def list_artifacts(all_conversations: bool = False) -> str:
    """List saved artifacts, newest first.

    By default lists only the current conversation's artifacts. Pass
    all_conversations=True to list every artifact across all conversations —
    use this to find a deliverable produced in earlier work, then fetch it
    with read_artifact(id). Each row includes its conversation_id for context.
    """
    conversation_id = None if all_conversations else current_ctx().conversation_id
    async with async_session() as session:
        rows = await db_list_artifacts(session, conversation_id=conversation_id)
    if not rows:
        return (
            "No artifacts found." if all_conversations
            else "No artifacts in this conversation."
        )
    # Include version count
    from db.ops import get_latest_artifact_version_number

    out = []
    async with async_session() as session:
        for a in rows:
            try:
                vcount = await get_latest_artifact_version_number(session, a.id)
            except Exception:
                vcount = 0
            out.append(
                {
                    "id": a.id,
                    "title": a.title,
                    "conversation_id": a.conversation_id,
                    "updated_at": a.updated_at.isoformat(),
                    "versions": vcount,
                }
            )
    return json.dumps(out)


@tool
async def list_artifact_versions(artifact_id: str) -> str:
    """List version history for an artifact, oldest first.

    Use read_artifact(artifact_id, version=N) to fetch a specific version.
    """
    async with async_session() as session:
        art = await get_artifact(session, artifact_id)
        if art is None:
            return f"Artifact not found: {artifact_id}"
        from db.ops import list_artifact_versions as db_list_versions

        versions = await db_list_versions(session, artifact_id)
    if not versions:
        return f"No versions found for artifact {artifact_id} (may predate versioning)."
    return json.dumps(
        [
            {
                "version": v.version,
                "title": v.title,
                "created_at": v.created_at.isoformat(),
                "filename": v.filename,
            }
            for v in versions
        ]
    )
