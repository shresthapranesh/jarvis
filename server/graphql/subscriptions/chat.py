"""Chat subscription — wraps core.state.stream_task_events as typed events."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import strawberry

from core.state import _tasks, stream_task_events
from db.models import Message

from ..types.events import ChatEvent, DoneEvent, ErrorEvent, coerce_chat_event


@strawberry.type
class ChatSubscription:
    @strawberry.subscription
    async def task_events(
        self,
        info: strawberry.Info,
        task_id: str,
    ) -> AsyncGenerator[ChatEvent, None]:
        """Stream events for a chat task.

        Mirrors REST `GET /stream/{task_id}`: if the task isn't in the live
        registry, falls back to the DB to surface a final `done` or `error`
        event for completed/interrupted tasks, then closes.
        """
        if task_id not in _tasks:
            session = info.context["session"]
            msg = await session.get(Message, task_id)
            if msg is None:
                yield ErrorEvent(error="task not found")
                return
            if msg.status == "done":
                yield DoneEvent(message=msg.content, conversation_id=msg.conversation_id)
                return
            yield ErrorEvent(error="task interrupted (server restarted)")
            return

        state = _tasks[task_id]
        async for raw in stream_task_events(state):
            event = coerce_chat_event(raw)
            if event is not None:
                yield event
