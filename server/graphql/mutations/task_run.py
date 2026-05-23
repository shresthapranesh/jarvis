"""Running task mutation — universal stop."""

from __future__ import annotations

import strawberry

from core.state import _tasks, get_queue

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

        # In-process fast path so the running handler observes immediately.
        state.cancelled = True
        state._stop_event.set()
        if state.resume_future and not state.resume_future.done():
            state.resume_future.cancel()

        # Durable + cross-process path. job.id == task_id by convention for
        # all three kinds (chat / automation / workflow).
        await get_queue().cancel(task_id)

        return StopRunningTaskPayload(ok=True, task_id=task_id, kind=state.kind)
