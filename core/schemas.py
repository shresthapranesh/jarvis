"""Pydantic request/response models for the API."""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .model_catalog import DEFAULT_MODEL


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
    title: str


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


def _invalid_model_response(model: str) -> JSONResponse:
    return JSONResponse(
        {"error": f"unknown model {model!r}; GET /models for the catalog"},
        status_code=400,
    )
