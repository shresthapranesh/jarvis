"""Agent memory query."""

from __future__ import annotations

import strawberry

from core.memory_consolidation import _MEMORY_KEY, _MEMORY_NS, _migrate_legacy_key
from core.state import get_store
from db.ops import list_memories

from ..types.memory import Memory, MemoryItem


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
