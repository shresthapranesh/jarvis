"""Document GraphQL type — Relay Node for uploaded files persisted per conversation."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import get_document


@strawberry.type
class Document(relay.Node):
    id: relay.NodeID[str]
    conversation_id: str
    message_id: str | None
    filename: str
    mime_type: str
    size: int
    created_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Document) -> Document:
        return cls(
            id=row.id,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            filename=row.filename,
            mime_type=row.mime_type,
            size=row.size,
            created_at=row.created_at,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> Document | None:
        session = info.context["session"]
        row = await get_document(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Document {node_id} not found")
            return None
        return cls.from_db(row)
