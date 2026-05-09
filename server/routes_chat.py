"""Chat endpoints — /run, /stop, /resume, /stream, and conversation CRUD."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from core.agents import build_agent, is_valid_model
from core.config import get_config
from core.log_callback import AgentLogger
from core.safety import gate_input, gate_output
from core.schemas import ConversationUpdate, ResumePayload, RunRequest, _invalid_model_response, _normalise_todos
from core.state import TaskState, _background_tasks, _notify, _tasks, get_async_checkpointer, get_store, stream_task_events
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _build_message_content, _finalize_message, _process_chunk

from db import async_session, get_session
from db.models import Message
from db.ops import (
    add_message,
    create_document,
    delete_conversation,
    get_conversation_meta,
    get_or_create_conversation,
    list_conversations,
    list_messages_paginated,
    update_conversation,
    update_message_status,
)


def _serialize_message(msg: Message) -> dict:
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "model": msg.model,
        "status": msg.status,
        "created_at": msg.created_at.isoformat(),
        "steps": [
            {
                "id": s.id,
                "node": s.node,
                "source": s.source,
                "data": s.data,
                "seq": s.seq,
                "created_at": s.created_at.isoformat(),
            }
            for s in sorted(msg.steps, key=lambda x: x.seq)
        ],
    }


router = APIRouter()

# ── Agent task runner ────────────────────────────────────────────────────────

async def _run_agent_task(
    task_id: str, query: str, model: str, conv_id: str,
    attachments: list | None = None,
) -> None:
    state = _tasks[task_id]

    accumulated: list[str] = []
    step_seq_ref = [0]
    coalescer = TokenCoalescer(state)

    try:
        # Input gate — judge the user's prompt before spinning up the agent.
        rejection = await gate_input(query, model)
        if rejection:
            await _finalize_message(task_id, rejection, "blocked")
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
        state.done = True
        _notify(state)
        # Delay popping the task so the frontend UI can show the "done" or
        # "stopped" state for a few seconds before the row vanishes.
        loop = asyncio.get_running_loop()
        loop.call_later(5.0, lambda tid=task_id: _tasks.pop(tid, None))


# ── Run endpoint ─────────────────────────────────────────────────────────────

@router.post("/run")
async def run_agent(
    request: RunRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if not is_valid_model(request.model):
        return _invalid_model_response(request.model)
    title = request.query[:60] if not request.conversation_id else None
    conv = await get_or_create_conversation(session, request.conversation_id, request.model, title)

    if request.attachments:
        display_parts: list[dict] = [{"type": "text", "text": request.query}]
        for att in request.attachments:
            display_parts.append({"type": att.type, "name": att.name, "size": att.size, "mimeType": att.mime_type})
        display_content = json.dumps(display_parts)
    else:
        display_content = request.query

    user_msg = await add_message(session, conv.id, "user", display_content)

    if request.attachments:
        cfg = get_config()
        cfg.documents_dir.mkdir(parents=True, exist_ok=True)
        for att in request.attachments:
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

    task_msg = await add_message(session, conv.id, "assistant", "", model=request.model, status="running")

    _tasks[task_msg.id] = TaskState(
        kind="chat",
        label=conv.title or request.query[:60],
        parent_id=conv.id,
    )

    def _task_done(t: asyncio.Task, task_id: str) -> None:
        _background_tasks.pop(task_id, None)
        if not t.cancelled() and (exc := t.exception()):
            logger.error("task %s raised unhandled %s", task_id, type(exc).__name__, exc_info=exc)

    t = asyncio.create_task(_run_agent_task(
        task_msg.id, request.query, request.model, conv.id, request.attachments
    ))
    _background_tasks[task_msg.id] = t
    t.add_done_callback(lambda _t: _task_done(_t, task_msg.id))

    return JSONResponse({"task_id": task_msg.id, "conversation_id": conv.id})


# ── Stop endpoint ────────────────────────────────────────────────────────────

@router.post("/stop/{task_id}")
async def stop_task(
    task_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    state = _tasks.get(task_id)
    if state is None:
        return JSONResponse({"error": "task not found or already finished"}, status_code=404)

    if state.done:
        return JSONResponse({"error": "task already finished"}, status_code=400)

    state.cancelled = True
    state._stop_event.set()

    if state.resume_future and not state.resume_future.done():
        state.resume_future.cancel()

    bg_task = _background_tasks.get(task_id)
    if bg_task and not bg_task.done():
        bg_task.cancel()

    return JSONResponse({"ok": True, "task_id": task_id})


# ── Resume endpoint ──────────────────────────────────────────────────────────

@router.post("/resume/{task_id}")
async def resume_task(task_id: str, body: ResumePayload) -> JSONResponse:
    state = _tasks.get(task_id)
    if state is None:
        return JSONResponse({"error": "task not found"}, status_code=404)
    if state.resume_future is None or state.resume_future.done():
        return JSONResponse({"error": "no pending interrupt for this task"}, status_code=404)

    pending_id = state.pending_interrupt_id
    state.resume_future.set_result(body.answer)
    state.events.append({"event": "interrupt_resolved", "data": json.dumps({
        "interrupt_id": pending_id,
    })})
    _notify(state)
    return JSONResponse({"ok": True})


# ── Stream endpoint ──────────────────────────────────────────────────────────

@router.get("/stream/{task_id}")
async def stream_task(
    task_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventSourceResponse:
    async def generate() -> AsyncIterator[dict]:
        if task_id not in _tasks:
            msg = await session.get(Message, task_id)
            if msg is None:
                yield {"event": "error", "data": json.dumps({"error": "task not found"})}
                return
            if msg.status == "done":
                yield {"event": "done", "data": json.dumps({
                    "message": msg.content,
                    "conversation_id": msg.conversation_id,
                })}
                return
            if msg.status == "running":
                await update_message_status(session, task_id, "error")
            yield {"event": "error", "data": json.dumps({"error": "task interrupted (server restarted)"})}
            return

        state = _tasks[task_id]
        async for event in stream_task_events(state):
            yield event

    return EventSourceResponse(generate())


# ── Conversation endpoints ───────────────────────────────────────────────────

@router.get("/conversations")
async def get_conversations(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    rows = await list_conversations(session)
    return JSONResponse(rows)


@router.get("/conversations/{conv_id}")
async def get_conversation_detail(
    conv_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 10,
    before: str | None = None,
) -> JSONResponse:
    conv = await get_conversation_meta(session, conv_id)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        before_dt = datetime.fromisoformat(before) if before else None
    except ValueError:
        return JSONResponse({"error": "invalid `before` timestamp"}, status_code=400)

    limit = max(1, min(limit, 100))
    msgs, has_more = await list_messages_paginated(session, conv_id, limit, before_dt)

    return JSONResponse({
        "id": conv.id,
        "title": conv.title,
        "model": conv.model,
        "created_at": conv.created_at.isoformat(),
        "messages": [_serialize_message(m) for m in msgs],
        "has_more": has_more,
    })


@router.patch("/conversations/{conv_id}")
async def patch_conversation(
    conv_id: str,
    body: ConversationUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if body.model is not None and not is_valid_model(body.model):
        return _invalid_model_response(body.model)
    if body.title is None and body.model is None:
        return JSONResponse({"error": "no fields to update"}, status_code=400)
    conv = await update_conversation(session, conv_id, title=body.title, model=body.model)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/conversations/{conv_id}")
async def delete_conversation_endpoint(
    conv_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    await delete_conversation(session, conv_id)
    return JSONResponse({"ok": True})


@router.get("/conversations/{conv_id}/todos")
async def get_conversation_todos(conv_id: str) -> JSONResponse:
    try:
        cp = get_async_checkpointer()
    except RuntimeError:
        return JSONResponse({"todos": []})
    snapshot = await cp.aget_tuple({"configurable": {"thread_id": conv_id}})
    if snapshot is None:
        return JSONResponse({"todos": []})
    raw = (snapshot.checkpoint or {}).get("channel_values", {}).get("todos", [])
    return JSONResponse({"todos": _normalise_todos(raw)})
