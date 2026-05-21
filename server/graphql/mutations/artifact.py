"""Artifact + Document mutations — update/delete artifact, delete document."""

from __future__ import annotations

from pathlib import Path

import strawberry
from strawberry import relay

from core.config import get_config
from db.ops import (
    delete_artifact as db_delete_artifact,
    delete_document as db_delete_document,
    get_artifact,
    update_artifact as db_update_artifact,
)

from ..types.artifact import Artifact


def _artifact_path(artifact_id: str) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}.md"


@strawberry.type
class ArtifactMutation:
    @strawberry.mutation
    async def update_artifact(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        title: str | None = None,
        content: str | None = None,
    ) -> Artifact:
        session = info.context["session"]
        art = await get_artifact(session, id.node_id)
        if art is None:
            raise ValueError("artifact not found")
        if title is not None:
            await db_update_artifact(session, id.node_id, title=title)
        if content is not None:
            _artifact_path(id.node_id).write_text(content, encoding="utf-8")
            await db_update_artifact(session, id.node_id)  # bump updated_at
        # Re-read so we return the post-update state (matches REST PATCH semantics
        # which the frontend follows up with a refetch).
        updated = await get_artifact(session, id.node_id)
        assert updated is not None
        return Artifact.from_db(updated)

    @strawberry.mutation
    async def delete_artifact(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        art = await get_artifact(session, id.node_id)
        if art is None:
            raise ValueError("artifact not found")
        path = _artifact_path(id.node_id)
        if path.exists():
            path.unlink()
        await db_delete_artifact(session, id.node_id)
        return True

    @strawberry.mutation
    async def delete_document(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        doc = await db_delete_document(session, id.node_id)
        if doc is None:
            raise ValueError("document not found")
        try:
            Path(doc.path).unlink(missing_ok=True)
        except OSError:
            pass
        return True
