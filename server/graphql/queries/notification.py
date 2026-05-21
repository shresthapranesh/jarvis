"""Notification channel query."""

from __future__ import annotations

import strawberry

from db.ops import list_notification_channels

from ..types.notification import NotificationChannel


@strawberry.type
class NotificationQuery:
    @strawberry.field
    async def notification_channels(self, info: strawberry.Info) -> list[NotificationChannel]:
        session = info.context["session"]
        rows = await list_notification_channels(session)
        return [NotificationChannel.from_db(c) for c in rows]
