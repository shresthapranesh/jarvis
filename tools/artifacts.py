"""Artifact tools — create, read, and list user-visible deliverables.

Artifacts are markdown documents the agent produces as deliverables (reports,
drafts, resumes, etc.) — distinct from `write_file`, which is for scratch work.
The frontend renders them in a side panel; the agent should call
`write_artifact` whenever the user asked for a document-shaped result.

Files are stored on disk under ``AppConfig.artifacts_dir`` as ``{uuid}.md``;
the DB row in ``artifacts`` tracks metadata (title, conversation, timestamps).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_core.tools import tool

from core.config import get_config
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


def _artifact_path(artifact_id: str) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}.md"


def _version_path(artifact_id: str, version: int) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}_v{version}.md"


@tool
async def write_artifact(
    title: str,
    content: str,
    artifact_id: str | None = None,
) -> str:
    """Save a markdown deliverable (report, draft, document, resume, etc.).

    Use this — not write_file — when the user asks for a finished document.
    The frontend renders the artifact in a side panel that the user can read,
    edit, copy, or download.

    Versioning (ADK ArtifactService analog): each write creates a versioned
    snapshot under artifacts_dir/{id}_v{version}.md and a DB row in
    artifact_versions. The live file {id}.md always holds the latest version.
    On first create, version 1 is the initial content. On update, previous
    history is preserved and a new version is added.

    Args:
        title: Short human-readable title (shown in the side panel and library).
        content: Markdown body. Use full headings, lists, tables — render-ready.
        artifact_id: Pass to update an existing artifact in place (creates new
            version). Omit to create a new one.

    Returns the artifact id and title as a JSON string the agent can refer to.
    """
    ctx = current_ctx()
    conversation_id = ctx.conversation_id
    cfg = get_config()

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


@tool
async def read_artifact(artifact_id: str, version: int | None = None) -> str:
    """Read the markdown content of a previously saved artifact.

    Args:
        artifact_id: The artifact id.
        version: Optional version number to read. If omitted, reads the latest
            live version. Versions are numbered from 1 (first save).
    """
    async with async_session() as session:
        art = await get_artifact(session, artifact_id)
        if art is None:
            return f"Artifact not found: {artifact_id}"
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
