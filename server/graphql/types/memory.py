"""GraphQL types for agent memory."""

from __future__ import annotations

import strawberry

from db.models import Memory as MemoryModel


@strawberry.type
class Memory:
    content: str
    exists: bool
    modified_at: str | None


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
