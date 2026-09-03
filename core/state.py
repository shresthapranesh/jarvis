"""Shared mutable state — globals, task registry, and notify helper."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.queue import JobQueue


TaskKind = Literal["chat", "automation", "workflow", "board_task"]

_task_log = logging.getLogger("jarvis.tasks")


# ── Infrastructure globals (set by lifespan, read everywhere) ────────────────

_async_checkpointer: AsyncSqliteSaver | None = None
_store: AsyncSqliteStore | None = None
_main_loop: asyncio.AbstractEventLoop | None = None
_http_client: httpx.AsyncClient | None = None
_telegram_bot: object | None = None  # telegram.Bot when set; lazy-typed to avoid forcing import
_discord_client: object | None = None  # discord.Client when set; lazy-typed to avoid forcing import
_queue: JobQueue | None = None


def _runner_resource(name: str):
    """Read a resource off the active JarvisRunner, or None if there isn't one.

    Imported lazily: core.runner imports core.queue, which this module also
    imports, so a module-level import would cycle.
    """
    from core.runner import get_runner_or_none

    runner = get_runner_or_none()
    return None if runner is None else getattr(runner, name, None)


def get_async_checkpointer() -> AsyncSqliteSaver:
    """The active AsyncSqliteSaver — the runner's if installed, else the global."""
    cp = _runner_resource("checkpointer") or _async_checkpointer
    if cp is None:
        raise RuntimeError("async checkpointer not initialized — server lifespan has not started")
    return cp


def get_store() -> AsyncSqliteStore:
    """The active AsyncSqliteStore — the runner's if installed, else the global."""
    store = _runner_resource("store") or _store
    if store is None:
        raise RuntimeError("store not initialized — server lifespan has not started")
    return store


def get_http_client() -> httpx.AsyncClient:
    """The active httpx.AsyncClient — the runner's if installed, else the global."""
    client = _runner_resource("http_client") or _http_client
    if client is None:
        raise RuntimeError("http client not initialized — server lifespan has not started")
    return client


def get_telegram_bot():
    """Return the process-wide telegram.Bot if the bot is enabled, else None."""
    return _telegram_bot


def get_discord_client():
    """Return the process-wide discord.Client if the bot is enabled, else None."""
    return _discord_client


def get_queue() -> JobQueue:
    """The active JobQueue — the runner's if installed, else the global."""
    queue = _runner_resource("queue") or _queue
    if queue is None:
        raise RuntimeError("job queue not initialized — server lifespan has not started")
    return queue


# ── Task registry ────────────────────────────────────────────────────────────

@dataclass
class InterruptRequest:
    """The human-in-the-loop request a paused run is currently waiting on.

    ``pending_interrupt_id`` answers *whether* a run is paused; the question
    itself only ever existed as an emitted event, readable by a subscriber
    already tailing that one run. The approvals inbox has no such subscription
    — it polls every run at once — so the payload is carried here too, set and
    cleared on exactly the same paths as the id (see ``set_interrupt`` /
    ``clear_interrupt``, which exist so no caller can update one without the
    other).
    """

    id: str
    question: str
    kind: Literal["approval", "input"] = "input"
    tool: str | None = None
    args_json: str | None = None
    node_id: str | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Id of the durable `approvals` row backing this pause, when one was
    # written. None means the persist failed — the run is still resumable by
    # its own subscriber, it just won't appear in the inbox.
    approval_id: str | None = None


@dataclass
class QueuedMessage:
    """A user message typed while the run it belongs to was already in flight.

    Lives here for the drain — the agent's model_request node takes these
    just before its LLM call — and is mirrored to a `messages` row with
    status ``queued``, which is what renders in the transcript and what
    survives a restart. TaskState does not: this list is the fast path, the
    row is the durable one.
    """

    id: str  # the durable Message row id; also the HumanMessage id on delivery
    text: str
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TaskState:
    events: list[dict] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    pending_interrupt_id: str | None = None
    pending_interrupt: InterruptRequest | None = None
    resume_future: asyncio.Future | None = None
    # Messages the user sent while this run was still going. Drained by the
    # agent's model_request node, so they land after the current tool batch's
    # results and before the next LLM call.
    pending_input: list[QueuedMessage] = field(default_factory=list)
    # Self-describing fields surfaced by the global /tasks endpoint.
    # Default-initialized for backwards compat; each subsystem sets them
    # at registration time.
    kind: TaskKind = "chat"
    label: str = ""
    parent_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _waiters: list[asyncio.Future] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    # ── Budget tracking (MAF TokenUsageTermination analog) ─────────────────
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    budget_exceeded: bool = False
    budget_reason: str | None = None
    _budget_tracker: object | None = field(default=None, repr=False)  # BudgetTracker, Any to avoid cycle
    # ── Throughput tracking (prefill / eval tok/s) ─────────────────────────
    _perf_tracker: object | None = field(default=None, repr=False)  # PerfTracker, Any to avoid cycle

    def set_interrupt(self, request: InterruptRequest) -> None:
        """Mark the run as paused on `request`. Sets id and payload together."""
        self.pending_interrupt = request
        self.pending_interrupt_id = request.id

    def clear_interrupt(self) -> None:
        """Mark the run as no longer paused. Clears id and payload together."""
        self.pending_interrupt = None
        self.pending_interrupt_id = None

    def queue_input(self, msg: QueuedMessage) -> int:
        """Append `msg` to this run's queue. Returns its 1-based position."""
        self.pending_input.append(msg)
        return len(self.pending_input)

    def unqueue_input(self, message_id: str) -> bool:
        """Drop a still-undelivered queued message. False if already drained."""
        for i, queued in enumerate(self.pending_input):
            if queued.id == message_id:
                del self.pending_input[i]
                return True
        return False

    def drain_input(self) -> list[QueuedMessage]:
        """Take everything queued, atomically.

        Sync on purpose. The swap has no await between reading the list and
        clearing it, and every producer runs on the same loop, so nothing can
        be queued into a list that is about to be discarded — which is why
        this needs no lock despite having two writers.
        """
        if not self.pending_input:
            return []
        drained, self.pending_input = self.pending_input, []
        return drained


_tasks: dict[str, TaskState] = {}


def task_id_of(state: TaskState) -> str | None:
    """The registry key for `state`, or None if it is not registered.

    A reverse lookup rather than an `id` field on TaskState: the id is assigned
    by whoever registers the run, and eleven call sites construct a TaskState.
    A field would be silently empty wherever one was missed — and the sites
    that would be missed are the nested ones (workflow map children), which is
    exactly where a wrong-but-present id would attach an approval to the wrong
    run. None is the correct answer for those.
    """
    for key, value in _tasks.items():
        if value is state:
            return key
    return None


def _resolve_waiters(state: TaskState) -> None:
    for fut in state._waiters:
        if not fut.done():
            fut.set_result(None)
    state._waiters.clear()


def _notify(state: TaskState) -> None:
    """Wake all subscription waiters that are blocked on this task.

    Safe to call from any thread: sync LangChain callbacks (budget, logging)
    run in an executor thread during async LLM calls, and asyncio futures may
    only be resolved on the loop that created them, so off-loop calls are
    marshalled via call_soon_threadsafe.
    """
    if _main_loop is not None:
        try:
            on_loop = asyncio.get_running_loop() is _main_loop
        except RuntimeError:
            on_loop = False
        if not on_loop:
            _main_loop.call_soon_threadsafe(_resolve_waiters, state)
            return
    _resolve_waiters(state)


def emit_event(state: TaskState, event: str, **payload) -> None:
    """Append an event to the task's stream and wake all subscribers.

    The canonical way to surface an event: every `{"event": ..., "data":
    json.dumps(...)}` append should go through here so a forgotten
    `_notify` can't leave subscribers hanging until the next event.
    """
    state.events.append({"event": event, "data": json.dumps(payload)})
    _notify(state)


def log_task_received(kind: TaskKind, parent_id: str, source: str) -> None:
    """Log that a trigger has arrived and a task is about to be spun up."""
    _task_log.info("task received: kind=%s parent=%s source=%s", kind, parent_id, source)


def log_task_created(task_id: str, state: TaskState, model: str | None = None) -> None:
    """Log that TaskState is registered and the background coroutine has been scheduled."""
    _task_log.info(
        "task created: kind=%s task=%s parent=%s model=%s",
        state.kind, task_id, state.parent_id, model or "-",
    )


def log_task_complete(task_id: str, state: TaskState, status: str) -> None:
    """Log task completion with status and duration since started_at."""
    duration_ms = int((datetime.now(timezone.utc) - state.started_at).total_seconds() * 1000)
    _task_log.info(
        "task complete: kind=%s task=%s parent=%s status=%s duration_ms=%d",
        state.kind, task_id, state.parent_id, status, duration_ms,
    )


async def stream_task_events(state: TaskState) -> AsyncIterator[dict]:
    """Yield events from a TaskState as they arrive. Used by all GraphQL
    subscription resolvers (taskEvents / automationRunEvents / workflowRunEvents)."""
    cursor = 0
    loop = asyncio.get_running_loop()
    while True:
        while cursor < len(state.events):
            yield state.events[cursor]
            cursor += 1
        if state.done:
            break
        fut: asyncio.Future = loop.create_future()
        state._waiters.append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            break
