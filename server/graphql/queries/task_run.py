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
        rows = []
        for task_id, state in _tasks.items():
            try:
                input_tokens = getattr(state, 'input_tokens', 0) or 0
                output_tokens = getattr(state, 'output_tokens', 0) or 0
                total_tokens = input_tokens + output_tokens
                llm_calls = getattr(state, 'llm_calls', 0) or 0
                tool_calls = getattr(state, 'tool_calls', 0) or 0
                budget_exceeded = bool(getattr(state, 'budget_exceeded', False))
                budget_reason = getattr(state, 'budget_reason', None)
            except Exception:
                input_tokens = output_tokens = total_tokens = llm_calls = tool_calls = 0
                budget_exceeded = False
                budget_reason = None
            rows.append(
                RunningTask(
                    id=task_id,
                    kind=state.kind,
                    label=state.label,
                    parent_id=state.parent_id,
                    started_at=state.started_at,
                    has_interrupt=state.pending_interrupt_id is not None,
                    cancelled=state.cancelled,
                    done=state.done,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    llm_calls=llm_calls,
                    tool_calls=tool_calls,
                    budget_exceeded=budget_exceeded,
                    budget_reason=budget_reason,
                )
            )
        rows.sort(key=lambda r: r.started_at, reverse=True)
        return rows
