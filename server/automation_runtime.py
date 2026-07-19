"""Automation runtime — execution helpers + run registration."""

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
from core.invocation_context import InvocationContext
from core.budget import BudgetTracker, get_budget_limits_for_task
from core.runner import build_callbacks
from core.queue import Job, JobQueue
from db import async_session
from db.models import Automation, AutomationRun
from db.ops import (
    add_message,
    automation_conversation_id,
    create_automation_run,
    finish_automation_run,
    get_automation,
    get_or_create_conversation,
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
    auto: Automation, state: TaskState, checkpointer, thread_id: str,
    invocation_context: InvocationContext | None = None,
) -> str:
    accumulated: list[str] = []
    coalescer = TokenCoalescer(state)
    limits = get_budget_limits_for_task("automation")
    tracker = BudgetTracker(limits, task_state=state)
    state._budget_tracker = tracker
    callbacks = build_callbacks(tracker, task_state=state)
    _store = invocation_context.store if invocation_context and invocation_context.store else get_store()
    agent = build_agent(auto.model or DEFAULT_MODEL, checkpointer=checkpointer, store=_store, invocation_context=invocation_context)

    user_content = auto.prompt_text or ""
    if auto.input_type == "monitor":
        user_content = _MONITOR_WRAPPER.format(target=user_content)

    async for raw_chunk in agent.astream(
        {"messages": [{"role": "user", "content": user_content}]},
        config={
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 100,
            "callbacks": callbacks,
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


# ── Monitor input type ───────────────────────────────────────────────────────
#
# A monitor is a prompt run that is always stateful (the shared thread holds
# the previous observations) and whose notifications are delta-gated: when the
# agent reports the sentinel, the run finishes with status "no_change" and no
# notification is sent — silence means "nothing new".

_NO_CHANGE_SENTINEL = "NO_CHANGE"

_MONITOR_WRAPPER = """\
You are running as a scheduled monitor. Check the target described below and \
compare what you observe against your previous observations earlier in this \
conversation.

- First check (no previous observations): reply with a concise baseline of the current state.
- Nothing meaningful has changed since the last check: reply with exactly NO_CHANGE on the \
first line. You may note minor details below it; nothing will be delivered.
- Something meaningful changed: reply with a concise report of what changed and the new \
state. Do NOT start the reply with NO_CHANGE.

Target to monitor:
{target}"""


def _monitor_reported_no_change(output: str) -> bool:
    stripped = (output or "").strip()
    if not stripped:
        return False
    first_line = stripped.splitlines()[0].strip().strip("*_`\"'. ")
    return first_line.upper() == _NO_CHANGE_SENTINEL


# ── Background execution dispatcher ─────────────────────────────────────────

def _is_stateful_prompt(auto: Automation) -> bool:
    if auto.input_type == "monitor":
        return True
    return auto.input_type == "prompt" and bool(auto.stateful)


async def _has_inflight_sibling(automation_id: str, run_id: str) -> bool:
    """True if another run of the same automation is currently executing.

    Stateful runs share one LangGraph thread, so overlapping runs would write
    the same checkpoint concurrently. Checked against the Job table rather
    than _tasks: a manual trigger pre-registers its TaskState while the job is
    still pending, and a merely-pending sibling must not skip the running one —
    job status flips to 'running' only once a worker claims it."""
    from sqlalchemy import select
    from db.models import Job as JobRow

    async with async_session() as session:
        rows = (await session.execute(
            select(JobRow.payload).where(
                JobRow.kind == "automation",
                JobRow.status == "running",
                JobRow.id != run_id,
            )
        )).scalars().all()
    for payload in rows:
        try:
            if json.loads(payload).get("automation_id") == automation_id:
                return True
        except (json.JSONDecodeError, AttributeError):
            continue
    return False


async def _persist_stateful_message(
    conv_id: str, role: str, content: str, status: str = "done",
) -> None:
    async with async_session() as session:
        await add_message(session, conv_id, role, content, status=status)


async def _run_automation_inner(
    auto: Automation,
    state: TaskState,
    run_id: str,
    invocation_context=None,
) -> None:
    """Execute the work for a single automation run: dispatch by input_type,
    write events to `state`, persist outcome to AutomationRun, send notifications,
    set state.done and schedule _tasks cleanup.

    The caller is responsible for creating the AutomationRun row and registering
    `state` in `_tasks` before invoking this.
    """
    final_status = "error"
    conv_id: str | None = None
    try:
        status = "done"

        if _is_stateful_prompt(auto):
            if await _has_inflight_sibling(auto.id, run_id):
                skip_msg = "skipped: a previous run of this stateful automation is still in flight"
                async with async_session() as session:
                    await finish_automation_run(session, run_id, "skipped", None, skip_msg)
                emit_event(state, "error", error=skip_msg, run_id=run_id)
                final_status = "skipped"
                return
            conv_id = automation_conversation_id(auto.id)
            async with async_session() as session:
                await get_or_create_conversation(
                    session, conv_id, auto.model or DEFAULT_MODEL, auto.name,
                    surface="automation",
                )
                await add_message(session, conv_id, "user", auto.prompt_text or "")

        if auto.input_type in ("prompt", "monitor"):
            thread_id = conv_id or f"automation_{run_id}"
            output = await _execute_prompt_type(auto, state, get_async_checkpointer(), thread_id, invocation_context=invocation_context)
        elif auto.input_type == "code":
            output = await _execute_code_type(auto, state)
        elif auto.input_type == "webhook":
            output = await _execute_webhook_type(auto, state)
        else:
            raise ValueError(f"Unknown input_type: {auto.input_type}")

        if state.budget_exceeded:
            reason = state.budget_reason or "budget exceeded"
            async with async_session() as session:
                await finish_automation_run(session, run_id, "error", output, f"budget exceeded: {reason}")
            if conv_id:
                await _persist_stateful_message(conv_id, "assistant", output or f"[budget exceeded: {reason}]", "error")
            final_status = "error"
            emit_event(state, "budget_exceeded", reason=reason, run_id=run_id)
            emit_event(state, "error", error=f"budget exceeded: {reason}", run_id=run_id)
        elif state.cancelled:
            async with async_session() as session:
                await finish_automation_run(session, run_id, "stopped", output, None)
            if conv_id:
                await _persist_stateful_message(conv_id, "assistant", output or "", "stopped")
            final_status = "stopped"
            emit_event(state, "stopped", output=output, run_id=run_id)
        else:
            if auto.input_type == "monitor" and _monitor_reported_no_change(output):
                status = "no_change"
            async with async_session() as session:
                await finish_automation_run(session, run_id, status, output, None)
                # Delta gate: an unchanged monitor stays silent.
                if status != "no_change":
                    await send_notifications(
                        session, auto.notifications,
                        status="done",
                        title=auto.name,
                        body=output or "",
                    )
            if conv_id:
                # "no_change" is a run status, not a chat message status.
                await _persist_stateful_message(conv_id, "assistant", output or "", "done")
            final_status = status
            emit_event(state, "done", output=output, run_id=run_id)

    except asyncio.CancelledError:
        if state.budget_exceeded:
            reason = state.budget_reason or "budget exceeded"
            async with async_session() as session:
                await finish_automation_run(session, run_id, "error", None, f"budget exceeded: {reason}")
            if conv_id:
                await _persist_stateful_message(conv_id, "assistant", f"[budget exceeded: {reason}]", "error")
            final_status = "error"
            emit_event(state, "budget_exceeded", reason=reason, run_id=run_id)
            emit_event(state, "error", error=f"budget exceeded: {reason}", run_id=run_id)
        else:
            async with async_session() as session:
                await finish_automation_run(session, run_id, "stopped", None, None)
            if conv_id:
                await _persist_stateful_message(conv_id, "assistant", "", "stopped")
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
        if conv_id:
            await _persist_stateful_message(conv_id, "assistant", err_text, "error")
        emit_event(state, "error", error=err_text)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        if invocation_context is not None:
            try:
                await invocation_context.persist_state_deltas()
            except Exception:
                pass
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
    invocation_context = None
    try:
        from core.runner import get_runner_or_none
        _r = get_runner_or_none()
        if _r is not None:
            invocation_context = _r.new_invocation_context(
                session_id=automation_id,
                kind="automation",
                initial_state={"automation_id": automation_id},
            )
            invocation_context.invocation_id = run_id
    except Exception:
        invocation_context = None

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
        await _run_automation_inner(auto, state, run_id, invocation_context)
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
