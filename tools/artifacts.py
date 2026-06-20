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
    get_artifact,
    list_artifacts as db_list_artifacts,
    update_artifact,
)
from tools.context import current_ctx

logger = logging.getLogger(__name__)


def _artifact_path(artifact_id: str) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}.md"


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

    Args:
        title: Short human-readable title (shown in the side panel and library).
        content: Markdown body. Use full headings, lists, tables — render-ready.
        artifact_id: Pass to update an existing artifact in place (overwrites).
            Omit to create a new one.

    Returns the artifact id and title as a JSON string the agent can refer to.
    """
    ctx = current_ctx()
    conversation_id = ctx.conversation_id

    async with async_session() as session:
        if artifact_id:
            existing = await get_artifact(session, artifact_id)
            if existing is None:
                return json.dumps({"error": f"artifact {artifact_id} not found"})
            await update_artifact(session, artifact_id, title=title)
            art = existing
            action = "updated"
        else:
            art = await create_artifact(
                session,
                title=title,
                filename="",  # set after we know the id
                kind="markdown",
                conversation_id=conversation_id,
            )
            path = _artifact_path(art.id)
            await update_artifact(session, art.id, filename=str(path))
            action = "created"

    path = _artifact_path(art.id)
    path.write_text(content, encoding="utf-8")

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
async def read_artifact(artifact_id: str) -> str:
    """Read the markdown content of a previously saved artifact."""
    async with async_session() as session:
        art = await get_artifact(session, artifact_id)
    if art is None:
        return f"Artifact not found: {artifact_id}"
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
    return json.dumps([
        {
            "id": a.id,
            "title": a.title,
            "conversation_id": a.conversation_id,
            "updated_at": a.updated_at.isoformat(),
        }
        for a in rows
    ])
