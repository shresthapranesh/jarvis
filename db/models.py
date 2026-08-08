from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model: Mapped[str] = mapped_column(String)
    # Where the conversation lives: "web" | "telegram" | "discord" | "automation" | "task".
    # The web UI's conversation list only shows surface="web".
    surface: Mapped[str] = mapped_column(String, default="web", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Incognito: the conversation persists normally during its run (the streaming
    # + job-queue pipeline is keyed off these rows), but it is hidden from the web
    # sidebar, all long-term-memory side effects are suppressed while it runs, and
    # it is hard-deleted (rows + on-disk files + checkpointer thread) on close.
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Optional project membership (web conversations only). Deliberately no
    # relationship on Project: deleting a project nulls this instead of
    # cascading into conversations.
    project_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
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


class Project(Base):
    """A group of conversations sharing instructions and agent-maintained memory.

    `instructions` are user-owned guidance injected into every conversation in
    the project; `memory` is a free-text blob the agent itself reads and edits
    via the project_memory tool (tools/projects.py). Both ride the volatile
    system-prompt suffix (core/agents.py), so edits apply on the next LLM call
    without busting the prompt cache. Deleting a project keeps its
    conversations — their project_id is nulled (db/ops.py delete_project).
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    memory: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="done")
    # Provider-reported token usage summed over every LLM call in the run that
    # produced this message (assistant rows only; NULL = not recorded).
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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
    # Worker identity ("<role>:<idx>", e.g. "researcher:1") for steps produced
    # inside a spawned worker; NULL for main-agent steps. Groups a worker's
    # steps together in the activity sidebar.
    subagent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
    mime_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

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
    versions: Mapped[list["ArtifactVersion"]] = relationship(
        "ArtifactVersion", back_populates="artifact", cascade="all, delete-orphan", order_by="ArtifactVersion.version"
    )


class ArtifactVersion(Base):
    """Version history for an artifact — ADK ArtifactService analog.

    Each overwrite of an artifact saves the previous content as a versioned file
    under artifacts_dir/{artifact_id}_v{version}.md and a DB row here. The
    live file {artifact_id}.md always holds the latest version.
    """

    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)  # on-disk path
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    artifact: Mapped["Artifact"] = relationship("Artifact", back_populates="versions")

    __table_args__ = (
        Index("ix_artifact_versions_artifact_version", "artifact_id", "version", unique=True),
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
    # Chunk-indexing state, for documents large enough to be indexed rather than
    # inlined: 'pending' | 'indexed' | 'failed'. NULL means never indexed (the
    # document was small enough to go straight into the message). Indexing runs
    # in the background so it doesn't block the first token, and the retrieval
    # tools wait on this — the kernel that hosts the `jarvis` SDK is a separate
    # process, so an in-memory task registry alone can't tell it when to look.
    index_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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


# ── Task board (kanban) ────────────────────────────────────────────────────────

class BoardTask(Base):
    """One card on the shared task board — a durable unit of agent work.

    Statuses: "todo" (waiting on parents or parked), "ready" (eligible for
    dispatch), "running" (claimed by a board_task job), "blocked" (stopped —
    error, safety, user stop, or agent-reported), "done", "archived".
    The dispatcher (server/task_board_runtime.py) promotes todo→ready when all
    parents are done and enqueues ready tasks onto the durable job queue.
    """

    __tablename__ = "board_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String, default="todo", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # higher runs first
    created_by: Mapped[str] = mapped_column(String, default="user")  # "user" | "agent"

    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    skill: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # skill name to apply

    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Why it's blocked: "needs_input" (agent asked a question — answerable from
    # the board) | "agent" (agent gave up) | "error" | "safety" | "stopped".
    blocked_kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # A human answer to a needs_input blocker, consumed (and cleared) by the
    # next dispatch — the resumed run continues the same thread with it.
    pending_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)

    # Completion handoff for downstream tasks: prose summary + optional JSON dict.
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_metadata: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # Job id of the current/most recent dispatch (job.kind == "board_task").
    # The live-event stream and stopBoardTask key off this.
    job_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class BoardTaskLink(Base):
    """Parent→child dependency edge between board tasks."""

    __tablename__ = "board_task_links"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("board_tasks.id"), nullable=False, index=True)
    child_id: Mapped[str] = mapped_column(ForeignKey("board_tasks.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_board_task_links_edge", "parent_id", "child_id", unique=True),
    )


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


class MemoryActivity(Base):
    """Audit log for when a Memory was surfaced.

    Separate table (not a column on Memory) so we keep history, avoid bumping
    Memory.updated_at on every read, and can prune independently. Fact-only
    for v1 to avoid noise from core memories injected every turn.
    """

    __tablename__ = "memory_activities"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    memory_id: Mapped[str] = mapped_column(
        String, ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String, index=True)  # core/fact
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # truncated 500 chars
    source: Mapped[str] = mapped_column(String, index=True)  # retrieval | explicit_search | core_injection
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    __table_args__ = (
        Index("ix_mem_act_mem_time", "memory_id", "accessed_at"),
        Index("ix_mem_act_conv_time", "conversation_id", "accessed_at"),
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
