"""GraphQL types for agent memory."""

from __future__ import annotations

import strawberry


@strawberry.type
class Memory:
    content: str
    exists: bool
    modified_at: str | None
