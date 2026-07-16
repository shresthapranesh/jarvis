"""Agent memory query."""

from __future__ import annotations

import strawberry

from core.memory_consolidation import _MEMORY_KEY, _MEMORY_NS, _migrate_legacy_key
from core.state import get_store
from db.ops import get_memory_usage_map, list_memories, list_memory_activities

from ..types.memory import Memory, MemoryActivity, MemoryItem


@strawberry.type
class MemoryQuery:
    @strawberry.field
    async def agent_memory(self) -> Memory:
        """The legacy free-text blob (keyless fallback / pre-migration backup)."""
        store = get_store()
        await _migrate_legacy_key(store)
        item = await store.aget(_MEMORY_NS, _MEMORY_KEY)
        if item is None:
            return Memory(content="", exists=False, modified_at=None)
        raw = item.value.get("content", "")
        if isinstance(raw, list):
            raw = "\n".join(raw)
        return Memory(
            content=raw,
            exists=True,
            modified_at=item.value.get("modified_at"),
        )

    @strawberry.field
    async def memories(
        self, info: strawberry.Info, kind: str | None = None
    ) -> list[MemoryItem]:
        """Discrete memory items (empty on keyless setups, which use the blob)."""
        session = info.context["session"]
        rows = await list_memories(session, kind=kind)
        return [MemoryItem.from_db(m) for m in rows]

    @strawberry.field
    async def memory_activities(
        self, info: strawberry.Info, memory_id: str, limit: int = 50
    ) -> list[MemoryActivity]:
        """Audit log for when a specific memory was surfaced."""
        session = info.context["session"]
        rows = await list_memory_activities(session, memory_id, limit=limit)
        return [MemoryActivity.from_db(r) for r in rows]

    @strawberry.field
    async def memory_usage(
        self, info: strawberry.Info
    ) -> list[MemoryItem]:
        """All memories (same as memories) but ensures usage resolvers have data —
        use memories query with lastUsedAt/useCount fields for per-item usage.
        This field exists for explicit usage overview.
        """
        session = info.context["session"]
        rows = await list_memories(session)
        # Prefetch usage map to warm cache? Resolvers will fetch per-id anyway.
        return [MemoryItem.from_db(m) for m in rows]
