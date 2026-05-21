"""Notification channel mutations — create, update, delete."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import (
    create_notification_channel,
    delete_notification_channel as db_delete_notification_channel,
    get_notification_channel,
    list_references_to_channel,
    update_notification_channel as db_update_notification_channel,
)

from ..types.notification import NotificationChannel


@strawberry.input
class NotificationChannelCreateInput:
    name: str
    type: str  # "telegram" | "discord"
    target: str


@strawberry.input
class NotificationChannelUpdateInput:
    name: str | None = None
    type: str | None = None  # "telegram" | "discord"
    target: str | None = None


@strawberry.type
class NotificationMutation:
    @strawberry.mutation
    async def create_notification_channel(
        self,
        info: strawberry.Info,
        input: NotificationChannelCreateInput,
    ) -> NotificationChannel:
        if not input.target.strip():
            raise ValueError("target required")
        if not input.name.strip():
            raise ValueError("name required")
        session = info.context["session"]
        ch = await create_notification_channel(
            session,
            name=input.name.strip(),
            type=input.type,
            target=input.target.strip(),
        )
        return NotificationChannel.from_db(ch)

    @strawberry.mutation
    async def update_notification_channel(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        input: NotificationChannelUpdateInput,
    ) -> NotificationChannel:
        session = info.context["session"]
        existing = await get_notification_channel(session, id.node_id)
        if existing is None:
            raise ValueError("channel not found")
        if input.target is not None and not input.target.strip():
            raise ValueError("target required")
        if input.name is not None and not input.name.strip():
            raise ValueError("name required")
        ch = await db_update_notification_channel(
            session, id.node_id,
            name=input.name.strip() if input.name is not None else None,
            type=input.type,
            target=input.target.strip() if input.target is not None else None,
        )
        assert ch is not None
        return NotificationChannel.from_db(ch)

    @strawberry.mutation
    async def delete_notification_channel(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        existing = await get_notification_channel(session, id.node_id)
        if existing is None:
            raise ValueError("channel not found")
        refs = await list_references_to_channel(session, id.node_id)
        if refs:
            summary = ", ".join(f"{r['kind']}:{r['name']}" for r in refs)
            raise ValueError(f"channel in use by {len(refs)} reference(s): {summary}")
        await db_delete_notification_channel(session, id.node_id)
        return True
