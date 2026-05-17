"""Pydantic request/response models for the API."""

from __future__ import annotations

from typing import Literal, TypedDict

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .model_catalog import DEFAULT_MODEL


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


class RunRequest(BaseModel):
    query: str
    model: str = DEFAULT_MODEL
    conversation_id: str | None = None
    attachments: list[AttachmentIn] | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    model: str | None = None


class AutomationRequest(BaseModel):
    name: str
    description: str | None = None
    input_type: str  # prompt | code | webhook
    prompt_text: str | None = None
    model: str | None = None
    code_text: str | None = None
    webhook_url: str | None = None
    webhook_method: str | None = None
    webhook_headers: str | None = None  # JSON string
    webhook_body: str | None = None
    schedule: str | None = None
    enabled: bool = True
    notifications: str | None = None  # JSON array of channel configs


class ResumePayload(BaseModel):
    answer: str


class TTSRequest(BaseModel):
    text: str


class MemoryUpdate(BaseModel):
    content: str


# ── Workflow schemas ───────────────────────────────────────────────────────────

class WorkflowCreateRequest(BaseModel):
    name: str
    description: str | None = None
    definition: str = "{}"  # JSON string
    notifications: str | None = None  # JSON array of channel configs


class WorkflowUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    definition: str | None = None  # JSON string
    notifications: str | None = None  # JSON array of channel configs


class WorkflowRunRequest(BaseModel):
    inputs: dict[str, str] = {}


# ── Notification channels ─────────────────────────────────────────────────────

class NotificationChannelCreate(BaseModel):
    name: str
    type: Literal["telegram", "discord"]
    target: str


class NotificationChannelUpdate(BaseModel):
    name: str | None = None
    type: Literal["telegram", "discord"] | None = None
    target: str | None = None


class NotificationChannelOut(BaseModel):
    id: str
    name: str
    type: str
    target: str
    created_at: str
    updated_at: str


def _invalid_model_response(model: str) -> JSONResponse:
    return JSONResponse(
        {"error": f"unknown model {model!r}; GET /models for the catalog"},
        status_code=400,
    )
