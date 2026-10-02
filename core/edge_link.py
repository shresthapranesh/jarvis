"""The worker link — this process's live runs, mirrored into the Rust edge.

The edge (`edge/`) serves every subscription and the run registry
(`runningTasks`, the stop mutations), but runs still execute here. So this
process reports to it over one loopback WebSocket, `/internal/worker`:

    worker → edge   hello, snapshot, register, events, state, unregister, reply,
                    dispatch, schedules, holds
    edge → worker   cancel, wake, adopt_queued, call

The edge also *starts* runs (`startTask`, `runWorkflow`, `triggerAutomation`):
it writes the job row itself and mirrors the run as pending until this
process's worker claims it, then sends `wake` so the claim doesn't wait for
the next poll. Operations that act on a run's in-memory state — answering an
interrupt, queueing a message onto it — arrive as a `call`, which runs the same
function the GraphQL resolver here would and answers with a `reply`.

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

**The edge may also own this process** (`JARVIS_WORKER_CMD`, see
`edge/src/supervisor.rs`): it starts Python when there is work and stops it
once idle. Idle is the edge's call, from what it can see — no live run, no job,
no request in flight — plus `holds`, the reasons only this process knows of to
stay up (a chat bot connected, a kernel still holding someone's variables). It
stops by `call`ing `drain` first, so that no job is claimed while it looks.
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

PROTOCOL = 4
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


def current_holds() -> list[str]:
    """Why this process must not be stopped for being idle, beyond what the
    edge sees itself: a bot holds a connection open to its chat service, and a
    kernel holds a conversation's variables until the reaper retires it."""
    import time

    from core import state as core_state
    from core.kernels import IDLE_TIMEOUT_SECONDS, get_kernel_registry

    holds = []
    if core_state._telegram_bot is not None:
        holds.append("telegram")
    if core_state._discord_client is not None:
        holds.append("discord")
    now = time.monotonic()
    sessions = list(get_kernel_registry()._sessions.values())
    if any(now - s.last_used <= IDLE_TIMEOUT_SECONDS for s in sessions):
        holds.append("kernels")
    return holds


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
        # Calls in flight; held so they aren't garbage-collected mid-await.
        self._pending: set[asyncio.Task] = set()
        self.connected = asyncio.Event()
        self._sent_holds: list[str] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        for task_id, state in _tasks.items():
            self._ids[id(state)] = task_id
        set_registry_observer(self)
        self._task = asyncio.create_task(self._run(), name="edge-link")

    async def stop(self) -> None:
        set_registry_observer(None)
        for task in list(self._pending):
            task.cancel()
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
        self._sent_holds = None
        self._flush_holds()
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
            self._flush_holds()

    # ── edge → worker ────────────────────────────────────────────────────────

    def _handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "cancel":
            state = _tasks.get(msg.get("task_id", ""))
            if state is not None:
                cancel_in_process(state, resume=bool(msg.get("resume")))
                self.task_changed(state)
        elif kind == "wake":
            from core.state import get_queue

            with contextlib.suppress(Exception):  # no queue yet: the poll finds it
                get_queue().wake()
        elif kind == "adopt_queued":
            self._spawn(self._adopt_queued(msg.get("task_id", ""), set(msg.get("announce") or ())))
        elif kind == "call":
            self._spawn(self._call(msg))
        else:
            logger.warning("edge link: unknown message %r", kind)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _adopt_queued(self, task_id: str, announce: set[str]) -> None:
        """The edge queued a message onto this run while it was still pending,
        and this process claimed it around the same time — so the claim-time
        adoption may have read the conversation before that row existed. Adopt
        again (it skips what it already holds). `announce` names messages the
        edge could not announce itself because the run was no longer its own."""
        from core.state import emit_event
        from server.chat_runtime import _adopt_queued_messages

        state = _tasks.get(task_id)
        if state is None or state.kind != "chat" or not state.parent_id or state.done:
            return
        await _adopt_queued_messages(state.parent_id, state)
        for position, queued in enumerate(state.pending_input, start=1):
            if queued.id in announce:
                emit_event(state, "queued_message", message_id=queued.id, text=queued.text, position=position)

    async def _call(self, msg: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"type": "reply", "id": msg.get("id")}
        try:
            reply["value"] = await _dispatch(msg.get("method", ""), msg.get("params") or {})
            reply["ok"] = True
        except Exception as exc:
            # The message the GraphQL resolver here would have raised.
            reply["ok"] = False
            reply["error"] = str(exc)
        self._send(reply)

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

    def _flush_holds(self) -> None:
        try:
            holds = current_holds()
        except Exception:  # never let a probe take the link down
            logger.debug("holds probe failed", exc_info=True)
            return
        if holds != self._sent_holds:
            self._sent_holds = holds
            self._send({"type": "holds", "holds": holds})

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


async def _dispatch(method: str, params: dict[str, Any]) -> Any:
    """A `call` from the edge: the run-control operations that need this
    process's in-memory run state, by the functions the resolvers here use."""
    from core.state import get_queue
    from db import async_session
    from server.chat_runtime import queue_chat_message, resume_chat_task, unqueue_chat_message
    from server.workflow_runtime import resolve_workflow_approval, resume_workflow_run

    # The edge is about to stop this process for being idle: claim nothing
    # while it checks the job table one last time (`edge/src/supervisor.rs`).
    if method == "drain":
        await get_queue().drain()
        return {"tasks": len(_tasks)}
    if method == "undrain":
        get_queue().undrain()
        return True

    async with async_session() as session:
        if method == "queue_message":
            message_id, position = await queue_chat_message(session, params["task_id"], params["query"])
            return {"message_id": message_id, "position": position}
        if method == "unqueue_message":
            return await unqueue_chat_message(session, params["task_id"], params["message_id"])
        if method == "resume_task":
            await resume_chat_task(session, params["task_id"], params["answer"])
            return True
        if method == "resume_workflow_run":
            await resume_workflow_run(session, params["run_id"], params["answer"])
            return True
        if method == "resolve_workflow_approval":
            await resolve_workflow_approval(
                session, params["run_id"], bool(params["approved"]), params.get("answer"),
            )
            return True
    raise ValueError(f"unknown link method {method!r}")


_link: EdgeLink | None = None


def behind_edge() -> bool:
    """Whether this process runs behind the Rust edge (`JARVIS_EDGE_URL`).

    Then the edge owns every timer — cron automations, the board dispatcher,
    the maintenance sweeps — and this process only runs what they enqueue.
    Decided by configuration rather than by whether the link is up at this
    instant, so a reconnecting link can never leave both sides firing (or
    neither). `core/scheduler.py` and `server/task_board_runtime.py` ask.
    """
    return bool(os.environ.get("JARVIS_EDGE_URL", "").strip())


def respawned_by_edge() -> bool:
    """Whether the edge started this process to replace one it stopped for
    being idle (`JARVIS_EDGE_RESPAWN=1`). Then the previous process exited
    cleanly with nothing running, and the startup sweeps that assume a crash
    must not treat what it left as abandoned."""
    return os.environ.get("JARVIS_EDGE_RESPAWN", "") == "1"


def notify_edge(kind: str) -> None:
    """Tell the edge something it schedules changed: `dispatch` (run a board
    dispatch pass now) or `schedules` (re-read the automations' cron
    schedules). Best effort — with the link down, the edge's own periodic
    pass picks the change up instead."""
    if _link is not None and _link._ws is not None:
        _link._send({"type": kind})


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
