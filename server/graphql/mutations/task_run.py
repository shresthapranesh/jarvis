"""Running task mutation — universal stop."""

from __future__ import annotations

import strawberry

from core.state import _background_tasks, _tasks

from ..types.task_run import StopRunningTaskPayload


@strawberry.type
class TaskRunMutation:
    @strawberry.mutation
    async def stop_running_task(self, task_id: str) -> StopRunningTaskPayload:
        state = _tasks.get(task_id)
        if state is None:
            raise ValueError("task not found or already finished")
        if state.done:
            raise ValueError("task already finished")

        state.cancelled = True
        state._stop_event.set()

        if state.resume_future and not state.resume_future.done():
            state.resume_future.cancel()

        bg_task = _background_tasks.get(task_id)
        if bg_task and not bg_task.done():
            bg_task.cancel()

        return StopRunningTaskPayload(ok=True, task_id=task_id, kind=state.kind)
