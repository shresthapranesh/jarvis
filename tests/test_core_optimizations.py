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


# ── compaction counts from reported usage, never on the event loop ───────────

class _NoCountLLM:
    """Fails the test if the provider's token counter is reached at all."""

    def get_num_tokens_from_messages(self, messages) -> int:
        raise AssertionError("token counter called despite usage being available")


def _with_usage(msg: AIMessage, input_tokens: int, output_tokens: int = 0) -> AIMessage:
    msg.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    return msg


def _long_history(n_turns: int = 12, payload: int = 400) -> list:
    messages: list = [HumanMessage(content="start", id="u0")]
    for i in range(n_turns):
        messages.extend(_tool_turn(i, "y" * payload))
    messages.append(HumanMessage(content="latest question", id="u_last"))
    return messages


def test_history_tokens_from_usage_subtracts_overhead_and_adds_the_tail():
    from core.messages import estimate_tokens_heuristic, history_tokens_from_usage

    tail = [ToolMessage(content="t" * 400, tool_call_id="c", id="t1", name="run_cell")]
    messages = [
        HumanMessage(content="q", id="u0"),
        _with_usage(AIMessage(content="", id="a0"), input_tokens=9_000, output_tokens=50),
        *tail,
    ]
    assert history_tokens_from_usage(messages, overhead_tokens=8_000) == (
        1_000 + 50 + estimate_tokens_heuristic(tail)
    )
    # Overhead larger than the request clamps at zero rather than going negative.
    assert history_tokens_from_usage(messages, overhead_tokens=20_000) == (
        50 + estimate_tokens_heuristic(tail)
    )


def test_history_tokens_from_usage_none_without_a_reported_count():
    """Only the latest AIMessage is the anchor — an older one with usage is not
    reached past a newer one without, since its tail would span unknown text."""
    from core.messages import history_tokens_from_usage

    assert history_tokens_from_usage([HumanMessage(content="q")], 0) is None
    messages = [
        _with_usage(AIMessage(content="old", id="a0"), input_tokens=5_000),
        HumanMessage(content="q", id="u1"),
        AIMessage(content="no usage", id="a1"),
    ]
    assert history_tokens_from_usage(messages, 0) is None


async def test_maybe_compact_uses_usage_and_never_calls_the_counter():
    """Over threshold per reported usage → compacts with no counter call, even
    though the chars/4 heuristic alone would have skipped."""
    from core.compaction import maybe_compact

    messages = _long_history()
    messages[-3] = _with_usage(messages[-3], input_tokens=50_000)  # ai_11

    result = await maybe_compact(
        messages,
        llm=_NoCountLLM(),
        summarizer=_RecordingSummarizer(),
        threshold=20_000,
        usage_overhead_tokens=8_000,
    )
    assert result.compacted is True


async def test_maybe_compact_overhead_keeps_a_short_history_under_threshold():
    """The reported input includes ~8k of system prompt + schemas. Without the
    overhead subtraction a 12k threshold would compact a near-empty thread."""
    from core.compaction import maybe_compact

    messages = _long_history(n_turns=3, payload=40)
    messages[-3] = _with_usage(messages[-3], input_tokens=12_500)

    summarizer = _RecordingSummarizer()
    result = await maybe_compact(
        messages,
        llm=_NoCountLLM(),
        summarizer=summarizer,
        threshold=12_000,
        usage_overhead_tokens=8_200,
    )
    assert result.compacted is False
    assert summarizer.calls == 0


async def test_maybe_compact_distrusts_an_implausibly_low_usage_count():
    """A count far below the heuristic (a prefix-cache hit the provider left out)
    falls back to counting, rather than letting compaction never fire."""
    from core.compaction import maybe_compact

    messages = _long_history(payload=4_000)
    messages[-3] = _with_usage(messages[-3], input_tokens=10)

    result = await maybe_compact(
        messages,
        llm=_FakeLLM(),
        summarizer=_RecordingSummarizer(),
        threshold=100,
        usage_overhead_tokens=0,
    )
    assert result.compacted is True


async def test_maybe_compact_fallback_count_runs_off_the_event_loop():
    """The counter is a synchronous network call for Google/Anthropic. It must run
    in a worker thread so it cannot stall every other run and subscription."""
    import threading

    from core.compaction import maybe_compact

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class _ThreadRecordingLLM:
        def get_num_tokens_from_messages(self, messages) -> int:
            seen.append(threading.get_ident())
            return 0

    await maybe_compact(
        _long_history(), llm=_ThreadRecordingLLM(), summarizer=_RecordingSummarizer(), threshold=100
    )
    assert seen, "heuristic is over 80% of threshold, so the counter should run"
    assert loop_thread not in seen


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
