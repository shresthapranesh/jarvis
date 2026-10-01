"""Message shaping utilities."""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


# Three names for the same thing: Anthropic emits `thinking`/`redacted_thinking`,
# while LangChain's v1 content-block format calls it `reasoning`. All three must
# be listed — a name missing here is a block that survives into history, and
# providers dereference these blocks unguarded. `langchain_google_genai` does a
# bare `part["reasoning"]`, so one stray v1 block from another provider (whose
# `summary`-shaped blocks carry no `reasoning` key at all) crashes every
# subsequent Gemini call on that thread with `KeyError: 'reasoning'`.
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})

# additional_kwargs mirrors of the same content, under the provider's own key.
_THINKING_KWARGS = ("thinking", "reasoning")


def _strip_thinking_from_message(msg: AIMessage) -> AIMessage:
    content = msg.content
    stale_kwargs = [k for k in _THINKING_KWARGS if k in (msg.additional_kwargs or {})]

    filtered = content
    if isinstance(content, list):
        filtered = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
        ]
        if len(filtered) == len(content):
            filtered = content
        elif not filtered:
            filtered = [{"type": "text", "text": ""}]

    if filtered is content and not stale_kwargs:
        return msg

    new_msg = msg.model_copy(update={"content": filtered})
    if stale_kwargs:
        new_msg = new_msg.model_copy(update={
            "additional_kwargs": {
                k: v for k, v in new_msg.additional_kwargs.items()
                if k not in _THINKING_KWARGS
            }
        })
    return new_msg


def strip_historical_thinking(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Strip thinking blocks from ALL AIMessages in the history.

    Thinking block signatures don't survive checkpointer round-trips, so
    keeping any historical thinking block risks Bedrock/Anthropic rejecting
    with "thinking.signature: Field required".  The model generates fresh
    thinking each turn — it doesn't need to see its own prior reasoning.
    """
    result: list[AnyMessage] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            result.append(_strip_thinking_from_message(msg))
        else:
            result.append(msg)
    return result


def _ai_tool_use_ids(msg: AIMessage) -> list[str]:
    """Collect tool_use ids from BOTH .tool_calls and content blocks.

    Mid-stream-cancelled accumulators can land tool_use blocks in `content`
    while `.tool_calls` stays empty (LangChain finalises that field at the
    end). Bedrock validates against content blocks directly, so we need the
    union to detect every id that needs a tool_result.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for tc in (getattr(msg, "tool_calls", None) or []):
        tcid = tc.get("id") if isinstance(tc, dict) else None
        if tcid and tcid not in seen:
            seen.add(tcid)
            ids.append(tcid)
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                bid = block.get("id")
                if bid and bid not in seen:
                    seen.add(bid)
                    ids.append(bid)
    return ids


def _msg_tool_result_ids(msg: AnyMessage) -> list[str]:
    """Collect tool_result ids from a message that satisfies tool_use.

    Native ToolMessage carries `tool_call_id`. Anthropic-style providers can
    also round-trip tool_results as a HumanMessage whose content list has
    `{"type": "tool_result", "tool_use_id": "..."}` blocks. We accept both.
    """
    ids: list[str] = []
    if getattr(msg, "type", "") == "tool":
        tcid = getattr(msg, "tool_call_id", None)
        if tcid:
            ids.append(tcid)
        return ids
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                bid = block.get("tool_use_id")
                if bid:
                    ids.append(bid)
    return ids


def _is_tool_result_carrier(msg: AnyMessage) -> bool:
    """A message that can carry tool_result blocks for the preceding AIMessage.

    Native ToolMessages and HumanMessages whose content list includes any
    tool_result block both qualify; everything else terminates the
    paired-result window.
    """
    if getattr(msg, "type", "") == "tool":
        return True
    if isinstance(msg, HumanMessage):
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
    return False


def repair_orphan_tool_calls(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Insert synthetic ToolMessages for any AIMessage tool_use id that has
    no matching tool_result in the immediately-following window.

    Bedrock/Anthropic reject histories where a tool_use isn't paired with a
    tool_result in the next turn ("Expected toolResult blocks at messages.N
    .content for the following Ids: ..."). Orphans appear when the agent
    run is cancelled between model_request and ToolNode, when ToolNode
    crashes partway through a parallel batch, or when streaming aborts
    mid-tool_use generation (in that case the orphan id lives in the
    AIMessage's content blocks but not yet in `.tool_calls`).
    """
    result: list[AnyMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        result.append(msg)
        if not isinstance(msg, AIMessage):
            i += 1
            continue
        expected_ids = _ai_tool_use_ids(msg)
        if not expected_ids:
            i += 1
            continue
        j = i + 1
        seen_ids: set[str] = set()
        while j < len(messages) and _is_tool_result_carrier(messages[j]):
            for tcid in _msg_tool_result_ids(messages[j]):
                seen_ids.add(tcid)
            result.append(messages[j])
            j += 1
        for tcid in expected_ids:
            if tcid not in seen_ids:
                result.append(ToolMessage(
                    content="[Tool result missing — previous run was cancelled or interrupted.]",
                    tool_call_id=tcid,
                ))
        i = j
    return result


# Tool results older than this many assistant turns get their bulky content
# clipped before the LLM call. Old tool outputs are the bulk of agent-loop
# history and are rarely re-read once the model has acted on them, yet they
# get re-billed as input tokens on every subsequent call.
TOOL_RESULT_KEEP_TURNS = 4
TOOL_RESULT_ELIDE_MIN_CHARS = 2500
TOOL_RESULT_ELIDE_HEAD_CHARS = 400
# The elision boundary advances in jumps of this many AI turns rather than one.
# Eliding rewrites a message that is already in the cached prompt prefix, so a
# boundary that slides every turn busts the history cache on every LLM call; a
# stepped one keeps the prefix byte-identical for `step` calls in between, at
# the price of up to `step - 1` extra turns of unclipped output.
TOOL_RESULT_ELIDE_STEP = 4


def elide_stale_tool_results(
    messages: list[AnyMessage],
    *,
    keep_turns: int = TOOL_RESULT_KEEP_TURNS,
    min_chars: int = TOOL_RESULT_ELIDE_MIN_CHARS,
    head_chars: int = TOOL_RESULT_ELIDE_HEAD_CHARS,
    step: int = TOOL_RESULT_ELIDE_STEP,
) -> list[AnyMessage]:
    """Clip bulky ToolMessages older than the last ``keep_turns`` AI turns.

    Purely per-call and non-destructive: it operates on message copies, so the
    checkpointer keeps the full output and every later call re-derives the
    same (deterministic) elision. Only plain-string tool content is touched —
    list content (vision blocks, structured tool results) passes through, as
    do results at or under ``min_chars``.

    The boundary is counted from the *start* of history and moves in jumps of
    ``step`` AI turns, so between jumps every call elides exactly the same
    messages and the cached prefix survives (see TOOL_RESULT_ELIDE_STEP).
    ``step=1`` is the old slide-by-one behaviour.

    NOTE: this is step 1 of `core/compaction.py:apply_per_call_compaction()`
    which also collapses old tool_call groups into short stubs.
    """
    ai_positions = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    stale = len(ai_positions) - keep_turns
    if stale <= 0:
        return messages
    cutoff = ai_positions[(stale // max(step, 1)) * max(step, 1)]
    if cutoff == 0:
        return messages
    result = list(messages)
    for i in range(cutoff):
        msg = result[i]
        if getattr(msg, "type", "") != "tool":
            continue
        content = msg.content
        if not isinstance(content, str) or len(content) <= min_chars:
            continue
        stub = (
            f"{content[:head_chars]}\n... [{len(content) - head_chars} chars of stale "
            "tool output elided to save context — re-run the tool if you need it again]"
        )
        result[i] = msg.model_copy(update={"content": stub})
    return result


def message_text(m: AnyMessage) -> str:
    """Flatten a message's content to a single string for token counting."""
    c = m.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for block in c:
            if isinstance(block, dict):
                parts.append(block.get("text", "") or block.get("thinking", ""))
        return "".join(parts)
    return ""


def estimate_tokens_heuristic(messages: list[AnyMessage]) -> int:
    """Zero-cost token approximation: 4 chars per token over flattened text.

    Roughly correct for English/code. Used as a cheap pre-filter so the
    accurate count (which for several providers is a count-tokens API call —
    a network round-trip per agent-loop iteration) only runs when the history
    is actually near the summarization threshold.
    """
    return sum(len(message_text(m)) for m in messages) // 4


def estimate_tokens(messages: list[AnyMessage], llm) -> int:
    """Best-effort token count, falling back to a chars-per-token heuristic.

    Uses the LLM's own tokenizer when available (most LangChain chat models
    expose `get_num_tokens_from_messages`); otherwise approximates at 4
    chars per token, which is roughly correct for English/code and biases
    high (so we summarise sooner) for token-dense content.
    """
    try:
        return cast(Any, llm).get_num_tokens_from_messages(messages)
    except Exception:
        return estimate_tokens_heuristic(messages)


def history_tokens_from_usage(messages: list[AnyMessage], overhead_tokens: int) -> int | None:
    """History size taken from the provider's own count on the previous call.

    The latest AIMessage's `usage_metadata.input_tokens` is the exact size of
    the request that produced it — every message before it, plus the non-history
    part of that request (system prompt, cached segments, volatile tail, tool
    schemas), which the caller estimates as `overhead_tokens` and is subtracted
    so the result means what `compact_threshold` means: history only. Measured
    on gemma-4-31b-it that overhead is ~8k tokens, so leaving it in would trip a
    12k threshold on a near-empty conversation.

    Then add what has arrived since: the response itself (`output_tokens`, which
    includes reasoning that `strip_historical_thinking` later drops — an
    overestimate bounded by one response) and the heuristic over the tail.

    Returns None when the latest AIMessage carries no input count, so the caller
    can fall back. This is what replaced `estimate_tokens` on the agent loop:
    for Google that is one blocking `count_tokens` HTTP call *per message*
    (2.1s for 20 messages, measured), run on the event loop.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, AIMessage):
            continue
        usage = msg.usage_metadata or {}
        input_tokens = usage.get("input_tokens") or 0
        if input_tokens <= 0:
            return None
        history_then = max(0, input_tokens - overhead_tokens)
        output_tokens = usage.get("output_tokens") or 0
        return history_then + output_tokens + estimate_tokens_heuristic(messages[i + 1:])
    return None


def _make_system_message(static_text: str, volatile_text: str, cache: bool) -> SystemMessage:
    """Legacy single-breakpoint builder. Kept for backwards compat.

    When ``cache`` is on (Bedrock/Anthropic), the static text carries the single
    ``cache_control`` breakpoint and any volatile text (memory, todos, folded
    summaries) goes in a *separate* block after it — so churn in the volatile
    suffix never invalidates the cached static prefix (system prompt + tool
    schemas). When ``cache`` is off, both are concatenated into one plain
    string (some non-Anthropic providers dislike multi-block system content).
    For multi-breakpoint (Jarvis-style) use `build_llm_messages` with cache_segments.
    """
    if cache:
        blocks: list[dict[str, Any] | str] = [
            {"type": "text", "text": static_text, "cache_control": {"type": "ephemeral"}}
        ]
        if volatile_text.strip():
            blocks.append({"type": "text", "text": volatile_text})
        return SystemMessage(content=blocks)
    full = static_text if not volatile_text.strip() else f"{static_text}\n\n{volatile_text}"
    return SystemMessage(full)


def _make_system_message_multi(
    static_text: str,
    segments: list[Any] | None,
    volatile_text: str,
    cache: bool,
    cache_ttl: str = "5m",
) -> SystemMessage:
    """multi-breakpoint builder — delegates to context_cache module.

    segments is list[CacheSegment]; if None, falls back to legacy builder.
    Even when cache=False we delegate to build_cached_system_message because
    its no-cache path concatenates segments + volatile correctly; the legacy
    path would silently drop memory/skills/project context on google_genai/Ollama.
    """
    if not segments:
        return _make_system_message(static_text, volatile_text, cache)

    try:
        from core.context_cache import ContextCacheConfig, build_cached_system_message

        sys_msg, _stats = build_cached_system_message(
            static_prompt=static_text,
            segments=segments,
            volatile_suffix=volatile_text,
            use_cache=cache,
            config=ContextCacheConfig(enabled=cache, cache_ttl=cache_ttl),
        )
        return sys_msg
    except Exception:
        # Fallback to legacy on any error (never break LLM call)
        return _make_system_message(static_text, volatile_text, cache)


def _system_text(msg: SystemMessage) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            b.get("text", "") for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def build_llm_messages(
    system_text: str,
    cache: bool,
    history: list[AnyMessage],
    *,
    volatile_suffix: str = "",
    cache_segments: list[Any] | None = None,
    cache_ttl: str = "5m",
    cache_provider: str = "anthropic",
) -> list[AnyMessage]:
    """Build the message list for an LLM call with exactly one SystemMessage.

    Bedrock/Anthropic reject "multiple non-consecutive system messages". The
    summarizer adds its result to state as a SystemMessage via the
    checkpointer's add_messages reducer, which appends it after the kept
    user/assistant/tool turns. On the next turn that summary System ends up
    after non-system messages — a non-consecutive system — so we fold any
    embedded SystemMessages into the prompt text and prepend a single
    SystemMessage at index 0.

    ``system_text`` is the *static* prefix; ``cache_segments`` are the stable
    sections after it; ``volatile_suffix`` (relevant memories, todos, project
    memory, …) is what may change on every call.

    With ``cache=True`` the request is laid out most-stable-first, because a
    prefix cache is invalidated from the first changed byte onward:

        system: static ▸ cacheable segments ▸ conversation summary
        history (normalized; rolling breakpoint on its newest markable message)
        tail: one user message carrying volatile_suffix

    The summary is the only SystemMessage that reaches history, and it changes
    only when compaction rewrites the history anyway, so it costs nothing to
    cache. The volatile tail sits after the last breakpoint, so changing it
    re-bills only itself. ``cache_provider`` picks the breakpoint spelling — see
    ContextCacheConfig.mark.

    With ``cache=False`` the old single-system-message layout is unchanged:
    everything, volatile and summary included, is concatenated into the system
    prompt and history passes through as-is.
    """
    extras: list[str] = []
    rest: list[AnyMessage] = []
    for m in history:
        if isinstance(m, SystemMessage):
            text = _system_text(m).strip()
            if text:
                extras.append(text)
        else:
            rest.append(m)

    if cache:
        return _build_cached_llm_messages(
            system_text,
            rest,
            summaries=extras,
            volatile_text=volatile_suffix.strip(),
            cache_segments=list(cache_segments or []),
            cache_ttl=cache_ttl,
            cache_provider=cache_provider,
        )

    volatile_parts = [p for p in [volatile_suffix.strip(), *extras] if p]
    volatile_text = "\n\n".join(volatile_parts)

    if cache_segments:
        return [
            _make_system_message_multi(
                system_text, cache_segments, volatile_text, cache, cache_ttl
            )
        ] + rest
    return [_make_system_message(system_text, volatile_text, cache)] + rest


# Volatile per-call context rides at the end of the request as a user message,
# so it has to say plainly that the user didn't write it — otherwise "Current
# Tasks" or a planning directive reads as the user's newest instruction.
_TURN_CONTEXT_OPEN = (
    "<turn_context>\n"
    "Context the application attached for this step (memories retrieved for the "
    "current request, task list, project state). It is not a message from the user.\n\n"
)
_TURN_CONTEXT_CLOSE = "\n</turn_context>"


def _build_cached_llm_messages(
    system_text: str,
    rest: list[AnyMessage],
    *,
    summaries: list[str],
    volatile_text: str,
    cache_segments: list[Any],
    cache_ttl: str,
    cache_provider: str,
) -> list[AnyMessage]:
    """The cache=True layout described in build_llm_messages."""
    from core.context_cache import (
        CacheSegment,
        ContextCacheConfig,
        build_cached_system_message,
        mark_history,
        normalize_history_content,
    )

    cfg = ContextCacheConfig(enabled=True, cache_ttl=cache_ttl, provider=cache_provider)
    segments = [s for s in cache_segments if s.cacheable]
    if summaries:
        segments.append(
            CacheSegment(name="conversation_summary", content="\n\n".join(summaries))
        )
    system, _stats = build_cached_system_message(
        static_prompt=system_text,
        # Non-cacheable segments belong in the tail with the rest of the
        # volatile content, not after the system breakpoints.
        segments=segments,
        use_cache=True,
        config=cfg,
    )
    volatile_parts = [s.content for s in cache_segments if not s.cacheable and s.content.strip()]
    if volatile_text:
        volatile_parts.append(volatile_text)

    messages = mark_history(normalize_history_content(rest), cfg)
    if volatile_parts:
        body = "\n\n".join(volatile_parts)
        messages.append(
            HumanMessage(
                content=[
                    {"type": "text", "text": f"{_TURN_CONTEXT_OPEN}{body}{_TURN_CONTEXT_CLOSE}"}
                ]
            )
        )
    return [system, *messages]
