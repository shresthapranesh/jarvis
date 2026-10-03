"""Agent threads in the database — the transcript tables.

A thread's history is its `ThreadMessage` rows in `seq` order, each a v1
transcript record (`core/transcript.py`); its todo list is `ThreadState`. This
is what replaces the LangGraph checkpointer: rows are written as messages
arrive — one INSERT per message — rather than the whole state re-serialized
on every step.

`apply_messages` merges new messages the way LangGraph's `add_messages`
reducer did, so a node's update means the same thing here: a message whose id
is already live replaces it in place, a `RemoveMessage` evicts one (compaction),
`REMOVE_ALL_MESSAGES` evicts everything before it, and anything else is
appended. A message with no id gets one.

`KvStore` replaces the LangGraph store (`aget` / `aput` / `adelete`) over the
`kv_store` table. `import_checkpoint` and `import_store` convert what
LangGraph left in `checkpoints.db`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, cast

from langchain_core.messages import BaseMessage, RemoveMessage, convert_to_messages, message_chunk_to_message
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.transcript import Blob, decode, encode
from db.models import KvItem, ThreadMessage, ThreadState, TranscriptBlob

# langgraph.graph.message.REMOVE_ALL_MESSAGES, without importing LangGraph.
REMOVE_ALL_MESSAGES = "__remove_all__"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Thread:
    """A thread as a run starts from it."""

    messages: list[BaseMessage] = field(default_factory=list)
    todos: list[Any] | None = None
    # Whether the thread has any rows at all (messages or state).
    exists: bool = False
    # `ThreadState.source`: "checkpoint" when it was converted from LangGraph.
    source: str | None = None


# ── threads ──────────────────────────────────────────────────────────────────


async def load_thread(session: AsyncSession, thread_id: str) -> Thread:
    """The thread's live messages in order, and its todos."""
    rows = (await session.execute(
        select(ThreadMessage.data)
        .where(ThreadMessage.thread_id == thread_id, ThreadMessage.evicted_at.is_(None))
        .order_by(ThreadMessage.seq)
    )).scalars().all()
    records = [json.loads(r) for r in rows]
    blobs = await _blobs_for(session, records)
    state = await session.get(ThreadState, thread_id)
    return Thread(
        messages=[decode(r, blobs) for r in records],
        todos=json.loads(state.todos) if state is not None and state.todos else None,
        exists=bool(records) or state is not None,
        source=state.source if state is not None else None,
    )


async def apply_messages(session: AsyncSession, thread_id: str, updates: Sequence[Any]) -> list[BaseMessage]:
    """Merge `updates` into the thread as `add_messages` would, and commit.

    Returns the messages as written (with the ids they were given). Raises
    `ValueError` for a `RemoveMessage` naming no live message, as
    `add_messages` did — before writing anything.
    """
    incoming = [message_chunk_to_message(m) for m in convert_to_messages(list(updates))]
    for m in incoming:
        if m.id is None:
            m.id = str(uuid.uuid4())

    live = {
        row.message_id: row
        for row in (await session.execute(
            select(ThreadMessage)
            .where(ThreadMessage.thread_id == thread_id, ThreadMessage.evicted_at.is_(None))
        )).scalars()
        if row.message_id is not None
    }
    remove_all = max((i for i, m in enumerate(incoming)
                      if isinstance(m, RemoveMessage) and m.id == REMOVE_ALL_MESSAGES), default=None)
    now = _now()
    if remove_all is not None:
        await session.execute(
            update(ThreadMessage)
            .where(ThreadMessage.thread_id == thread_id, ThreadMessage.evicted_at.is_(None))
            .values(evicted_at=now)
        )
        live = {}
        incoming = incoming[remove_all + 1:]

    pending: dict[str, BaseMessage] = {}  # id -> latest message for it in this batch (None-free)
    order: list[str] = []
    removed: set[str] = set()
    for m in incoming:
        assert m.id is not None
        if isinstance(m, RemoveMessage):
            if m.id not in live and m.id not in pending:
                raise ValueError(f"Attempting to delete a message with an ID that doesn't exist ('{m.id}')")
            removed.add(m.id)
            continue
        removed.discard(m.id)
        if m.id not in pending:
            order.append(m.id)
        pending[m.id] = m

    next_seq = await _next_seq(session, thread_id)
    written: list[BaseMessage] = []
    for message_id in order:
        if message_id in removed:
            if message_id in live:
                live[message_id].evicted_at = now
            continue  # added and removed in one batch: never live
        msg = pending[message_id]
        rec, blobs = encode(msg)
        await _store_blobs(session, blobs)
        data = json.dumps(rec, ensure_ascii=False)
        if message_id in live:
            row = live[message_id]
            row.data, row.role = data, rec["role"]
        else:
            session.add(ThreadMessage(thread_id=thread_id, seq=next_seq, message_id=message_id,
                                      role=rec["role"], data=data, created_at=now))
            next_seq += 1
        written.append(msg)
    for message_id in removed - set(order):
        live[message_id].evicted_at = now
    await session.commit()
    return written


async def set_todos(session: AsyncSession, thread_id: str, todos: list[Any] | None) -> None:
    state = await session.get(ThreadState, thread_id)
    value = None if todos is None else json.dumps(todos, ensure_ascii=False)
    if state is None:
        session.add(ThreadState(thread_id=thread_id, todos=value))
    else:
        state.todos = value
    await session.commit()


async def delete_thread(session: AsyncSession, thread_id: str) -> None:
    """Every row of the thread, and each of its blobs no other thread uses.
    Commits."""
    datas = (await session.execute(
        select(ThreadMessage.data).where(ThreadMessage.thread_id == thread_id)
    )).scalars().all()
    refs = {ref for data in datas for ref in _blob_refs(json.loads(data).get("content"))}
    await session.execute(delete(ThreadMessage).where(ThreadMessage.thread_id == thread_id))
    await session.execute(delete(ThreadState).where(ThreadState.thread_id == thread_id))
    for ref in refs:
        still_used = await session.scalar(select(ThreadMessage.id).where(ThreadMessage.data.contains(ref)).limit(1))
        if still_used is None:
            await session.execute(delete(TranscriptBlob).where(TranscriptBlob.hash == ref))
    await session.commit()


async def _next_seq(session: AsyncSession, thread_id: str) -> int:
    top = await session.scalar(select(func.max(ThreadMessage.seq)).where(ThreadMessage.thread_id == thread_id))
    return 0 if top is None else top + 1


async def _store_blobs(session: AsyncSession, blobs: Iterable[Blob]) -> None:
    for blob in blobs:
        await session.execute(
            sqlite_insert(TranscriptBlob)
            .values(hash=blob.hash, mime_type=blob.mime_type, size=len(blob.data), data=blob.data,
                    created_at=_now())
            .on_conflict_do_nothing(index_elements=["hash"])
        )


def _blob_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        ref = value.get("blob")
        if isinstance(ref, str):
            yield ref
        for v in value.values():
            yield from _blob_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _blob_refs(v)


async def _blobs_for(session: AsyncSession, records: list[dict[str, Any]]) -> dict[str, bytes]:
    refs = {ref for rec in records for ref in _blob_refs(rec.get("content"))}
    if not refs:
        return {}
    rows = await session.execute(select(TranscriptBlob.hash, TranscriptBlob.data).where(TranscriptBlob.hash.in_(refs)))
    return {h: d for h, d in rows.tuples()}


# ── the key-value store ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class KvEntry:
    """What `KvStore.aget` returns — the slice of LangGraph's `Item` in use."""

    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class KvStore:
    """`aget` / `aput` / `adelete` over `kv_store`, each in its own session —
    a drop-in for the LangGraph store where jarvis uses one."""

    def __init__(self, session_factory: Any = None):
        if session_factory is None:
            from db import async_session as session_factory  # noqa: PLC0415
        self._sessions = session_factory

    async def aget(self, namespace: tuple[str, ...], key: str) -> KvEntry | None:
        async with self._sessions() as s:
            row = await s.get(KvItem, (_ns(namespace), key))
            if row is None:
                return None
            return KvEntry(tuple(namespace), key, json.loads(row.value), row.created_at, row.updated_at)

    async def aput(self, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None:
        async with self._sessions() as s:
            row = await s.get(KvItem, (_ns(namespace), key))
            if row is None:
                s.add(KvItem(namespace=_ns(namespace), key=key, value=json.dumps(value, ensure_ascii=False)))
            else:
                row.value = json.dumps(value, ensure_ascii=False)
            await s.commit()

    async def adelete(self, namespace: tuple[str, ...], key: str) -> None:
        async with self._sessions() as s:
            await s.execute(delete(KvItem).where(KvItem.namespace == _ns(namespace), KvItem.key == key))
            await s.commit()


def _ns(namespace: tuple[str, ...]) -> str:
    return ".".join(namespace)


# ── converting what LangGraph left ───────────────────────────────────────────


async def import_checkpoint(session: AsyncSession, checkpointer: Any, thread_id: str) -> bool:
    """Write a LangGraph thread's latest state as transcript rows, once.

    The newest root checkpoint's `messages` and `todos`, with its pending
    writes applied — what a run resuming the thread would have started from.
    Does nothing (returns False) for a thread already converted, or that has
    rows of its own already. Commits.
    """
    if (await load_thread(session, thread_id)).exists:
        return False
    tup = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    if tup is None:
        return False
    from langgraph.graph.message import add_messages  # noqa: PLC0415 — only while converting

    values = tup.checkpoint.get("channel_values") or {}
    messages: list[BaseMessage] = list(values.get("messages") or [])
    todos = values.get("todos")
    for _task, channel, value in tup.pending_writes or []:
        if channel == "messages":
            messages = cast(list[BaseMessage], add_messages(cast(Any, messages), value))
        elif channel == "todos":
            todos = value
    # Ids for messages that had none, stable across a retried conversion.
    for i, m in enumerate(messages):
        if m.id is None:
            m.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis-thread:{thread_id}:{i}"))

    now = _now()
    for seq, msg in enumerate(messages):
        rec, blobs = encode(msg)
        await _store_blobs(session, blobs)
        session.add(ThreadMessage(thread_id=thread_id, seq=seq, message_id=msg.id, role=rec["role"],
                                  data=json.dumps(rec, ensure_ascii=False), created_at=now))
    session.add(ThreadState(thread_id=thread_id, source="checkpoint",
                            todos=None if todos is None else json.dumps(todos, ensure_ascii=False)))
    await session.commit()
    return True


async def import_store(session: AsyncSession, checkpoints_db: str) -> int:
    """Copy the LangGraph store's items into `kv_store`, keeping any key that
    is already there. Returns how many were copied. Commits."""
    import sqlite3  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    conn = sqlite3.connect(f"{Path(checkpoints_db).resolve().as_uri()}?mode=ro", uri=True)
    try:
        has_store = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store'").fetchone()
        rows = conn.execute("SELECT prefix, key, value FROM store").fetchall() if has_store else []
    finally:
        conn.close()
    copied = 0
    for prefix, key, value in rows:
        result = await session.execute(
            sqlite_insert(KvItem)
            .values(namespace=prefix, key=key, value=value, created_at=_now(), updated_at=_now())
            .on_conflict_do_nothing(index_elements=["namespace", "key"])
        )
        copied += result.rowcount or 0  # type: ignore[attr-defined]
    await session.commit()
    return copied
