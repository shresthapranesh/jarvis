"""Streaming pipeline — step extraction, token coalescing, and chunk processing.

Shared between the chat path (_run_agent_task in chat_runtime) and the
automation prompt path (_execute_prompt_type in automation_runtime).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any, TypeAlias

from db import async_session
from db.ops import add_step, update_message_content, update_message_status

from langgraph.types import StreamMode

from .document_extractor import extract_text as extract_document_text
from .schemas import AttachmentIn
from .state import TaskState, emit_event

# LangGraph's astream(subgraphs=True) yields (namespace, mode, data) tuples,
# but the type stubs don't expose this shape. We define it here so callers
# can cast and downstream functions can accept a properly typed parameter.
StreamChunk: TypeAlias = tuple[tuple[str, ...] | None, str, Any]

# Typed constant for the stream_mode parameter — avoids pyrefly inferring
# list[str] which doesn't match the Literal-based overload signatures.
STREAM_MODES: Sequence[StreamMode] = ["updates", "messages", "custom"]


# ── Step data extraction ─────────────────────────────────────────────────────


def _subagent_name_from_ns(ns: tuple[str, ...] | None) -> str | None:
    """Pull a label out of a LangGraph subgraph namespace.

    `ns` looks like `('worker:abc123',)` or `('task:something',)`. We use
    the first colon-segment as the label so the UI has something to show
    next to streamed events; the agent currently only spawns generic
    workers, so there are no specialised subagent names to recognise.
    """
    if not ns:
        return None
    return ns[0].split(":", 1)[0] or None


def _extract_step_data(node_name: str, node_data: dict) -> str:
    try:
        messages = node_data.get("messages", [])
        if node_name == "tools":
            entries = []
            for msg in messages:
                if getattr(msg, "type", "") == "tool" and hasattr(msg, "content"):
                    entries.append({
                        "tool": getattr(msg, "name", ""),
                        "output": str(msg.content)[:400],
                    })
            if entries:
                return json.dumps(entries if len(entries) > 1 else entries[0])

        if node_name == "model_request":
            for msg in messages:
                # Only inspect AIMessages — when the in-graph summarizer fires it
                # also emits RemoveMessage entries and a SystemMessage summary;
                # those would otherwise hijack the step display before the actual
                # model response is reached.
                if getattr(msg, "type", "") not in ("ai", "AIMessageChunk"):
                    continue
                tool_calls = getattr(msg, "tool_calls", [])
                if tool_calls:
                    return json.dumps({
                        "tool_calls": [
                            {"name": tc.get("name"), "args": tc.get("args")}
                            for tc in tool_calls
                        ]
                    })
                raw_content = getattr(msg, "content", "")
                if isinstance(raw_content, list):
                    # Reasoning models: extract text blocks, skip thinking blocks
                    text = " ".join(
                        b.get("text", "") for b in raw_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                else:
                    text = str(raw_content)
                if text:
                    return json.dumps({"text": text[:400]})
    except Exception:
        pass
    return json.dumps({"raw": str(node_data)[:400]})


# ── Multimodal content builder ───────────────────────────────────────────────

async def _build_message_content(
    query: str,
    attachments: list[AttachmentIn] | None,
    model: str,
) -> str | list:
    if not attachments:
        return query

    loop = asyncio.get_running_loop()
    doc_futures: dict[int, asyncio.Future[str]] = {}
    for idx, att in enumerate(attachments):
        if att.type == "document":
            doc_futures[idx] = loop.run_in_executor(
                None, extract_document_text, att.mime_type, att.data, att.name,
            )
    if doc_futures:
        await asyncio.gather(*doc_futures.values())

    parts: list[dict] = [{"type": "text", "text": query}]
    is_google = (
        model.startswith("google_genai:")
        or model.startswith("google:")
        or model.startswith("gemini")
    )
    for idx, att in enumerate(attachments):
        data_url = f"data:{att.mime_type};base64,{att.data}"
        if att.type == "document":
            parts.append({"type": "text", "text": doc_futures[idx].result()})
        elif att.type == "image":
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        elif is_google:
            parts.append({"type": "media", "mime_type": att.mime_type, "data": att.data})
        else:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return parts


# ── Token coalescer ──────────────────────────────────────────────────────────

class _Bucket:
    """Per-event-type token buffer, keyed by source. Flushes through
    `emit_event` when either the size or the age threshold is hit."""

    def __init__(self, state: TaskState, event_name: str, max_chars: int, max_delay: float):
        self.state = state
        self.event_name = event_name
        self.max_chars = max_chars
        self.max_delay = max_delay
        self._chunks: dict[str, list[str]] = {}
        self._lengths: dict[str, int] = {}
        self._first_enqueued: dict[str, float] = {}

    def add(self, text: str, source: str) -> None:
        if source not in self._chunks:
            self._chunks[source] = []
            self._lengths[source] = 0
            self._first_enqueued[source] = time.monotonic()
        self._chunks[source].append(text)
        self._lengths[source] += len(text)
        if (
            self._lengths[source] >= self.max_chars
            or time.monotonic() - self._first_enqueued[source] >= self.max_delay
        ):
            self.flush(source)

    def flush(self, source: str) -> None:
        text_chunks = self._chunks.pop(source, None)
        self._lengths.pop(source, None)
        self._first_enqueued.pop(source, None)
        if not text_chunks:
            return
        emit_event(self.state, self.event_name, text="".join(text_chunks), source=source)

    def flush_all(self) -> None:
        for source in list(self._chunks.keys()):
            self.flush(source)


class TokenCoalescer:
    """Batches streaming-token events per source to cut wake-up frequency.

    A verbose writer subagent can emit thousands of single-character tokens;
    each one previously appended its own event and woke every waiter.
    This buffers tokens and flushes when EITHER threshold hits first:
      - buffered text for that source reaches `max_chars` characters, or
      - the oldest buffered token is `max_delay_sec` old.

    The 50ms latency bound is indistinguishable from uncoalesced output in
    the UI but cuts notify calls dramatically under bursty writers. Any
    non-token event (step, browser_step, interrupt, done, error) MUST call
    `flush_all()` first to preserve ordering.

    `accumulated` (the persisted message content) is NOT routed through this
    coalescer — tokens must be appended there immediately by the caller for
    correctness.

    Thinking/reasoning tokens from models with reasoning enabled are tracked
    in a separate bucket and emitted as `thinking_token` events (same payload
    shape as `token`). They are never appended to `accumulated`.
    """

    def __init__(self, state: TaskState, *, max_chars: int = 64, max_delay_sec: float = 0.05):
        self._tokens = _Bucket(state, "token", max_chars, max_delay_sec)
        self._thinking = _Bucket(state, "thinking_token", max_chars, max_delay_sec)

    def add_token(self, text: str, source: str) -> None:
        if text:
            self._tokens.add(text, source)

    def add_thinking(self, text: str, source: str) -> None:
        """Buffer a reasoning/thinking token. Flushed as a `thinking_token` event."""
        if text:
            self._thinking.add(text, source)

    def flush_all(self) -> None:
        self._tokens.flush_all()
        self._thinking.flush_all()


# ── Shared chunk processor ───────────────────────────────────────────────────

async def _process_chunk(
    chunk: StreamChunk,
    state: TaskState,
    coalescer: TokenCoalescer,
    accumulated: list[str],
    *,
    task_id: str | None = None,
    conv_id: str | None = None,
    step_seq_ref: list[int] | None = None,
    persist_steps: bool = False,
) -> bool:
    """Process a single astream chunk. Returns True if an interrupt was encountered
    (caller should stop iterating and await the resume future).

    Shared between the chat path (``_run_agent_task``) and the automation prompt
    path (``_execute_prompt_type``). The chat path passes ``persist_steps=True``
    with a ``task_id``/``conv_id``/``step_seq_ref`` so every step is persisted
    through ``add_step``; automations skip persistence but still emit the same
    step/browser/token SSE events.

    DB writes open short-lived sessions (one per chunk) rather than holding a
    single session for the full agent run, which previously pinned a SQLite
    transaction open for minutes and blocked concurrent writers.
    """
    ns, mode, data = chunk
    subagent = _subagent_name_from_ns(ns)
    source = "subagent" if subagent else "main"

    if mode == "messages":
        token, metadata = data
        # Drop tokens emitted by the safety judge — its LLM call inherits the
        # parent agent's callbacks via contextvars, so its JSON verdict /
        # thinking blocks would otherwise leak into the user-visible stream.
        tags = (metadata or {}).get("tags") or []
        if "safety_judge" in tags:
            return False
        is_ai = getattr(token, "type", "") in ("ai", "AIMessageChunk")
        if not is_ai or not hasattr(token, "content"):
            return False
        content = token.content
        if isinstance(content, str):
            if content:
                # Stream all tokens live. The system prompt instructs the model
                # to call tools silently; well-behaved models won't emit prefix
                # text before tool_calls. Misbehaving models will leak some, but
                # batched-then-flushed is worse UX than a small leak.
                coalescer.add_token(content, source)
                if not ns:
                    accumulated.append(content)
        elif isinstance(content, list):
            # Reasoning models (Ollama reasoning=True, Gemini thinking, Bedrock extended
            # thinking) return content as a list of typed blocks. Extract both thinking
            # and text blocks so each reaches the correct SSE event type.
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "thinking":
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        coalescer.add_thinking(thinking_text, source)
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        coalescer.add_token(text, source)
                        if not ns:
                            accumulated.append(text)
        return False

    if mode == "custom":
        if not isinstance(data, dict):
            return False
        event_type = data.get("type")
        if event_type == "browser_step":
            coalescer.flush_all()
            emit_event(
                state, "browser_step",
                thought=data.get("thought"),
                actions=data.get("actions"),
                source=source,
            )
        elif event_type == "worker_done":
            coalescer.flush_all()
            idx = data.get("idx", "?")
            task = data.get("task", "")
            result = data.get("result", "")
            text = f"\n**[Worker {idx}]** {task}\n{result}\n"
            emit_event(state, "token", text=text, source="worker")
        elif event_type == "artifact":
            coalescer.flush_all()
            payload = {k: v for k, v in data.items() if k != "type"}
            emit_event(state, "artifact", **payload)
        elif event_type == "todos_updated":
            coalescer.flush_all()
            emit_event(state, "todos_updated", todos=data.get("todos", []), source=source)
        elif event_type in ("safety_review_start", "safety_review_passed", "safety_review_blocked"):
            # Surface the judge's activity as a step so the UI sidebar shows it
            # alongside the model and tool nodes. Payload mirrors what
            # `_extract_step_data` would produce for a real node.
            coalescer.flush_all()
            payload = {k: v for k, v in data.items() if k != "type"}
            emit_event(
                state, "step",
                node=event_type,
                source=source,
                subagent=subagent,
                data=json.dumps(payload),
            )
        return False

    if mode == "updates":
        if isinstance(data, dict) and "__interrupt__" in data:
            coalescer.flush_all()
            interrupts = data["__interrupt__"]
            for intr in interrupts:
                value = getattr(intr, "value", None)
                if isinstance(value, dict):
                    question = value.get("reason") or value.get("question") or str(value)
                else:
                    question = str(value)
                interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None) or task_id
                state.pending_interrupt_id = str(interrupt_id) if interrupt_id is not None else None
                emit_event(
                    state, "interrupt",
                    interrupt_id=str(interrupt_id) if interrupt_id is not None else None,
                    question=question,
                )
            return True

        if isinstance(data, dict):
            step_records: list[tuple[str, str, str]] = []
            for node_name, node_data in data.items():
                if not node_name or node_name.startswith("__"):
                    continue
                step_data = _extract_step_data(node_name, node_data if isinstance(node_data, dict) else {})
                step_records.append((node_name, source, step_data))

            if step_records:
                coalescer.flush_all()

                def _emit_step(node_name: str, src: str, step_data: str) -> None:
                    emit_event(state, "step", node=node_name, source=src, subagent=subagent, data=step_data)

                if persist_steps and task_id and conv_id and step_seq_ref is not None:
                    async with async_session() as session:
                        for node_name, src, step_data in step_records:
                            await add_step(
                                session, task_id, conv_id, node_name, src, step_data, step_seq_ref[0],
                            )
                            _emit_step(node_name, src, step_data)
                            step_seq_ref[0] += 1
                else:
                    for node_name, src, step_data in step_records:
                        _emit_step(node_name, src, step_data)
        return False

    return False


# ── Finalize message ─────────────────────────────────────────────────────────

async def _finalize_message(task_id: str, content: str, status: str) -> None:
    """Short-lived session write for a final message state update."""
    async with async_session() as session:
        await update_message_content(session, task_id, content)
        await update_message_status(session, task_id, status)
