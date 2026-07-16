"""GraphQL types for agent memory."""

from __future__ import annotations

import strawberry
from strawberry import Info

from db.models import Memory as MemoryModel
from db.models import MemoryActivity as MemoryActivityModel


@strawberry.type
class Memory:
    content: str
    exists: bool
    modified_at: str | None


@strawberry.type
class MemoryActivity:
    """One audit entry for when a memory was surfaced."""

    id: str
    memory_id: str
    conversation_id: str | None
    kind: str
    score: float | None
    query: str | None
    source: str
    accessed_at: str

    @classmethod
    def from_db(cls, m: MemoryActivityModel) -> "MemoryActivity":
        return cls(
            id=m.id,
            memory_id=m.memory_id,
            conversation_id=m.conversation_id,
            kind=m.kind,
            score=m.score,
            query=m.query,
            source=m.source,
            accessed_at=m.accessed_at.isoformat(),
        )


@strawberry.type
class MemoryItem:
    """One discrete memory item (kind = 'core' | 'fact'). Raw DB id."""

    id: str
    kind: str
    text: str
    updated_at: str

    @classmethod
    def from_db(cls, m: MemoryModel) -> "MemoryItem":
        return cls(
            id=m.id,
            kind=m.kind,
            text=m.text,
            updated_at=m.updated_at.isoformat(),
        )

    @strawberry.field
    async def last_used_at(self, info: Info) -> str | None:
        """When this memory was last surfaced (from memory_activities)."""
        session = info.context["session"]
        from db.ops import get_memory_usage_map

        usage = await get_memory_usage_map(session, [self.id])
        entry = usage.get(self.id)
        if entry and entry.get("last_used"):
            return entry["last_used"].isoformat()
        return None

    @strawberry.field
    async def use_count(self, info: Info) -> int:
        """How many times this memory was surfaced."""
        session = info.context["session"]
        from db.ops import get_memory_usage_map

        usage = await get_memory_usage_map(session, [self.id])
        entry = usage.get(self.id)
        return entry["count"] if entry else 0

    @strawberry.field
    async def activities(self, info: Info, limit: int = 20) -> list[MemoryActivity]:
        """Recent audit log entries for this memory."""
        session = info.context["session"]
        from db.ops import list_memory_activities

        rows = await list_memory_activities(session, self.id, limit=limit)
        return [MemoryActivity.from_db(r) for r in rows]
