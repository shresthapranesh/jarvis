"""Regression tests for the core hot-path optimizations.

Each of these guards a specific behavior that the optimization could plausibly
have broken, not the speedup itself:

- `cosine_ranking` must rank identically to the per-row loop it replaced, and
  keep that loop's tolerance for rows embedded by a different model.
- `maybe_compact` must hand back the leaned view on every path, since the caller
  no longer recomputes it.
- `add_steps` must persist the same rows the per-row `add_step` loop did.
- Background indexing must not let a reader observe an unfinished index.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# Conversation.model is required by create_conversation, but nothing in these
# tests builds an agent or resolves a model — no LLM is ever constructed.
_UNUSED_MODEL = "unused-in-this-test"


# ── cosine_ranking (vectorized dense arm) ────────────────────────────────────

def _cosine_reference(qvec: np.ndarray, blob: bytes) -> float | None:
    """The per-row implementation this replaced, kept as the oracle."""
    qnorm = float(np.linalg.norm(qvec)) or 1.0
    vec = np.frombuffer(blob, dtype=np.float32)
    if vec.shape != qvec.shape:
        return None
    return float(np.dot(vec, qvec) / ((float(np.linalg.norm(vec)) or 1.0) * qnorm))


def _blob(*values: float) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


def test_cosine_ranking_matches_per_row_scores_and_order():
    from core.retrieval import cosine_ranking

    rng = np.random.default_rng(1234)
    qvec = rng.standard_normal(16).astype(np.float32)
    items = [(f"m{i}", rng.standard_normal(16).astype(np.float32).tobytes()) for i in range(50)]

    ranked = cosine_ranking(qvec, items)

    assert len(ranked) == len(items)
    expected = {i: _cosine_reference(qvec, b) for i, b in items}
    for item_id, score in ranked:
        assert score == pytest.approx(expected[item_id], abs=1e-6)
    # Best first, same as the old `sort(reverse=True)`.
    assert [s for _, s in ranked] == sorted((s for _, s in ranked), reverse=True)


def test_cosine_ranking_skips_missing_and_mismatched_vectors():
    from core.retrieval import cosine_ranking

    qvec = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
    ranked = cosine_ranking(qvec, [
        ("aligned", _blob(1.0, 0.0, 0.0)),
        ("no-embedding", None),
        ("wrong-model", _blob(1.0, 0.0)),       # different dimensionality
        ("orthogonal", _blob(0.0, 1.0, 0.0)),
    ])

    assert [i for i, _ in ranked] == ["aligned", "orthogonal"]
    assert ranked[0][1] == pytest.approx(1.0)
    assert ranked[1][1] == pytest.approx(0.0)


def test_cosine_ranking_zero_vector_does_not_produce_nan():
    """A zero-norm row would divide to nan and scramble the sort."""
    from core.retrieval import cosine_ranking

    qvec = np.asarray([1.0, 0.0], dtype=np.float32)
    ranked = cosine_ranking(qvec, [("zero", _blob(0.0, 0.0)), ("real", _blob(1.0, 0.0))])

    scores = dict(ranked)
    assert not np.isnan(scores["zero"])
    assert scores["zero"] == pytest.approx(0.0)
    assert ranked[0][0] == "real"


def test_cosine_ranking_empty_input():
    from core.retrieval import cosine_ranking

    qvec = np.asarray([1.0, 0.0], dtype=np.float32)
    assert cosine_ranking(qvec, []) == []
    assert cosine_ranking(qvec, [("none", None)]) == []


# ── maybe_compact always returns the leaned view ─────────────────────────────

class _FakeLLM:
    """Stands in for the counting LLM; heuristic gating never reaches it here."""

    def get_num_tokens_from_messages(self, messages) -> int:
        return sum(len(str(getattr(m, "content", ""))) for m in messages) // 4


class _RecordingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content="summary text")


def _tool_turn(idx: int, payload: str) -> list:
    call_id = f"call_{idx}"
    return [
        AIMessage(
            content="",
            id=f"ai_{idx}",
            tool_calls=[{"name": "run_cell", "args": {"code": "x"}, "id": call_id}],
        ),
        ToolMessage(content=payload, tool_call_id=call_id, id=f"tool_{idx}", name="run_cell"),
    ]


async def test_maybe_compact_returns_leaned_messages_when_under_threshold():
    """The caller uses `.messages` directly now — it must be the compacted view,
    equal to what a separate apply_per_call_compaction call would have produced."""
    from core.compaction import apply_per_call_compaction, maybe_compact

    messages: list = [HumanMessage(content="hello", id="u0")]
    for i in range(8):
        messages.extend(_tool_turn(i, f"result {i} " + "x" * 200))
    messages.append(HumanMessage(content="follow up", id="u1"))

    summarizer = _RecordingSummarizer()
    result = await maybe_compact(messages, llm=_FakeLLM(), summarizer=summarizer)

    assert result.compacted is False
    assert result.state_update == []
    assert summarizer.calls == 0
    expected = apply_per_call_compaction(messages)
    assert [type(m) for m in result.messages] == [type(m) for m in expected]
    assert [str(m.content) for m in result.messages] == [str(m.content) for m in expected]
    # Old tool groups really were collapsed — this is not just the raw list.
    assert len(result.messages) < len(messages)


async def test_maybe_compact_empty_history():
    from core.compaction import maybe_compact

    result = await maybe_compact([], llm=_FakeLLM(), summarizer=_RecordingSummarizer())
    assert result.messages == []
    assert result.compacted is False


async def test_maybe_compact_summarizes_and_reports_state_update():
    """Past the threshold it must summarize, report compacted=True, and return
    messages that are already per-call compacted (the caller won't do it)."""
    from core.compaction import maybe_compact

    messages: list = [HumanMessage(content="start", id="u0")]
    for i in range(12):
        messages.extend(_tool_turn(i, "y" * 400))
    messages.append(HumanMessage(content="latest question", id="u_last"))

    summarizer = _RecordingSummarizer()
    result = await maybe_compact(
        messages, llm=_FakeLLM(), summarizer=summarizer, threshold=100
    )

    assert result.compacted is True
    assert summarizer.calls >= 1
    assert any(isinstance(m, SystemMessage) for m in result.messages)
    assert result.state_update, "expected RemoveMessage deltas + the summary"
    # The pinned latest user turn survives eviction.
    assert any(
        isinstance(m, HumanMessage) and "latest question" in str(m.content)
        for m in result.messages
    )


async def test_maybe_compact_summarizer_failure_falls_back_to_leaned_view():
    """A failing summarizer must not strand the caller without messages."""
    from core.compaction import maybe_compact

    class _Boom:
        async def ainvoke(self, messages):
            raise RuntimeError("summarizer down")

    messages: list = [HumanMessage(content="start", id="u0")]
    for i in range(12):
        messages.extend(_tool_turn(i, "z" * 400))
    messages.append(HumanMessage(content="latest", id="u_last"))

    result = await maybe_compact(messages, llm=_FakeLLM(), summarizer=_Boom(), threshold=100)

    assert result.compacted is False
    assert result.state_update == []
    assert result.messages, "must still return a usable history"


# ── batched step persistence ─────────────────────────────────────────────────

async def _steps_for(message_id: str) -> list:
    from sqlalchemy import select

    from db.engine import async_session
    from db.models import Step

    async with async_session() as session:
        return list((await session.execute(
            select(Step).where(Step.message_id == message_id).order_by(Step.seq)
        )).scalars())


async def test_add_steps_persists_every_row(database):
    from db.engine import async_session
    from db.ops import add_message, add_steps, create_conversation

    async with async_session() as session:
        conv = await create_conversation(session, model=_UNUSED_MODEL, title="steps")
        msg = await add_message(session, conv.id, "assistant", "")
        await add_steps(session, msg.id, conv.id, [
            ("model_request", "main", '{"a": 1}', 0, None),
            ("tools", "main", '{"b": 2}', 1, None),
            ("worker_done", "subagent", '{"c": 3}', 2, "researcher:0"),
        ])

    steps = await _steps_for(msg.id)

    assert [s.seq for s in steps] == [0, 1, 2]
    assert [s.node for s in steps] == ["model_request", "tools", "worker_done"]
    assert [s.source for s in steps] == ["main", "main", "subagent"]
    assert [s.data for s in steps] == ['{"a": 1}', '{"b": 2}', '{"c": 3}']
    assert steps[2].subagent == "researcher:0"


async def test_add_steps_empty_is_a_noop(database):
    from db.engine import async_session
    from db.ops import add_message, add_steps, create_conversation

    async with async_session() as session:
        conv = await create_conversation(session, model=_UNUSED_MODEL, title="steps")
        msg = await add_message(session, conv.id, "assistant", "")
        await add_steps(session, msg.id, conv.id, [])

    assert await _steps_for(msg.id) == []


# ── background indexing readiness ────────────────────────────────────────────

async def _make_document(conv_title: str = "docs"):
    from db.engine import async_session
    from db.models import Document
    from db.ops import create_conversation
    from uuid import uuid4

    async with async_session() as session:
        conv = await create_conversation(session, model=_UNUSED_MODEL, title=conv_title)
        doc = Document(
            id=str(uuid4()),
            conversation_id=conv.id,
            filename="big.pdf",
            mime_type="application/pdf",
            size=1,
            path="/dev/null",
        )
        session.add(doc)
        await session.commit()
    return conv.id, doc.id


async def test_await_index_ready_waits_for_the_in_flight_task(database, monkeypatch):
    """The whole point of the readiness signal: a reader must not observe the
    document before indexing settles, or an empty search reads as 'not found'."""
    from core import doc_index

    conv_id, doc_id = await _make_document()
    released = asyncio.Event()
    observed_status_midway: list[str | None] = []

    async def _slow_index(document_id: str, text: str) -> int:
        observed_status_midway.append(await doc_index._read_index_status(document_id))
        await released.wait()
        await doc_index._set_index_status(document_id, doc_index.INDEX_INDEXED)
        return 3

    monkeypatch.setattr(doc_index, "index_document", _slow_index)

    task = doc_index.start_indexing(doc_id, "text")
    waiter = asyncio.create_task(doc_index.await_index_ready(doc_id, timeout=5))
    await asyncio.sleep(0.05)

    assert not waiter.done(), "reader returned while indexing was still running"
    released.set()
    assert await waiter == doc_index.INDEX_INDEXED
    await task
    # The status was visible as 'pending' to other processes during the run.
    assert observed_status_midway == [doc_index.INDEX_PENDING]


async def test_start_indexing_records_failure(database, monkeypatch):
    """Background indexing can no longer fall back to inlining, so a failure has
    to be recorded where the retrieval tools can report it."""
    from core import doc_index

    conv_id, doc_id = await _make_document()

    async def _boom(document_id: str, text: str) -> int:
        raise RuntimeError("embedding endpoint down")

    monkeypatch.setattr(doc_index, "index_document", _boom)

    task = doc_index.start_indexing(doc_id, "text")
    with pytest.raises(RuntimeError):
        await task

    assert await doc_index.await_index_ready(doc_id, timeout=5) == doc_index.INDEX_FAILED


async def test_start_indexing_is_idempotent_while_running(database, monkeypatch):
    from core import doc_index

    conv_id, doc_id = await _make_document()
    released = asyncio.Event()
    starts = 0

    async def _slow_index(document_id: str, text: str) -> int:
        nonlocal starts
        starts += 1
        await released.wait()
        return 1

    monkeypatch.setattr(doc_index, "index_document", _slow_index)

    first = doc_index.start_indexing(doc_id, "text")
    second = doc_index.start_indexing(doc_id, "text")
    assert first is second

    released.set()
    await first
    assert starts == 1


async def test_await_conversation_indexes_catches_a_just_started_index(database, monkeypatch):
    """start_indexing registers its task synchronously but writes the 'pending'
    flag from inside it, so a DB-only filter can miss an index that started
    microseconds ago — exactly the window a fast first search would hit."""
    from core import doc_index

    conv_id, doc_id = await _make_document()
    released = asyncio.Event()

    async def _slow_index(document_id: str, text: str) -> int:
        await released.wait()
        await doc_index._set_index_status(document_id, doc_index.INDEX_INDEXED)
        return 1

    monkeypatch.setattr(doc_index, "index_document", _slow_index)

    task = doc_index.start_indexing(doc_id, "text")
    # Deliberately no sleep: the task has not run, so index_status is still NULL.
    assert await doc_index._read_index_status(doc_id) is None

    waiter = asyncio.create_task(doc_index.await_conversation_indexes(conv_id, timeout=5))
    await asyncio.sleep(0.05)
    assert not waiter.done(), "search would have run against an unindexed document"

    released.set()
    await waiter
    await task


async def test_await_conversation_indexes_covers_every_pending_document(database, monkeypatch):
    """search_documents is conversation-scoped, so one pending attachment
    anywhere in the conversation has to hold it up."""
    from core import doc_index
    from db.engine import async_session
    from db.models import Document
    from uuid import uuid4

    conv_id, doc_a = await _make_document()
    async with async_session() as session:
        doc_b_row = Document(
            id=str(uuid4()),
            conversation_id=conv_id,
            filename="second.pdf",
            mime_type="application/pdf",
            size=1,
            path="/dev/null",
        )
        session.add(doc_b_row)
        await session.commit()
        doc_b = doc_b_row.id

    released = asyncio.Event()

    async def _slow_index(document_id: str, text: str) -> int:
        await released.wait()
        await doc_index._set_index_status(document_id, doc_index.INDEX_INDEXED)
        return 1

    monkeypatch.setattr(doc_index, "index_document", _slow_index)
    tasks = [doc_index.start_indexing(doc_a, "a"), doc_index.start_indexing(doc_b, "b")]
    # Let both mark themselves pending before we snapshot the conversation.
    await asyncio.sleep(0.05)

    waiter = asyncio.create_task(doc_index.await_conversation_indexes(conv_id, timeout=5))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    released.set()
    await waiter
    await asyncio.gather(*tasks)

    for doc_id in (doc_a, doc_b):
        assert await doc_index._read_index_status(doc_id) == doc_index.INDEX_INDEXED


# ── typed cache segments ─────────────────────────────────────────────────────

def test_cache_segments_sort_most_stable_first():
    """Ordering is now a rank lookup on the producer's tag rather than sniffing
    heading text, so renaming a heading can't silently move a block."""
    from core.agents import _SEGMENT_STABILITY, _SEGMENT_STABILITY_DEFAULT
    from core.context_cache import CacheSegment

    segments = [
        CacheSegment(name="skills", content="s" * 100),
        CacheSegment(name="core_memory", content="c" * 100),
        CacheSegment(name="mystery", content="m" * 100),
        CacheSegment(name="project_header", content="p" * 100),
        CacheSegment(name="memory_howto", content="h" * 100),
    ]
    ordered = sorted(
        segments,
        key=lambda s: _SEGMENT_STABILITY.get(s.name, _SEGMENT_STABILITY_DEFAULT),
    )
    assert [s.name for s in ordered] == [
        "memory_howto", "core_memory", "project_header", "skills", "mystery",
    ]


async def test_project_segments_keep_memory_out_of_the_cached_prefix(database):
    """Project memory is edited mid-turn by the agent's own tool, so it must stay
    uncached — caching it would delay its own writes by a full turn."""
    from core.agents import _project_volatile_parts
    from db.engine import async_session
    from db.ops import create_project

    async with async_session() as session:
        project = await create_project(
            session, name="Jarvis", instructions="Be terse.", description="d"
        )

    segments = await _project_volatile_parts(project.id)
    by_name = {s.name: s for s in segments}

    assert by_name["project_header"].cacheable is True
    assert by_name["project_instructions"].cacheable is True
    assert by_name["project_memory"].cacheable is False


async def test_memory_segments_tag_relevant_memories_volatile(monkeypatch):
    """`## Relevant Memories` is re-ranked per query; caching it would bust the
    stable prefix on every turn."""
    from core import agents

    monkeypatch.setattr(agents, "embeddings_available", lambda: True)
    monkeypatch.setattr(agents, "load_core", lambda: _async_value("core fact"))

    async def _search(query, k=6):
        return [{"id": "m1", "text": "a retrieved fact", "score": 0.9}]

    monkeypatch.setattr(agents, "search_memory", _search)

    segments = await agents._memory_volatile_parts(None, "what did we decide about caching")
    by_name = {s.name: s for s in segments}

    assert by_name["memory_howto"].cacheable is True
    assert by_name["core_memory"].cacheable is True
    assert by_name["relevant_memories"].cacheable is False


async def _async_value(value):
    return value
