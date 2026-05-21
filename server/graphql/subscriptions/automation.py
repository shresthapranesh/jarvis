"""Automation run subscription — wraps stream_task_events for automation runs."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import strawberry

from core.state import _tasks, stream_task_events
from db.ops import get_automation_run

from ..types.automation_events import (
    AutomationDoneEvent,
    AutomationEvent,
    coerce_automation_event,
)
from ..types.events import ErrorEvent


@strawberry.type
class AutomationSubscription:
    @strawberry.subscription
    async def automation_run_events(
        self,
        info: strawberry.Info,
        run_id: str,
    ) -> AsyncGenerator[AutomationEvent, None]:
        """Mirrors REST `GET /stream/automation/{run_id}`: if the run isn't in
        the live registry, falls back to the DB for a final `done`/`error`
        event, then closes."""
        if run_id not in _tasks:
            session = info.context["session"]
            run = await get_automation_run(session, run_id)
            if run is None:
                yield ErrorEvent(error="run not found")
                return
            if run.status == "done":
                yield AutomationDoneEvent(output=run.output, run_id=run_id)
                return
            if run.status == "error":
                yield ErrorEvent(error=run.error or "unknown error")
                return
            yield ErrorEvent(error="run interrupted (server restarted)")
            return

        state = _tasks[run_id]
        async for raw in stream_task_events(state):
            event = coerce_automation_event(raw)
            if event is not None:
                yield event
