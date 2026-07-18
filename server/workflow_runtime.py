"""Workflow runtime — execution + run registration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession

from core.budget import BudgetTracker, get_budget_limits_for_task
from core.queue import Job, JobQueue
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
    _notify,
    _tasks,
    emit_event,
    log_task_complete,
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
        # Budget for workflow overall
        limits = get_budget_limits_for_task("workflow")
        tracker = BudgetTracker(limits, task_state=state)
        state._budget_tracker = tracker

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
        log_task_complete(run_id, state, final_status)
        state.done = True
        _notify(state)
        loop = asyncio.get_running_loop()
        loop.call_later(5.0, lambda rid=run_id: _tasks.pop(rid, None))


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

    invocation_context = None
    try:
        from core.runner import get_runner_or_none
        from core.invocation_context import InvocationContext
        r = get_runner_or_none()
        if r is not None:
            invocation_context = r.new_invocation_context(
                session_id=run_id,
                kind="workflow",
                initial_state={"workflow_id": workflow_id},
            )
            invocation_context.invocation_id = run_id
    except Exception:
        invocation_context = None

    state = _tasks.get(run_id)
    if state is None:
        state = TaskState(
            kind="workflow",
            label=wf.name,
            parent_id=workflow_id,
        )
        _tasks[run_id] = state
        log_task_created(run_id, state, None)

    from core.state import get_queue
    queue = get_queue()
    cancel_watcher = asyncio.create_task(_watch_queue_cancel(queue, job.id, state))
    try:
        await _run_workflow_inner(wf, state, run_id, inputs)
    finally:
        cancel_watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_watcher


async def _watch_queue_cancel(
    queue: JobQueue, job_id: str, state: TaskState,
) -> None:
    """Poll the queue for cancel; mirror into TaskState.cancelled / _stop_event."""
    while not state.done and not state.cancelled:
        if await queue.is_cancel_requested(job_id):
            state.cancelled = True
            state._stop_event.set()
            if state.resume_future and not state.resume_future.done():
                state.resume_future.cancel()
            return
        await asyncio.sleep(1.0)


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
