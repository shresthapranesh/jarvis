"""Workflow endpoints — CRUD for definitions, run triggering, and SSE streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from db import async_session, get_session
from db.models import Workflow, WorkflowRun
from db.ops import (
    create_workflow,
    create_workflow_run,
    delete_workflow,
    finish_workflow_run,
    get_workflow,
    get_workflow_run,
    list_workflow_runs,
    list_workflows,
    update_workflow,
)
from core.notifications import send_notifications
from core.schemas import WorkflowCreateRequest, WorkflowRunRequest, WorkflowUpdateRequest
from core.state import (
    TaskState,
    _background_tasks,
    _notify,
    _tasks,
    log_task_complete,
    log_task_created,
    log_task_received,
    stream_task_events,
)
from workflow.engine import execute_workflow

router = APIRouter()


# ── Serializers ───────────────────────────────────────────────────────────────

def _serialize_workflow(wf: Workflow) -> dict[str, Any]:
    return {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description,
        "definition": wf.definition,
        "notifications": wf.notifications,
        "created_at": wf.created_at.isoformat(),
        "updated_at": wf.updated_at.isoformat(),
    }


def _serialize_run(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "status": run.status,
        "inputs": run.inputs,
        "outputs": run.outputs,
        "node_results": run.node_results,
        "error": run.error,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


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


# ── Workflow CRUD ─────────────────────────────────────────────────────────────

@router.get("/workflows")
async def get_workflows(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    workflows = await list_workflows(session)
    return JSONResponse([_serialize_workflow(wf) for wf in workflows])


@router.post("/workflows")
async def create_workflow_endpoint(
    request: WorkflowCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    wf = await create_workflow(
        session,
        name=request.name,
        description=request.description,
        definition=request.definition,
        notifications=request.notifications,
    )
    return JSONResponse(_serialize_workflow(wf), status_code=201)


@router.get("/workflows/{workflow_id}")
async def get_workflow_endpoint(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    wf = await get_workflow(session, workflow_id)
    if wf is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serialize_workflow(wf))


@router.put("/workflows/{workflow_id}")
async def update_workflow_endpoint(
    workflow_id: str,
    request: WorkflowUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        wf = await get_workflow(session, workflow_id)
        if wf is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(_serialize_workflow(wf))

    wf = await update_workflow(session, workflow_id, **updates)
    if wf is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serialize_workflow(wf))


@router.delete("/workflows/{workflow_id}")
async def delete_workflow_endpoint(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    deleted = await delete_workflow(session, workflow_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


# ── Run endpoints ─────────────────────────────────────────────────────────────

@router.post("/workflows/{workflow_id}/run")
async def trigger_workflow_run(
    workflow_id: str,
    request: WorkflowRunRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    wf = await get_workflow(session, workflow_id)
    if wf is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    log_task_received("workflow", workflow_id, "http")
    run = await create_workflow_run(session, workflow_id, json.dumps(request.inputs))

    # Register TaskState BEFORE returning — prevents SSE client race condition
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

    t = asyncio.create_task(
        _execute_workflow_bg(workflow_id, run.id, request.inputs)
    )
    _background_tasks[run.id] = t
    t.add_done_callback(lambda _t: _task_done(_t, run.id))

    return JSONResponse({"run_id": run.id}, status_code=202)


@router.post("/workflow-runs/{run_id}/stop")
async def stop_workflow_run(run_id: str) -> JSONResponse:
    state = _tasks.get(run_id)
    if state is None:
        return JSONResponse({"error": "run not found or already finished"}, status_code=404)
    if state.done:
        return JSONResponse({"error": "run already finished"}, status_code=400)

    state.cancelled = True
    state._stop_event.set()

    bg_task = _background_tasks.get(run_id)
    if bg_task and not bg_task.done():
        bg_task.cancel()

    return JSONResponse({"ok": True, "run_id": run_id})


@router.get("/workflows/{workflow_id}/runs")
async def get_workflow_runs(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    runs = await list_workflow_runs(session, workflow_id)
    return JSONResponse([_serialize_run(r) for r in runs])


@router.get("/workflow-runs/{run_id}")
async def get_workflow_run_endpoint(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    run = await get_workflow_run(session, run_id)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serialize_run(run))


# ── SSE stream ────────────────────────────────────────────────────────────────

@router.get("/stream/workflow/{run_id}")
async def stream_workflow_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventSourceResponse:
    async def generate() -> AsyncIterator[dict]:
        if run_id not in _tasks:
            # Historical / reconnect path — serve from DB
            run = await get_workflow_run(session, run_id)
            if run is None:
                yield {"event": "workflow_error", "data": json.dumps({"error": "run not found"})}
                return
            if run.status == "done":
                yield {"event": "workflow_done", "data": json.dumps({
                    "outputs": json.loads(run.outputs or "{}"),
                    "run_id": run_id,
                })}
                return
            if run.status == "error":
                yield {"event": "workflow_error", "data": json.dumps({"error": run.error})}
                return
            yield {"event": "workflow_error", "data": json.dumps({"error": "run interrupted (server restarted)"})}
            return

        state = _tasks[run_id]
        async for event in stream_task_events(state):
            yield event

    return EventSourceResponse(generate())
