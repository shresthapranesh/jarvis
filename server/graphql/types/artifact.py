"""Artifact GraphQL type — Relay Node, with on-demand `content` resolver."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import strawberry
from strawberry import relay

from core.config import get_config
from db import models as db_models
from db.ops import get_artifact, list_artifact_versions


@strawberry.type
class ArtifactVersion:
    id: str
    artifact_id: str
    version: int
    title: str
    filename: str
    created_at: datetime

    @strawberry.field
    def content(self) -> str:
        p = Path(self.filename)
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ""

    @classmethod
    def from_db(cls, row: db_models.ArtifactVersion) -> "ArtifactVersion":
        return cls(
            id=row.id,
            artifact_id=row.artifact_id,
            version=row.version,
            title=row.title,
            filename=row.filename,
            created_at=row.created_at,
        )


@strawberry.type
class Artifact(relay.Node):
    id: relay.NodeID[str]
    title: str
    filename: str
    kind: str
    mime_type: str | None
    conversation_id: str | None
    message_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Artifact) -> Artifact:
        return cls(
            id=row.id,
            title=row.title,
            filename=row.filename,
            kind=row.kind,
            mime_type=row.mime_type,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> Artifact | None:
        session = info.context["session"]
        row = await get_artifact(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Artifact {node_id} not found")
            return None
        return cls.from_db(row)

    @strawberry.field
    def content(self) -> str:
        """File-backed body. Reads on demand so list queries stay cheap.

        Binary kinds (audio/video/image/binary) return "" here — fetch their
        bytes via the raw download REST endpoint instead.
        """
        path = Path(self.filename)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ""

    @strawberry.field
    async def versions(self, info: strawberry.Info) -> list[ArtifactVersion]:
        session = info.context["session"]
        rows = await list_artifact_versions(session, self.id)
        return [ArtifactVersion.from_db(r) for r in rows]

    @strawberry.field
    def version_count(self) -> int:
        # Cheap sync? We have versions field for full list; this is a helper
        # but we can't async here easily. Use file glob as quick estimate.
        # Glob any extension — versioned files aren't always markdown.
        try:
            cfg = get_config()
            return len(list(cfg.artifacts_dir.glob(f"{self.id}_v*")))
        except Exception:
            return 0
