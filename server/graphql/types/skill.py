"""Skill GraphQL type."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import get_skill


@strawberry.type
class Skill(relay.Node):
    id: relay.NodeID[str]
    name: str
    description: str
    body: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Skill) -> Skill:
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            body=row.body,
            enabled=row.enabled,
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
    ) -> Skill | None:
        session = info.context["session"]
        row = await get_skill(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Skill {node_id} not found")
            return None
        return cls.from_db(row)
