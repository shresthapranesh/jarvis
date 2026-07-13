"""Conversation, Message, and Step GraphQL types.

Conversation and Message implement relay.Node so the frontend can refetch them
by global ID. Step is inlined under Message and doesn't need Node.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

import strawberry
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from strawberry import relay

from db import models as db_models
from db.ops import get_conversation_meta, get_project, list_messages_connection

if TYPE_CHECKING:
    from .project import Project


def _encode_cursor(msg: db_models.Message) -> str:
    raw = f"{msg.created_at.isoformat()}|{msg.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts, mid = raw.split("|", 1)
    return datetime.fromisoformat(ts), mid


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
    input_tokens: int | None
    output_tokens: int | None
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
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
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
class MessageEdge:
    node: Message
    cursor: str


@strawberry.type
class MessageConnection:
    edges: list[MessageEdge]
    page_info: relay.PageInfo


@strawberry.type
class Conversation(relay.Node):
    id: relay.NodeID[str]
    title: str | None
    model: str
    surface: str  # "web" | "telegram" | "discord" | "automation"
    pinned: bool
    project_id: str | None  # raw DB id of the owning project, if any
    created_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Conversation) -> Conversation:
        return cls(
            id=row.id,
            title=row.title,
            model=row.model,
            surface=row.surface,
            pinned=row.pinned,
            project_id=row.project_id,
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
    async def project(
        self, info: strawberry.Info
    ) -> Annotated["Project", strawberry.lazy(".project")] | None:
        if not self.project_id:
            return None
        from .project import Project
        session = info.context["session"]
        row = await get_project(session, self.project_id)
        return Project.from_db(row) if row else None

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
        last: int = 10,
        before: str | None = None,
    ) -> MessageConnection:
        """Backward-paginated message connection (Relay Cursor Connections spec).

        UI only scrolls newest→older, so forward args (first/after) are omitted.
        Cursor is opaque: base64(`{created_at.isoformat()}|{id}`).
        """
        session = info.context["session"]
        last = max(1, min(last, 100))
        before_ts, before_id = _decode_cursor(before) if before else (None, None)
        rows, has_previous = await list_messages_connection(
            session, self.id, last, before_ts, before_id,
        )
        edges = [
            MessageEdge(node=Message.from_db(m), cursor=_encode_cursor(m))
            for m in rows
        ]
        return MessageConnection(
            edges=edges,
            page_info=relay.PageInfo(
                has_next_page=False,
                has_previous_page=has_previous,
                start_cursor=edges[0].cursor if edges else None,
                end_cursor=edges[-1].cursor if edges else None,
            ),
        )
