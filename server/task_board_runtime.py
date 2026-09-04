"""Task board runtime — dispatcher + board_task job handler."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from core.agents import build_agent
from core.invocation_context import InvocationContext
from core.queue import Job
from core.run_scaffold import (
    finish_task_state,
    get_or_create_task_state,
    new_invocation_context,
    queue_cancel_watch,
    start_run_callbacks,
)
from db import async_session
from db.models import BoardTask
from db.models import Job as JobRow
from db.ops import (
    add_message,
    board_task_conversation_id,
    get_board_task,
    update_board_task,
    get_board_task_parents,
    get_or_create_conversation,
    promote_ready_board_tasks,
    resolve_model,
)
from core.state import (
    TaskState,
    _notify,
    _tasks,
    emit_event,
    get_async_checkpointer,
    get_queue,
    get_store,
    log_task_created,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig

from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _process_chunk

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

            # Skip ready tasks whose previous run's job is still alive: a task
            # blocked/completed by its tool call and immediately re-readied
            # (answer, unblock, re-run) must not get a second concurrent run
            # while the first agent loop is still wrapping up — both would
            # write one thread, and the older run could clobber the newer row.
            live_jobs = select(JobRow.id).where(JobRow.status.in_(["pending", "running"]))
            ready = (await session.execute(
                select(BoardTask)
                .where(
                    BoardTask.status == "ready",
                    (BoardTask.job_id.is_(None)) | (BoardTask.job_id.not_in(live_jobs)),
                )
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
string). If you cannot finish, call block_task(reason=...) instead — pass
needs_input=True when a human answer would unblock you; the answer is delivered
when the task resumes. If you call neither, your final reply is recorded as the
summary."""

# Resumed run after a needs_input block: the thread already holds the original
# task and the agent's question, so the prompt only carries the answer.
_RESUME_PROMPT = """\
You previously blocked the board task "{title}" with a question. The user has \
answered:

{answer}

Continue the task using this answer. When you have finished, call \
complete_task(summary=...); if you are still blocked, call block_task(reason=...)."""


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

async def _run_agent(
    task: BoardTask, state: TaskState, conv_id: str, prompt: str, model: str,
    invocation_context: InvocationContext | None = None,
) -> str:
    accumulated: list[str] = []
    coalescer = TokenCoalescer(state)
    # Declared as the base type: `list` is invariant, so the concrete handler
    # list is not assignable to RunnableConfig's `list[BaseCallbackHandler]`
    # without this.
    callbacks: list[BaseCallbackHandler] = start_run_callbacks(state, "board_task").handlers

    if invocation_context is not None and invocation_context.store is not None:
        cp = invocation_context.checkpointer
        st = invocation_context.store
    else:
        cp = get_async_checkpointer()
        st = get_store()
    agent = build_agent(
        model,
        checkpointer=cp,
        store=st,
        invocation_context=invocation_context,
        board=True,
    )
    # Annotated rather than inlined: an inline dict literal infers a
    # heterogeneous value type that doesn't match the RunnableConfig TypedDict,
    # which is what made this astream call fail overload resolution.
    run_config: RunnableConfig = {
        "configurable": {
            "thread_id": conv_id,
            "conversation_id": conv_id,
            "board_task_id": task.id,
        },
        "recursion_limit": 100,
        "callbacks": callbacks,
    }
    async for raw_chunk in agent.astream(
        {"messages": [{"role": "user", "content": prompt}]},
        config=run_config,
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
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    blocked_reason: str | None = None,
    blocked_kind: str | None = None,
    bump_failures: bool = False,
) -> None:
    """Terminal update, respecting state the run no longer owns:
    - a newer run claimed the task (job_id != run_id) → touch nothing;
    - a human re-queued it mid-run (status ready/todo) → touch nothing;
    - the agent already set a terminal status via complete_task/block_task →
      keep it, only stamp finished_at;
    - otherwise (still 'running') apply this run's outcome."""
    async with async_session() as session:
        task = await session.get(BoardTask, task_id)
        if task is None or task.job_id != run_id or task.status in ("ready", "todo"):
            return
        now = datetime.now(timezone.utc)
        if task.status == "running":
            task.status = status
            if summary is not None:
                task.summary = summary
            task.blocked_reason = blocked_reason
            task.blocked_kind = blocked_kind
            if bump_failures:
                task.failure_count += 1
        task.finished_at = now
        task.updated_at = now
        await session.commit()


async def _run_board_task_inner(
    task: BoardTask, state: TaskState, run_id: str,
    pending_answer: str | None = None,
    invocation_context=None,
) -> None:
    final_status = "error"
    conv_id = board_task_conversation_id(task.id)
    try:
        async with async_session() as session:
            model = await resolve_model(task.model, session)
            if invocation_context is not None:
                invocation_context.run_config.model = model
                invocation_context.session_id = conv_id or task.id
            parents = await get_board_task_parents(session, task.id)
            await get_or_create_conversation(
                session, conv_id, model, task.title, surface="task",
            )
        if pending_answer:
            prompt = _RESUME_PROMPT.format(title=task.title, answer=pending_answer)
        else:
            prompt = _compose_task_prompt(task, parents)

        async with async_session() as session:
            await add_message(session, conv_id, "user", prompt)

        output = await _run_agent(task, state, conv_id, prompt, model, invocation_context=invocation_context)
        if invocation_context is not None:
            try:
                await invocation_context.persist_state_deltas()
            except Exception:
                pass

        if state.budget_exceeded:
            reason = state.budget_reason or "budget exceeded"
            await _finish_task(
                task.id, run_id, status="blocked", summary=output or None,
                blocked_reason=f"budget exceeded: {reason}", blocked_kind="budget",
            )
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", output or f"[budget exceeded: {reason}]", status="blocked")
            final_status = "blocked"
            emit_event(state, "budget_exceeded", reason=reason, run_id=run_id)
            emit_event(state, "stopped", output=output, run_id=run_id)
            return

        if state.cancelled:
            await _finish_task(
                task.id, run_id, status="blocked", summary=output or None,
                blocked_reason="stopped by user", blocked_kind="stopped",
            )
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", output or "", status="stopped")
            final_status = "stopped"
            emit_event(state, "stopped", output=output, run_id=run_id)
            return

        # The agent may have already set a terminal status via
        # complete_task/block_task; _finish_task only overwrites 'running'.
        await _finish_task(task.id, run_id, status="done", summary=output)
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
            task.id, run_id, status="blocked",
            blocked_reason="interrupted by shutdown", blocked_kind="stopped",
        )
        final_status = "stopped"
        emit_event(state, "stopped", run_id=run_id)

    except BaseException as exc:
        err_text = str(exc)
        await _finish_task(
            task.id, run_id, status="blocked", blocked_reason=f"error: {err_text}",
            blocked_kind="error", bump_failures=True,
        )
        with contextlib.suppress(Exception):
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", err_text, status="error")
                # The answer was consumed at claim time; put it back so a
                # retry after a transient failure still resumes with it.
                if pending_answer:
                    t = await session.get(BoardTask, task.id)
                    if t is not None and t.job_id == run_id and not t.pending_answer:
                        t.pending_answer = pending_answer
                        await session.commit()
        emit_event(state, "error", error=err_text)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        finish_task_state(run_id, state, final_status)


# ── Auto-decompose ───────────────────────────────────────────────────────────
#
# A planner LLM breaks one standalone task into 2–MAX_SUBTASKS subtasks with
# dependencies among themselves; every subtask becomes a PARENT of the original
# task, which is parked in todo — so the original runs last as the synthesis
# step, receiving each subtask's summary as handoff context.

MAX_SUBTASKS = 8

_DECOMPOSE_SYSTEM = (
    "You are a planner for a multi-agent task board. Respond ONLY with a JSON "
    "object — no prose, no code fences."
)

_DECOMPOSE_PROMPT = """\
Break the following task into 2-{max} smaller subtasks that together accomplish it.

# Task: {title}
{body}

Rules:
- Each subtask needs a short imperative "title" and a self-contained "body" an \
agent can execute without seeing the other subtasks (dependency results are \
handed to it automatically).
- "depends_on" lists the 0-based indexes of other subtasks whose output this \
one needs; it may only reference EARLIER subtasks (smaller index). Prefer no \
dependencies so subtasks run in parallel.
- Do NOT add a final "combine the results" subtask — the original task runs \
last automatically with every subtask's summary as context.

JSON shape: {{"subtasks": [{{"title": "...", "body": "...", "depends_on": []}}]}}"""


def _parse_decomposition(raw: object) -> list[dict]:
    """LLM content → validated subtask specs. Raises ValueError on garbage."""
    if isinstance(raw, list):  # reasoning models return a list of blocks
        raw = " ".join(
            b.get("text", "") for b in raw
            if isinstance(b, dict) and b.get("type") == "text"
        )
    text = str(raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("decomposer returned no JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"decomposer returned invalid JSON: {exc}")
    subtasks = data.get("subtasks")
    if not isinstance(subtasks, list) or not (2 <= len(subtasks) <= MAX_SUBTASKS):
        raise ValueError(
            f"decomposer must return 2-{MAX_SUBTASKS} subtasks "
            f"(got {len(subtasks) if isinstance(subtasks, list) else 'none'})"
        )
    specs: list[dict] = []
    for i, s in enumerate(subtasks):
        title = str(s.get("title") or "").strip()
        body = str(s.get("body") or "").strip()
        if not title or not body:
            raise ValueError(f"subtask {i} is missing a title or body")
        deps = s.get("depends_on") or []
        if not isinstance(deps, list) or any(
            not isinstance(d, int) or d < 0 or d >= i for d in deps
        ):
            raise ValueError(
                f"subtask {i} has invalid depends_on (must be earlier indexes)"
            )
        specs.append({"title": title, "body": body, "depends_on": sorted(set(deps))})
    return specs


async def decompose_board_task(task_id: str) -> list[BoardTask]:
    """Split a standalone waiting task into subtasks (see module note above).
    Returns the created subtasks. Raises ValueError on bad state or a
    decomposer output that can't be parsed — the task is left untouched then."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from core.model_catalog import resolve_model_spec
    from db.models import BoardTaskLink
    from db.ops import create_board_task

    async with async_session() as session:
        task = await get_board_task(session, task_id)
        if task is None:
            raise ValueError("task not found")
        if task.status not in ("todo", "ready", "blocked"):
            raise ValueError("only waiting (todo/ready/blocked) tasks can be decomposed")
        if await get_board_task_parents(session, task_id):
            raise ValueError("task already has dependencies — decompose only standalone tasks")
        model = await resolve_model(task.model, session)

    llm = resolve_model_spec(model).build_llm()
    response = await llm.ainvoke([
        SystemMessage(content=_DECOMPOSE_SYSTEM),
        HumanMessage(content=_DECOMPOSE_PROMPT.format(
            max=MAX_SUBTASKS, title=task.title, body=task.body or "",
        )),
    ])
    specs = _parse_decomposition(response.content)

    created: list[BoardTask] = []
    async with async_session() as session:
        # Park the original FIRST — it must not get dispatched by an interval
        # tick while its gating subtasks are still being created below.
        original = await session.get(BoardTask, task_id)
        if original is None:
            raise ValueError("task not found")
        original.status = "todo"
        original.blocked_reason = None
        original.blocked_kind = None
        original.finished_at = None
        original.updated_at = datetime.now(timezone.utc)
        await session.commit()

        for spec in specs:
            sub = await create_board_task(
                session,
                title=spec["title"],
                body=spec["body"],
                status="ready",  # forced to todo by create when it has parents
                priority=task.priority,
                created_by="agent",
                model=task.model,
                parent_ids=[created[d].id for d in spec["depends_on"]],
            )
            created.append(sub)
        # Every subtask gates the original: it runs last as the synthesis step.
        for sub in created:
            session.add(BoardTaskLink(id=str(uuid4()), parent_id=sub.id, child_id=task_id))
        await session.commit()

    logger.info("board decompose: task %s split into %d subtasks", task_id, len(created))
    await dispatch_board_tasks()
    return created


# ── Queue handler ────────────────────────────────────────────────────────────

async def board_task_job_handler(job: Job) -> None:
    """Consume a 'board_task' job. Payload: {"task_id": str}. job.id is the
    run id (== BoardTask.job_id at dispatch time)."""
    task_id: str = job.payload["task_id"]
    run_id = job.id
    invocation_context = await new_invocation_context(
        kind="board_task",
        session_id=task_id,
        invocation_id=run_id,
        initial_state={"board_task_id": task_id},
    )

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
        # Consume a needs_input answer at claim time; the value rides along in
        # the local variable (and lands in the thread as a user message).
        pending_answer = task.pending_answer
        task.pending_answer = None
        await session.commit()

    state = get_or_create_task_state(
        run_id, kind="board_task", label=task.title, parent_id=task.id,
        model=task.model,
    )

    async with queue_cancel_watch(run_id, state):
        await _run_board_task_inner(
            task, state, run_id, pending_answer,
            invocation_context=invocation_context,
        )


# ── Stop (shared by GraphQL stopBoardTask) ───────────────────────────────────

async def answer_board_task(session, task_id: str, answer: str):
    """Deliver a human answer to a blocked task and re-queue it.

    Shared by the board card and the approvals inbox — two entry points for the
    same act, and the preconditions (non-empty answer, task still blocked) must
    not drift between them. The answer is consumed at claim time by the next
    dispatch, which runs `_RESUME_PROMPT` on the same conversation, so the
    agent still has the context of what it asked.
    """
    from db.ops import close_open_approvals

    if not answer.strip():
        raise ValueError("answer must not be empty")
    task = await get_board_task(session, task_id)
    if task is None:
        raise ValueError("task not found")
    if task.status != "blocked":
        raise ValueError("only blocked tasks can be answered")
    # Close the inbox row first. `update_board_task` closes anything still open
    # as `cancelled` (the task is no longer waiting), so doing this afterwards
    # would record a genuine answer as a cancellation. Validation above has
    # already run, so there is nothing left that can reject the update.
    await close_open_approvals(
        session, board_task_id=task_id,
        status="answered", answer=answer.strip(), result="Task resumed.",
    )
    task = await update_board_task(
        session, task_id,
        status="ready",
        pending_answer=answer.strip(),
        blocked_reason=None,
        blocked_kind=None,
        finished_at=None,
    )
    assert task is not None
    await _kick_dispatch_from_runtime()
    return task


async def _kick_dispatch_from_runtime() -> None:
    """Fire a dispatch pass without waiting for the 15s tick."""
    try:
        await dispatch_board_tasks()
    except Exception:
        logger.exception("board dispatch after answer failed")


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
                task.blocked_kind = "stopped"
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
