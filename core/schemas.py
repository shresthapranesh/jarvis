"""Pydantic / TypedDict shapes shared across runtime modules."""

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


_TODO_RANK = {"pending": 0, "in_progress": 1, "done": 2}


def reduce_todos(current: object, update: object) -> list[TodoItem]:
    """Reducer for the `todos` state channel.

    Plain LastValue rejects two writes in one super-step, so when the model
    emits parallel todo tool calls (e.g. two `set_todo_status` in one turn)
    LangGraph raises INVALID_CONCURRENT_GRAPH_UPDATE and the run dies. Each tool
    returns the *full* intended list, so we merge index-by-index, keeping the
    more-advanced status — neither concurrent change is lost.

    A length change means the list was replaced (`write_todos`) or cleared, so
    the incoming value wins outright; that also lets `todos: []` reset the plan.
    """
    cur = _normalise_todos(current)
    upd = _normalise_todos(update)
    if not cur or not upd or len(cur) != len(upd):
        return upd
    return [
        u if _TODO_RANK[u["status"]] >= _TODO_RANK[c["status"]] else c
        for c, u in zip(cur, upd)
    ]


class AttachmentIn(BaseModel):
    type: str       # image | audio | video | document
    name: str
    mime_type: str
    data: str       # raw base64 (no data URL prefix)
    size: int
    # Set by register_chat_task when it persists a Document row; lets the
    # chat handler chunk-index large documents instead of inlining them.
    # None for sources that don't persist documents (bots, CLI) → inlined.
    document_id: str | None = None


class TTSRequest(BaseModel):
    text: str
