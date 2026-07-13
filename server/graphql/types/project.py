"""Project GraphQL type — a group of conversations with shared instructions/memory."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import count_project_conversations, get_project, list_project_conversations

from .conversation import Conversation


@strawberry.type
class Project(relay.Node):
    id: relay.NodeID[str]
    name: str
    description: str | None
    instructions: str
    memory: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Project) -> Project:
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            instructions=row.instructions,
            memory=row.memory,
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
    ) -> Project | None:
        session = info.context["session"]
        row = await get_project(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Project {node_id} not found")
            return None
        return cls.from_db(row)

    @strawberry.field
    async def conversation_count(self, info: strawberry.Info) -> int:
        session = info.context["session"]
        return await count_project_conversations(session, self.id)

    @strawberry.field
    async def conversations(self, info: strawberry.Info) -> list[Conversation]:
        session = info.context["session"]
        rows = await list_project_conversations(session, self.id)
        return [Conversation.from_db(r) for r in rows]
