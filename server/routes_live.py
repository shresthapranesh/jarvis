"""Live mode WebSocket endpoint."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.agents import DEFAULT_MODEL, build_agent, is_valid_model
from core.log_callback import AgentLogger
from core.state import TaskState, get_async_checkpointer, get_store
from core.streaming import STREAM_MODES, StreamChunk, TokenCoalescer, _process_chunk

router = APIRouter()


async def _drain_events(state: TaskState, websocket: WebSocket, cursor: int) -> int:
    """Forward any new TaskState.events to the websocket as JSON. Returns new cursor."""
    while cursor < len(state.events):
        evt = state.events[cursor]
        cursor += 1
        try:
            payload = json.loads(evt.get("data") or "{}")
        except (TypeError, ValueError):
            payload = {}
        await websocket.send_json({"type": evt.get("event", "message"), **payload})
    return cursor


@router.websocket("/ws/live")
async def live_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    model = websocket.query_params.get("model", DEFAULT_MODEL)
    if not is_valid_model(model):
        await websocket.close(code=1008, reason=f"unknown model {model!r}")
        return

    # One thread_id per socket lets the checkpointer chain turns together so
    # the in-graph summarization persists its trim across turns.
    thread_id = f"live-{uuid4()}"
    agent = build_agent(model, checkpointer=get_async_checkpointer(), store=get_store())

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") != "user_message":
                continue
            text = data.get("text", "").strip()
            if not text:
                continue

            await websocket.send_json({"type": "status", "state": "thinking"})

            state = TaskState()
            coalescer = TokenCoalescer(state)
            accumulated: list[str] = []
            stream_input: Any = {"messages": [{"role": "user", "content": text}]}
            event_cursor = 0

            try:
                async for raw_chunk in agent.astream(
                    stream_input,
                    config={
                        "configurable": {"thread_id": thread_id},
                        "recursion_limit": 100,
                        "callbacks": [AgentLogger()],
                    },
                    stream_mode=STREAM_MODES,
                    subgraphs=True,
                ):
                    chunk: StreamChunk = raw_chunk  # type: ignore[assignment]
                    await _process_chunk(chunk, state, coalescer, accumulated)
                    event_cursor = await _drain_events(state, websocket, event_cursor)

                coalescer.flush_all()
                event_cursor = await _drain_events(state, websocket, event_cursor)
            except Exception as exc:
                coalescer.flush_all()
                event_cursor = await _drain_events(state, websocket, event_cursor)
                await websocket.send_json({"type": "error", "error": str(exc)})

            final = "".join(accumulated)
            await websocket.send_json({"type": "done", "text": final})
            await websocket.send_json({"type": "status", "state": "idle"})

    except WebSocketDisconnect:
        pass
