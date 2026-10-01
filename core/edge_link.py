"""The worker link — this process's live runs, mirrored into the Rust edge.

The edge (`edge/`) serves every subscription and the run registry
(`runningTasks`, the stop mutations), but runs still execute here. So this
process reports to it over one loopback WebSocket, `/internal/worker`:

    worker → edge   hello, snapshot, register, events, state, unregister
    edge → worker   cancel

Every message is one JSON object with a `type`. The protocol is versioned
(`PROTOCOL`); the edge refuses a version it doesn't speak, so a mismatched pair
degrades to the old path — the edge proxies subscriptions to Python — rather
than to a stream that silently drops events.

**Nothing here is durable, by design.** On every (re)connect the link sends a
snapshot of `_tasks` with each run's full event history, and the edge replaces
its mirror with it. That one rule covers an edge restart, a worker restart and
a dropped connection alike, and is why a disconnected link buffers nothing:
whatever happened meanwhile is in the next snapshot.

Events are raw `{"event", "data"}` records, exactly as `emit_event` appended
them. Turning them into typed GraphQL events is the edge's job now, so this
side never has to know which subscription is watching.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from typing import Any

from core.state import (
    RegistryObserver,
    TaskState,
    _tasks,
    set_registry_observer,
)

logger = logging.getLogger("jarvis.edge_link")

PROTOCOL = 1
_BACKOFF_SECONDS = (0.2, 0.5, 1.0, 2.0, 5.0)
# Counters (tokens, LLM calls) are written by callbacks that don't always
# notify, so state is also re-checked on a timer.
_STATE_SWEEP_SECONDS = 1.0


def _state_fields(state: TaskState) -> dict[str, Any]:
    """What `runningTasks` shows about a run, beside its identity."""
    input_tokens = state.input_tokens or 0
    output_tokens = state.output_tokens or 0
    return {
        "done": state.done,
        "cancelled": state.cancelled,
        "has_interrupt": state.pending_interrupt_id is not None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "llm_calls": state.llm_calls or 0,
        "tool_calls": state.tool_calls or 0,
        "budget_exceeded": bool(state.budget_exceeded),
        "budget_reason": state.budget_reason,
    }


def _meta(state: TaskState) -> dict[str, Any]:
    return {
        "kind": state.kind,
        "label": state.label,
        "parent_id": state.parent_id,
        # Exactly what strawberry renders for RunningTask.startedAt.
        "started_at": state.started_at.isoformat(),
    }


def cancel_in_process(state: TaskState, *, resume: bool) -> None:
    """The in-process half of a stop, as the stop mutations do it here."""
    state.cancelled = True
    state._stop_event.set()
    if resume and state.resume_future and not state.resume_future.done():
        state.resume_future.cancel()


class EdgeLink(RegistryObserver):
    def __init__(self, url: str) -> None:
        self.url = url
        # Tells the edge whether a reconnect is this process again (runs carry
        # over) or a new one (every run it mirrored is gone). A pid can be reused.
        self.instance = uuid.uuid4().hex
        self._ws: Any = None
        # Per run: how many of its events the edge has, and the last state sent.
        self._cursor: dict[str, int] = {}
        self._sent_state: dict[str, dict[str, Any]] = {}
        # TaskState is unhashable-by-value and re-registered under one id, so
        # map object identity back to the id it was registered as.
        self._ids: dict[int, str] = {}
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.connected = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        for task_id, state in _tasks.items():
            self._ids[id(state)] = task_id
        set_registry_observer(self)
        self._task = asyncio.create_task(self._run(), name="edge-link")

    async def stop(self) -> None:
        set_registry_observer(None)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        import websockets

        attempt = 0
        while True:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20) as ws:
                    attempt = 0
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # refused, reset, closed, rejected
                if attempt == 0:
                    logger.info("edge link down (%s); retrying", exc)
            finally:
                self._ws = None
                self.connected.clear()
            await asyncio.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
            attempt += 1

    async def _session(self, ws: Any) -> None:
        # Hello and snapshot are queued synchronously, before `_ws` is set, so
        # no hook can slip a message in between them.
        self._outbox = asyncio.Queue()
        self._send({"type": "hello", "protocol": PROTOCOL, "instance": self.instance, "pid": os.getpid()})
        self._send({"type": "snapshot", "tasks": [self._full(tid, st) for tid, st in _tasks.items()]})
        self._ws = ws
        self.connected.set()
        logger.info("edge link up: %s (%d live runs)", self.url, len(_tasks))

        writer = asyncio.create_task(self._write(ws))
        sweeper = asyncio.create_task(self._sweep())
        try:
            async for raw in ws:
                self._handle(json.loads(raw))
        finally:
            for t in (writer, sweeper):
                t.cancel()
            for t in (writer, sweeper):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    async def _write(self, ws: Any) -> None:
        while True:
            await ws.send(await self._outbox.get())

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(_STATE_SWEEP_SECONDS)
            for task_id, state in list(_tasks.items()):
                self._flush_state(task_id, state)

    # ── edge → worker ────────────────────────────────────────────────────────

    def _handle(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "cancel":
            state = _tasks.get(msg.get("task_id", ""))
            if state is not None:
                cancel_in_process(state, resume=bool(msg.get("resume")))
                self.task_changed(state)
        else:
            logger.warning("edge link: unknown message %r", msg.get("type"))

    # ── worker → edge ────────────────────────────────────────────────────────

    def _send(self, msg: dict[str, Any]) -> None:
        self._outbox.put_nowait(json.dumps(msg))

    def _full(self, task_id: str, state: TaskState) -> dict[str, Any]:
        """A run as the snapshot carries it: identity, every event, state."""
        self._ids[id(state)] = task_id
        self._cursor[task_id] = len(state.events)
        fields = _state_fields(state)
        self._sent_state[task_id] = fields
        return {"task_id": task_id, **_meta(state), "events": list(state.events), **fields}

    def _flush_events(self, task_id: str, state: TaskState) -> None:
        start = self._cursor.get(task_id, 0)
        if start < len(state.events):
            self._send({"type": "events", "task_id": task_id, "from": start, "events": state.events[start:]})
            self._cursor[task_id] = len(state.events)

    def _flush_state(self, task_id: str, state: TaskState) -> None:
        fields = _state_fields(state)
        if fields != self._sent_state.get(task_id):
            self._sent_state[task_id] = fields
            self._send({"type": "state", "task_id": task_id, **fields})

    # ── RegistryObserver ─────────────────────────────────────────────────────

    def task_added(self, task_id: str, state: TaskState) -> None:
        self._ids[id(state)] = task_id
        if self._ws is None:
            return  # the next snapshot carries it
        self._send({"type": "register", **self._full(task_id, state)})

    def task_removed(self, task_id: str) -> None:
        self._ids = {k: v for k, v in self._ids.items() if v != task_id}
        self._cursor.pop(task_id, None)
        self._sent_state.pop(task_id, None)
        if self._ws is not None:
            self._send({"type": "unregister", "task_id": task_id})

    def task_changed(self, state: TaskState) -> None:
        task_id = self._ids.get(id(state))
        if self._ws is None or task_id is None or _tasks.get(task_id) is not state:
            return
        # Events before state: a subscriber woken by `done` must already have
        # the `done` event it is about to look for.
        self._flush_events(task_id, state)
        self._flush_state(task_id, state)


_link: EdgeLink | None = None


def start_edge_link() -> EdgeLink | None:
    """Connect to the edge named by `JARVIS_EDGE_URL`, if any. Called from the
    lifespan; without the variable this process runs standalone, as before."""
    global _link
    base = os.environ.get("JARVIS_EDGE_URL", "").strip().rstrip("/")
    if not base:
        return None
    if base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    _link = EdgeLink(f"{base}/internal/worker")
    _link.start()
    return _link


async def stop_edge_link() -> None:
    global _link
    if _link is not None:
        await _link.stop()
        _link = None
