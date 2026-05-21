"""Chat runtime — agent task execution + task registration.

Used by GraphQL ``startTask`` mutation, plus the Telegram and Discord bots
(via ``_run_agent_task``). REST routes for chat were removed once the
frontend migrated to GraphQL; the runtime helpers stayed.
"""

from __future__ import annotations

import asyncio
import base64
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
from core.log_callback import AgentLogger
from core.safety import gate_input, gate_output
from core.state import (
    TaskState,
    _background_tasks,
    _notify,
    _tasks,
    get_async_checkpointer,
    get_store,
    log_task_complete,
    log_task_created,
    log_task_received,
)
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _build_message_content, _finalize_message, _process_chunk

from db import async_session
from db.ops import (
    add_message,
    create_document,
    get_or_create_conversation,
    update_message_status,
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
    status = "error"

    try:
        # Input gate — judge the user's prompt before spinning up the agent.
        # Surface a step immediately so the activity sidebar shows feedback
        # while the judge runs (a cold Bedrock connection can take ~60s).
        state.events.append({"event": "step", "data": json.dumps({
            "node": "safety", "source": "main", "data": "Reviewing input",
        })})
        _notify(state)
        rejection = await gate_input(query, model)
        if rejection:
            await _finalize_message(task_id, rejection, "blocked")
            status = "blocked"
            state.events.append({"event": "safety_input_blocked", "data": json.dumps({
                "message": rejection, "conversation_id": conv_id,
            })})
            state.events.append({"event": "done", "data": json.dumps({
                "message": rejection, "conversation_id": conv_id,
            })})
            return

        content = await _build_message_content(query, attachments, model)

        agent = build_agent(model, checkpointer=get_async_checkpointer(), store=get_store())
        config = {
            "configurable": {"thread_id": conv_id, "conversation_id": conv_id},
            "recursion_limit": 100,
            "callbacks": [AgentLogger()],
        }
        stream_input: Any = {"messages": [{"role": "user", "content": content}]}

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
            await _finalize_message(task_id, final_message, "stopped")
            status = "stopped"
            state.events.append({"event": "stopped", "data": json.dumps({
                "message": final_message,
                "conversation_id": conv_id,
            })})
        else:
            persisted, output_verdict = await gate_output(final_message, model)
            status = "blocked" if output_verdict else "done"
            await _finalize_message(task_id, persisted, status)
            if output_verdict:
                state.events.append({"event": "safety_output_blocked", "data": json.dumps({
                    "severity": output_verdict.severity,
                    "reason": output_verdict.reason,
                    "redacted_message": persisted,
                    "conversation_id": conv_id,
                })})
            state.events.append({"event": "done", "data": json.dumps({
                "message": persisted,
                "conversation_id": conv_id,
            })})

    except asyncio.CancelledError:
        coalescer.flush_all()
        final_message = "".join(accumulated)
        await _finalize_message(task_id, final_message, "stopped")
        status = "stopped"
        state.events.append({"event": "stopped", "data": json.dumps({
            "message": final_message,
            "conversation_id": conv_id,
        })})

    except GraphRecursionError:
        coalescer.flush_all()
        final_message = "".join(accumulated) or "(agent reached iteration limit)"
        persisted, output_verdict = await gate_output(final_message, model)
        status = "blocked" if output_verdict else "done"
        await _finalize_message(task_id, persisted, status)
        if output_verdict:
            state.events.append({"event": "safety_output_blocked", "data": json.dumps({
                "severity": output_verdict.severity,
                "reason": output_verdict.reason,
                "redacted_message": persisted,
                "conversation_id": conv_id,
            })})
        state.events.append({"event": "done", "data": json.dumps({
            "message": persisted,
            "conversation_id": conv_id,
        })})

    except BaseException as exc:
        coalescer.flush_all()
        state.events.append({"event": "error", "data": json.dumps({"error": str(exc)})})
        try:
            async with async_session() as session:
                await update_message_status(session, task_id, "error")
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


# ── Task registration (shared by GraphQL startTask) ──────────────────────────

async def register_chat_task(
    session: AsyncSession,
    query: str,
    model: str,
    conversation_id: str | None = None,
    attachments: list | None = None,
) -> tuple[str, str]:
    """Set up DB rows, register TaskState, and kick off the background agent task.

    Returns (task_id, conversation_id). Caller is responsible for validating
    `model` (`is_valid_model`) before invoking — this helper assumes it's OK.
    """
    title = query[:60] if not conversation_id else None
    conv = await get_or_create_conversation(session, conversation_id, model, title)
    log_task_received("chat", conv.id, "http")

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
                await create_document(
                    session,
                    conversation_id=conv.id,
                    message_id=user_msg.id,
                    filename=att.name,
                    mime_type=att.mime_type,
                    size=att.size,
                    path=str(doc_path),
                )
            except Exception as e:
                logger.warning("Failed to persist document %s: %s", att.name, e)

    task_msg = await add_message(session, conv.id, "assistant", "", model=model, status="running")

    _tasks[task_msg.id] = TaskState(
        kind="chat",
        label=conv.title or query[:60],
        parent_id=conv.id,
    )
    log_task_created(task_msg.id, _tasks[task_msg.id], model)

    def _task_done(t: asyncio.Task, task_id: str) -> None:
        _background_tasks.pop(task_id, None)
        if not t.cancelled() and (exc := t.exception()):
            logger.error("task %s raised unhandled %s", task_id, type(exc).__name__, exc_info=exc)

    t = asyncio.create_task(_run_agent_task(
        task_msg.id, query, model, conv.id, attachments
    ))
    _background_tasks[task_msg.id] = t
    t.add_done_callback(lambda _t: _task_done(_t, task_msg.id))

    return (task_msg.id, conv.id)
