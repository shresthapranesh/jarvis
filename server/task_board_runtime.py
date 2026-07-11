"""Task board runtime — dispatcher + board_task job handler.

The board (db.models.BoardTask) is a durable kanban layer on top of the job
queue: tasks move todo → ready → running → done/blocked. `dispatch_board_tasks`
is the single scheduling entrypoint — the APScheduler interval job ticks it,
and mutations / agent tools call it directly after creating or readying a task
so dispatch doesn't wait for the next tick. Each dispatch enqueues one
"board_task" job (job.id == BoardTask.job_id, a fresh UUID per run so re-runs
don't collide with finished job rows); `board_task_job_handler` consumes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from core.agents import DEFAULT_MODEL, build_agent
from core.log_callback import AgentLogger
from core.queue import Job
from core.safety import gate_input, gate_output
from db import async_session
from db.models import BoardTask
from db.models import Job as JobRow
from db.ops import (
    add_message,
    board_task_conversation_id,
    get_board_task,
    get_board_task_parents,
    get_or_create_conversation,
    promote_ready_board_tasks,
)
from core.state import (
    TaskState,
    _notify,
    _tasks,
    emit_event,
    get_async_checkpointer,
    get_queue,
    get_store,
    log_task_complete,
    log_task_created,
)
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _process_chunk
from server.automation_runtime import _watch_queue_cancel

logger = logging.getLogger(__name__)

# Board-wide cap on tasks in flight (pending or running board_task jobs).
# Bounds SQLite writer contention the same way the worker concurrency caps do.
MAX_IN_PROGRESS = 3

_dispatch_lock = asyncio.Lock()


# ── Dispatcher ───────────────────────────────────────────────────────────────

async def dispatch_board_tasks() -> int:
    """One dispatch pass: promote todo→ready, enqueue ready tasks up to the cap.

    Returns the number of tasks dispatched. Serialized by a lock so an
    interval tick and a mutation-triggered kick can't double-claim a task.
    """
    from sqlalchemy import func, select

    async with _dispatch_lock:
        async with async_session() as session:
            promoted = await promote_ready_board_tasks(session)

            inflight = (await session.execute(
                select(func.count()).select_from(JobRow).where(
                    JobRow.kind == "board_task",
                    JobRow.status.in_(["pending", "running"]),
                )
            )).scalar_one()
            capacity = MAX_IN_PROGRESS - inflight
            if capacity <= 0:
                if promoted:
                    await session.commit()
                return 0

            ready = (await session.execute(
                select(BoardTask)
                .where(BoardTask.status == "ready")
                .order_by(BoardTask.priority.desc(), BoardTask.created_at.asc())
                .limit(capacity)
            )).scalars().all()

            queue = get_queue()
            for task in ready:
                run_id = str(uuid4())
                task.status = "running"
                task.job_id = run_id
                task.updated_at = datetime.now(timezone.utc)
                # Pre-register the TaskState before commit so a subscriber
                # that sees job_id can't race the worker (same pattern as
                # register_automation_run).
                _tasks[run_id] = TaskState(
                    kind="board_task", label=task.title, parent_id=task.id,
                )
                log_task_created(run_id, _tasks[run_id], task.model)
                await queue.enqueue(
                    "board_task", {"task_id": task.id}, job_id=run_id, session=session,
                )
            await session.commit()
            if ready:
                logger.info("board dispatch: %d task(s) enqueued", len(ready))
            return len(ready)


# ── Task prompt composition ──────────────────────────────────────────────────

_TASK_PROMPT = """\
You are executing a task from the shared task board.

# Task: {title}
{body}
{skill_part}{handoff_part}
When you have finished the task, call complete_task(summary=...) with a concise
handoff summary for downstream tasks (optionally metadata as a JSON object
string). If you cannot finish, call block_task(reason=...) instead. If you call
neither, your final reply is recorded as the summary."""


def _compose_task_prompt(task: BoardTask, parents: list[BoardTask]) -> str:
    skill_part = (
        f"\nFirst call use_skill('{task.skill}') and follow its instructions.\n"
        if task.skill else ""
    )
    handoff_part = ""
    done_parents = [p for p in parents if p.status == "done"]
    if done_parents:
        sections = []
        for p in done_parents:
            block = f"### {p.title}\n{p.summary or '(no summary)'}"
            if p.result_metadata:
                block += f"\nMetadata: {p.result_metadata}"
            sections.append(block)
        handoff_part = (
            "\n## Handoffs from completed upstream tasks\n\n"
            + "\n\n".join(sections) + "\n"
        )
    return _TASK_PROMPT.format(
        title=task.title,
        body=task.body or "",
        skill_part=skill_part,
        handoff_part=handoff_part,
    )


# ── Execution ────────────────────────────────────────────────────────────────

async def _run_agent(task: BoardTask, state: TaskState, conv_id: str, prompt: str) -> str:
    accumulated: list[str] = []
    coalescer = TokenCoalescer(state)
    agent = build_agent(
        task.model or DEFAULT_MODEL,
        checkpointer=get_async_checkpointer(),
        store=get_store(),
    )
    async for raw_chunk in agent.astream(
        {"messages": [{"role": "user", "content": prompt}]},
        config={
            "configurable": {
                "thread_id": conv_id,
                "conversation_id": conv_id,
                "board_task_id": task.id,
            },
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


async def _finish_task(
    task_id: str,
    *,
    status: str,
    summary: str | None = None,
    blocked_reason: str | None = None,
    bump_failures: bool = False,
) -> None:
    """Terminal update, respecting a status the agent already set via
    complete_task/block_task — the handler only overwrites 'running'."""
    async with async_session() as session:
        task = await session.get(BoardTask, task_id)
        if task is None:
            return
        now = datetime.now(timezone.utc)
        if task.status == "running":
            task.status = status
            if summary is not None:
                task.summary = summary
            task.blocked_reason = blocked_reason
            if bump_failures:
                task.failure_count += 1
        task.finished_at = now
        task.updated_at = now
        await session.commit()


async def _run_board_task_inner(task: BoardTask, state: TaskState, run_id: str) -> None:
    final_status = "error"
    conv_id = board_task_conversation_id(task.id)
    model = task.model or DEFAULT_MODEL
    try:
        async with async_session() as session:
            parents = await get_board_task_parents(session, task.id)
            await get_or_create_conversation(
                session, conv_id, model, task.title, surface="task",
            )
        prompt = _compose_task_prompt(task, parents)

        rejection = await gate_input(f"{task.title}\n{task.body or ''}", model)
        if rejection:
            emit_event(state, "safety_input_blocked", message=rejection, run_id=run_id)
            await _finish_task(
                task.id, status="blocked", blocked_reason=f"safety: {rejection}",
            )
            final_status = "blocked"
            return

        async with async_session() as session:
            await add_message(session, conv_id, "user", prompt)

        raw_output = await _run_agent(task, state, conv_id, prompt)
        output, output_verdict = await gate_output(raw_output, model)
        if output_verdict:
            emit_event(
                state, "safety_output_blocked",
                severity=output_verdict.severity,
                reason=output_verdict.reason,
                redacted_output=output,
                run_id=run_id,
            )

        if state.cancelled:
            await _finish_task(
                task.id, status="blocked", summary=output or None,
                blocked_reason="stopped by user",
            )
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", output or "", status="stopped")
            final_status = "stopped"
            emit_event(state, "stopped", output=output, run_id=run_id)
            return

        if output_verdict:
            await _finish_task(
                task.id, status="blocked", summary=output,
                blocked_reason=f"safety: {output_verdict.reason}",
            )
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", output or "", status="blocked")
            final_status = "blocked"
            return

        # The agent may have already set a terminal status via
        # complete_task/block_task; _finish_task only overwrites 'running'.
        await _finish_task(task.id, status="done", summary=output)
        async with async_session() as session:
            await add_message(session, conv_id, "assistant", output or "", status="done")
            refreshed = await get_board_task(session, task.id)
        final_status = refreshed.status if refreshed else "done"
        emit_event(state, "done", output=output, run_id=run_id)

        # A completed parent may unblock children — dispatch them now rather
        # than waiting for the next interval tick.
        if final_status == "done":
            await dispatch_board_tasks()

    except asyncio.CancelledError:
        await _finish_task(
            task.id, status="blocked", blocked_reason="interrupted by shutdown",
        )
        final_status = "stopped"
        emit_event(state, "stopped", run_id=run_id)

    except BaseException as exc:
        err_text = str(exc)
        await _finish_task(
            task.id, status="blocked",
            blocked_reason=f"error: {err_text}", bump_failures=True,
        )
        with contextlib.suppress(Exception):
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", err_text, status="error")
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

async def board_task_job_handler(job: Job) -> None:
    """Consume a 'board_task' job. Payload: {"task_id": str}. job.id is the
    run id (== BoardTask.job_id at dispatch time)."""
    task_id: str = job.payload["task_id"]
    run_id = job.id

    async with async_session() as session:
        task = await get_board_task(session, task_id)
        if task is None or task.status in ("done", "archived"):
            return
        # Post-restart resume: re-assert the claim the dispatcher made.
        now = datetime.now(timezone.utc)
        task.status = "running"
        task.job_id = run_id
        task.started_at = now
        task.updated_at = now
        await session.commit()

    state = _tasks.get(run_id)
    if state is None:
        state = TaskState(kind="board_task", label=task.title, parent_id=task.id)
        _tasks[run_id] = state
        log_task_created(run_id, state, task.model)

    queue = get_queue()
    cancel_watcher = asyncio.create_task(_watch_queue_cancel(queue, job.id, state))
    try:
        await _run_board_task_inner(task, state, run_id)
    finally:
        cancel_watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_watcher


# ── Stop (shared by GraphQL stopBoardTask) ───────────────────────────────────

async def stop_board_task(task_id: str) -> bool:
    """Cancel the current run of a board task. Returns False when the task
    isn't running. In-process fast path + durable queue cancel, mirroring
    stopAutomationRun."""
    async with async_session() as session:
        task = await get_board_task(session, task_id)
        if task is None or task.status != "running" or not task.job_id:
            return False
        run_id = task.job_id
    state = _tasks.get(run_id)
    if state is not None:
        state.cancelled = True
        state._stop_event.set()
    await get_queue().cancel(run_id)

    # If the job was still pending, cancel() finished it without any handler
    # ever running — flip the row here or the task stays "running" forever.
    from sqlalchemy import select
    async with async_session() as session:
        job = (await session.execute(
            select(JobRow).where(JobRow.id == run_id)
        )).scalars().first()
        if job is not None and job.status == "cancelled":
            task = await session.get(BoardTask, task_id)
            if task is not None and task.status == "running":
                now = datetime.now(timezone.utc)
                task.status = "blocked"
                task.blocked_reason = "stopped by user"
                task.finished_at = now
                task.updated_at = now
                await session.commit()
            if state is not None and not state.done:
                emit_event(state, "stopped", run_id=run_id)
                state.done = True
                _notify(state)
                asyncio.get_running_loop().call_later(
                    5.0, lambda rid=run_id: _tasks.pop(rid, None),
                )
    return True
