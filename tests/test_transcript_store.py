"""Threads in the transcript tables (`core/transcript_store.py`).

The merge rules are LangGraph's `add_messages`, and are checked against it:
the same updates, folded by both, give the same history.
"""

from __future__ import annotations

import base64
import contextlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages

from core.transcript_store import (
    REMOVE_ALL_MESSAGES,
    KvStore,
    apply_messages,
    delete_thread,
    import_store,
    load_thread,
    merge_messages,
    set_todos,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))


def _image(text: str) -> HumanMessage:
    url = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    return HumanMessage(content=[{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}])


async def _history(thread_id: str):
    from db import async_session

    async with async_session() as s:
        return (await load_thread(s, thread_id)).messages


async def _apply(thread_id: str, updates):
    from db import async_session

    async with async_session() as s:
        return await apply_messages(s, thread_id, updates)


def _rows(work_dir: Path, sql: str, *args):
    with sqlite3.connect(work_dir / "database.db") as conn:
        return conn.execute(sql, args).fetchall()


async def test_appends_in_order_and_assigns_ids(database):
    written = await _apply("t1", [HumanMessage(content="hi"), AIMessage(content="hello", id="a1")])
    assert written[0].id and written[1].id == "a1"
    history = await _history("t1")
    assert [m.content for m in history] == ["hi", "hello"]
    assert history[0].id == written[0].id
    # Dict-shaped messages are accepted, as add_messages accepted them.
    await _apply("t1", [{"role": "user", "content": "more"}])
    assert [type(m).__name__ for m in await _history("t1")] == ["HumanMessage", "AIMessage", "HumanMessage"]


async def test_same_id_replaces_in_place(database):
    await _apply("t", [HumanMessage(content="q", id="u"), AIMessage(content="draft", id="a"), HumanMessage(content="next", id="n")])
    await _apply("t", [AIMessage(content="final", id="a")])
    assert [m.content for m in await _history("t")] == ["q", "final", "next"]


async def test_compaction_evicts_and_keeps_the_rows(database, work_dir):
    await _apply("t", [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(4)])
    # What maybe_compact's state_update looks like: removals, then the summary.
    await _apply("t", [RemoveMessage(id="m0"), RemoveMessage(id="m1"),
                       SystemMessage(content="[Conversation summary]\nm0, m1", id="s")])
    history = await _history("t")
    assert [m.id for m in history] == ["m2", "m3", "s"]
    evicted = _rows(work_dir, "SELECT message_id FROM thread_messages WHERE thread_id = 't' AND evicted_at IS NOT NULL ORDER BY seq")
    assert evicted == [("m0",), ("m1",)]


async def test_removing_an_unknown_message_writes_nothing(database):
    await _apply("t", [HumanMessage(content="a", id="a")])
    with pytest.raises(ValueError, match="doesn't exist"):
        await _apply("t", [HumanMessage(content="b", id="b"), RemoveMessage(id="nope")])
    assert [m.id for m in await _history("t")] == ["a"]


async def test_matches_add_messages(database):
    """Random batches of appends, replacements and removals, folded by all
    three: LangGraph, the rows, and the in-memory merge."""
    rng = random.Random(7)
    expected: list[Any] = []
    in_memory: list[Any] = []
    ids = [f"id{i}" for i in range(12)]
    for step in range(40):
        live = [m.id for m in expected]
        batch: list[Any] = []
        for _ in range(rng.randint(1, 4)):
            roll = rng.random()
            if roll < 0.15 and live:
                batch.append(RemoveMessage(id=rng.choice(live)))
            elif roll < 0.2 and step % 10 == 9:
                batch.append(RemoveMessage(id=REMOVE_ALL_MESSAGES))
            else:
                batch.append(rng.choice([HumanMessage, AIMessage])(content=f"s{step}", id=rng.choice(ids)))
        try:
            folded = cast(list[Any], add_messages(expected, batch))
        except ValueError:
            with pytest.raises(ValueError):
                await _apply("t", batch)
            with pytest.raises(ValueError):
                merge_messages(in_memory, batch)
            continue
        expected = folded
        await _apply("t", batch)
        in_memory = merge_messages(in_memory, batch)
        assert await _history("t") == expected, f"step {step}: {batch}"
        assert in_memory == expected, f"step {step}: {batch}"


async def test_blobs_are_shared_and_cleaned_up(database, work_dir):
    await _apply("a", [_image("one"), _image("two")])
    await _apply("b", [_image("three")])
    assert _rows(work_dir, "SELECT count(*) FROM transcript_blobs") == [(1,)]
    history = await _history("a")
    assert cast(list[Any], history[1].content)[1]["image_url"]["url"].endswith(base64.b64encode(PNG).decode())
    # Image bytes are in the blob table, not the message rows.
    assert all(base64.b64encode(PNG).decode() not in d for (d,) in _rows(work_dir, "SELECT data FROM thread_messages"))

    from db import async_session

    async with async_session() as s:
        await delete_thread(s, "a")
    assert _rows(work_dir, "SELECT count(*) FROM transcript_blobs") == [(1,)]  # "b" still uses it
    async with async_session() as s:
        await delete_thread(s, "b")
    assert _rows(work_dir, "SELECT count(*) FROM transcript_blobs") == [(0,)]
    assert _rows(work_dir, "SELECT count(*) FROM thread_messages") == [(0,)]


async def test_todos(database):
    from db import async_session

    async with async_session() as s:
        assert (await load_thread(s, "t")).exists is False
        await set_todos(s, "t", [{"text": "plan", "status": "in_progress"}])
        thread = await load_thread(s, "t")
        assert thread.exists and thread.todos == [{"text": "plan", "status": "in_progress"}]
        await set_todos(s, "t", [])
        assert (await load_thread(s, "t")).todos == []


async def test_deleting_a_conversation_deletes_its_thread(database, work_dir):
    from db import async_session
    from db.models import Conversation
    from db.ops import delete_conversation

    async with async_session() as s:
        s.add(Conversation(id="c1", model="m"))
        await s.commit()
    await _apply("c1", [HumanMessage(content="hi")])
    async with async_session() as s:
        await delete_conversation(s, "c1")
    assert _rows(work_dir, "SELECT count(*) FROM thread_messages") == [(0,)]


async def _kv(namespace: tuple[str, ...], key: str) -> Any:
    item = await KvStore().aget(namespace, key)
    return None if item is None else item.value


async def test_kv_store(database):
    store = KvStore()
    assert await store.aget(("memory",), "AGENTS.md") is None
    await store.aput(("memory",), "AGENTS.md", {"content": "likes tea"})
    await store.aput(("user_state", "u.1"), "state", {"n": 1})
    assert await _kv(("memory",), "AGENTS.md") == {"content": "likes tea"}
    await store.aput(("memory",), "AGENTS.md", {"content": "likes coffee"})
    assert await _kv(("memory",), "AGENTS.md") == {"content": "likes coffee"}
    assert await _kv(("user_state", "u.1"), "state") == {"n": 1}
    await store.adelete(("memory",), "AGENTS.md")
    assert await store.aget(("memory",), "AGENTS.md") is None


def _langgraph_store(path: str, *items: tuple[str, str, dict]) -> None:
    """A `checkpoints.db` holding LangGraph's store table, as its saver left it."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS store (prefix text NOT NULL, key text NOT NULL, value text NOT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "expires_at TIMESTAMP, ttl_minutes REAL, PRIMARY KEY (prefix, key))"
        )
        conn.executemany("INSERT INTO store (prefix, key, value) VALUES (?, ?, ?)",
                         [(prefix, key, json.dumps(value)) for prefix, key, value in items])
        conn.commit()


async def test_import_store_keeps_what_is_already_there(database, tmp_path, work_dir):
    from db import async_session

    db = str(tmp_path / "checkpoints.db")
    _langgraph_store(db, ("memory", "AGENTS.md", {"content": "blob"}),
                     ("memory_consolidation", "state", {"watermark": "m9"}))
    await KvStore().aput(("memory",), "AGENTS.md", {"content": "already here"})
    async with async_session() as s:
        assert await import_store(s, db) == 1
    assert await _kv(("memory",), "AGENTS.md") == {"content": "already here"}
    assert await _kv(("memory_consolidation",), "state") == {"watermark": "m9"}
    assert json.loads(_rows(work_dir, "SELECT value FROM kv_store WHERE namespace = 'memory_consolidation'")[0][0])


async def test_the_langgraph_store_is_imported_once(database, tmp_path):
    """After the first import `kv_store` is the store: a key deleted from it
    must not come back from checkpoints.db on the next start."""
    from core.transcript_store import import_store_once
    from db import async_session

    db = str(tmp_path / "checkpoints.db")
    async with async_session() as s:
        assert await import_store_once(s, str(tmp_path / "none.db")) == 0  # nothing to import, still marked
        assert await import_store_once(s, db) is None
    await KvStore().adelete(("jarvis", "migrations"), "langgraph_store")

    _langgraph_store(db, ("memory", "AGENTS.md", {"content": "blob"}))
    async with async_session() as s:
        assert await import_store_once(s, db) == 1
    item = await KvStore().aget(("memory",), "AGENTS.md")
    assert item is not None and item.value == {"content": "blob"}
    assert item.updated_at.year > 2000  # LangGraph's own stamp, carried over

    await KvStore().adelete(("memory",), "AGENTS.md")
    async with async_session() as s:
        assert await import_store_once(s, db) is None
    assert await KvStore().aget(("memory",), "AGENTS.md") is None
