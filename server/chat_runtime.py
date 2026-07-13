"""Chat runtime — agent task execution + task registration.

Both GraphQL ``startTask`` and the Telegram/Discord bot dispatchers enqueue
a queue job; the ``chat_job_handler`` here consumes them. Convention:
``job.id == Message.id`` (the assistant Message UUID), so cancellation
through ``stopRunningTask`` is a one-line ``queue.cancel(task_id)``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from core.agents import build_agent
from core.config import get_config
from core.log_callback import AgentLogger, UsageAccumulator
from core.queue import Job, JobQueue
from core.safety import gate_input, gate_output
from core.schemas import AttachmentIn
from core.state import (
    TaskState,
    _notify,
    _tasks,
    emit_event,
    get_async_checkpointer,
    get_store,
    log_task_complete,
    log_task_created,
    log_task_received,
)
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _build_message_content, _finalize_message, _process_chunk

from db import async_session
from db.models import Conversation
from db.ops import (
    add_message,
    create_document,
    get_or_create_conversation,
    get_project,
)


# ── Agent task runner ────────────────────────────────────────────────────────

async def _run_agent_task(
    task_id: str, query: str, model: str, conv_id: str,
    attachments: list | None = None,
) -> None:
    state = _tasks[task_id]

    accumulated: list[str] = []
    step_seq_ref = [0]
    coalescer = TokenCoalescer(state)
    usage = UsageAccumulator()
    status = "error"

    async def finalize(content: str, final_status: str) -> None:
        await _finalize_message(
            task_id, content, final_status,
            input_tokens=usage.input_tokens if usage.has_usage else None,
            output_tokens=usage.output_tokens if usage.has_usage else None,
        )

    try:
        # Input gate — judge the user's prompt before spinning up the agent.
        # Surface a step immediately so the activity sidebar shows feedback
        # while the judge runs (a cold Bedrock connection can take ~60s).
        emit_event(state, "step", node="safety", source="main", data="Reviewing input")
        rejection = await gate_input(query, model)
        if rejection:
            await finalize(rejection, "blocked")
            status = "blocked"
            emit_event(state, "safety_input_blocked", message=rejection, conversation_id=conv_id)
            emit_event(state, "done", message=rejection, conversation_id=conv_id)
            return

        content = await _build_message_content(query, attachments, model)

        agent = build_agent(model, checkpointer=get_async_checkpointer(), store=get_store())
        # Project membership is resolved fresh at run start (not baked into the
        # job payload) so bots and post-restart resumes need no payload changes
        # and add/remove-from-project applies on the next run.
        project_id: str | None = None
        async with async_session() as session:
            conv_row = await session.get(Conversation, conv_id)
            if conv_row is not None:
                project_id = conv_row.project_id
        configurable: dict[str, Any] = {"thread_id": conv_id, "conversation_id": conv_id}
        if project_id:
            configurable["project_id"] = project_id
        config = {
            "configurable": configurable,
            "recursion_limit": 100,
            "callbacks": [AgentLogger(), usage],
        }
        # Reset the per-conversation plan at the start of each new turn. Todos
        # live in the checkpointer keyed by thread_id, so without this a plan
        # written on an earlier turn lingers — often frozen at 0/N when that run
        # errored before advancing it — and renders on every later message and
        # in the activity sidebar. Passing todos=[] overwrites the LastValue
        # channel (correctness on reload); the explicit event clears live
        # subscribers immediately, since a channel write alone dispatches none.
        stream_input: Any = {"messages": [{"role": "user", "content": content}], "todos": []}
        emit_event(state, "todos_updated", todos=[], source="main")

        while True:
            interrupted = False
            async for raw_chunk in agent.astream(  # type: ignore[call-overload]
                stream_input,
                config=config,
                stream_mode=STREAM_MODES,
                subgraphs=True,
            ):
                chunk: StreamChunk = raw_chunk  # type: ignore[assignment]
                if state.cancelled:
                    break
                interrupted = await _process_chunk(
                    chunk, state, coalescer, accumulated,
                    task_id=task_id, conv_id=conv_id,
                    step_seq_ref=step_seq_ref, persist_steps=True,
                )
                if interrupted:
                    break

            if not interrupted or state.cancelled:
                break

            state.resume_future = asyncio.get_running_loop().create_future()
            try:
                answer = await state.resume_future
            except asyncio.CancelledError:
                state.cancelled = True
                break
            finally:
                state.resume_future = None
                state.pending_interrupt_id = None

            stream_input = Command(resume=answer)

        coalescer.flush_all()
        final_message = "".join(accumulated)
        if state.cancelled:
            # Cancelled mid-run — partial output is non-final; skip the gate.
            await finalize(final_message, "stopped")
            status = "stopped"
            emit_event(state, "stopped", message=final_message, conversation_id=conv_id)
        else:
            persisted, output_verdict = await gate_output(final_message, model)
            status = "blocked" if output_verdict else "done"
            await finalize(persisted, status)
            if output_verdict:
                emit_event(
                    state, "safety_output_blocked",
                    severity=output_verdict.severity,
                    reason=output_verdict.reason,
                    redacted_message=persisted,
                    conversation_id=conv_id,
                )
            emit_event(state, "done", message=persisted, conversation_id=conv_id)

    except asyncio.CancelledError:
        coalescer.flush_all()
        final_message = "".join(accumulated)
        await finalize(final_message, "stopped")
        status = "stopped"
        emit_event(state, "stopped", message=final_message, conversation_id=conv_id)

    except GraphRecursionError:
        coalescer.flush_all()
        final_message = "".join(accumulated) or "(agent reached iteration limit)"
        persisted, output_verdict = await gate_output(final_message, model)
        status = "blocked" if output_verdict else "done"
        await finalize(persisted, status)
        if output_verdict:
            emit_event(
                state, "safety_output_blocked",
                severity=output_verdict.severity,
                reason=output_verdict.reason,
                redacted_message=persisted,
                conversation_id=conv_id,
            )
        emit_event(state, "done", message=persisted, conversation_id=conv_id)

    except BaseException as exc:
        coalescer.flush_all()
        # Persist whatever streamed before the crash; otherwise surface the cause
        # so the user sees why the run stopped instead of an empty assistant
        # bubble. The other terminal branches all call _finalize_message; this
        # one previously only flipped status, leaving content="" on disk.
        final_message = "".join(accumulated) or f"The run failed before completing: {exc}"
        emit_event(state, "error", error=str(exc))
        try:
            await finalize(final_message, "error")
        except Exception:
            pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise

    finally:
        if state.resume_future and not state.resume_future.done():
            state.resume_future.cancel()
        log_task_complete(task_id, state, status)
        state.done = True
        _notify(state)
        # Delay popping the task so the frontend UI can show the "done" or
        # "stopped" state for a few seconds before the row vanishes.
        loop = asyncio.get_running_loop()
        loop.call_later(5.0, lambda tid=task_id: _tasks.pop(tid, None))


# ── Queue handler ────────────────────────────────────────────────────────────

async def chat_job_handler(job: Job) -> None:
    """JobQueue handler — chat jobs that have been claimed flow through here.
    Convention: ``job.id == Message.id`` (the assistant placeholder Message).

    Payload: ``{"query": str, "model": str, "conv_id": str,
                "attachments": list[dict] | None}``.

    On restart the LangGraph checkpointer (keyed by ``conv_id`` thread_id)
    resumes the agent from the last persisted node boundary, so a job that
    crashed mid-run picks up roughly where it left off rather than restarting
    from the user's original prompt.
    """
    payload = job.payload
    task_id = job.id
    query: str = payload["query"]
    model: str = payload["model"]
    conv_id: str = payload["conv_id"]
    raw_atts = payload.get("attachments") or []
    attachments = [AttachmentIn.model_validate(a) for a in raw_atts] if raw_atts else None

    # Re-create the TaskState if the worker is picking up a job whose original
    # trigger is gone (post-restart resume path). Otherwise reuse the entry the
    # trigger pre-created so SSE subscribers connected before the worker
    # claimed see the live stream.
    state = _tasks.get(task_id)
    if state is None:
        async with async_session() as session:
            from db.models import Conversation
            conv = await session.get(Conversation, conv_id)
        label = (conv.title if conv and conv.title else query[:60])
        state = TaskState(kind="chat", label=label, parent_id=conv_id)
        _tasks[task_id] = state
        log_task_created(task_id, state, model)

    from core.state import get_queue
    queue = get_queue()
    cancel_watcher = asyncio.create_task(_watch_queue_cancel(queue, task_id, state))
    try:
        await _run_agent_task(task_id, query, model, conv_id, attachments)
    finally:
        cancel_watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_watcher


async def _watch_queue_cancel(
    queue: JobQueue, job_id: str, state: TaskState,
) -> None:
    """Poll the queue for cancel; mirror into TaskState.cancelled / _stop_event /
    resume_future so the existing in-runtime observers see it."""
    while not state.done and not state.cancelled:
        if await queue.is_cancel_requested(job_id):
            state.cancelled = True
            state._stop_event.set()
            if state.resume_future and not state.resume_future.done():
                state.resume_future.cancel()
            return
        await asyncio.sleep(1.0)


# ── Task registration (shared by GraphQL startTask, Telegram, Discord) ───────

async def enqueue_chat_task(
    session: AsyncSession,
    query: str,
    model: str,
    conv_id: str,
    attachments: list[AttachmentIn] | None = None,
    *,
    source: str = "http",
) -> str:
    """Insert the assistant Message placeholder, register the TaskState, and
    enqueue a chat job in one transaction. Returns the assistant Message id
    (used as the job_id by convention).

    Callers (GraphQL startTask, Telegram, Discord) are responsible for
    creating the user Message + any per-source side effects (saving documents
    to disk, etc.) BEFORE invoking this. They should also commit those rows
    in the same session this function uses, so everything is atomic.
    """
    from core.state import get_queue

    log_task_received("chat", conv_id, source)
    task_id = str(uuid4())

    session.add(_assistant_placeholder(task_id, conv_id, model))
    payload: dict = {
        "query": query,
        "model": model,
        "conv_id": conv_id,
    }
    if attachments:
        payload["attachments"] = [a.model_dump() for a in attachments]

    await get_queue().enqueue(
        "chat", payload, job_id=task_id, session=session,
    )

    # Register TaskState BEFORE commit so SSE subscribers see it the moment
    # they get task_id back; the queue's after_commit wake fires after the
    # commit returns, so the worker can't race the lookup.
    _tasks[task_id] = TaskState(
        kind="chat",
        label=query[:60],
        parent_id=conv_id,
    )
    log_task_created(task_id, _tasks[task_id], model)

    await session.commit()
    return task_id


def _assistant_placeholder(task_id: str, conv_id: str, model: str):
    from db.models import Message
    return Message(
        id=task_id,
        conversation_id=conv_id,
        role="assistant",
        content="",
        model=model,
        status="running",
    )


async def register_chat_task(
    session: AsyncSession,
    query: str,
    model: str,
    conversation_id: str | None = None,
    attachments: list | None = None,
    project_id: str | None = None,
) -> tuple[str, str]:
    """GraphQL-side wrapper: create conversation if missing, write the user
    Message + any document attachments, then enqueue the chat job.

    ``project_id`` only applies when a new conversation is created here;
    joining an existing conversation goes through ``setConversationProject``.
    Returns ``(task_id, conversation_id)``. Caller validates ``model``.
    """
    if project_id and await get_project(session, project_id) is None:
        raise ValueError(f"project not found: {project_id}")
    title = query[:60] if not conversation_id else None
    conv = await get_or_create_conversation(
        session, conversation_id, model, title, project_id=project_id
    )

    if attachments:
        display_parts: list[dict] = [{"type": "text", "text": query}]
        for att in attachments:
            display_parts.append({"type": att.type, "name": att.name, "size": att.size, "mimeType": att.mime_type})
        display_content = json.dumps(display_parts)
    else:
        display_content = query

    user_msg = await add_message(session, conv.id, "user", display_content)

    if attachments:
        cfg = get_config()
        cfg.documents_dir.mkdir(parents=True, exist_ok=True)
        for att in attachments:
            if att.type != "document":
                continue
            try:
                doc_id = str(uuid4())
                ext = os.path.splitext(att.name)[1] or ".bin"
                doc_path = cfg.documents_dir / f"{doc_id}{ext}"
                doc_path.write_bytes(base64.b64decode(att.data))
                doc = await create_document(
                    session,
                    conversation_id=conv.id,
                    message_id=user_msg.id,
                    filename=att.name,
                    mime_type=att.mime_type,
                    size=att.size,
                    path=str(doc_path),
                )
                # Mark the attachment before enqueue_chat_task serializes it
                # into the job payload — the chat handler uses this id to
                # chunk-index large documents instead of inlining them.
                att.document_id = doc.id
            except Exception as e:
                logger.warning("Failed to persist document %s: %s", att.name, e)

    task_id = await enqueue_chat_task(
        session, query, model, conv.id, attachments=attachments, source="http",
    )
    return (task_id, conv.id)
