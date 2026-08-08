"""History compaction helpers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

from .messages import (
    _is_tool_result_carrier,
    elide_stale_tool_results,
    estimate_tokens,
    estimate_tokens_heuristic,
    message_text,
)

logger = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────────────────

KEEP_RECENT_GROUPS = 2  # keep last N groups verbatim (user asked 2, but pinning may expand)
KEEP_LAST_TOOL_GROUPS_RAW = 4  # per-call: keep last 4 tool_call groups full (aligned with elide TOOL_RESULT_KEEP_TURNS=4)
TOOL_RESULT_COLLAPSE_MAX_CHARS = 300


def compact_threshold() -> int:
    """Token count at which destructive summarization triggers.

    Defaults to 80k (original was 100k, briefly 40k). 40k is too aggressive for
    destructive trimming — per-call compaction handles that regime.
    Override with JARVIS_COMPACT_TOKEN_THRESHOLD for testing.
    """
    raw = os.environ.get("JARVIS_COMPACT_TOKEN_THRESHOLD") or os.environ.get(
        "JARVIS_SUMMARIZE_TOKEN_THRESHOLD"
    )
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 80_000


# ── Grouping ─────────────────────────────────────────────────────────────────

GroupKind = Literal["system", "user", "assistant_text", "tool_call", "summary"]


@dataclass
class MessageGroup:
    id: str
    kind: GroupKind
    start: int
    end: int  # inclusive
    messages: list[AnyMessage]


def _is_summary_system(msg: AnyMessage) -> bool:
    if not isinstance(msg, SystemMessage):
        return False
    txt = message_text(msg).lower()
    return "[conversation summary" in txt or "[prior conversation summary" in txt


def _is_system(msg: AnyMessage) -> bool:
    return isinstance(msg, SystemMessage)


def _is_human_user(msg: AnyMessage) -> bool:
    # HumanMessage that carries tool_result blocks is NOT a user turn
    if _is_tool_result_carrier(msg):
        return False
    return isinstance(msg, HumanMessage) or getattr(msg, "type", "") == "human"


def _is_ai_with_tools(msg: AnyMessage) -> bool:
    return isinstance(msg, AIMessage) and bool(getattr(msg, "tool_calls", None))


def group_messages(messages: list[AnyMessage]) -> list[MessageGroup]:
    """Group messages into semantic turns like MAF's group_messages."""
    groups: list[MessageGroup] = []
    i = 0
    idx = 0
    n = len(messages)

    while i < n:
        msg = messages[i]

        if _is_summary_system(msg):
            groups.append(MessageGroup(id=f"group_{idx}", kind="summary", start=i, end=i, messages=[msg]))
            i += 1
            idx += 1
            continue

        if _is_system(msg):
            groups.append(MessageGroup(id=f"group_{idx}", kind="system", start=i, end=i, messages=[msg]))
            i += 1
            idx += 1
            continue

        if _is_human_user(msg):
            groups.append(MessageGroup(id=f"group_{idx}", kind="user", start=i, end=i, messages=[msg]))
            i += 1
            idx += 1
            continue

        if _is_ai_with_tools(msg):
            start = i
            j = i + 1
            while j < n and _is_tool_result_carrier(messages[j]):
                j += 1
            groups.append(MessageGroup(id=f"group_{idx}", kind="tool_call", start=start, end=j - 1, messages=messages[start:j]))
            i = j
            idx += 1
            continue

        if _is_tool_result_carrier(msg):
            start = i
            j = i + 1
            while j < n and _is_tool_result_carrier(messages[j]):
                j += 1
            groups.append(MessageGroup(id=f"group_{idx}", kind="tool_call", start=start, end=j - 1, messages=messages[start:j]))
            i = j
            idx += 1
            continue

        # Default: assistant_text
        groups.append(MessageGroup(id=f"group_{idx}", kind="assistant_text", start=i, end=i, messages=[msg]))
        i += 1
        idx += 1

    return groups


# ── Per-call cheap compaction (MAF ToolResultCompaction analog) ──────────────

def _summarize_tool_group_brief(group: MessageGroup) -> str:
    """Create a short stub for a tool_call group."""
    names: list[str] = []
    results: list[str] = []
    for m in group.messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls or []:
                if isinstance(tc, dict):
                    names.append(tc.get("name", "?"))
        elif _is_tool_result_carrier(m):
            tname = getattr(m, "name", "tool") or "tool"
            # Use message_text which handles both str and list content (vision, structured)
            txt = message_text(m)[:TOOL_RESULT_COLLAPSE_MAX_CHARS]
            if not txt:
                # Fallback to raw content repr slicing
                txt = str(getattr(m, "content", ""))[:TOOL_RESULT_COLLAPSE_MAX_CHARS]
            names.append(tname)
            if txt:
                results.append(f"{tname}: {txt}")
    if not names:
        names = ["tool"]
    # Dedup preserve order
    deduped_names = list(dict.fromkeys(names))
    summary = "; ".join(results)[:500] if results else ", ".join(deduped_names)
    return f"[Previous tool activity: {', '.join(deduped_names)} => {summary}]"


def collapse_old_tool_results(
    messages: list[AnyMessage],
    *,
    keep_last_tool_groups: int = KEEP_LAST_TOOL_GROUPS_RAW,
    groups: list[MessageGroup] | None = None,
) -> list[AnyMessage]:
    """Per-call, non-persistent collapse of old tool_call groups into short AIMessage stubs.

    `groups` lets a caller that already grouped `messages` hand the result in
    rather than paying for a second O(n) pass; it must be the grouping of
    exactly these messages.
    """
    if groups is None:
        groups = group_messages(messages)
    tool_groups = [g for g in groups if g.kind == "tool_call"]
    if len(tool_groups) <= keep_last_tool_groups:
        return messages

    to_collapse_ids = set(g.id for g in tool_groups[:-keep_last_tool_groups])

    new_messages: list[AnyMessage] = []
    for g in groups:
        if g.id in to_collapse_ids:
            stub_text = _summarize_tool_group_brief(g)
            first_id = getattr(g.messages[0], "id", None)
            new_messages.append(AIMessage(content=stub_text, id=first_id))
        else:
            new_messages.extend(g.messages)
    return new_messages


def apply_per_call_compaction(
    messages: list[AnyMessage], *, keep_last_tool_groups: int = KEEP_LAST_TOOL_GROUPS_RAW
) -> list[AnyMessage]:
    """Cheap path: elide stale + collapse old tool results. No persistence, no LLM calls.

    One elide pass and one grouping pass. Callers that also need a token count
    should go through `maybe_compact`, which does this once and hands the result
    back on its `CompactionResult` — calling both duplicates every pass.
    """
    leaned = elide_stale_tool_results(messages)
    groups = group_messages(leaned)
    return collapse_old_tool_results(
        leaned, keep_last_tool_groups=keep_last_tool_groups, groups=groups
    )


# ── Persistent incremental summarization ─────────────────────────────────────

def _build_safe_messages_for_summary(groups: list[MessageGroup]) -> list[AnyMessage]:
    """Convert groups to plain Human/AI/System messages safe for any summarizer LLM."""
    safe: list[AnyMessage] = []
    for g in groups:
        for m in g.messages:
            if _is_human_user(m):
                safe.append(m)
            elif isinstance(m, AIMessage):
                text = message_text(m)
                tool_calls = getattr(m, "tool_calls", [])
                if tool_calls:
                    tc_desc = ", ".join(
                        f"{tc.get('name', '?')}({', '.join(f'{k}=...' for k in (tc.get('args') or {}))})"
                        for tc in tool_calls
                        if isinstance(tc, dict)
                    )
                    text = f"{text}\n[Called tools: {tc_desc}]" if text else f"[Called tools: {tc_desc}]"
                if text:
                    safe.append(AIMessage(content=text))
            elif _is_tool_result_carrier(m):
                tool_name = getattr(m, "name", "tool")
                # Use message_text to handle list content (vision, structured)
                tool_content = message_text(m)[:500] or str(getattr(m, "content", ""))[:500]
                # If it's a HumanMessage carrying tool_result, still label as tool result
                safe.append(HumanMessage(content=f"[Tool result from {tool_name}]: {tool_content}"))
            elif isinstance(m, SystemMessage):
                if not _is_summary_system(m):
                    safe.append(m)
    return safe


@dataclass
class CompactionResult:
    """What one compaction check produced.

    `messages` is always the per-call-compacted view ready to hand to the LLM,
    whether or not destructive summarization fired — the leaned view has to be
    built to count tokens anyway, so returning it saves the caller an identical
    second `apply_per_call_compaction` pass on every agent-loop iteration.

    `state_update` is the RemoveMessage list + summary to write back into graph
    state; empty unless `compacted` is True.
    """

    messages: list[AnyMessage]
    state_update: list = field(default_factory=list)
    compacted: bool = False


async def maybe_compact(
    messages: list[AnyMessage],
    *,
    llm,
    summarizer,
    threshold: int | None = None,
    keep_recent_groups: int = KEEP_RECENT_GROUPS,
) -> CompactionResult:
    """Incremental sliding-window summarization with caching.

    - Token estimate is over per-call-compacted view (elide-first restored)
    - Grouping/removal is over raw messages
    - Most recent user group is pinned always-kept
    - Kept window forced to start with user if possible
    - Merges ALL prior summaries, not just first

    Always returns a `CompactionResult`; `compacted` says whether summarization
    actually fired. Every early return still carries the leaned view, so the
    caller never recomputes it.
    """
    if not messages:
        return CompactionResult(messages=[])

    th = threshold if threshold is not None else compact_threshold()

    # ── 1. Cheap view — used both for token counting and, unchanged, as the
    #      messages handed to the LLM when compaction doesn't fire. ─────────
    leaned_for_count = apply_per_call_compaction(messages)
    no_compaction = CompactionResult(messages=leaned_for_count)

    heuristic = estimate_tokens_heuristic(leaned_for_count)
    if heuristic <= int(th * 0.8):
        logger.debug(
            "compact check: ~%d tokens / %d msgs under %d threshold — skip (heuristic)",
            heuristic,
            len(leaned_for_count),
            th,
        )
        return no_compaction

    token_count = estimate_tokens(leaned_for_count, llm)
    if token_count <= th:
        logger.debug(
            "compact check: %d tokens / %d msgs under %d threshold — skip",
            token_count,
            len(leaned_for_count),
            th,
        )
        return no_compaction

    # ── 2. Group raw messages for actual eviction ───────────────────────
    groups = group_messages(messages)
    if len(groups) <= keep_recent_groups:
        logger.debug("compact: only %d groups, keep=%d — skip", len(groups), keep_recent_groups)
        return no_compaction

    # Identify ALL old summary groups (could be multiple after migration)
    old_summary_groups: list[MessageGroup] = [g for g in groups if g.kind == "summary"]
    old_summary_texts: list[str] = [message_text(m) for g in old_summary_groups for m in g.messages]
    old_summary_text = "\n\n".join(old_summary_texts) if old_summary_texts else None

    # ── 3. Determine kept set with pinning rules (non-contiguous support) ─
    non_summary_groups = [g for g in groups if g.kind != "summary"]
    if len(non_summary_groups) <= keep_recent_groups:
        return no_compaction

    # Start with last N groups
    kept_set: set[str] = set(g.id for g in non_summary_groups[-keep_recent_groups:])

    # Pin most recent user group as always-kept (fix #3)
    user_groups = [g for g in non_summary_groups if g.kind == "user"]
    if user_groups:
        most_recent_user = user_groups[-1]
        kept_set.add(most_recent_user.id)

    # Ensure first kept is user if possible - avoid assistant-first after compaction
    kept_groups_tmp = sorted([g for g in non_summary_groups if g.id in kept_set], key=lambda g: g.start)
    if kept_groups_tmp and kept_groups_tmp[0].kind != "user":
        for g in reversed(non_summary_groups):
            if g.end < kept_groups_tmp[0].start and g.kind == "user":
                kept_set.add(g.id)
                break

    kept_groups = sorted([g for g in non_summary_groups if g.id in kept_set], key=lambda g: g.start)
    kept_start = min(g.start for g in kept_groups) if kept_groups else len(messages)

    # Groups to summarize = all non-summary groups not in kept set
    groups_to_summarize = [g for g in groups if g.kind != "summary" and g.id not in kept_set]

    if not groups_to_summarize:
        logger.debug("compact: no new groups to summarize beyond kept %d (pinned start %d)", keep_recent_groups, kept_start)
        return no_compaction

    logger.info(
        "compact triggered: %d tokens (leaned %d msgs) / %d raw msgs / %d groups -> keeping %d recent groups (from idx %d), summarizing %d old groups%s",
        token_count,
        len(leaned_for_count),
        len(messages),
        len(groups),
        len(kept_groups),
        kept_start,
        len(groups_to_summarize),
        f" (merging {len(old_summary_groups)} existing summaries)" if old_summary_text else "",
    )

    safe_msgs = _build_safe_messages_for_summary(groups_to_summarize)
    if not safe_msgs:
        logger.warning("compact: safe_msgs empty after conversion — skip")
        return no_compaction

    # ── 4. Summarize delta ───────────────────────────────────────────────
    try:
        delta_summary_resp = await summarizer.ainvoke(
            [
                SystemMessage(
                    "Summarize the following conversation history concisely. "
                    "Preserve key facts, decisions, tool outputs, file changes, and unresolved items. "
                    "Keep it under 600 words."
                ),
                *safe_msgs,
            ]
        )
        delta_summary_text = (
            delta_summary_resp.content
            if isinstance(delta_summary_resp.content, str)
            else str(delta_summary_resp.content)
        )
    except Exception as exc:
        logger.warning("compaction delta summarization failed (%s: %s) — skip", type(exc).__name__, exc)
        return no_compaction

    # ── 5. Merge with ALL old summaries if exist ─────────────────────────
    if old_summary_text:
        try:
            merged_resp = await summarizer.ainvoke(
                [
                    SystemMessage(
                        "You have an existing conversation summary and a new chunk summary. "
                        "Merge them into a single coherent summary under 800 words. "
                        "Preserve all durable facts, decisions, tool outputs, and goals. "
                        "Do not invent details. Prioritize newer information if conflict."
                    ),
                    HumanMessage(
                        content=f"Existing summary:\n{old_summary_text}\n\nNew chunk summary:\n{delta_summary_text}"
                    ),
                ]
            )
            final_summary_text = (
                merged_resp.content if isinstance(merged_resp.content, str) else str(merged_resp.content)
            )
        except Exception as exc:
            logger.warning("compaction merge failed (%s: %s) — falling back to concatenation", type(exc).__name__, exc)
            final_summary_text = f"{old_summary_text}\n\nRecent: {delta_summary_text}"
    else:
        final_summary_text = delta_summary_text

    logger.info("compacted %d groups into ~%d chars", len(groups_to_summarize), len(final_summary_text))

    summary_msg = SystemMessage(f"[Conversation summary]\n{final_summary_text}")
    # kept_messages is concatenation of kept groups (may be non-contiguous due to pinning)
    kept_messages: list[AnyMessage] = []
    for g in kept_groups:
        kept_messages.extend(g.messages)

    # Build removals for all summarized groups + ALL old summary groups (avoid leaking multiple summaries)
    removals: list = []
    for g in groups_to_summarize + old_summary_groups:
        for m in g.messages:
            if hasattr(m, "id") and getattr(m, "id"):
                removals.append(RemoveMessage(id=m.id))

    state_update: list = [*removals, summary_msg]
    # The kept window is a different list than what we leaned above, so it needs
    # its own per-call pass — but only on the rare turn compaction actually fires.
    new_messages_for_llm = apply_per_call_compaction([summary_msg] + kept_messages)

    return CompactionResult(
        messages=new_messages_for_llm, state_update=state_update, compacted=True
    )
