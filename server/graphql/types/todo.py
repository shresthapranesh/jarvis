"""GraphQL type for a single todo item (mirrors core.schemas.TodoItem)."""

from __future__ import annotations

import strawberry


@strawberry.type
class TodoItem:
    text: str
    status: str  # "pending" | "in_progress" | "done"
