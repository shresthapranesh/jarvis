"""DEPRECATED - use core.compaction.maybe_compact."""

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
    """Deprecated wrapper - delegates to compaction.maybe_compact.

    Keeps the old ``(messages, state_update) | None`` shape; maybe_compact now
    always returns a CompactionResult and reports "didn't fire" via `compacted`.
    """
    warnings.warn(
        "maybe_summarize is deprecated, use compaction.maybe_compact",
        DeprecationWarning,
        stacklevel=2,
    )
    from .compaction import maybe_compact

    result = await maybe_compact(messages, llm=llm, summarizer=summarizer)
    if not result.compacted:
        return None
    return result.messages, result.state_update
