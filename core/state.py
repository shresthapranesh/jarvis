"""Shared mutable state — globals, task registry, and notify helper.

This module is the dependency root: nothing it imports can import it back.
Other modules (routes, streaming, scheduler) read/write these globals; only
the lifespan context manager in server.py sets the infrastructure globals
(_async_checkpointer, _main_loop, _http_client).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore


TaskKind = Literal["chat", "automation", "workflow"]


# ── Infrastructure globals (set by lifespan, read everywhere) ────────────────

_async_checkpointer: AsyncSqliteSaver | None = None
_store: AsyncSqliteStore | None = None
_main_loop: asyncio.AbstractEventLoop | None = None
_http_client: httpx.AsyncClient | None = None
_telegram_bot: object | None = None  # telegram.Bot when set; lazy-typed to avoid forcing import


def get_async_checkpointer() -> AsyncSqliteSaver:
    """Return the process-wide AsyncSqliteSaver. Must be called after lifespan init."""
    if _async_checkpointer is None:
        raise RuntimeError("async checkpointer not initialized — server lifespan has not started")
    return _async_checkpointer


def get_store() -> AsyncSqliteStore:
    """Return the process-wide AsyncSqliteStore. Must be called after lifespan init."""
    if _store is None:
        raise RuntimeError("store not initialized — server lifespan has not started")
    return _store


def get_http_client() -> httpx.AsyncClient:
    """Return the process-wide httpx.AsyncClient. Must be called after lifespan init."""
    if _http_client is None:
        raise RuntimeError("http client not initialized — server lifespan has not started")
    return _http_client


def get_telegram_bot():
    """Return the process-wide telegram.Bot if the bot is enabled, else None."""
    return _telegram_bot


# ── Task registry ────────────────────────────────────────────────────────────

@dataclass
class TaskState:
    events: list[dict] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    pending_interrupt_id: str | None = None
    resume_future: asyncio.Future | None = None
    # Self-describing fields surfaced by the global /tasks endpoint.
    # Default-initialized for backwards compat; each subsystem sets them
    # at registration time.
    kind: TaskKind = "chat"
    label: str = ""
    parent_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _waiters: list[asyncio.Future] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)


_tasks: dict[str, TaskState] = {}
_background_tasks: dict[str, asyncio.Task] = {}


def _notify(state: TaskState) -> None:
    """Wake all SSE/WebSocket waiters that are blocked on this task."""
    for fut in state._waiters:
        if not fut.done():
            fut.set_result(None)
    state._waiters.clear()


async def stream_task_events(state: TaskState) -> AsyncIterator[dict]:
    """Yield events from a TaskState as they arrive. Used by all SSE endpoints."""
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
