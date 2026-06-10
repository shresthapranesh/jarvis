"""Conversation-history summarization for the agent loop.

When a thread's history grows past a token threshold, the oldest complete
exchanges are condensed into a single summary SystemMessage and evicted from
the checkpointer via RemoveMessage entries. Extracted from `core/agents.py`
so the graph-building code stays focused on graph building; the only
coupling is the (llm, summarizer) pair passed in by `model_request_node`.
"""

from __future__ import annotations

import logging
import os

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

from .messages import estimate_tokens, message_text

logger = logging.getLogger(__name__)


def summarize_threshold() -> int:
    """Token count at which conversation history gets summarized.

    Defaults to 100_000 (well under typical 200k contexts). Override with
    JARVIS_SUMMARIZE_TOKEN_THRESHOLD for manual testing — set it low (e.g. 200)
    to force-trigger the summarize path on a short conversation.
    """
    raw = os.environ.get("JARVIS_SUMMARIZE_TOKEN_THRESHOLD")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 100_000


KEEP_RECENT = 10  # messages to keep verbatim


async def maybe_summarize(
    messages: list[AnyMessage],
    *,
    llm,
    summarizer,
) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
    """Trim conversation history when it grows past the threshold.

    `llm` is used only for token counting (its tokenizer, when available);
    `summarizer` is the retry-wrapped model that produces the summary —
    the raw LLM without tool bindings, since summarization doesn't tool-call.

    Returns (new_messages_for_this_turn, state_update_messages) on summarize,
    or None if no trim is needed. The state_update_messages are
    RemoveMessage entries plus the summary SystemMessage that get returned
    from the model node so the checkpointer persists the trim and we don't
    re-summarize the same history every turn.

    Async so the summarization LLM call uses ``ainvoke`` and doesn't block
    the event loop.
    """
    threshold = summarize_threshold()
    token_count = estimate_tokens(messages, llm)
    if token_count <= threshold:
        logger.debug(
            "summarize check: %d tokens / %d msgs (under %d threshold) — skip",
            token_count, len(messages), threshold,
        )
        return None

    logger.info(
        "summarize triggered: %d tokens / %d msgs (over %d threshold)",
        token_count, len(messages), threshold,
    )

    # Find a safe split point that never breaks an AIMessage→ToolMessage
    # group.  Anthropic (and Bedrock) reject messages where a tool_use
    # block has no matching tool_result, or a tool_result references a
    # tool_use_id that doesn't exist in the preceding assistant turn.
    #
    # Walk backward from the ideal split (len - KEEP_RECENT) until we
    # find a message that is NOT a ToolMessage and whose predecessor (if
    # an AIMessage) has no pending tool_calls.  That gives us a clean
    # boundary: everything before it is a complete exchange.
    ideal = max(len(messages) - KEEP_RECENT, 0)
    split = ideal
    while split > 0:
        msg_at_split = messages[split]
        # A ToolMessage at the split means its parent AIMessage is in
        # to_summarize but the result would be in kept — not allowed.
        if getattr(msg_at_split, "type", "") == "tool":
            split -= 1
            continue
        # The message just before the split is in to_summarize.  If it's
        # an AIMessage with tool_calls, the matching ToolMessages would be
        # at split+ — also not allowed.
        prev = messages[split - 1]
        if isinstance(prev, AIMessage) and getattr(prev, "tool_calls", None):
            split -= 1
            continue
        break

    to_summarize = messages[:split]
    if not to_summarize:
        logger.warning(
            "summarize triggered at %d tokens but no safe split found "
            "(len=%d ideal=%d final_split=%d) — history kept intact, will keep growing",
            token_count, len(messages), ideal, split,
        )
        return None

    logger.info(
        "summarize: condensing %d msgs, keeping %d recent",
        len(to_summarize), len(messages) - split,
    )

    # Build a safe message list for the summarization LLM call.
    # Anthropic rejects raw tool-call exchanges (tool_use without
    # tool_result, etc.), so we convert the history into plain
    # HumanMessage/AIMessage text that any model can digest.
    safe_msgs: list[AnyMessage] = []
    for m in to_summarize:
        mtype = getattr(m, "type", "")
        if mtype == "human":
            safe_msgs.append(m)
        elif mtype == "ai":
            text = message_text(m)
            tool_calls = getattr(m, "tool_calls", [])
            if tool_calls:
                tc_desc = ", ".join(
                    f"{tc.get('name', '?')}({', '.join(f'{k}=...' for k in (tc.get('args') or {}))})"
                    for tc in tool_calls
                )
                text = f"{text}\n[Called tools: {tc_desc}]" if text else f"[Called tools: {tc_desc}]"
            if text:
                safe_msgs.append(AIMessage(content=text))
        elif mtype == "tool":
            # Fold tool results into the preceding AI turn's context
            tool_name = getattr(m, "name", "tool")
            tool_content = str(getattr(m, "content", ""))[:500]
            safe_msgs.append(HumanMessage(content=f"[Tool result from {tool_name}]: {tool_content}"))
        elif isinstance(m, SystemMessage):
            safe_msgs.append(m)

    try:
        summary = await summarizer.ainvoke([
            SystemMessage(
                "Summarize the following conversation history concisely. "
                "Preserve all key facts, decisions, tool outputs, and results."
            ),
            *safe_msgs,
        ])
        summary_text = summary.content if isinstance(summary.content, str) else str(summary.content)
    except Exception as exc:
        logger.warning(
            "summarization LLM call failed (%s: %s) — skipping; history will keep growing",
            type(exc).__name__, exc,
        )
        return None
    logger.info("summarized %d messages into ~%d chars", len(to_summarize), len(summary_text))
    summary_msg = SystemMessage(f"[Prior conversation summary]\n{summary_text}")
    kept = messages[split:]
    removals = [RemoveMessage(id=m.id) for m in to_summarize if hasattr(m, "id") and m.id]
    # RemoveMessage isn't part of AnyMessage in the stubs but LangGraph's add_messages
    # reducer handles it natively to evict messages from the checkpointer.
    state_update: list = [*removals, summary_msg]
    return [summary_msg] + kept, state_update
