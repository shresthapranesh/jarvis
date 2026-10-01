"""The cached request layout: what makes consecutive calls share a prefix.

A provider prompt cache reuses a request's prefix up to the latest breakpoint a
*previous* request wrote, and invalidates everything from the first byte that
differs. So the property that matters is not "breakpoints exist" but "the next
call's payload starts with this call's payload". The tests render requests
through the real LangChain integrations (no network) and compare payloads, since
each integration rewrites content differently and a mock would agree with any
layout.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_anthropic.chat_models import _format_messages
from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai.chat_models.base import _convert_message_to_dict

from core.context_cache import CacheSegment
from core.messages import build_llm_messages


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


def _strip_markers(obj: Any) -> Any:
    """Drop breakpoints, which move between calls by design."""
    if isinstance(obj, dict):
        return {k: _strip_markers(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_markers(v) for v in obj if not (isinstance(v, dict) and "cachePoint" in v)]
    return obj


_SEGMENTS = [
    CacheSegment(name="core_memory", content="## Agent Memory\n\n- prefers terse answers"),
    CacheSegment(name="relevant_memories", content="## Relevant Memories\n\n- x", cacheable=False),
]


def _anthropic(history: list, volatile: str) -> tuple[Any, Any]:
    out = build_llm_messages(
        "STATIC PROMPT", True, history, volatile_suffix=volatile,
        cache_segments=_SEGMENTS, cache_provider="anthropic",
    )
    return _format_messages(out)


# ── The prefix property ──────────────────────────────────────────────────────

def test_next_agent_iteration_extends_the_cached_prefix():
    """One tool round-trip later, with different per-turn context, the earlier
    request's system + history must be a byte-for-byte prefix of the new one."""
    history = [HumanMessage("question", id="u0"), *_tool_turn(0, "first result")]
    sys1, msgs1 = _anthropic(history, "## Current Tasks\n\n[ ] step one")
    history2 = history + _tool_turn(1, "second result")
    sys2, msgs2 = _anthropic(history2, "## Current Tasks\n\n[x] step one")

    assert _strip_markers(sys1) == _strip_markers(sys2)
    # msgs1's last user message is history + the volatile tail merged in; the
    # cached part ends at the breakpoint, before the tail block.
    cached1 = _strip_markers(msgs1)
    cached1[-1] = {**cached1[-1], "content": cached1[-1]["content"][:-1]}
    stripped2 = _strip_markers(msgs2)
    assert stripped2[: len(cached1)] == cached1


def test_prefix_survives_many_iterations_between_compaction_steps():
    """Per-call compaction rewrites older messages; stepping it means most
    consecutive calls still share their whole prefix."""
    from core.compaction import apply_per_call_compaction

    history: list = [HumanMessage("go", id="u0")]
    shared = 0
    calls = 0
    prev = None
    for i in range(16):
        history.extend(_tool_turn(i, f"result {i} " + "y" * 3000))
        payload = _strip_markers(_format_messages(build_llm_messages(
            "STATIC", True, apply_per_call_compaction(history), cache_provider="anthropic",
        ))[1])
        if prev is not None:
            calls += 1
            shared += payload[: len(prev)] == prev
        prev = payload
    # With step 4 the boundary moves on 1 call in 4; slide-by-one broke all of them.
    assert shared >= calls * 0.7, f"only {shared}/{calls} calls kept their prefix"


# ── Layout per provider ──────────────────────────────────────────────────────

def test_anthropic_layout():
    history = [
        SystemMessage("[Conversation summary]\nearlier"),
        HumanMessage("q", id="u0"),
        *_tool_turn(0, "result"),
    ]
    system, msgs = _anthropic(history, "## Current Tasks\n\n[ ] a")

    texts = [b["text"] for b in system]
    assert texts[0] == "STATIC PROMPT"
    assert any("prefers terse" in t for t in texts)
    assert "[Conversation summary]" in texts[-1], "summary belongs in the cached system region"
    assert all("Relevant Memories" not in t for t in texts), "per-turn content stays out of the system"
    assert sum("cache_control" in b for b in system) == 2

    last = msgs[-1]["content"]
    assert last[0]["type"] == "tool_result" and "cache_control" in last[0]
    assert "<turn_context>" in last[-1]["text"] and "cache_control" not in last[-1]
    assert "Relevant Memories" in last[-1]["text"] and "Current Tasks" in last[-1]["text"]
    total = json.dumps([system, msgs]).count('"cache_control"')
    assert total <= 4


def test_bedrock_uses_cache_points_not_cache_control():
    """ChatBedrockConverse drops `cache_control` on text blocks, so Bedrock
    needs explicit cachePoint blocks or it caches nothing."""
    history = [HumanMessage("q", id="u0"), *_tool_turn(0, "result")]
    out = build_llm_messages(
        "STATIC", True, history, volatile_suffix="ctx",
        cache_segments=_SEGMENTS, cache_provider="bedrock",
    )
    bedrock_msgs, system = _messages_to_bedrock(list[Any](out))
    payload = json.dumps([system, bedrock_msgs])

    assert "cache_control" not in payload
    points = payload.count('"cachePoint"')
    assert points == 3 and points <= 4
    last = bedrock_msgs[-1]["content"]
    assert "toolResult" in last[0] and "cachePoint" in last[1]
    assert "ctx" in last[-1]["text"]


def test_openrouter_marks_a_user_message_not_a_tool_result():
    """ChatOpenAI strips cache_control from role=tool, so the history
    breakpoint has to sit on the newest user message instead."""
    history = [HumanMessage("q", id="u0"), *_tool_turn(0, "result")]
    out = build_llm_messages("STATIC", True, history, cache_provider="openrouter")
    dicts = [_convert_message_to_dict(m) for m in out]

    tool = next(d for d in dicts if d["role"] == "tool")
    assert "cache_control" not in json.dumps(tool)
    user = next(d for d in dicts if d["role"] == "user")
    assert user["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_uncached_path_is_unchanged():
    """Providers without breakpoints keep the single concatenated system prompt."""
    history: list[AnyMessage] = [SystemMessage("[Conversation summary]\nearlier"), HumanMessage("q", id="u0")]
    out = build_llm_messages(
        "STATIC", False, history, volatile_suffix="ctx", cache_segments=_SEGMENTS,
    )
    assert isinstance(out[0], SystemMessage) and isinstance(out[0].content, str)
    assert "ctx" in out[0].content and "[Conversation summary]" in out[0].content
    assert out[1:] == [history[1]]


# ── Stepped per-call compaction ──────────────────────────────────────────────

def test_elision_boundary_moves_in_steps():
    from core.messages import elide_stale_tool_results

    def elided_ids(n_turns: int, step: int) -> set[str]:
        msgs: list = [HumanMessage("go", id="u0")]
        for i in range(n_turns):
            msgs.extend(_tool_turn(i, "z" * 3000))
        out = elide_stale_tool_results(msgs, step=step)
        return {str(m.id) for m in out if isinstance(m, ToolMessage) and "elided" in str(m.content)}

    # keep_turns=4: 8..11 AI turns all elide exactly the first 4 tool results.
    assert elided_ids(8, 4) == elided_ids(11, 4) == {f"tool_{i}" for i in range(4)}
    assert elided_ids(12, 4) == {f"tool_{i}" for i in range(8)}
    # step=1 is the old slide-by-one behaviour.
    assert elided_ids(9, 1) == {f"tool_{i}" for i in range(5)}


def test_collapse_count_moves_in_steps():
    from core.compaction import collapse_old_tool_results

    def stubs(n_groups: int, step: int) -> int:
        msgs: list = [HumanMessage("go", id="u0")]
        for i in range(n_groups):
            msgs.extend(_tool_turn(i, "r"))
        out = collapse_old_tool_results(msgs, step=step)
        return sum(
            isinstance(m, AIMessage) and str(m.content).startswith("[Previous tool activity")
            for m in out
        )

    assert [stubs(n, 4) for n in (4, 7, 8, 11, 12)] == [0, 0, 4, 4, 8]
    assert stubs(7, 1) == 3


# ── Segments that must not churn per turn ────────────────────────────────────

async def test_trivial_turn_keeps_memory_prefix(monkeypatch):
    """A greeting used to drop the how-to block, rewriting the cached prefix
    ahead of the conversation for that turn and the next."""
    from core import agents

    monkeypatch.setattr(agents, "embeddings_available", lambda: True)

    async def _core():
        return "core fact"

    async def _search(query, k=6):
        return [{"id": "m1", "text": "a retrieved fact", "score": 0.9}]

    monkeypatch.setattr(agents, "load_core", _core)
    monkeypatch.setattr(agents, "search_memory", _search)

    trivial = await agents._memory_volatile_parts(None, "thanks")
    real = await agents._memory_volatile_parts(None, "what did we decide about caching")

    cached = lambda segs: [(s.name, s.content) for s in segs if s.cacheable]  # noqa: E731
    assert cached(trivial) == cached(real)
    assert not any(s.name == "relevant_memories" for s in trivial)


async def test_ranked_skill_shortlist_is_not_cached(monkeypatch):
    from core import agents

    async def _ranked(query):
        return [{"name": "deploy", "description": "ship it"}], True

    async def _full(query):
        return [{"name": "deploy", "description": "ship it"}], False

    monkeypatch.setattr(agents, "skill_catalog", _ranked)
    assert (await agents._skills_volatile_parts("deploy"))[0].cacheable is False
    monkeypatch.setattr(agents, "skill_catalog", _full)
    assert (await agents._skills_volatile_parts("deploy"))[0].cacheable is True


def test_bedrock_cache_points_only_for_claude():
    from core.model_catalog import ModelSpec, honors_cache_control

    providers = {"bedrock", "anthropic", "openrouter"}
    claude = ModelSpec("bedrock:us.anthropic.claude-sonnet-4-6", "c", "bedrock")
    llama = ModelSpec("bedrock:us.meta.llama4-maverick-17b-instruct-v1:0", "l", "bedrock")
    assert honors_cache_control(claude, providers) is True
    assert honors_cache_control(llama, providers) is False
