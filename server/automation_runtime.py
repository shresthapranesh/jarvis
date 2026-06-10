"""Automation runtime — execution helpers + run registration.

Both manual triggers (``triggerAutomation``) and cron triggers
(``_run_scheduled_automation`` in core.scheduler) enqueue a queue job; the
``automation_job_handler`` defined here consumes those jobs. The GraphQL
``Automation.nextRunAt`` resolver uses ``_compute_next_run_at``.
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
    _notify,
    _tasks,
    emit_event,
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
                emit_event(state, "token", text=text, source="main")
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
    emit_event(state, "token", text=result, source="main")
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
                emit_event(state, "safety_input_blocked", message=rejection, run_id=run_id)
            else:
                raw_output = await _execute_prompt_type(auto, state, get_async_checkpointer(), run_id)
                gated, output_verdict = await gate_output(raw_output, model)
                output = gated
                if output_verdict:
                    status = "blocked"
                    notify_status = "blocked"
                    emit_event(
                        state, "safety_output_blocked",
                        severity=output_verdict.severity,
                        reason=output_verdict.reason,
                        redacted_output=gated,
                        run_id=run_id,
                    )
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
            emit_event(state, "stopped", output=output, run_id=run_id)
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
            emit_event(state, "done", output=output, run_id=run_id)

    except asyncio.CancelledError:
        async with async_session() as session:
            await finish_automation_run(session, run_id, "stopped", None, None)
        final_status = "stopped"
        emit_event(state, "stopped", run_id=run_id)

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
        emit_event(state, "error", error=err_text)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        log_task_complete(run_id, state, final_status)
        state.done = True
        _notify(state)
        loop = asyncio.get_running_loop()
        loop.call_later(5.0, lambda rid=run_id: _tasks.pop(rid, None))


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


# ── Manual trigger (shared by GraphQL triggerAutomation) ─────────────────────

async def register_automation_run(
    session: AsyncSession, automation_id: str,
) -> str | None:
    """Create an AutomationRun + enqueue a queue job atomically, register the
    TaskState in `_tasks` so SSE subscribers can connect immediately, and
    return the run_id. Returns None if the automation doesn't exist.

    The convention is ``job.id == AutomationRun.id``, which makes
    ``stopAutomationRun(run_id)`` a trivial ``queue.cancel(run_id)`` call.
    """
    from uuid import uuid4
    from core.state import get_queue

    auto = await get_automation(session, automation_id)
    if auto is None:
        return None

    log_task_received("automation", automation_id, "http")
    run_id = str(uuid4())

    # Insert AutomationRun + enqueue Job in one transaction so we can't end
    # up with a job pointing at a row that doesn't exist (or vice-versa).
    session.add(AutomationRun(
        id=run_id,
        automation_id=automation_id,
        triggered_by="manual",
        status="running",
    ))
    await get_queue().enqueue(
        "automation",
        {"automation_id": automation_id, "triggered_by": "manual"},
        job_id=run_id,
        session=session,
    )

    # Register the TaskState BEFORE committing. The queue's wake-on-commit
    # listener fires after we commit; if the worker were to claim before
    # `_tasks[run_id]` existed, the handler would create its own TaskState
    # and we'd race here overwriting it.
    _tasks[run_id] = TaskState(
        kind="automation",
        label=auto.name,
        parent_id=automation_id,
    )
    log_task_created(run_id, _tasks[run_id], auto.model)

    await session.commit()
    return run_id
