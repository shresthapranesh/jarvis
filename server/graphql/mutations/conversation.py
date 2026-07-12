"""Conversation mutations — startTask, stopTask, resumeTask, update, delete."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import strawberry
from strawberry import relay

from core.agents import is_valid_model
from core.config import get_config
from core.model_catalog import DEFAULT_MODEL
from core.schemas import AttachmentIn
from core.state import _tasks, emit_event
from db.ops import delete_conversation, update_conversation

from ..types.conversation import Conversation
from ..types.upload import UploadReferenceInput
from server.chat_runtime import register_chat_task


@strawberry.input
class StartTaskInput:
    query: str
    model: str | None = None
    conversation_id: str | None = None
    attachment_uploads: list[UploadReferenceInput] | None = None


@strawberry.type
class StartTaskPayload:
    task_id: str
    conversation_id: str


def _infer_attachment_type(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "document"


def _resolve_staged_uploads(
    uploads: list[UploadReferenceInput],
) -> tuple[list[AttachmentIn], list[tuple[Path, Path]]]:
    """Read each staged upload's bytes + sidecar metadata, return AttachmentIn
    objects plus the staging paths to clean up after `register_chat_task` succeeds."""
    cfg = get_config()
    out: list[AttachmentIn] = []
    paths: list[tuple[Path, Path]] = []
    for u in uploads:
        bytes_path = cfg.staging_dir / u.upload_id
        meta_path = cfg.staging_dir / f"{u.upload_id}.meta.json"
        if not bytes_path.exists() or not meta_path.exists():
            raise ValueError(f"unknown or expired upload id: {u.upload_id}")
        meta = json.loads(meta_path.read_text())
        data = bytes_path.read_bytes()
        out.append(AttachmentIn(
            type=_infer_attachment_type(meta["mime_type"]),
            name=meta["filename"],
            mime_type=meta["mime_type"],
            data=base64.b64encode(data).decode(),
            size=meta["size"],
        ))
        paths.append((bytes_path, meta_path))
    return out, paths


@strawberry.type
class ConversationMutation:
    @strawberry.mutation
    async def start_task(
        self,
        info: strawberry.Info,
        input: StartTaskInput,
    ) -> StartTaskPayload:
        model = input.model or DEFAULT_MODEL
        if not is_valid_model(model):
            raise ValueError(f"unknown model {model!r}; query `models` for the catalog")
        session = info.context["session"]

        attachments: list[AttachmentIn] | None = None
        staging_paths: list[tuple[Path, Path]] = []
        if input.attachment_uploads:
            attachments, staging_paths = _resolve_staged_uploads(input.attachment_uploads)

        task_id, conv_id = await register_chat_task(
            session, input.query, model, input.conversation_id, attachments,
        )
        # register_chat_task has now copied document bytes to documents_dir and
        # baked image/audio/video bytes into the message content; safe to drop
        # the staging copies. On failure (exception above) the periodic cleanup
        # job will sweep them after an hour.
        for bytes_path, meta_path in staging_paths:
            bytes_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
        return StartTaskPayload(task_id=task_id, conversation_id=conv_id)

    @strawberry.mutation
    async def stop_task(self, task_id: str) -> bool:
        state = _tasks.get(task_id)
        if state is None:
            raise ValueError("task not found or already finished")
        if state.done:
            raise ValueError("task already finished")

        state.cancelled = True
        state._stop_event.set()

        if state.resume_future and not state.resume_future.done():
            state.resume_future.cancel()
        return True

    @strawberry.mutation
    async def resume_task(self, task_id: str, answer: str) -> bool:
        state = _tasks.get(task_id)
        if state is None:
            raise ValueError("task not found")
        if state.resume_future is None or state.resume_future.done():
            raise ValueError("no pending interrupt for this task")

        pending_id = state.pending_interrupt_id
        state.resume_future.set_result(answer)
        emit_event(state, "interrupt_resolved", interrupt_id=pending_id)
        return True

    @strawberry.mutation
    async def update_conversation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        title: str | None = None,
        model: str | None = None,
        pinned: bool | None = None,
    ) -> Conversation:
        if model is not None and not is_valid_model(model):
            raise ValueError(f"unknown model {model!r}; query `models` for the catalog")
        if title is None and model is None and pinned is None:
            raise ValueError("no fields to update")
        session = info.context["session"]
        conv = await update_conversation(
            session, id.node_id, title=title, model=model, pinned=pinned,
        )
        if conv is None:
            raise ValueError("conversation not found")
        return Conversation.from_db(conv)

    @strawberry.mutation
    async def delete_conversation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        await delete_conversation(session, id.node_id)
        return True
