"""Streaming pipeline — step extraction, token coalescing, and chunk processing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Sequence
from typing import Any, TypeAlias

from db import async_session
from db.ops import add_step, add_steps, close_open_approvals, update_message_content, update_message_status, update_message_usage

from langgraph.types import StreamMode

from .doc_index import INLINE_THRESHOLD, embeddings_available, start_indexing
from .document_extractor import MAX_CHARS, extract_raw_text, format_inline, is_tabular, is_text_tabular
from .schemas import AttachmentIn
from .approvals import record_blocking_request
from .state import InterruptRequest, TaskState, emit_event

logger = logging.getLogger(__name__)

# LangGraph's astream(subgraphs=True) yields (namespace, mode, data) tuples,
# but the type stubs don't expose this shape. We define it here so callers
# can cast and downstream functions can accept a properly typed parameter.
StreamChunk: TypeAlias = tuple[tuple[str, ...] | None, str, Any]

# Typed constant for the stream_mode parameter — avoids pyrefly inferring
# list[str] which doesn't match the Literal-based overload signatures.
STREAM_MODES: Sequence[StreamMode] = ["updates", "messages", "custom"]

# Cap on the worker result text stored in a worker_done Step row. The live
# event keeps the full result; only the persisted transcript copy is clipped.
WORKER_RESULT_PERSIST_CAP = 2000


# ── Step data extraction ─────────────────────────────────────────────────────


_APPROVAL_ARGS_CAP = 2000


def _safe_args_json(args: Any) -> str | None:
    """Serialize interrupt args for the approvals inbox, never raising.

    Tool args are arbitrary — file bytes, model objects, anything the agent
    passed — so this is best-effort by design: unserializable values fall back
    to `repr`, and the whole blob is capped, because a paused run must stay
    listable even when its arguments are not JSON.
    """
    if args is None:
        return None
    try:
        blob = json.dumps(args, default=repr)
    except Exception:
        blob = repr(args)
    return blob[:_APPROVAL_ARGS_CAP]


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

def _extract_for_message(mime_type: str, data: str, name: str) -> tuple[str | None, str | None]:
    """Executor target: (raw_text, None) on success, (None, error) on failure."""
    try:
        return extract_raw_text(mime_type, data, name), None
    except Exception as exc:
        return None, str(exc)


_PREVIEW_LINES = 5
_PREVIEW_BYTES = 64 * 1024
_COUNT_BLOCK = 1024 * 1024


def _routes_to_code(att: AttachmentIn) -> bool:
    """True when this attachment reaches the agent as a path, not as text.

    Requires a persisted file: sources that don't write a Document row (bots,
    CLI) have nothing on disk to open, so they keep the inline path.
    """
    return (
        att.type == "document"
        and bool(att.document_path)
        and is_tabular(att.mime_type, att.name)
    )


def _tabular_preview(path: str, mime_type: str, filename: str) -> dict:
    """Executor target: head-of-file preview + line count for a tabular file.

    Reads a bounded head for the preview and streams the remainder only to count
    newlines, so an arbitrarily large CSV costs one sequential pass and constant
    memory. Every field is optional by design — a preview that fails must not
    stop the attachment from reaching the agent, which needs only the path.
    """
    info: dict[str, Any] = {"lines": None, "preview": None, "error": None, "bytes": None}
    try:
        info["bytes"] = os.path.getsize(path)
        if not is_text_tabular(mime_type, filename):
            return info
        with open(path, "rb") as fh:
            head = fh.read(_PREVIEW_BYTES)
            info["preview"] = "\n".join(
                head.decode("utf-8", errors="replace").splitlines()[:_PREVIEW_LINES]
            )
            count = head.count(b"\n")
            while block := fh.read(_COUNT_BLOCK):
                count += block.count(b"\n")
        info["lines"] = count
    except Exception as exc:
        info["error"] = str(exc)
    return info


def _tabular_part(att: AttachmentIn, info: dict) -> dict:
    """Stub for a tabular attachment: where the file is, not what it says.

    The bytes are on disk and `run_cell` can open them, so the message carries a
    path and a few head lines instead of the file's text. That is the whole point
    of the branch — what anyone asks of a CSV is an aggregate, and neither pasted
    rows nor embedded chunks can produce one. Line count and preview are stated
    only when they were actually measured; a guess here would be read as fact.
    """
    size = info.get("bytes") or att.size
    lines = info.get("lines")
    header = (
        f"[Tabular file attached: {att.name} — {size:,} bytes"
        + (f", {lines:,} lines" if lines is not None else "")
        + "]"
    )
    bits = [
        header,
        f"path: {att.document_path}",
        "",
        "The contents are NOT included here. Open the path with code in run_cell "
        "— polars, pandas, or duckdb — and compute the answer. Do not read the "
        "file into the conversation row by row.",
    ]
    if info.get("preview"):
        bits += ["", f"First {_PREVIEW_LINES} lines:", info["preview"]]
    if info.get("error"):
        bits += ["", f"(preview unavailable: {info['error']} — the path above is still valid)"]
    return {"type": "text", "text": "\n".join(bits)}


def _path_note(att: AttachmentIn, why: str) -> str:
    """The ' the file is also at <path>' clause, or nothing when it isn't."""
    if not att.document_path:
        return ""
    return (
        f" The file itself is at {att.document_path} — open it with code in "
        f"run_cell when {why}."
    )


async def _document_part(att: AttachmentIn, raw: str | None, error: str | None) -> dict:
    """Build the message part for one document attachment.

    Small documents (or any document when indexing isn't possible) are
    inlined as before. Large documents with a persisted Document row are
    chunk-indexed and replaced by a short stub pointing the agent at the
    search_documents / read_document tools — keeping a big PDF out of the
    per-turn token bill and out of the summarizer's reach.

    Indexing is started, not awaited: embedding a large PDF took seconds off the
    first token for passages the agent may never ask for. The retrieval tools
    block on readiness instead (core/doc_index.await_index_ready), so a search
    still can't race the indexer and conclude the document is empty.
    """
    if error is not None:
        return {"type": "text", "text": f"[Document: {att.name}]\n[Extraction failed: {error}]\n[End of document]"}
    assert raw is not None
    if att.document_id and len(raw) > INLINE_THRESHOLD and embeddings_available():
        try:
            start_indexing(att.document_id, raw)
            return {"type": "text", "text": (
                f"[Document attached: {att.name} — {len(raw):,} characters, "
                f"being indexed for search now "
                f"(document_id={att.document_id!r}). Too large to include inline: "
                f'use search_documents("...") to find relevant passages, or '
                f"read_document({att.document_id!r}, offset=0) to read it sequentially. "
                f"Those calls wait for indexing to finish, so the first one may pause briefly."
                + _path_note(att, "computing over the whole document beats reading it in windows")
                + "]"
            )}
        except Exception as exc:
            logger.warning("could not start indexing %s (%s) — inlining instead", att.name, exc)
    text = format_inline(att.name, raw)
    if att.document_path and len(raw) > MAX_CHARS:
        # The inline path truncates silently; saying so — and where the rest is —
        # is what stops an answer confidently drawn from the first 80k characters.
        text += (
            f"\n[Only the first {MAX_CHARS:,} of {len(raw):,} characters are shown above. "
            f"The complete file is at {att.document_path} — open it with code in run_cell "
            f"to work with all of it.]"
        )
    elif att.document_path:
        text += f"\n[File on disk: {att.document_path}]"
    return {"type": "text", "text": text}


async def _build_message_content(
    query: str,
    attachments: list[AttachmentIn] | None,
    model: str,
) -> str | list:
    if not attachments:
        return query

    loop = asyncio.get_running_loop()
    doc_futures: dict[int, asyncio.Future[tuple[str | None, str | None]]] = {}
    tabular_futures: dict[int, asyncio.Future[dict]] = {}
    for idx, att in enumerate(attachments):
        if att.type != "document":
            continue
        if _routes_to_code(att):
            # Extraction is skipped entirely, not just discarded: decoding a
            # 100MB CSV to a str only to throw it away is the cost this branch
            # exists to avoid.
            tabular_futures[idx] = loop.run_in_executor(
                None, _tabular_preview, att.document_path or "", att.mime_type, att.name,
            )
        else:
            doc_futures[idx] = loop.run_in_executor(
                None, _extract_for_message, att.mime_type, att.data, att.name,
            )
    if doc_futures or tabular_futures:
        await asyncio.gather(*doc_futures.values(), *tabular_futures.values())

    parts: list[dict] = [{"type": "text", "text": query}]
    is_google = (
        model.startswith("google_genai:")
        or model.startswith("google:")
        or model.startswith("gemini")
    )
    for idx, att in enumerate(attachments):
        data_url = f"data:{att.mime_type};base64,{att.data}"
        if att.type == "document":
            if idx in tabular_futures:
                parts.append(_tabular_part(att, tabular_futures[idx].result()))
            else:
                raw, error = doc_futures[idx].result()
                parts.append(await _document_part(att, raw, error))
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
                url=data.get("url"),
                phase=data.get("phase", "start"),
                source=source,
            )
        elif event_type in ("worker_start", "worker_step", "worker_token", "worker_done"):
            # Worker lifecycle events from tools/workers.py — forwarded to the
            # live stream and (except tokens) persisted as Step rows so the
            # transcript can rebuild per-worker groups after a reload.
            # worker_token is already coalesced at the source (_TokenTail) and
            # never interleaves with main-agent text (the parent is blocked in
            # the spawn_workers tool call), so it skips the flush.
            if event_type != "worker_token":
                coalescer.flush_all()
            payload = {k: v for k, v in data.items() if k != "type"}
            if (
                event_type != "worker_token"
                and persist_steps and task_id and conv_id and step_seq_ref is not None
            ):
                # Group key must match what useTaskEvents builds live: "<role>:<idx>".
                worker_key = f"{payload.get('role', 'worker')}:{payload.get('idx', '?')}"
                if event_type == "worker_step":
                    node = payload.get("node", "worker")
                    step_data = payload.get("data")
                else:
                    node = event_type
                    record = {k: payload.get(k) for k in ("idx", "role", "task")}
                    if event_type == "worker_done":
                        record["status"] = payload.get("status", "done")
                        record["result"] = str(payload.get("result") or "")[:WORKER_RESULT_PERSIST_CAP]
                    step_data = json.dumps(record)
                async with async_session() as session:
                    await add_step(
                        session, task_id, conv_id, node, "subagent", step_data,
                        step_seq_ref[0], subagent=worker_key,
                    )
                step_seq_ref[0] += 1
            emit_event(state, event_type, **payload)
        elif event_type == "artifact":
            coalescer.flush_all()
            payload = {k: v for k, v in data.items() if k != "type"}
            emit_event(state, "artifact", **payload)
        elif event_type == "todos_updated":
            coalescer.flush_all()
            emit_event(state, "todos_updated", todos=data.get("todos", []), source=source)
        elif event_type in ("approval_request", "approval_resolved", "workflow_event", "budget_exceeded", "budget_update"):
            coalescer.flush_all()
            payload = {k: v for k, v in data.items() if k != "type"}
            emit_event(state, event_type, **payload)
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
                interrupt_id = str(interrupt_id) if interrupt_id is not None else None
                # Record the payload, not just the id, so the approvals inbox
                # can render this pause without tailing the run's event stream.
                # `request_tool_approval` interrupts with a dict tagged
                # type="approval" carrying the tool + args; a bare
                # `request_input` (free-text HITL) carries neither.
                if interrupt_id is not None:
                    is_approval = isinstance(value, dict) and value.get("type") == "approval"
                    request = InterruptRequest(
                        id=interrupt_id,
                        question=question,
                        kind="approval" if is_approval else "input",
                        tool=value.get("tool") if isinstance(value, dict) else None,
                        args_json=_safe_args_json(value.get("args")) if isinstance(value, dict) else None,
                    )
                    state.set_interrupt(request)
                    # Persist it too: the in-memory copy dies with the process,
                    # and the durable row is what lets the run be resumed after
                    # a restart (see core/approvals.reconcile_startup).
                    request.approval_id = await record_blocking_request(
                        source=state.kind,
                        kind=request.kind,
                        question=request.question,
                        label=state.label,
                        task_id=task_id,
                        interrupt_id=request.id,
                        parent_id=conv_id or state.parent_id,
                        tool=request.tool,
                        args_json=request.args_json,
                    )
                else:
                    state.pending_interrupt_id = None
                emit_event(
                    state, "interrupt",
                    interrupt_id=interrupt_id,
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
                    # One commit for the whole chunk. Events are emitted after
                    # the write so a subscriber never sees a step the transcript
                    # wouldn't have on reload.
                    rows: list[tuple[str, str, str | None, int, str | None]] = []
                    for node_name, src, step_data in step_records:
                        rows.append((node_name, src, step_data, step_seq_ref[0], subagent))
                        step_seq_ref[0] += 1
                    async with async_session() as session:
                        await add_steps(session, task_id, conv_id, rows)
                    for node_name, src, step_data in step_records:
                        _emit_step(node_name, src, step_data)
                else:
                    for node_name, src, step_data in step_records:
                        _emit_step(node_name, src, step_data)
        return False

    return False


# ── Finalize message ─────────────────────────────────────────────────────────

async def _finalize_message(
    task_id: str,
    content: str,
    status: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    perf: dict[str, float | None] | None = None,
    duration_ms: float | None = None,
) -> None:
    """Short-lived session write for a final message state update."""
    async with async_session() as session:
        await update_message_content(session, task_id, content)
        await update_message_status(session, task_id, status)
        if input_tokens is not None or output_tokens is not None or perf or duration_ms is not None:
            await update_message_usage(
                session, task_id, input_tokens, output_tokens, perf, duration_ms=duration_ms,
            )
        # The run is over, so anything it was still waiting on is unanswerable.
        # Leaving the row pending would put a button in the inbox that resumes
        # nothing — the exact failure `expired` exists to avoid.
        await close_open_approvals(
            session, task_id=task_id, status="expired",
            result=f"The run finished ({status}) before this was answered.",
        )
