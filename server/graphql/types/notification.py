"""NotificationChannel GraphQL type."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import get_notification_channel


@strawberry.type
class NotificationChannel(relay.Node):
    id: relay.NodeID[str]
    name: str
    type: str  # "telegram" | "discord"
    target: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row: db_models.NotificationChannel) -> NotificationChannel:
        return cls(
            id=row.id,
            name=row.name,
            type=row.type,
            target=row.target,
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
    ) -> NotificationChannel | None:
        session = info.context["session"]
        row = await get_notification_channel(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"NotificationChannel {node_id} not found")
            return None
        return cls.from_db(row)
