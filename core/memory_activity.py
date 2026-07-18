"""Memory activity audit log — tracks when memories were surfaced."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from db.engine import async_session
from db.ops import log_memory_activities

logger = logging.getLogger(__name__)


async def touch_memories(
    memory_ids: list[str],
    *,
    scores: dict[str, float] | None = None,
    query: str | None = None,
    conversation_id: str | None = None,
    kind: str = "fact",
    source: str = "retrieval",
) -> int:
    """Log that memory_ids were accessed. Best-effort, never raises."""
    try:
        if not memory_ids:
            return 0

        activities = []
        for mid in memory_ids:
            activities.append(
                {
                    "memory_id": mid,
                    "conversation_id": conversation_id,
                    "kind": kind,
                    "score": (scores or {}).get(mid),
                    "query": query,
                    "source": source,
                }
            )

        async with async_session() as session:
            count = await log_memory_activities(session, activities)

        logger.debug(
            "memory_activity logged %d memories source=%s query=%r",
            count,
            source,
            (query or "")[:80],
        )
        return count

    except Exception as exc:
        logger.warning("memory_activity touch failed: %s", exc)
        return 0


def touch_memories_background(
    memory_ids: list[str],
    *,
    scores: dict[str, float] | None = None,
    query: str | None = None,
    conversation_id: str | None = None,
    kind: str = "fact",
    source: str = "retrieval",
) -> None:
    """Fire-and-forget wrapper — schedules touch_memories without blocking."""
    try:
        asyncio.create_task(
            touch_memories(
                memory_ids,
                scores=scores,
                query=query,
                conversation_id=conversation_id,
                kind=kind,
                source=source,
            )
        )
    except Exception as exc:
        logger.debug("memory_activity background scheduling failed: %s", exc)
