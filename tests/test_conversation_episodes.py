"""Conversation episodes: compacted chunks kept retrievable in never-ending threads.

Real DB (including the FTS5 mirror), embeddings stubbed with fixed vectors so the
dense arm is deterministic, no LLM.
"""

from __future__ import annotations

import numpy as np
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core import episodes

_UNUSED_MODEL = "unused-in-this-test"

# Three orthogonal "topics" so cosine is 1.0 on-topic and 0.0 off-topic.
_TOPICS = {
    "postgres": np.array([1.0, 0.0, 0.0], dtype=np.float32),
    "billing": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    "weather": np.array([0.0, 0.0, 1.0], dtype=np.float32),
}


def _vec(text: str) -> np.ndarray:
    for word, vec in _TOPICS.items():
        if word in text.lower():
            return vec
    return np.array([0.3, 0.3, 0.3], dtype=np.float32)


def _stub_embeddings(monkeypatch, *, dense: bool = True):
    import core.doc_index as doc_index
    import core.memory_store as memory_store

    async def fake_store_embed(text):
        return _vec(text).tobytes() if dense else None

    async def fake_query_embed(query, allow_trivial=False):
        return _vec(query) if dense else None

    monkeypatch.setattr(memory_store, "embed_for_storage", fake_store_embed)
    monkeypatch.setattr(doc_index, "aembed_query_cached", fake_query_embed)


async def _conversation() -> str:
    from db.engine import async_session
    from db.ops import create_conversation

    async with async_session() as s:
        return (await create_conversation(s, _UNUSED_MODEL, "Long thread", surface="telegram")).id


async def _seed_three(conv_id: str) -> None:
    await episodes.record_episode(conv_id, "Chose Postgres 16 for the ledger; migration runs nightly.", ["a1", "a2"])
    await episodes.record_episode(conv_id, "Billing moves to Stripe invoices on the 1st of each month.", ["b1", "b2"])
    await episodes.record_episode(conv_id, "Small talk about the weather in Austin.", ["c1", "c2"])


# ── compaction hands the chunk summary back ──────────────────────────────────

async def test_maybe_compact_returns_the_evicted_chunk_as_an_episode():
    from core.compaction import maybe_compact

    class _Counter:
        def get_num_tokens_from_messages(self, messages):
            return sum(len(str(m.content)) for m in messages) // 4

    class _Summarizer:
        async def ainvoke(self, messages):
            return AIMessage(content="chunk summary: chose postgres")

    messages: list = [HumanMessage(content="start", id="u0")]
    for i in range(12):
        messages.append(AIMessage(content="", id=f"ai_{i}", tool_calls=[{"name": "run_cell", "args": {}, "id": f"c{i}"}]))
        messages.append(ToolMessage(content="y" * 400, tool_call_id=f"c{i}", id=f"t_{i}"))
    messages.append(HumanMessage(content="latest", id="u_last"))

    result = await maybe_compact(messages, llm=_Counter(), summarizer=_Summarizer(), threshold=100)

    assert result.compacted
    assert result.episode == "chunk summary: chose postgres"
    # The first user turn is pinned (the kept window must start with a user
    # message), so the evicted chunk is the tool traffic between the two.
    assert "ai_0" in result.evicted_ids and "t_0" in result.evicted_ids
    assert "u0" not in result.evicted_ids and "u_last" not in result.evicted_ids


# ── storage ──────────────────────────────────────────────────────────────────

async def test_record_is_idempotent_per_evicted_chunk(database, monkeypatch):
    from db.engine import async_session
    from db.ops import list_episodes

    _stub_embeddings(monkeypatch)
    conv = await _conversation()

    assert await episodes.record_episode(conv, "Chose Postgres.", ["m1", "m2"]) is True
    # A replayed compaction step re-summarizes the same messages, maybe differently.
    assert await episodes.record_episode(conv, "Picked Postgres.", ["m1", "m2"]) is False
    async with async_session() as s:
        rows = await list_episodes(s, conv)
    assert [r.text for r in rows] == ["Chose Postgres."]
    assert rows[0].embedding is not None


async def test_thread_without_a_conversation_row_is_not_recorded(database, monkeypatch):
    """A stateless automation run's per-run thread: nothing would ever delete it."""
    _stub_embeddings(monkeypatch)
    assert await episodes.record_episode("automation_run_123", "x", ["m1"]) is False


async def test_episodes_are_deleted_with_their_conversation(database, monkeypatch):
    from db.engine import async_session
    from db.ops import delete_conversation, list_episodes

    _stub_embeddings(monkeypatch)
    conv = await _conversation()
    await episodes.record_episode(conv, "Chose Postgres.", ["m1"])
    async with async_session() as s:
        await delete_conversation(s, conv)
    async with async_session() as s:
        assert await list_episodes(s, conv) == []


# ── retrieval ────────────────────────────────────────────────────────────────

async def test_dense_retrieval_returns_only_relevant_episodes(database, monkeypatch):
    _stub_embeddings(monkeypatch)
    conv = await _conversation()
    await _seed_three(conv)

    # "about" also matches the weather episode lexically; with an embedder the
    # shared summarizer vocabulary must not be enough to pull it in.
    hits = await episodes.search_episodes(conv, "remind me what we decided about postgres?")
    assert [h["text"][:12] for h in hits] == ["Chose Postgr"]


async def test_lexical_arm_works_without_an_embedder(database, monkeypatch):
    _stub_embeddings(monkeypatch, dense=False)
    conv = await _conversation()
    await _seed_three(conv)

    hits = await episodes.search_episodes(conv, "when do Stripe invoices go out")
    assert len(hits) == 1 and "Stripe" in hits[0]["text"]
    assert await episodes.search_episodes(conv, "quantum chromodynamics lecture notes") == []


async def test_rows_without_an_embedding_stay_reachable_lexically(database, monkeypatch):
    """Episodes stored before an embedder was configured have no vector."""
    _stub_embeddings(monkeypatch, dense=False)
    conv = await _conversation()
    await episodes.record_episode(conv, "Billing moves to Stripe invoices monthly.", ["b1"])
    _stub_embeddings(monkeypatch, dense=True)
    await episodes.record_episode(conv, "Chose Postgres 16 for the ledger.", ["a1"])

    hits = await episodes.search_episodes(conv, "billing: when do Stripe invoices go out")
    assert [h["text"][:7] for h in hits] == ["Billing"]


async def test_retrieval_is_scoped_to_the_conversation(database, monkeypatch):
    _stub_embeddings(monkeypatch)
    mine, other = await _conversation(), await _conversation()
    await episodes.record_episode(other, "Chose Postgres in someone else's chat.", ["x1"])

    assert await episodes.search_episodes(mine, "what about postgres") == []


async def test_trivial_turns_and_unknown_threads_retrieve_nothing(database, monkeypatch):
    _stub_embeddings(monkeypatch)
    conv = await _conversation()
    await _seed_three(conv)
    assert await episodes.search_episodes(conv, "thanks") == []
    assert await episodes.search_episodes("", "postgres") == []


# ── injection ────────────────────────────────────────────────────────────────

async def test_episode_segment_is_uncached_and_says_where_the_verbatim_text_is(database, monkeypatch):
    from core.agents import _episode_volatile_parts

    _stub_embeddings(monkeypatch)
    conv = await _conversation()
    await _seed_three(conv)

    [segment] = await _episode_volatile_parts(conv, "postgres migration schedule?")
    assert segment.cacheable is False, "re-ranked per turn, so it belongs in the tail"
    assert segment.content.startswith("## Earlier in this conversation")
    assert "Postgres 16" in segment.content and "Stripe" not in segment.content
    assert "read_conversation" in segment.content


async def test_prefetch_carries_the_conversation_id_to_episode_retrieval(monkeypatch):
    """The graph reuses the prefetched task, so a prefetch that dropped the
    conversation id would silently disable episodes for that whole turn."""
    from core import agents

    seen: list = []

    async def fake_episodes(conversation_id, query):
        seen.append(conversation_id)
        return []

    async def nothing(*args, **kwargs):
        return []

    monkeypatch.setattr(agents, "_episode_volatile_parts", fake_episodes)
    monkeypatch.setattr(agents, "_memory_volatile_parts", nothing)
    monkeypatch.setattr(agents, "_skills_volatile_parts", nothing)

    agents.prefetch_retrieval(None, "postgres?", "msg-prefetch-1", "conv-42")
    await agents._retrieved_volatile_parts(
        None, [HumanMessage(content="postgres?", id="msg-prefetch-1")], "conv-42"
    )
    assert seen == ["conv-42"], "one computation, keyed by message, carrying the conversation"
