"""DEPRECATED - use core.compaction.maybe_compact.

Kept for backwards compatibility; now proxies to compaction module.
Original implementation kept 10 messages and summarized everything each time,
which was wasteful. New implementation keeps last 2 groups, caches summary,
and only summarizes delta (see core/compaction.py).
"""

from __future__ import annotations

import logging
import os
import warnings

from langchain_core.messages import AnyMessage

logger = logging.getLogger(__name__)


def summarize_threshold() -> int:
    """Deprecated - use compaction.compact_threshold()."""
    from .compaction import compact_threshold

    return compact_threshold()


KEEP_RECENT = 10  # kept for compat, new default is KEEP_RECENT_GROUPS=2 in compaction.py


async def maybe_summarize(
    messages: list[AnyMessage],
    *,
    llm,
    summarizer,
) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
    """Deprecated wrapper - delegates to compaction.maybe_compact."""
    warnings.warn(
        "maybe_summarize is deprecated, use compaction.maybe_compact",
        DeprecationWarning,
        stacklevel=2,
    )
    from .compaction import maybe_compact

    return await maybe_compact(messages, llm=llm, summarizer=summarizer)
