"""Automation runtime — execution helpers + run registration.

Used by GraphQL ``triggerAutomation`` mutation, the scheduler (cron-fired runs
go through ``_execute_automation_bg``), and the GraphQL ``Automation.nextRunAt``
resolver (``_compute_next_run_at``). REST routes were removed once the
frontend migrated to GraphQL; the runtime helpers stayed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from core.agents import DEFAULT_MODEL, build_agent
from core.log_callback import AgentLogger
from core.queue import Job, JobQueue
from core.safety import gate_input, gate_output
from db import async_session
from db.models import Automation, AutomationRun
from db.ops import (
    create_automation_run,
    finish_automation_run,
    get_automation,
)
from core.notifications import send_notifications
from core.state import (
    TaskState,
    _background_tasks,
    _notify,
    _tasks,
    get_async_checkpointer,
    get_http_client,
    get_store,
    log_task_complete,
    log_task_created,
    log_task_received,
)
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _process_chunk


# ── Schedule introspection ───────────────────────────────────────────────────

def _compute_next_run_at(auto: Automation) -> str | None:
    if not (auto.schedule and auto.enabled):
        return None
    try:
        trigger = CronTrigger.from_crontab(auto.schedule, timezone=timezone.utc)
        next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        return next_fire.isoformat() if next_fire else None
    except Exception:
        return None


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

async def _run_automation_inner(
    auto: Automation,
    state: TaskState,
    run_id: str,
) -> None:
    """Execute the work for a single automation run: dispatch by input_type,
    write events to `state`, persist outcome to AutomationRun, send notifications,
    set state.done and schedule _tasks cleanup.

    The caller is responsible for creating the AutomationRun row and registering
    `state` in `_tasks` before invoking this.
    """
    final_status = "error"
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
            final_status = "stopped"
            state.events.append({"event": "stopped", "data": json.dumps({"output": output, "run_id": run_id})})
        else:
            async with async_session() as session:
                await finish_automation_run(session, run_id, status, output, None)
                await send_notifications(
                    session, auto.notifications,
                    status=notify_status,
                    title=auto.name,
                    body=output or "",
                )
            final_status = status
            state.events.append({"event": "done", "data": json.dumps({"output": output, "run_id": run_id})})

    except asyncio.CancelledError:
        async with async_session() as session:
            await finish_automation_run(session, run_id, "stopped", None, None)
        final_status = "stopped"
        state.events.append({"event": "stopped", "data": json.dumps({"run_id": run_id})})

    except BaseException as exc:
        err_text = str(exc)
        async with async_session() as session:
            await finish_automation_run(session, run_id, "error", None, err_text)
            await send_notifications(
                session, auto.notifications,
                status="error",
                title=auto.name,
                body=err_text,
            )
        state.events.append({"event": "error", "data": json.dumps({"error": err_text})})
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        log_task_complete(run_id, state, final_status)
        state.done = True
        _notify(state)
        loop = asyncio.get_running_loop()
        loop.call_later(5.0, lambda rid=run_id: _tasks.pop(rid, None))


async def _execute_automation_bg(
    automation_id: str,
    run_id: str | None = None,
    triggered_by: str = "manual",
) -> None:
    """Legacy asyncio.create_task entry point. Used by manual
    register_automation_run and the cron scheduler until those switch to
    enqueueing through the JobQueue."""
    async with async_session() as session:
        auto = await get_automation(session, automation_id)
        if auto is None:
            return

        if run_id is None:
            log_task_received("automation", automation_id, triggered_by)
            run = await create_automation_run(session, automation_id, triggered_by)
            run_id = run.id
            _tasks[run_id] = TaskState(
                kind="automation",
                label=auto.name,
                parent_id=automation_id,
            )
            log_task_created(run_id, _tasks[run_id], auto.model)
            if (t := asyncio.current_task()) is not None:
                _background_tasks[run_id] = t

    state = _tasks[run_id]
    await _run_automation_inner(auto, state, run_id)


# ── Queue handler ────────────────────────────────────────────────────────────

async def automation_job_handler(job: Job) -> None:
    """JobQueue handler — automation jobs that have been claimed flow through
    here. Convention: ``job.id == AutomationRun.id`` so cancellation and SSE
    lookups don't need a join. The manual path pre-creates the AutomationRun
    with status='pending'; the scheduled path leaves it for the worker.

    Payload: ``{"automation_id": str, "triggered_by": "manual"|"schedule"}``.
    """
    payload = job.payload
    automation_id: str = payload["automation_id"]
    triggered_by: str = payload.get("triggered_by", "manual")
    run_id = job.id

    async with async_session() as session:
        auto = await get_automation(session, automation_id)
        if auto is None:
            return

        existing = await session.get(AutomationRun, run_id)
        if existing is None:
            log_task_received("automation", automation_id, triggered_by)
            await create_automation_run(
                session, automation_id, triggered_by, run_id=run_id,
            )
        elif existing.status == "pending":
            existing.status = "running"
            await session.commit()

    state = _tasks.get(run_id)
    if state is None:
        state = TaskState(
            kind="automation",
            label=auto.name,
            parent_id=automation_id,
        )
        _tasks[run_id] = state
        log_task_created(run_id, state, auto.model)

    # Import here to avoid a circular dependency at module load — state owns
    # the queue accessor, and the queue is only set after the lifespan starts.
    from core.state import get_queue
    queue = get_queue()
    cancel_watcher = asyncio.create_task(_watch_queue_cancel(queue, job.id, state))
    try:
        await _run_automation_inner(auto, state, run_id)
    finally:
        cancel_watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_watcher


async def _watch_queue_cancel(
    queue: JobQueue, job_id: str, state: TaskState,
) -> None:
    """Poll the queue for cancel; mirror into TaskState.cancelled / _stop_event
    so the existing in-runtime observers (state.cancelled checks, the code-type
    cancel watcher) see it without any other plumbing."""
    while not state.done and not state.cancelled:
        if await queue.is_cancel_requested(job_id):
            state.cancelled = True
            state._stop_event.set()
            return
        await asyncio.sleep(1.0)


# ── Run registration (shared by GraphQL triggerAutomation) ───────────────────

async def register_automation_run(
    session: AsyncSession, automation_id: str,
) -> str | None:
    """Register a TaskState + AutomationRun row + background task. Returns run_id,
    or None if the automation doesn't exist."""
    auto = await get_automation(session, automation_id)
    if auto is None:
        return None

    log_task_received("automation", automation_id, "http")
    run = await create_automation_run(session, automation_id, "manual")
    _tasks[run.id] = TaskState(
        kind="automation",
        label=auto.name,
        parent_id=automation_id,
    )
    log_task_created(run.id, _tasks[run.id], auto.model)

    def _task_done(t: asyncio.Task, run_id: str) -> None:
        _background_tasks.pop(run_id, None)
        if not t.cancelled() and (exc := t.exception()):
            logger.error("automation run %s raised unhandled %s", run_id, type(exc).__name__, exc_info=exc)

    t = asyncio.create_task(_execute_automation_bg(automation_id, run_id=run.id, triggered_by="manual"))
    _background_tasks[run.id] = t
    t.add_done_callback(lambda _t: _task_done(_t, run.id))

    return run.id
