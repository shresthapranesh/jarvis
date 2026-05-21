"""Pydantic / TypedDict shapes shared across runtime modules.

The REST request/response models that used to live here were removed when
the API surface moved to GraphQL. What remains is the cross-cutting state
shapes still consumed by the agent runtime, bots, and GraphQL resolvers.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel


# ── Agent state shapes ────────────────────────────────────────────────────────

class TodoItem(TypedDict):
    text: str
    status: Literal["pending", "in_progress", "done"]


def _normalise_todos(raw: object) -> list[TodoItem]:
    """Coerce todos read from state/checkpointer into a uniform shape.

    Accepts both legacy `list[str]` (from older checkpoints / older
    `write_todos` calls) and the new `list[TodoItem]` shape.
    """
    if not raw or not isinstance(raw, list):
        return []
    out: list[TodoItem] = []
    for item in raw:
        if isinstance(item, str):
            out.append({"text": item, "status": "pending"})
        elif isinstance(item, dict) and "text" in item:
            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "done"):
                status = "pending"
            out.append({"text": str(item["text"]), "status": status})  # type: ignore[typeddict-item]
    return out


class AttachmentIn(BaseModel):
    type: str       # image | audio | video | document
    name: str
    mime_type: str
    data: str       # raw base64 (no data URL prefix)
    size: int


class TTSRequest(BaseModel):
    text: str
