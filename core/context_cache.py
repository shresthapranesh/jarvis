"""Context caching config and helpers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

# Anthropic/Bedrock allow up to 4 cache breakpoints per request. The system
# message spends at most two (static prompt, end of the stable segments) and
# the history one (mark_history), so the budget is never the limiting factor.
MAX_CACHE_BREAKPOINTS = 4

# Providers whose LangChain integration only understands a standalone
# `{"cachePoint": ...}` block. ChatBedrockConverse rebuilds every text block as
# `{"text": ...}` and silently drops a `cache_control` key on it, so marking
# Bedrock the Anthropic way sends a request with no cache points at all.
_CACHE_POINT_PROVIDERS = frozenset({"bedrock"})

# ChatOpenAI (the OpenRouter integration) keeps `cache_control` on user and
# assistant content parts but strips it from role=tool messages, so a tool
# result can't carry the history breakpoint there.
_TOOL_MESSAGE_UNMARKABLE_PROVIDERS = frozenset({"openrouter"})


@dataclass
class CacheSegment:
    """One logical piece of the system prompt.

    name:       human label for logs (core_memory, skills, etc.)
    content:    text; empty content is skipped
    cacheable:  if True, gets its own cache_control block when use_cache=True
                and we still have breakpoint budget.
    tokens_estimate: optional rough token count for stats (chars//4)
    """

    name: str
    content: str
    cacheable: bool = True
    tokens_estimate: int | None = None

    def __post_init__(self):
        if self.tokens_estimate is None and self.content:
            self.tokens_estimate = len(self.content) // 4


# Providers whose API accepts the extended `ttl` field on a cache_control block.
# Deliberately anthropic-only: Bedrock's Converse API exposes cache points with a
# different shape, and an unsupported field there would fail *every* call rather
# than degrade, so it stays on the 5m default until someone verifies it.
_EXTENDED_TTL_PROVIDERS = frozenset({"anthropic"})
_VALID_TTLS = frozenset({"5m", "1h"})
DEFAULT_CACHE_TTL = "5m"


def resolve_cache_ttl(provider: str) -> str:
    """Cache-block TTL for `provider`, from `JARVIS_CACHE_TTL` (`5m` | `1h`).

    Defaults to 5m — the API default, and the value that keeps the emitted
    cache_control byte-identical to what we sent before this was configurable.

    1h is a cost trade, not a free win: it keeps the prefix warm across a user's
    pause, but bills cache *writes* at 2x base instead of 1.25x. It only pays off
    if reads within the hour actually happen, so it's opt-in and worth measuring
    against your own traffic rather than switching on by default.
    """
    raw = (os.environ.get("JARVIS_CACHE_TTL") or DEFAULT_CACHE_TTL).strip()
    if raw not in _VALID_TTLS:
        logger.warning(
            "ignoring JARVIS_CACHE_TTL=%r — expected one of %s",
            raw,
            ", ".join(sorted(_VALID_TTLS)),
        )
        return DEFAULT_CACHE_TTL
    if raw != DEFAULT_CACHE_TTL and provider not in _EXTENDED_TTL_PROVIDERS:
        logger.info(
            "JARVIS_CACHE_TTL=%s ignored for provider %r — extended TTL is only "
            "wired for %s",
            raw,
            provider,
            ", ".join(sorted(_EXTENDED_TTL_PROVIDERS)),
        )
        return DEFAULT_CACHE_TTL
    return raw


@dataclass
class ContextCacheConfig:
    """Jarvis-like config for how caching is applied.

    enabled: whether caching is on (model provider supports it)
    max_breakpoints: max breakpoints per request (Anthropic/Bedrock limit 4)
    min_chars_for_cache: retained for RunnerConfig compatibility. Placement no
        longer depends on segment size: one breakpoint closes every block before
        it, so a small segment rides in the cached prefix without spending one.
    cache_ttl: "5m" (API default) or "1h" — see resolve_cache_ttl
    provider: decides how a breakpoint is spelled (`cache_control` key vs a
        Bedrock `cachePoint` block) and which messages can carry one
    """

    enabled: bool = True
    max_breakpoints: int = MAX_CACHE_BREAKPOINTS
    min_chars_for_cache: int = 50
    cache_ttl: str = DEFAULT_CACHE_TTL
    provider: str = "anthropic"

    def cache_control(self) -> dict[str, str]:
        """The cache_control payload for one block.

        `ttl` is omitted at the default so the request body stays exactly what it
        was before this setting existed — an added field is an added way to break.
        """
        if self.cache_ttl == DEFAULT_CACHE_TTL:
            return {"type": "ephemeral"}
        return {"type": "ephemeral", "ttl": self.cache_ttl}

    def mark(self, blocks: list[Any]) -> list[Any]:
        """Return `blocks` with a breakpoint closing the last one.

        Bedrock gets a trailing `cachePoint` block; everything else gets
        `cache_control` on the last block itself. Never mutates `blocks`.
        """
        if self.provider in _CACHE_POINT_PROVIDERS:
            return [*blocks, {"cachePoint": {"type": "default"}}]
        last = blocks[-1]
        if isinstance(last, str):
            last = {"type": "text", "text": last}
        return [*blocks[:-1], {**last, "cache_control": self.cache_control()}]


@dataclass
class CacheStats:
    """Per-call cache stats for /server-logs observability."""

    segments_total: int = 0
    segments_cached: int = 0
    cached_tokens_est: int = 0
    volatile_tokens_est: int = 0
    breakpoints_used: int = 0


_last_stats: CacheStats | None = None


def get_last_cache_stats() -> CacheStats | None:
    return _last_stats


def build_cached_system_message(
    *,
    static_prompt: str,
    segments: list[CacheSegment],
    volatile_suffix: str = "",
    use_cache: bool = False,
    config: ContextCacheConfig | None = None,
) -> tuple[SystemMessage, CacheStats]:
    """Build a SystemMessage with explicit cache breakpoints (caching pattern).

    Layout when use_cache=True:
        static_prompt                          ← breakpoint
        cacheable segments, in the given order ← one breakpoint after the last
        non-cacheable segments + volatile_suffix (unmarked)

    Every cacheable segment stays in the cached region whatever its size; a
    breakpoint covers all blocks before it, so per-segment breakpoints only buy
    a fallback for when a *later* segment changes, which is not worth the
    budget the history breakpoint needs. Callers on the agent path pass no
    volatile content here — it goes after the history (build_llm_messages), so
    churning it can't invalidate the cached conversation.

    When use_cache=False: single text block with all concatenated.

    Returns (SystemMessage, CacheStats).
    """
    global _last_stats

    cfg = config or ContextCacheConfig(enabled=use_cache)
    stats = CacheStats()
    all_segments = [s for s in segments if s.content and s.content.strip()]

    # Quick path: no cache — concatenate everything (no cache_control blocks)
    if not cfg.enabled or not use_cache:
        parts = [static_prompt]
        for seg in all_segments:
            parts.append(seg.content)
            stats.volatile_tokens_est += seg.tokens_estimate or 0
        if volatile_suffix and volatile_suffix.strip():
            parts.append(volatile_suffix)
            stats.volatile_tokens_est += len(volatile_suffix) // 4
        stats.segments_total = len(all_segments)
        full = "\n\n".join(p for p in parts if p and p.strip())
        _last_stats = stats
        return SystemMessage(content=full), stats

    # Cache path: build blocks
    blocks: list[dict[str, Any] | str] = []
    stats.breakpoints_used = 0

    # Static prompt — shared by every conversation, so it gets its own breakpoint.
    if static_prompt.strip():
        blocks.extend(cfg.mark([{"type": "text", "text": static_prompt}]))
        stats.breakpoints_used = 1
        stats.segments_cached = 1
        stats.cached_tokens_est += len(static_prompt) // 4

    stats.segments_total = len(all_segments)
    cached_parts = [s for s in all_segments if s.cacheable]
    # Counted below via the joined volatile block, so no per-segment increment.
    non_cached_parts = [s.content for s in all_segments if not s.cacheable]

    if cached_parts:
        blocks.extend(cfg.mark([{"type": "text", "text": s.content} for s in cached_parts]))
        stats.breakpoints_used += 1
        for seg in cached_parts:
            stats.segments_cached += 1
            stats.cached_tokens_est += seg.tokens_estimate or 0
            logger.debug("cache segment %s cached: ~%d tokens", seg.name, seg.tokens_estimate or 0)

    # Final volatile block: non-cached segments + volatile_suffix (no cache_control)
    volatile_parts = [p for p in non_cached_parts if p and p.strip()]
    if volatile_suffix and volatile_suffix.strip():
        volatile_parts.append(volatile_suffix)

    if volatile_parts:
        volatile_text = "\n\n".join(volatile_parts)
        if volatile_text.strip():
            blocks.append({"type": "text", "text": volatile_text})
            stats.volatile_tokens_est += len(volatile_text) // 4
    else:
        # No volatile, but we need at least something? Already have cached blocks.
        pass

    # Edge: if somehow no blocks (empty prompt), fallback
    if not blocks:
        blocks = [static_prompt]

    _last_stats = stats
    logger.debug(
        "context cache built: cached_segments=%d/%d breakpoints=%d/%d cached_tokens~%d volatile~%d",
        stats.segments_cached,
        stats.segments_total,
        stats.breakpoints_used,
        cfg.max_breakpoints,
        stats.cached_tokens_est,
        stats.volatile_tokens_est,
    )

    return SystemMessage(content=blocks), stats


# ── History breakpoint ───────────────────────────────────────────────────────

def _is_tool_result_carrier(msg: AnyMessage) -> bool:
    content = getattr(msg, "content", None)
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def normalize_history_content(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Give every user/tool message list-of-blocks content.

    The history breakpoint can only be attached to a block, so the message
    carrying it is sent as `[{"type": "text", ...}]` — and on the next call,
    when the breakpoint has moved on, that same message must serialize the same
    way or the prefix it was cached under no longer matches (langchain_anthropic
    sends a plain-string ToolMessage as `"content": "r"`, a list one as a list
    of blocks). Converting all of them makes marked and unmarked differ only by
    the breakpoint itself. AI messages are never marked, so they're left alone.
    """
    out: list[AnyMessage] = []
    for m in messages:
        content = m.content
        if (
            isinstance(m, (HumanMessage, ToolMessage))
            and isinstance(content, str)
            and content.strip()
        ):
            m = m.model_copy(update={"content": [{"type": "text", "text": content}]})
        out.append(m)
    return out


def _markable(msg: AnyMessage, provider: str) -> bool:
    if isinstance(msg, (AIMessage, SystemMessage)):
        # An AI message's last block may be tool_use or thinking, and the
        # integrations disagree on whether either can take a breakpoint.
        return False
    if not isinstance(msg, (HumanMessage, ToolMessage)):
        return False
    if provider in _TOOL_MESSAGE_UNMARKABLE_PROVIDERS and (
        isinstance(msg, ToolMessage) or _is_tool_result_carrier(msg)
    ):
        return False
    content = msg.content
    if not isinstance(content, list) or not content:
        return False
    if provider in _CACHE_POINT_PROVIDERS:
        return True
    # cache_control has to sit on the block itself: only a non-empty text
    # block or a tool_result is safe on every Anthropic-shaped integration.
    last = content[-1]
    if isinstance(last, str):
        return bool(last.strip())
    if not isinstance(last, dict):
        return False
    if last.get("type") == "text":
        return bool(str(last.get("text", "")).strip())
    return last.get("type") == "tool_result"


def mark_history(messages: list[AnyMessage], config: ContextCacheConfig) -> list[AnyMessage]:
    """Put the rolling breakpoint on the newest message that can carry one.

    Prefix caching reuses everything up to the latest breakpoint a previous
    request wrote, so marking the end of the history each call is what lets
    the next call — one tool round-trip later, or the next turn — read the
    whole conversation from cache instead of paying for it again. Expects
    `normalize_history_content` to have run, so the candidate's content is
    already a block list. Returns `messages` unchanged when nothing qualifies.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if _markable(msg, config.provider):
            marked = msg.model_copy(update={"content": config.mark(list(msg.content))})
            return [*messages[:i], marked, *messages[i + 1:]]
    return messages
