"""Workflow runtime — execution + run registration.

Used by GraphQL ``runWorkflow`` mutation. REST routes were removed once the
frontend migrated to GraphQL; the runtime helpers stayed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session
from db.ops import (
    create_workflow_run,
    finish_workflow_run,
    get_workflow,
)
from core.notifications import send_notifications
from core.state import (
    TaskState,
    _background_tasks,
    _notify,
    _tasks,
    log_task_complete,
    log_task_created,
    log_task_received,
)
from workflow.engine import execute_workflow


# ── Background executor ───────────────────────────────────────────────────────

async def _execute_workflow_bg(
    workflow_id: str,
    run_id: str,
    inputs: dict[str, Any],
) -> None:
    async with async_session() as session:
        wf = await get_workflow(session, workflow_id)
        if wf is None:
            return

    state = _tasks[run_id]
    final_status = "error"

    try:
        definition = json.loads(wf.definition or "{}")
        final_outputs, node_records = await execute_workflow(
            run_id=run_id,
            definition=definition,
            inputs=inputs,
            task_state=state,
        )

        async with async_session() as session:
            await finish_workflow_run(
                session,
                run_id,
                status="done",
                outputs=json.dumps(final_outputs),
                node_results=json.dumps(node_records),
                error=None,
            )
            await send_notifications(
                session, wf.notifications,
                status="done",
                title=wf.name,
                body=json.dumps(final_outputs, indent=2),
            )
        final_status = "done"

    except asyncio.CancelledError:
        async with async_session() as session:
            await finish_workflow_run(session, run_id, "stopped", None, None, None)
        final_status = "stopped"
        state.events.append({
            "event": "workflow_stopped",
            "data": json.dumps({"run_id": run_id}),
        })

    except BaseException as exc:
        err = str(exc)
        async with async_session() as session:
            await finish_workflow_run(session, run_id, "error", None, None, err)
            await send_notifications(
                session, wf.notifications,
                status="error",
                title=wf.name,
                body=err,
            )
        state.events.append({
            "event": "workflow_error",
            "data": json.dumps({"error": err, "run_id": run_id}),
        })
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        log_task_complete(run_id, state, final_status)
        state.done = True
        _notify(state)
        loop = asyncio.get_running_loop()
        loop.call_later(5.0, lambda rid=run_id: _tasks.pop(rid, None))


# ── Run registration (shared by GraphQL runWorkflow) ─────────────────────────

async def register_workflow_run(
    session: AsyncSession,
    workflow_id: str,
    inputs: dict[str, Any],
) -> str | None:
    """Set up the DB row + TaskState + background task for a workflow run.
    Returns run_id, or None if the workflow doesn't exist."""
    wf = await get_workflow(session, workflow_id)
    if wf is None:
        return None

    log_task_received("workflow", workflow_id, "http")
    run = await create_workflow_run(session, workflow_id, json.dumps(inputs))

    # Register TaskState BEFORE returning — prevents subscription race condition
    _tasks[run.id] = TaskState(
        kind="workflow",
        label=wf.name,
        parent_id=workflow_id,
    )
    log_task_created(run.id, _tasks[run.id], None)

    def _task_done(t: asyncio.Task, run_id: str) -> None:
        _background_tasks.pop(run_id, None)
        if not t.cancelled() and (exc := t.exception()):
            logger.error("workflow run %s raised unhandled %s", run_id, type(exc).__name__, exc_info=exc)

    t = asyncio.create_task(_execute_workflow_bg(workflow_id, run.id, inputs))
    _background_tasks[run.id] = t
    t.add_done_callback(lambda _t: _task_done(_t, run.id))

    return run.id
