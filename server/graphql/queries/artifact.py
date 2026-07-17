"""Artifact + Document queries."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import get_artifact, list_artifact_versions, list_artifacts, list_documents

from ..types.artifact import Artifact, ArtifactVersion
from ..types.document import Document


@strawberry.type
class ArtifactQuery:
    @strawberry.field
    async def artifacts(
        self,
        info: strawberry.Info,
        conversation_id: str | None = None,
    ) -> list[Artifact]:
        session = info.context["session"]
        rows = await list_artifacts(session, conversation_id=conversation_id)
        return [Artifact.from_db(a) for a in rows]

    @strawberry.field
    async def artifact(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> Artifact | None:
        session = info.context["session"]
        row = await get_artifact(session, id.node_id)
        if row is None:
            return None
        return Artifact.from_db(row)

    @strawberry.field
    async def documents(
        self,
        info: strawberry.Info,
        conversation_id: str,
    ) -> list[Document]:
        session = info.context["session"]
        rows = await list_documents(session, conversation_id)
        return [Document.from_db(d) for d in rows]

    @strawberry.field
    async def artifact_versions(
        self,
        info: strawberry.Info,
        artifact_id: str,
    ) -> list[ArtifactVersion]:
        session = info.context["session"]
        rows = await list_artifact_versions(session, artifact_id)
        return [ArtifactVersion.from_db(r) for r in rows]
