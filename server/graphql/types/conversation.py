"""Conversation, Message, and Step GraphQL types.

Conversation and Message implement relay.Node so the frontend can refetch them
by global ID. Step is inlined under Message and doesn't need Node.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import strawberry
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from strawberry import relay

from db import models as db_models
from db.ops import get_conversation_meta, list_messages_paginated

if TYPE_CHECKING:
    pass


@strawberry.type
class Step:
    id: str
    node: str
    source: str
    data: str | None
    seq: int
    created_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Step) -> Step:
        return cls(
            id=row.id,
            node=row.node,
            source=row.source,
            data=row.data,
            seq=row.seq,
            created_at=row.created_at,
        )


@strawberry.type
class Message(relay.Node):
    id: relay.NodeID[str]
    role: str
    content: str
    model: str | None
    status: str
    created_at: datetime
    steps: list[Step]

    @classmethod
    def from_db(cls, row: db_models.Message) -> Message:
        return cls(
            id=row.id,
            role=row.role,
            content=row.content,
            model=row.model,
            status=row.status,
            created_at=row.created_at,
            steps=[Step.from_db(s) for s in sorted(row.steps, key=lambda x: x.seq)],
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> Message | None:
        session = info.context["session"]
        stmt = (
            select(db_models.Message)
            .where(db_models.Message.id == node_id)
            .options(selectinload(db_models.Message.steps))
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            if required:
                raise ValueError(f"Message {node_id} not found")
            return None
        return cls.from_db(row)


@strawberry.type
class MessagePage:
    """Page of messages — mirrors the REST GET /conversations/{id} shape."""
    messages: list[Message]
    has_more: bool


@strawberry.type
class Conversation(relay.Node):
    id: relay.NodeID[str]
    title: str | None
    model: str
    created_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Conversation) -> Conversation:
        return cls(
            id=row.id,
            title=row.title,
            model=row.model,
            created_at=row.created_at,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> Conversation | None:
        session = info.context["session"]
        row = await get_conversation_meta(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Conversation {node_id} not found")
            return None
        return cls.from_db(row)

    @strawberry.field
    async def message_count(self, info: strawberry.Info) -> int:
        session = info.context["session"]
        result = await session.execute(
            select(func.count(db_models.Message.id))
            .where(db_models.Message.conversation_id == self.id)
        )
        return result.scalar() or 0

    @strawberry.field
    async def messages(
        self,
        info: strawberry.Info,
        limit: int = 10,
        before: datetime | None = None,
    ) -> MessagePage:
        """Page of messages, oldest-first within the page.

        Mirrors REST `GET /conversations/{id}?limit=&before=`: pass `before` as
        the oldest message's `createdAt` from the previous page to walk back.
        """
        session = info.context["session"]
        limit = max(1, min(limit, 100))
        rows, has_more = await list_messages_paginated(session, self.id, limit, before)
        return MessagePage(
            messages=[Message.from_db(m) for m in rows],
            has_more=has_more,
        )
