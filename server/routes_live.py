"""Live mode WebSocket endpoint."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.agents import DEFAULT_MODEL, build_agent, is_valid_model
from core.state import get_async_checkpointer, get_store
from core.streaming import StreamChunk, _extract_step_data, _subagent_name_from_ns

router = APIRouter()


@router.websocket("/ws/live")
async def live_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    model = websocket.query_params.get("model", DEFAULT_MODEL)
    if not is_valid_model(model):
        await websocket.close(code=1008, reason=f"unknown model {model!r}")
        return

    # One thread_id per socket lets the checkpointer chain turns together,
    # so deepagents' SummarizationMiddleware can evict+offload older messages
    # once instead of re-running summarization against a full-history replay
    # on every turn.
    thread_id = f"live-{uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") != "user_message":
                continue
            text = data.get("text", "").strip()
            if not text:
                continue

            await websocket.send_json({"type": "status", "state": "thinking"})

            accumulated: list[str] = []
            stream_input: Any = {"messages": [{"role": "user", "content": text}]}
            agent = build_agent(model, checkpointer=get_async_checkpointer(), store=get_store())

            try:
                async for raw_chunk in agent.astream(
                    stream_input,
                    config=config,
                    stream_mode=["updates", "messages", "custom"],
                    subgraphs=True,
                ):
                    chunk: StreamChunk = raw_chunk  # type: ignore[assignment]
                    ns, mode, data = chunk
                    subagent = _subagent_name_from_ns(ns)
                    source = "subagent" if subagent else "main"

                    if mode == "messages":
                        token, _ = data
                        tok_text = getattr(token, "content", "")
                        tok_text = tok_text if isinstance(tok_text, str) else ""
                        is_ai = getattr(token, "type", "") in ("ai", "AIMessageChunk")
                        if tok_text and is_ai and not ns:
                            accumulated.append(tok_text)
                            await websocket.send_json({"type": "token", "text": tok_text})

                    elif mode == "updates" and isinstance(data, dict):
                        for node_name, node_data in data.items():
                            if not node_name or node_name.startswith("__"):
                                continue
                            step_data = _extract_step_data(
                                node_name, node_data if isinstance(node_data, dict) else {},
                            )
                            await websocket.send_json({
                                "type": "step",
                                "node": node_name,
                                "source": source,
                                "subagent": subagent,
                                "data": step_data,
                            })
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": str(exc)})

            final = "".join(accumulated)
            await websocket.send_json({"type": "done", "text": final})
            await websocket.send_json({"type": "status", "state": "idle"})

    except WebSocketDisconnect:
        pass
