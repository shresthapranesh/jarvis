"""Automation endpoints — execution, CRUD, trigger, runs, and SSE streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
from collections.abc import AsyncIterator
from typing import Annotated, Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from core.agents import DEFAULT_MODEL, build_agent, is_valid_model
from core.log_callback import AgentLogger
from core.safety import gate_input, gate_output
from db import async_session, get_session
from db.models import Automation, AutomationRun
from db.ops import (
    create_automation,
    create_automation_run,
    delete_automation,
    finish_automation_run,
    get_automation,
    get_automation_run,
    list_automation_runs,
    list_automations,
    list_automations_with_stats,
    update_automation,
)
from core.notifications import parse_notifications, send_notifications
from core.schemas import AutomationRequest, _invalid_model_response
from core.scheduler import _register_scheduler_job, _remove_scheduler_job
from core.state import TaskState, _background_tasks, _notify, _tasks, get_async_checkpointer, get_http_client, get_store, stream_task_events
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _process_chunk

router = APIRouter()


def _validate_cron(schedule: str | None) -> JSONResponse | None:
    if not schedule:
        return None
    try:
        CronTrigger.from_crontab(schedule)
    except Exception:
        return JSONResponse({"error": "invalid cron expression"}, status_code=400)
    return None


# ── Serializers ──────────────────────────────────────────────────────────────

def _compute_next_run_at(auto: Automation) -> str | None:
    if not (auto.schedule and auto.enabled):
        return None
    try:
        trigger = CronTrigger.from_crontab(auto.schedule, timezone=timezone.utc)
        next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        return next_fire.isoformat() if next_fire else None
    except Exception:
        return None


def _serialize_automation(
    auto: Automation,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": auto.id,
        "name": auto.name,
        "description": auto.description,
        "input_type": auto.input_type,
        "prompt_text": auto.prompt_text,
        "model": auto.model,
        "code_text": auto.code_text,
        "webhook_url": auto.webhook_url,
        "webhook_method": auto.webhook_method,
        "webhook_headers": auto.webhook_headers,
        "webhook_body": auto.webhook_body,
        "schedule": auto.schedule,
        "enabled": auto.enabled,
        "notifications": auto.notifications,
        "created_at": auto.created_at.isoformat(),
        "updated_at": auto.updated_at.isoformat(),
        "next_run_at": _compute_next_run_at(auto),
    }
    if stats is not None:
        payload.update(stats)
    return payload


def _serialize_run(run: AutomationRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "automation_id": run.automation_id,
        "status": run.status,
        "triggered_by": run.triggered_by,
        "output": run.output,
        "error": run.error,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


# ── Execution engines ───────────────────────────────────────────────────────

async def _execute_prompt_type(
    auto: Automation, state: TaskState, checkpointer, run_id: str,
) -> str:
    accumulated: list[str] = []
    coalescer = TokenCoalescer(state)
    agent = build_agent(auto.model or DEFAULT_MODEL, checkpointer=checkpointer, store=get_store())

    async for raw_chunk in agent.astream(
        {"messages": [{"role": "user", "content": auto.prompt_text or ""}]},
        config={
            "configurable": {"thread_id": f"automation_{run_id}"},
            "recursion_limit": 100,
            "callbacks": [AgentLogger()],
        },
        stream_mode=STREAM_MODES,
        subgraphs=True,
    ):
        chunk: StreamChunk = raw_chunk  # type: ignore[assignment]
        if state.cancelled:
            break
        interrupted = await _process_chunk(
            chunk, state, coalescer, accumulated, persist_steps=False,
        )
        if interrupted:
            break

    coalescer.flush_all()
    return "".join(accumulated)


_CODE_TIMEOUT_SECONDS = 60.0


async def _execute_code_type(auto: Automation, state: TaskState) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(auto.code_text or "")
        fname = f.name

    proc: asyncio.subprocess.Process | None = None
    cancel_watcher: asyncio.Task | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            fname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output_lines: list[str] = []

        async def drain_and_wait() -> None:
            async for line in proc.stdout:  # type: ignore[union-attr]
                text = line.decode(errors="replace")
                output_lines.append(text)
                state.events.append({
                    "event": "token",
                    "data": json.dumps({"text": text, "source": "main"}),
                })
                _notify(state)
            await proc.wait()  # type: ignore[union-attr]

        async def watch_cancel() -> None:
            # Polling check is cheap and keeps this independent from
            # threading.Event without requiring loop integration.
            while not state.cancelled:
                await asyncio.sleep(0.2)
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()

        cancel_watcher = asyncio.create_task(watch_cancel())

        try:
            await asyncio.wait_for(drain_and_wait(), timeout=_CODE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            raise RuntimeError(
                f"code timed out after {_CODE_TIMEOUT_SECONDS:.0f}s and was killed"
            )

        if state.cancelled:
            raise asyncio.CancelledError()

        return "".join(output_lines).strip()
    finally:
        if cancel_watcher is not None and not cancel_watcher.done():
            cancel_watcher.cancel()
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        try:
            os.unlink(fname)
        except OSError:
            pass


async def _execute_webhook_type(auto: Automation, state: TaskState) -> str:
    headers: dict = {}
    if auto.webhook_headers:
        try:
            headers = json.loads(auto.webhook_headers)
        except json.JSONDecodeError:
            pass

    client = get_http_client()
    resp = await client.request(
        method=(auto.webhook_method or "POST").upper(),
        url=auto.webhook_url or "",
        headers=headers,
        content=(auto.webhook_body or "").encode(),
    )
    result = f"HTTP {resp.status_code}\n{resp.text}"
    event = {"event": "token", "data": json.dumps({"text": result, "source": "main"})}
    state.events.append(event)
    _notify(state)
    return result


# ── Background execution dispatcher ─────────────────────────────────────────

async def _execute_automation_bg(
    automation_id: str,
    run_id: str | None = None,
    triggered_by: str = "manual",
) -> None:
    """Core async execution for a single automation run."""
    async with async_session() as session:
        auto = await get_automation(session, automation_id)
        if auto is None:
            return

        if run_id is None:
            run = await create_automation_run(session, automation_id, triggered_by)
            run_id = run.id
            # Scheduled path: caller didn't pre-register the TaskState; do it
            # here so the run shows up in /tasks and stays cancellable.
            _tasks[run_id] = TaskState(
                kind="automation",
                label=auto.name,
                parent_id=automation_id,
            )

    state = _tasks[run_id]

    try:
        status = "done"
        notify_status = "done"

        if auto.input_type == "prompt":
            model = auto.model or DEFAULT_MODEL
            rejection = await gate_input(auto.prompt_text or "", model)
            if rejection:
                output = rejection
                status = "blocked"
                notify_status = "blocked"
                state.events.append({"event": "safety_input_blocked", "data": json.dumps({
                    "message": rejection, "run_id": run_id,
                })})
            else:
                raw_output = await _execute_prompt_type(auto, state, get_async_checkpointer(), run_id)
                gated, output_verdict = await gate_output(raw_output, model)
                output = gated
                if output_verdict:
                    status = "blocked"
                    notify_status = "blocked"
                    state.events.append({"event": "safety_output_blocked", "data": json.dumps({
                        "severity": output_verdict.severity,
                        "reason": output_verdict.reason,
                        "redacted_output": gated,
                        "run_id": run_id,
                    })})
        elif auto.input_type == "code":
            output = await _execute_code_type(auto, state)
        elif auto.input_type == "webhook":
            output = await _execute_webhook_type(auto, state)
        else:
            raise ValueError(f"Unknown input_type: {auto.input_type}")

        if state.cancelled:
            async with async_session() as session:
                await finish_automation_run(session, run_id, "stopped", output, None)
            state.events.append({"event": "stopped", "data": json.dumps({"output": output, "run_id": run_id})})
        else:
            async with async_session() as session:
                await finish_automation_run(session, run_id, status, output, None)
            state.events.append({"event": "done", "data": json.dumps({"output": output, "run_id": run_id})})

            await send_notifications(
                parse_notifications(auto.notifications),
                status=notify_status,
                title=auto.name,
                body=output or "",
            )

    except asyncio.CancelledError:
        async with async_session() as session:
            await finish_automation_run(session, run_id, "stopped", None, None)
        state.events.append({"event": "stopped", "data": json.dumps({"run_id": run_id})})

    except BaseException as exc:
        err_text = str(exc)
        async with async_session() as session:
            await finish_automation_run(session, run_id, "error", None, err_text)
        state.events.append({"event": "error", "data": json.dumps({"error": err_text})})
        await send_notifications(
            parse_notifications(auto.notifications),
            status="error",
            title=auto.name,
            body=err_text,
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        state.done = True
        _notify(state)
        _tasks.pop(run_id, None)


# ── CRUD endpoints ───────────────────────────────────────────────────────────

@router.get("/automations")
async def get_automations(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    rows = await list_automations_with_stats(session)
    return JSONResponse([_serialize_automation(a, stats) for a, stats in rows])


@router.post("/automations")
async def create_automation_endpoint(
    request: AutomationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if request.model is not None and not is_valid_model(request.model):
        return _invalid_model_response(request.model)
    if err := _validate_cron(request.schedule):
        return err

    auto = await create_automation(
        session,
        name=request.name,
        description=request.description,
        input_type=request.input_type,
        prompt_text=request.prompt_text,
        model=request.model,
        code_text=request.code_text,
        webhook_url=request.webhook_url,
        webhook_method=request.webhook_method,
        webhook_headers=request.webhook_headers,
        webhook_body=request.webhook_body,
        schedule=request.schedule,
        enabled=request.enabled,
        notifications=request.notifications,
    )

    if auto.enabled and auto.schedule:
        _register_scheduler_job(auto)

    return JSONResponse(_serialize_automation(auto), status_code=201)


@router.get("/automations/{automation_id}")
async def get_automation_endpoint(
    automation_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    auto = await get_automation(session, automation_id)
    if auto is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serialize_automation(auto))


@router.put("/automations/{automation_id}")
async def update_automation_endpoint(
    automation_id: str,
    request: AutomationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if request.model is not None and not is_valid_model(request.model):
        return _invalid_model_response(request.model)
    if err := _validate_cron(request.schedule):
        return err

    auto = await update_automation(
        session,
        automation_id,
        name=request.name,
        description=request.description,
        input_type=request.input_type,
        prompt_text=request.prompt_text,
        model=request.model,
        code_text=request.code_text,
        webhook_url=request.webhook_url,
        webhook_method=request.webhook_method,
        webhook_headers=request.webhook_headers,
        webhook_body=request.webhook_body,
        schedule=request.schedule,
        enabled=request.enabled,
        notifications=request.notifications,
    )
    if auto is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    _remove_scheduler_job(automation_id)
    if auto.enabled and auto.schedule:
        _register_scheduler_job(auto)

    return JSONResponse(_serialize_automation(auto))


@router.delete("/automations/{automation_id}")
async def delete_automation_endpoint(
    automation_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    _remove_scheduler_job(automation_id)
    deleted = await delete_automation(session, automation_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/automations/{automation_id}/trigger")
async def trigger_automation(
    automation_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    auto = await get_automation(session, automation_id)
    if auto is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    run = await create_automation_run(session, automation_id, "manual")
    _tasks[run.id] = TaskState(
        kind="automation",
        label=auto.name,
        parent_id=automation_id,
    )

    def _task_done(t: asyncio.Task, run_id: str) -> None:
        _background_tasks.pop(run_id, None)
        if not t.cancelled() and (exc := t.exception()):
            logger.error("automation run %s raised unhandled %s", run_id, type(exc).__name__, exc_info=exc)

    t = asyncio.create_task(_execute_automation_bg(automation_id, run_id=run.id, triggered_by="manual"))
    _background_tasks[run.id] = t
    t.add_done_callback(lambda _t: _task_done(_t, run.id))

    return JSONResponse({"run_id": run.id})


@router.post("/automations/runs/{run_id}/stop")
async def stop_automation_run(run_id: str) -> JSONResponse:
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


@router.get("/automations/{automation_id}/runs")
async def get_automation_runs(
    automation_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    runs = await list_automation_runs(session, automation_id)
    return JSONResponse([_serialize_run(r) for r in runs])


# ── Automation SSE stream ────────────────────────────────────────────────────

@router.get("/stream/automation/{run_id}")
async def stream_automation_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventSourceResponse:
    async def generate() -> AsyncIterator[dict]:
        if run_id not in _tasks:
            run = await get_automation_run(session, run_id)
            if run is None:
                yield {"event": "error", "data": json.dumps({"error": "run not found"})}
                return
            if run.status == "done":
                yield {"event": "done", "data": json.dumps({"output": run.output, "run_id": run_id})}
                return
            if run.status == "error":
                yield {"event": "error", "data": json.dumps({"error": run.error})}
                return
            yield {"event": "error", "data": json.dumps({"error": "run interrupted (server restarted)"})}
            return

        state = _tasks[run_id]
        async for event in stream_task_events(state):
            yield event

    return EventSourceResponse(generate())
