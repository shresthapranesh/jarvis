"""Context caching config and helpers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)

# Anthropic/Bedrock allow up to 4 cache breakpoints per request.
MAX_CACHE_BREAKPOINTS = 4


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
    max_breakpoints: max cache_control blocks (Anthropic limit 4)
    min_chars_for_cache: don't cache tiny segments (< this) — waste of breakpoint
    cache_ttl: "5m" (API default) or "1h" — see resolve_cache_ttl
    """

    enabled: bool = True
    max_breakpoints: int = MAX_CACHE_BREAKPOINTS
    min_chars_for_cache: int = 50
    cache_ttl: str = DEFAULT_CACHE_TTL

    def cache_control(self) -> dict[str, str]:
        """The cache_control payload for one block.

        `ttl` is omitted at the default so the request body stays exactly what it
        was before this setting existed — an added field is an added way to break.
        """
        if self.cache_ttl == DEFAULT_CACHE_TTL:
            return {"type": "ephemeral"}
        return {"type": "ephemeral", "ttl": self.cache_ttl}


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
        [0] static_prompt (cached)
        [1] segment[0] (cached if cacheable and big enough and budget)
        [2] segment[1] (cached ...)
        [3] ...
        [N] concatenated non-cached + volatile_suffix (not cached)

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

    # Block 0: static prompt — always cached, first breakpoint
    if static_prompt.strip():
        blocks.append(
            {"type": "text", "text": static_prompt, "cache_control": cfg.cache_control()}
        )
        stats.breakpoints_used = 1
        stats.segments_cached = 1
        stats.cached_tokens_est += len(static_prompt) // 4

    # Remaining cache budget: max_breakpoints - 1 (volatile must stay uncached)
    # Actually we want last block to be uncached volatile, so reserve 0 for it.
    # We have max_breakpoints total blocks can be marked cached. If we have N cached
    # blocks, last cached block's breakpoint still valid. Volatile after it is uncached.
    cache_budget = cfg.max_breakpoints - stats.breakpoints_used

    cached_parts: list[CacheSegment] = []
    non_cached_parts: list[str] = []

    for seg in all_segments:
        stats.segments_total += 1
        is_big_enough = len(seg.content) >= cfg.min_chars_for_cache
        if seg.cacheable and is_big_enough and cache_budget > 0:
            # Will be its own cached block
            cached_parts.append(seg)
            cache_budget -= 1
        else:
            # Goes to volatile concatenation — counted below via the joined
            # volatile block, so no per-segment increment here (would double-count).
            non_cached_parts.append(seg.content)

    # Emit cached segments as individual blocks
    for seg in cached_parts:
        blocks.append(
            {"type": "text", "text": seg.content, "cache_control": cfg.cache_control()}
        )
        stats.segments_cached += 1
        stats.breakpoints_used += 1
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
