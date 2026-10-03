"""Threads in the transcript tables (`core/transcript_store.py`).

The merge rules are LangGraph's `add_messages`, and are checked against it:
the same updates, folded by both, give the same history.
"""

from __future__ import annotations

import base64
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
    import_checkpoint,
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


async def test_import_from_langgraph(database, tmp_path, work_dir):
    """A LangGraph thread converted once, as a run resuming it would see it."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.store.sqlite.aio import AsyncSqliteStore

    from db import async_session

    class State(MessagesState):
        todos: list

    reply = AIMessage(content="", tool_calls=[{"id": "c1", "name": "run_cell", "args": {"code": "1"}}],
                      response_metadata={"model_provider": "google_genai", "model_name": "gemini-2.5-pro"})

    def node(state):
        return {"messages": [reply, ToolMessage(content="1", tool_call_id="c1")],
                "todos": [{"text": "x", "status": "done"}]}

    graph = StateGraph(State)  # type: ignore[bad-specialization]
    graph.add_node("n", node)
    graph.add_edge(START, "n")
    db = str(tmp_path / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await graph.compile(checkpointer=saver).ainvoke(
            {"messages": [_image("look")]}, {"configurable": {"thread_id": "conv-1"}},
        )
        tup = await saver.aget_tuple({"configurable": {"thread_id": "conv-1"}})
        assert tup is not None
        expected = tup.checkpoint["channel_values"]
        async with async_session() as s:
            assert await import_checkpoint(s, saver, "conv-1") is True
            assert await import_checkpoint(s, saver, "conv-1") is False  # once
            assert await import_checkpoint(s, saver, "never-ran") is False
            thread = await load_thread(s, "conv-1")
    assert thread.messages == expected["messages"]
    assert thread.todos == [{"text": "x", "status": "done"}]
    assert thread.source == "checkpoint"

    async with AsyncSqliteStore.from_conn_string(db) as store:
        await store.setup()
        await store.aput(("memory",), "AGENTS.md", {"content": "blob"})
        await store.aput(("memory_consolidation",), "state", {"watermark": "m9"})
    await KvStore().aput(("memory",), "AGENTS.md", {"content": "already here"})
    async with async_session() as s:
        assert await import_store(s, db) == 1
    assert await _kv(("memory",), "AGENTS.md") == {"content": "already here"}
    assert await _kv(("memory_consolidation",), "state") == {"watermark": "m9"}
    assert json.loads(_rows(work_dir, "SELECT value FROM kv_store WHERE namespace = 'memory_consolidation'")[0][0])
