from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model: Mapped[str] = mapped_column(String)
    # Where the conversation lives: "web" | "telegram" | "discord" | "automation".
    # The web UI's conversation list only shows surface="web".
    surface: Mapped[str] = mapped_column(String, default="web", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        "Artifact", back_populates="conversation", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        "Document", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped[Conversation] = relationship("Conversation", back_populates="messages")
    steps: Mapped[list[Step]] = relationship(
        "Step", back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
    )


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    node: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    message: Mapped[Message] = relationship("Message", back_populates="steps")


class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # "prompt" | "code" | "webhook" | "monitor" (prompt run that is always
    # stateful and only notifies when the observed target changed)
    input_type: Mapped[str] = mapped_column(String, nullable=False)

    # prompt fields
    prompt_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # code fields
    code_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # webhook fields
    webhook_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    webhook_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    webhook_headers: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string
    webhook_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    schedule: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # cron expression or null
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Prompt automations only: when True, every run shares the LangGraph thread
    # (and Conversation row) "automation_{id}" so state persists across runs.
    stateful: Mapped[bool] = mapped_column(Boolean, default=False)

    notifications: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array of channel configs

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    runs: Mapped[list["AutomationRun"]] = relationship(
        "AutomationRun", back_populates="automation", cascade="all, delete-orphan"
    )


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    automation_id: Mapped[str] = mapped_column(ForeignKey("automations.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String, default="running")  # running | done | error | stopped | blocked | skipped | no_change
    triggered_by: Mapped[str] = mapped_column(String, nullable=False)  # schedule | manual

    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    automation: Mapped["Automation"] = relationship("Automation", back_populates="runs")


# ── Config settings ───────────────────────────────────────────────────────────

class ConfigSetting(Base):
    __tablename__ = "config_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


# ── Notification channels ─────────────────────────────────────────────────────

class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # "telegram" | "discord"
    target: Mapped[str] = mapped_column(String, nullable=False)  # chat_id or channel_id, opaque string

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


# ── Artifacts ──────────────────────────────────────────────────────────────────

class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, default="markdown")

    conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True
    )
    message_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("messages.id"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    conversation: Mapped[Optional["Conversation"]] = relationship(
        "Conversation", back_populates="artifacts"
    )


# ── Documents (uploaded files persisted per conversation) ─────────────────────

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    message_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("messages.id"), nullable=True, index=True
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="documents"
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        "DocumentChunk", back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """One indexed slice of a large attached document.

    Only documents above the inline threshold get chunked (see
    core/doc_index.py); small documents are stuffed straight into the
    message and never appear here. `embedding` holds the float32 vector
    bytes from the configured embedding model; `conversation_id` is
    denormalized so semantic search can scope to a conversation without
    a join.
    """

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")


# ── Workflow models ────────────────────────────────────────────────────────────

class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    definition: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON

    notifications: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array of channel configs

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    runs: Mapped[list["WorkflowRun"]] = relationship(
        "WorkflowRun", back_populates="workflow", cascade="all, delete-orphan"
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String, default="running")  # running | done | error
    inputs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)       # JSON dict
    outputs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)      # JSON dict
    node_results: Mapped[Optional[str]] = mapped_column(Text, nullable=True) # JSON array
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="runs")


# ── Durable job queue ──────────────────────────────────────────────────────────

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON dict

    # pending | running | done | error | cancelled
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")

    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    locked_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_jobs_kind_status_run_at", "kind", "status", "run_at"),
        Index("ix_jobs_locked_until", "locked_until"),
    )


class Memory(Base):
    """One discrete agent-memory item.

    Replaces the single free-text `AGENTS.md` blob (still kept as a keyless
    fallback). `kind='core'` items are durable identity/preferences that load
    on every turn; `kind='fact'` items are vector-retrieved per turn by
    relevance. Global, not conversation-scoped. `embedding` holds the float32
    vector bytes (same layout as DocumentChunk.embedding); null when no
    embedder was available at write time. See core/memory_store.py.
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="fact", index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Skill(Base):
    """A reusable, named capability the agent can invoke on demand.

    A skill is `name` + `description` + `body`: the *description* is the routing
    key — embedded for intent retrieval (same float32 layout as Memory.embedding)
    and surfaced cheaply each turn — while the *body* is the full procedure,
    loaded only when the skill is actually used (progressive disclosure). Global,
    not conversation-scoped: skills are reusable like NotificationChannel /
    Automation. `embedding` is null when no embedder was available at write time,
    in which case retrieval falls back to every enabled skill. See
    core/skill_store.py.
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
