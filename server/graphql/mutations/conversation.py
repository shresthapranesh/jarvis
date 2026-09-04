"""Conversation mutations — startTask, stopTask, resumeTask, update, delete."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import strawberry
from strawberry import relay

from core.agents import is_valid_model
from core.config import get_config
from core.schemas import AttachmentIn
from core.state import _tasks, emit_event
from db.ops import close_open_approvals, delete_conversation, resolve_model, update_conversation

from ..types.conversation import Conversation
from ..types.upload import UploadReferenceInput
from server.chat_runtime import queue_chat_message, register_chat_task, unqueue_chat_message


@strawberry.input
class StartTaskInput:
    query: str
    model: str | None = None
    conversation_id: str | None = None
    attachment_uploads: list[UploadReferenceInput] | None = None
    # Raw project id (matches conversation_id's convention); applies only when
    # this call creates a new conversation.
    project_id: str | None = None
    # Incognito: create the new conversation as ephemeral (hidden from history,
    # no long-term-memory writes, hard-deleted on close). Applies only when this
    # call creates the conversation; ignored when joining an existing one.
    ephemeral: bool = False


@strawberry.type
class StartTaskPayload:
    task_id: str
    conversation_id: str
    # True when the conversation was already running and this message was
    # queued onto that run instead of starting a second one. `task_id` is then
    # the running task — subscribe to it either way.
    queued: bool = False
    queued_message_id: str | None = None


@strawberry.type
class QueueMessagePayload:
    message_id: str
    position: int


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
        session = info.context["session"]
        # resolve_model guarantees a model the catalog has: an id the request
        # names that has since been removed degrades to the operator's default
        # (logged) rather than failing the turn. The strict check belongs on the
        # *write* boundary — updateConversation below — not here.
        model = await resolve_model(input.model, session)

        attachments: list[AttachmentIn] | None = None
        staging_paths: list[tuple[Path, Path]] = []
        if input.attachment_uploads:
            attachments, staging_paths = _resolve_staged_uploads(input.attachment_uploads)

        dispatch = await register_chat_task(
            session, input.query, model, input.conversation_id, attachments,
            project_id=input.project_id, ephemeral=input.ephemeral,
        )
        # register_chat_task has now copied document bytes to documents_dir and
        # baked image/audio/video bytes into the message content; safe to drop
        # the staging copies. On failure (exception above) the periodic cleanup
        # job will sweep them after an hour.
        for bytes_path, meta_path in staging_paths:
            bytes_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
        return StartTaskPayload(
            task_id=dispatch.task_id,
            conversation_id=dispatch.conversation_id,
            queued=dispatch.queued,
            queued_message_id=dispatch.queued_message_id,
        )

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
    async def queue_message(
        self, info: strawberry.Info, task_id: str, query: str,
    ) -> QueueMessagePayload:
        """Queue a message for a run that is already in flight.

        Not a second `startTask`: two runs on one conversation share a
        LangGraph `thread_id` and would race the checkpointer. The queued text
        is delivered by the agent's model_request node just before its next LLM
        call — i.e. after the tool batch currently running, without cancelling
        it.
        """
        message_id, position = await queue_chat_message(
            info.context["session"], task_id, query,
        )
        return QueueMessagePayload(message_id=message_id, position=position)

    @strawberry.mutation
    async def unqueue_message(
        self, info: strawberry.Info, task_id: str, message_id: str,
    ) -> bool:
        """Withdraw a queued message. False if the run already delivered it."""
        return await unqueue_chat_message(
            info.context["session"], task_id, message_id,
        )

    @strawberry.mutation
    async def resume_task(self, info: strawberry.Info, task_id: str, answer: str) -> bool:
        state = _tasks.get(task_id)
        if state is None:
            raise ValueError("task not found")
        if state.resume_future is None or state.resume_future.done():
            raise ValueError("no pending interrupt for this task")

        pending_id = state.pending_interrupt_id
        state.resume_future.set_result(answer)
        emit_event(state, "interrupt_resolved", interrupt_id=pending_id)
        # Answering from the conversation and answering from the inbox are two
        # views of one question — clear it from both, whichever was used.
        await close_open_approvals(
            info.context["session"], task_id=task_id,
            status="answered", result="Delivered to the run.",
        )
        state.clear_interrupt()
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

    @strawberry.mutation
    async def discard_conversation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        """Tear down an incognito conversation (fire on tab-close / end-incognito).
        Guarded to ephemeral rows so a stray call can never delete a real
        conversation. No-ops (returns False) for non-ephemeral or missing rows."""
        from db.models import Conversation

        session = info.context["session"]
        conv = await session.get(Conversation, id.node_id)
        if conv is None or not conv.ephemeral:
            return False
        await delete_conversation(session, id.node_id)
        return True
