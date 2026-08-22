"""Workflow runtime — execution + run registration."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession

from core.queue import Job
from core.run_scaffold import (
    finish_task_state,
    get_or_create_task_state,
    new_invocation_context,
    queue_cancel_watch,
    start_budget,
)
from db import async_session
from db.models import Workflow, WorkflowRun
from db.ops import (
    create_workflow_run,
    finish_workflow_run,
    get_workflow,
)
from core.notifications import send_notifications
from core.state import (
    TaskState,
    _tasks,
    emit_event,
    log_task_created,
    log_task_received,
)
from workflow.engine import execute_workflow


# ── Inner work loop ──────────────────────────────────────────────────────────

async def _run_workflow_inner(
    wf: Workflow,
    state: TaskState,
    run_id: str,
    inputs: dict[str, Any],
) -> None:
    """Execute the workflow graph, write events to `state`, persist outcome
    to WorkflowRun, send notifications, set state.done and schedule _tasks
    cleanup. Caller created the WorkflowRun row and registered `state`."""
    final_status = "error"
    try:
        # Budget for workflow overall. Nodes build their own callback sets
        # (workflow/nodes.py:_run_agent_text), so only the tracker is needed here.
        start_budget(state, "workflow")

        definition = json.loads(wf.definition or "{}")
        # Early abort if budget already exceeded before any node
        if state.budget_exceeded:
            raise RuntimeError(f"budget exceeded before start: {state.budget_reason}")
        final_outputs, node_records = await execute_workflow(
            run_id=run_id,
            definition=definition,
            inputs=inputs,
            task_state=state,
        )
        if state.budget_exceeded:
            raise RuntimeError(f"budget exceeded: {state.budget_reason}")

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
        emit_event(state, "workflow_stopped", run_id=run_id)

    except BaseException as exc:
        err = str(exc)
        if state.budget_exceeded:
            emit_event(state, "budget_exceeded", reason=state.budget_reason or err, run_id=run_id)
        async with async_session() as session:
            await finish_workflow_run(session, run_id, "error", None, None, err)
            await send_notifications(
                session, wf.notifications,
                status="error",
                title=wf.name,
                body=err,
            )
        emit_event(state, "workflow_error", error=err, run_id=run_id)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        finish_task_state(run_id, state, final_status)


# ── Queue handler ────────────────────────────────────────────────────────────

async def workflow_job_handler(job: Job) -> None:
    """JobQueue handler — workflow jobs that have been claimed flow through
    here. Convention: ``job.id == WorkflowRun.id``.

    Payload: ``{"workflow_id": str, "inputs": dict}``.
    """
    payload = job.payload
    workflow_id: str = payload["workflow_id"]
    inputs: dict[str, Any] = payload.get("inputs") or {}
    run_id = job.id

    async with async_session() as session:
        wf = await get_workflow(session, workflow_id)
        if wf is None:
            return

        existing = await session.get(WorkflowRun, run_id)
        if existing is None:
            log_task_received("workflow", workflow_id, "queue")
            await create_workflow_run(
                session, workflow_id, json.dumps(inputs), run_id=run_id,
            )
        elif existing.status == "pending":
            existing.status = "running"
            await session.commit()

    # Built but not passed to _run_workflow_inner: the engine resolves its own
    # infra per node. Kept so the run still registers with the runner.
    await new_invocation_context(
        kind="workflow",
        session_id=run_id,
        invocation_id=run_id,
        initial_state={"workflow_id": workflow_id},
    )

    state = get_or_create_task_state(
        run_id, kind="workflow", label=wf.name, parent_id=workflow_id,
    )

    async with queue_cancel_watch(run_id, state):
        await _run_workflow_inner(wf, state, run_id, inputs)


# ── Manual trigger (shared by GraphQL runWorkflow) ───────────────────────────

async def register_workflow_run(
    session: AsyncSession,
    workflow_id: str,
    inputs: dict[str, Any],
) -> str | None:
    """Create a WorkflowRun + enqueue a queue job atomically, register the
    TaskState before commit so SSE subscribers see it, return the run_id.
    Returns None if the workflow doesn't exist.

    job.id == WorkflowRun.id, so stopWorkflowRun(run_id) is a one-line
    queue.cancel(run_id) call.
    """
    from core.state import get_queue

    wf = await get_workflow(session, workflow_id)
    if wf is None:
        return None

    log_task_received("workflow", workflow_id, "http")
    run_id = str(uuid4())

    session.add(WorkflowRun(
        id=run_id,
        workflow_id=workflow_id,
        status="running",
        inputs=json.dumps(inputs),
        node_results="[]",
    ))
    await get_queue().enqueue(
        "workflow",
        {"workflow_id": workflow_id, "inputs": inputs},
        job_id=run_id,
        session=session,
    )

    # Register TaskState BEFORE commit; the queue's after_commit wake fires
    # after we return from session.commit(), so the worker can't race the
    # subscriber lookup.
    _tasks[run_id] = TaskState(
        kind="workflow",
        label=wf.name,
        parent_id=workflow_id,
    )
    log_task_created(run_id, _tasks[run_id], None)

    await session.commit()
    return run_id
