"""Running task registry query."""

from __future__ import annotations

import strawberry

from core.state import _tasks

from ..types.task_run import RunningTask


@strawberry.type
class TaskRunQuery:
    @strawberry.field
    def running_tasks(self) -> list[RunningTask]:
        """All currently-tracked in-flight tasks, newest first."""
        rows = [
            RunningTask(
                id=task_id,
                kind=state.kind,
                label=state.label,
                parent_id=state.parent_id,
                started_at=state.started_at,
                has_interrupt=state.pending_interrupt_id is not None,
                cancelled=state.cancelled,
                done=state.done,
            )
            for task_id, state in _tasks.items()
        ]
        rows.sort(key=lambda r: r.started_at, reverse=True)
        return rows
