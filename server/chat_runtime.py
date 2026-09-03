"""Chat runtime — agent task execution + task registration."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession

from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from core.agents import build_agent, prefetch_retrieval
from core.config import get_config
from core.invocation_context import InvocationContext
from core.queue import Job
from core.run_scaffold import (
    finish_task_state,
    get_or_create_task_state,
    new_invocation_context,
    queue_cancel_watch,
    start_run_callbacks,
)
from core.schemas import AttachmentIn
from core.state import (
    InterruptRequest,
    QueuedMessage,
    TaskState,
    _tasks,
    emit_event,
    get_async_checkpointer,
    get_store,
    log_task_created,
    log_task_received,
)
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _build_message_content, _finalize_message, _process_chunk

from db import async_session
from db.models import Conversation
from db.ops import (
    add_message,
    create_document,
    delete_message,
    get_or_create_conversation,
    get_project,
    get_queued_messages,
    mark_messages_delivered,
)


# ── Agent task runner ────────────────────────────────────────────────────────

async def _pending_approval_for(task_id: str) -> InterruptRequest | None:
    """The durable pause this run left behind, if it is picking one up.

    Returns None on the overwhelmingly common path (a fresh run), so this costs
    one indexed lookup per turn. Restored as an `InterruptRequest` rather than
    re-deriving the question from the graph: LangGraph keeps the interrupt, but
    not the human-readable prompt that was streamed alongside it.
    """
    from db.ops import find_open_approval_for_task

    async with async_session() as session:
        row = await find_open_approval_for_task(session, task_id)
    if row is None:
        return None
    return InterruptRequest(
        id=row.interrupt_id or row.id,
        question=row.question,
        kind=row.kind,  # type: ignore[arg-type]
        tool=row.tool,
        args_json=row.args_json,
        approval_id=row.id,
    )


async def _run_agent_task(
    task_id: str, query: str, model: str, conv_id: str,
    attachments: list | None = None,
    invocation_context: InvocationContext | None = None,
) -> None:
    state = _tasks[task_id]
    ctx = invocation_context

    accumulated: list[str] = []
    step_seq_ref = [0]
    coalescer = TokenCoalescer(state)
    run_cb = start_run_callbacks(state, "chat", with_perf=True)
    callbacks = run_cb.handlers
    usage = run_cb.usage
    perf = run_cb.perf

    status = "error"
    project_id: str | None = None
    ephemeral: bool = False

    async def finalize(content: str, final_status: str) -> None:
        await _finalize_message(
            task_id, content, final_status,
            input_tokens=usage.input_tokens if usage.has_usage else None,
            output_tokens=usage.output_tokens if usage.has_usage else None,
            perf=perf.message_perf(),
        )

    try:
        # Turn setup — message content (attachment extraction / doc indexing),
        # the project lookup, and the turn's memory+skill retrieval are
        # independent of each other, so they run concurrently instead of
        # stacking their latencies.
        user_msg_id = str(uuid4())
        # Jarvis-style: prefer InvocationContext infra refs over globals
        if ctx is not None and ctx.store is not None:
            store = ctx.store
            checkpointer = ctx.checkpointer
        else:
            store = get_store()
            checkpointer = get_async_checkpointer()
        # record session_id into ctx for state scoping
        if ctx is not None:
            if not ctx.session_id:
                ctx.session_id = conv_id
            ctx.state.set(f"session:{conv_id}:last_query", query[:200])
        prefetch_retrieval(store, query, user_msg_id)
        content_task = asyncio.create_task(_build_message_content(query, attachments, model))
        scope_task = asyncio.create_task(_resolve_conv_scope(conv_id))

        content = await content_task

        agent = build_agent(model, checkpointer=checkpointer, store=store, invocation_context=ctx)
        project_id, ephemeral = await scope_task
        # message_id == task_id: the assistant Message row this turn writes.
        # Artifacts stamp it so the UI can render each one under the message
        # that produced it (tools/artifacts.py).
        configurable: dict[str, Any] = {
            "thread_id": conv_id,
            "conversation_id": conv_id,
            "message_id": task_id,
        }
        if project_id:
            configurable["project_id"] = project_id
        if ephemeral:
            configurable["ephemeral"] = True
        config = {
            "configurable": configurable,
            "recursion_limit": 100,
            "callbacks": callbacks,
        }
        # Reset the per-conversation plan at the start of each new turn. Todos
        # live in the checkpointer keyed by thread_id, so without this a plan
        # written on an earlier turn lingers — often frozen at 0/N when that run
        # errored before advancing it — and renders on every later message and
        # in the activity sidebar. Passing todos=[] overwrites the LastValue
        # channel (correctness on reload); the explicit event clears live
        # subscribers immediately, since a channel write alone dispatches none.
        # The explicit id matches the prefetch_retrieval key above — add_messages
        # preserves provided ids, so the graph's retrieval-cache lookup hits the
        # task started before the gate resolved.
        stream_input: Any = {"messages": [HumanMessage(content=content, id=user_msg_id)], "todos": []}
        emit_event(state, "todos_updated", todos=[], source="main")

        # Restart-resume. A job re-claimed after the server died may belong to a
        # run that was suspended on an interrupt. Re-sending the prompt would
        # discard the checkpointed pause and replay the whole turn; the durable
        # approval row is what lets us tell the difference, and it still holds
        # the question the in-memory copy lost. Re-await the answer instead,
        # then continue with Command(resume=...) exactly as the live path does.
        pending = await _pending_approval_for(task_id)
        if pending is not None:
            state.set_interrupt(pending)
            emit_event(
                state, "interrupt",
                interrupt_id=pending.id, question=pending.question,
            )
            state.resume_future = asyncio.get_running_loop().create_future()
            try:
                stream_input = Command(resume=await state.resume_future)
            except asyncio.CancelledError:
                state.cancelled = True
                stream_input = None
            finally:
                state.resume_future = None
                state.clear_interrupt()

        while True:
            if stream_input is None:
                break
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
                state.clear_interrupt()

            stream_input = Command(resume=answer)

        coalescer.flush_all()
        final_message = "".join(accumulated)
        if state.budget_exceeded:
            reason = state.budget_reason or "budget exceeded"
            if not final_message.strip():
                final_message = f"Stopped: budget exceeded ({reason})"
            else:
                final_message = final_message + f"\n\n[Stopped: budget exceeded ({reason})]"
            await finalize(final_message, "stopped")
            status = "stopped"
            emit_event(state, "budget_exceeded", reason=reason, message=final_message, conversation_id=conv_id)
            emit_event(state, "stopped", message=final_message, conversation_id=conv_id)
        elif state.cancelled:
            await finalize(final_message, "stopped")
            status = "stopped"
            emit_event(state, "stopped", message=final_message, conversation_id=conv_id)
        else:
            status = "done"
            await finalize(final_message, status)
            emit_event(state, "done", message=final_message, conversation_id=conv_id)
            await _redispatch_queued(state, conv_id, model)

            # No project-memory work here. This used to fire a consolidation
            # attempt after every completed chat, which is once per *turn* —
            # there is no end-of-conversation signal to hook. The scheduled
            # sweep (core/project_memory_consolidation.py) batches over the
            # messages table once a conversation goes quiet instead, so the
            # messages written above are already all the buffering it needs.

    except asyncio.CancelledError:
        coalescer.flush_all()
        final_message = "".join(accumulated)
        if state.budget_exceeded:
            reason = state.budget_reason or "budget exceeded"
            if not final_message.strip():
                final_message = f"Stopped: budget exceeded ({reason})"
            emit_event(state, "budget_exceeded", reason=reason, message=final_message, conversation_id=conv_id)
        await finalize(final_message, "stopped")
        status = "stopped"
        emit_event(state, "stopped", message=final_message, conversation_id=conv_id)

    except GraphRecursionError:
        coalescer.flush_all()
        final_message = "".join(accumulated) or "(agent reached iteration limit)"
        status = "done"
        await finalize(final_message, status)
        emit_event(state, "done", message=final_message, conversation_id=conv_id)

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
        if ctx is not None:
            try:
                await ctx.persist_state_deltas()
            except Exception:
                pass
        finish_task_state(task_id, state, status)


# ── Mid-run message queue ────────────────────────────────────────────────────
#
# A message typed while a run is in flight is not a second run: two runs on one
# `thread_id` race the checkpointer. It is queued instead, and the agent's
# model_request node drains it just before its next LLM call
# (core/agents.py:_drain_queued_input).
#
# Two carriers, on purpose. `TaskState.pending_input` is the fast path the
# drain reads with no IO; the `messages` row (status `queued`) is what renders
# in the transcript and what survives a restart, since TaskState does not.


async def queue_chat_message(session: AsyncSession, task_id: str, text: str) -> tuple[str, int]:
    """Queue `text` for the in-flight run `task_id`. Returns (message_id, position)."""
    text = text.strip()
    if not text:
        raise ValueError("empty message")
    state = _tasks.get(task_id)
    if state is None or state.done:
        raise ValueError("task not found or already finished")
    # A paused run is not waiting for the next turn, it is waiting for *this*
    # answer — and it will not reach the drain until it gets one. resumeTask is
    # the path for that, so refuse rather than park the text behind the pause.
    if state.pending_interrupt_id:
        raise ValueError("this run is waiting on an answer — use resumeTask")
    conv_id = state.parent_id
    if not conv_id:
        raise ValueError("task is not attached to a conversation")

    msg = await add_message(session, conv_id, "user", text, status="queued")
    position = state.queue_input(QueuedMessage(id=msg.id, text=text))
    # The drain makes this the newest user turn, and _retrieved_volatile_parts
    # keys its memory+skill lookup off the last HumanMessage's id. Warming it
    # here — with the id the drain will deliver — keeps that work off the
    # critical path of the iteration that consumes it.
    try:
        prefetch_retrieval(get_store(), text, msg.id)
    except Exception as exc:
        logger.debug("queued-message retrieval prefetch skipped: %s", exc)
    emit_event(state, "queued_message", message_id=msg.id, text=text, position=position)
    logger.info("queued message for run %s (position %d)", task_id, position)
    return msg.id, position


async def unqueue_chat_message(session: AsyncSession, task_id: str, message_id: str) -> bool:
    """Withdraw a queued message. False if it was already delivered."""
    state = _tasks.get(task_id)
    if state is None:
        raise ValueError("task not found")
    if not state.unqueue_input(message_id):
        return False
    await delete_message(session, message_id)
    emit_event(state, "queued_withdrawn", message_id=message_id)
    return True


def in_flight_chat_task(conv_id: str) -> str | None:
    """The chat run currently up on `conv_id`, or None if it is idle.

    `_tasks` covers both pending and running work — `enqueue_chat_task`
    registers the state before it commits the job — so this sees a turn that
    has been accepted but not yet claimed. The one gap is a job that outlived a
    restart: nothing re-registers it until the worker claims it, so a message
    sent inside that window starts its own turn. `_adopt_queued_messages` is
    what keeps that from losing anything.
    """
    for task_id, state in _tasks.items():
        if state.kind == "chat" and state.parent_id == conv_id and not state.done:
            return task_id
    return None


async def route_to_live_run(
    session: AsyncSession,
    conv_id: str,
    query: str,
    attachments: list | None = None,
) -> tuple[str, str] | None:
    """Hand `query` to the run already up on this conversation, if there is one.

    Returns ``(task_id, message_id)`` when the text was queued, or None when
    the conversation is idle and the caller should start a turn as usual. This
    is the single rule every surface goes through, because starting a second
    turn here is not a heavier version of the same thing — both runs share the
    conversation's LangGraph `thread_id` and race the checkpointer.

    Raises when a run is up but this message cannot join it: attachments (a
    queued row has to be replayable from the DB alone, and it stores attachment
    metadata, not bytes) or a run paused on an interrupt, which is waiting for
    that answer rather than for the next turn.
    """
    task_id = in_flight_chat_task(conv_id)
    if task_id is None:
        return None
    if attachments:
        raise ValueError(
            "a run is already in flight on this conversation and attachments cannot be "
            "queued onto it — wait for it to finish, or stop it first"
        )
    message_id, _ = await queue_chat_message(session, task_id, query)
    return task_id, message_id


async def _adopt_queued_messages(conv_id: str, state: TaskState) -> None:
    """Pick up messages queued against a run that is no longer around.

    A restart loses TaskState but not the rows, and a stopped or errored run
    deliberately leaves its queue behind (see `_redispatch_queued`). Either
    way the next run on the conversation is the one that should deliver them,
    so adoption happens per job rather than per restart.
    """
    try:
        async with async_session() as session:
            rows = await get_queued_messages(session, conv_id)
    except Exception as exc:
        logger.warning("could not load queued messages for %s: %s", conv_id, exc)
        return
    known = {m.id for m in state.pending_input}
    adopted = 0
    for row in rows:
        if row.id in known:
            continue
        state.queue_input(QueuedMessage(id=row.id, text=row.content))
        adopted += 1
    if adopted:
        logger.info("adopted %d queued message(s) into run on %s", adopted, conv_id)


async def _redispatch_queued(state: TaskState, conv_id: str, model: str) -> None:
    """A message queued after the last drain has no run left to join — start one.

    Only on a clean finish. A stopped or errored run leaves its queue as
    `queued` rows instead, so the next run adopts them (or the user withdraws
    them) rather than firing a fresh turn at a conversation they just
    interrupted.

    Only the first leftover becomes the new turn's prompt; the rest stay
    `queued` and `_adopt_queued_messages` hands them to that run, which is the
    same path a restart takes.
    """
    leftovers = state.drain_input()
    if not leftovers:
        return
    first = leftovers[0]
    try:
        async with async_session() as session:
            await mark_messages_delivered(session, [first.id], status="done")
            await enqueue_chat_task(
                session, first.text, model, conv_id, source="queue",
            )
    except Exception as exc:
        # The rows are still `queued`, so nothing is lost — the next run on
        # this conversation adopts all of them, including `first`.
        logger.warning("re-dispatch of queued messages failed: %s", exc)


async def _resolve_conv_scope(conv_id: str) -> tuple[str | None, bool]:
    """Project membership + incognito flag, resolved fresh at run start (not
    baked into the job payload) so bots and post-restart resumes need no payload
    changes and add/remove-from-project applies on the next run. Returns
    ``(project_id, ephemeral)``."""
    async with async_session() as session:
        conv_row = await session.get(Conversation, conv_id)
        if conv_row is None:
            return None, False
        return conv_row.project_id, bool(conv_row.ephemeral)


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
    invocation_context = await new_invocation_context(
        kind="chat",
        session_id=conv_id,
        invocation_id=task_id,
        model=model,
        initial_state={"query": query[:500]},
        load_persisted=True,
    )

    if task_id not in _tasks:
        async with async_session() as session:
            conv = await session.get(Conversation, conv_id)
        label = (conv.title if conv and conv.title else query[:60])
    else:
        label = query[:60]
    state = get_or_create_task_state(
        task_id, kind="chat", label=label, parent_id=conv_id, model=model,
    )

    await _adopt_queued_messages(conv_id, state)

    async with queue_cancel_watch(task_id, state):
        await _run_agent_task(
            task_id, query, model, conv_id, attachments,
            invocation_context=invocation_context,
        )


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


@dataclass
class ChatDispatch:
    """What a chat request turned into: a new turn, or a message queued onto
    the run already going. `task_id` is the run to subscribe to either way."""

    task_id: str
    conversation_id: str
    queued_message_id: str | None = None

    @property
    def queued(self) -> bool:
        return self.queued_message_id is not None


async def register_chat_task(
    session: AsyncSession,
    query: str,
    model: str,
    conversation_id: str | None = None,
    attachments: list | None = None,
    project_id: str | None = None,
    ephemeral: bool = False,
) -> ChatDispatch:
    """GraphQL-side wrapper: create conversation if missing, write the user
    Message + any document attachments, then enqueue the chat job.

    ``project_id`` and ``ephemeral`` only apply when a new conversation is
    created here; joining an existing conversation goes through
    ``setConversationProject`` (project) and inherits the stored incognito flag.
    Incognito and project membership are mutually exclusive — an ephemeral chat
    ignores ``project_id``. Caller validates ``model``.

    A request arriving while this conversation is already running is queued
    onto that run rather than started beside it (see `route_to_live_run`); the
    returned `ChatDispatch` says which happened, and carries the running task's
    id either way so the caller subscribes to the right stream.
    """
    if ephemeral:
        project_id = None
    if project_id and await get_project(session, project_id) is None:
        raise ValueError(f"project not found: {project_id}")
    title = query[:60] if not conversation_id else None
    conv = await get_or_create_conversation(
        session, conversation_id, model, title, project_id=project_id, ephemeral=ephemeral
    )

    routed = await route_to_live_run(session, conv.id, query, attachments)
    if routed is not None:
        task_id, message_id = routed
        return ChatDispatch(
            task_id=task_id, conversation_id=conv.id, queued_message_id=message_id,
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
    return ChatDispatch(task_id=task_id, conversation_id=conv.id)
