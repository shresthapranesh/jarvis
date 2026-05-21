"""Workflow run subscription — wraps stream_task_events for workflow runs."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import strawberry

from core.state import _tasks, stream_task_events
from db.ops import get_workflow_run

from ..types.workflow_events import (
    WorkflowDoneEvent,
    WorkflowErrorEvent,
    WorkflowEvent,
    coerce_workflow_event,
)


@strawberry.type
class WorkflowSubscription:
    @strawberry.subscription
    async def workflow_run_events(
        self,
        info: strawberry.Info,
        run_id: str,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Mirrors REST `GET /stream/workflow/{run_id}`: if the run isn't in
        the live registry, falls back to the DB for a final event, then closes."""
        if run_id not in _tasks:
            session = info.context["session"]
            run = await get_workflow_run(session, run_id)
            if run is None:
                yield WorkflowErrorEvent(error="run not found", run_id=run_id)
                return
            if run.status == "done":
                yield WorkflowDoneEvent(
                    outputs=json.loads(run.outputs or "{}"),
                    run_id=run_id,
                )
                return
            if run.status == "error":
                yield WorkflowErrorEvent(
                    error=run.error or "unknown error",
                    run_id=run_id,
                )
                return
            yield WorkflowErrorEvent(
                error="run interrupted (server restarted)",
                run_id=run_id,
            )
            return

        state = _tasks[run_id]
        async for raw in stream_task_events(state):
            event = coerce_workflow_event(raw)
            if event is not None:
                yield event
