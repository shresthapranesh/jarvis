"""Global task registry endpoints.

Provides a unified view of in-flight chat / automation / workflow runs and
a single stop endpoint. Backed by the in-memory ``_tasks`` registry; runs
that have completed (or were lost on restart) are not returned here —
historical state lives in conversation/automation/workflow detail endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.state import _background_tasks, _tasks


router = APIRouter()


@router.get("/tasks")
async def list_running_tasks() -> JSONResponse:
    """Return all currently-tracked tasks. Sorted newest-first."""
    rows = [
        {
            "id": task_id,
            "kind": state.kind,
            "label": state.label,
            "parent_id": state.parent_id,
            "started_at": state.started_at.isoformat(),
            "has_interrupt": state.pending_interrupt_id is not None,
            "cancelled": state.cancelled,
            "done": state.done,
        }
        for task_id, state in _tasks.items()
    ]
    rows.sort(key=lambda r: r["started_at"], reverse=True)
    return JSONResponse(rows)


@router.post("/tasks/{task_id}/stop")
async def stop_running_task(task_id: str) -> JSONResponse:
    """Cooperatively cancel a task regardless of its kind.

    Sets ``state.cancelled`` and ``state._stop_event`` so cooperative loops
    bail out, then hard-cancels the underlying asyncio.Task. Per-subsystem
    finally handlers will persist the run with status ``stopped``.
    """
    state = _tasks.get(task_id)
    if state is None:
        return JSONResponse({"error": "task not found or already finished"}, status_code=404)
    if state.done:
        return JSONResponse({"error": "task already finished"}, status_code=400)

    state.cancelled = True
    state._stop_event.set()

    if state.resume_future and not state.resume_future.done():
        state.resume_future.cancel()

    bg_task = _background_tasks.get(task_id)
    if bg_task and not bg_task.done():
        bg_task.cancel()

    return JSONResponse({"ok": True, "task_id": task_id, "kind": state.kind})
