"""Message-shape utilities for LLM calls.

Pure helpers that sanitize, repair, and shape `langchain_core.messages` lists
before `.ainvoke`. Kept out of `core/agents.py` because they have no
LangGraph/agent dependency and are reused across every agent-loop node.

Required pre-`.ainvoke` pipeline for any new agent-loop node:
    history = strip_historical_thinking(history)
    history = repair_orphan_tool_calls(history)
    msgs = build_llm_messages(system_prompt, use_cache, history)
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _strip_thinking_from_message(msg: AIMessage) -> AIMessage:
    content = msg.content
    if not isinstance(content, list):
        return msg
    filtered = [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
    ]
    if len(filtered) == len(content):
        return msg
    if not filtered:
        filtered = [{"type": "text", "text": ""}]
    new_msg = msg.model_copy(update={"content": filtered})
    if "thinking" in (new_msg.additional_kwargs or {}):
        new_msg = new_msg.model_copy(update={
            "additional_kwargs": {k: v for k, v in new_msg.additional_kwargs.items() if k != "thinking"}
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
        return sum(len(message_text(m)) for m in messages) // 4


def _make_system_message(static_text: str, volatile_text: str, cache: bool) -> SystemMessage:
    """Build a SystemMessage with the static prompt as the cacheable prefix.

    When ``cache`` is on (Bedrock/Anthropic), the static text carries the single
    ``cache_control`` breakpoint and any volatile text (memory, todos, folded
    summaries) goes in a *separate* block after it — so churn in the volatile
    suffix never invalidates the cached static prefix (system prompt + tool
    schemas). When ``cache`` is off, both are concatenated into one plain
    string (some non-Anthropic providers dislike multi-block system content).
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
) -> list[AnyMessage]:
    """Build the message list for an LLM call with exactly one SystemMessage.

    Bedrock/Anthropic reject "multiple non-consecutive system messages". The
    summarizer adds its result to state as a SystemMessage via the
    checkpointer's add_messages reducer, which appends it after the kept
    user/assistant/tool turns. On the next turn that summary System ends up
    after non-system messages — a non-consecutive system — so we fold any
    embedded SystemMessages into the prompt text and prepend a single
    SystemMessage at index 0.

    ``system_text`` is the *static* (cacheable) prefix. ``volatile_suffix``
    (memory, todos, …) plus any folded summarizer SystemMessages form the
    volatile region, which is placed after the cache breakpoint so it can
    change every turn without busting the cached prefix.
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
    volatile_parts = [p for p in [volatile_suffix.strip(), *extras] if p]
    volatile_text = "\n\n".join(volatile_parts)
    return [_make_system_message(system_text, volatile_text, cache)] + rest
