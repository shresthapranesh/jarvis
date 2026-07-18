"""Board-task run subscription — wraps stream_task_events for board tasks."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import strawberry

from core.state import _tasks, stream_task_events
from db.ops import get_board_task_by_job

from ..types.automation_events import (
    AutomationDoneEvent,
    AutomationEvent,
    coerce_automation_event,
)
from ..types.events import ErrorEvent


@strawberry.type
class BoardTaskSubscription:
    @strawberry.subscription
    async def board_task_events(
        self,
        info: strawberry.Info,
        run_id: str,
    ) -> AsyncGenerator[AutomationEvent, None]:
        """Live events for one board-task run (run_id == BoardTask.runId).
        Falls back to the DB for a terminal event when the run isn't live."""
        if run_id not in _tasks:
            session = info.context["session"]
            task = await get_board_task_by_job(session, run_id)
            if task is None:
                yield ErrorEvent(error="run not found")
                return
            if task.status == "done":
                yield AutomationDoneEvent(output=task.summary, run_id=run_id)
                return
            if task.status == "blocked":
                yield ErrorEvent(error=task.blocked_reason or "blocked")
                return
            yield ErrorEvent(error="run interrupted (server restarted)")
            return

        state = _tasks[run_id]
        async for raw in stream_task_events(state):
            event = coerce_automation_event(raw)
            if event is not None:
                yield event
