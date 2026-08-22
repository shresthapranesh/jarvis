"""Shared scaffolding for the four run kinds (chat, automation, workflow, board).

Every runtime's queue handler wraps its own inner work loop in the same five
things: build an `InvocationContext`, reuse-or-recreate the `TaskState`, start a
budget tracker + callback set, mirror durable cancellation into the state, and
tear the task down on the way out. Those were copied per runtime and had already
drifted — one `_watch_queue_cancel` forgot to cancel `resume_future`, and two of
the four hand-rolled the callback assembly that `core.runner.build_callbacks`
exists to own. This module is the single copy.

Nothing here knows what a run *does*; the per-kind work loops stay in
`server/*_runtime.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from core.queue import CANCEL_POLL_INTERVAL_SECONDS, JobQueue
from core.state import (
    TaskState,
    _notify,
    _tasks,
    log_task_complete,
    log_task_created,
)

logger = logging.getLogger(__name__)

# How long a finished task lingers in `_tasks` so the UI can render its terminal
# state before the row vanishes.
TASK_LINGER_SECONDS = 5.0


# ── Durable cancellation ─────────────────────────────────────────────────────

async def watch_queue_cancel(queue: JobQueue, job_id: str, state: TaskState) -> None:
    """Poll the queue for cancel; mirror into TaskState.cancelled / _stop_event /
    resume_future so the existing in-runtime observers see it.

    This covers only the durable/cross-process path — a same-process stop
    mutation already flips these flags itself. See CANCEL_POLL_INTERVAL_SECONDS.

    Cancelling `resume_future` matters for any run that can suspend on a human:
    a chat interrupt, or a workflow `approval`/`human_input` node that was given
    a `timeout_seconds` (without one those nodes poll `state.cancelled`, but
    inside `asyncio.wait_for` they cannot).
    """
    while not state.done and not state.cancelled:
        if await queue.is_cancel_requested(job_id):
            state.cancelled = True
            state._stop_event.set()
            if state.resume_future and not state.resume_future.done():
                state.resume_future.cancel()
            return
        await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def queue_cancel_watch(job_id: str, state: TaskState) -> AsyncIterator[None]:
    """Run the cancel poller for the duration of the block, then stop it."""
    # Imported here, not at module scope: `core.state` owns the queue accessor
    # and the queue only exists once the lifespan has started.
    from core.state import get_queue

    watcher = asyncio.create_task(watch_queue_cancel(get_queue(), job_id, state))
    try:
        yield
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


# ── Invocation context ───────────────────────────────────────────────────────

async def new_invocation_context(
    *,
    kind: str,
    session_id: str,
    invocation_id: str,
    model: str | None = None,
    initial_state: dict[str, Any] | None = None,
    load_persisted: bool = False,
) -> Any | None:
    """Build the run's `InvocationContext`, or None when there is no runner.

    Best-effort by design: a context is an optimisation over the module-level
    checkpointer/store accessors, so failing to build one must not take down a
    run that would otherwise work (CLI and tests have no runner at all).
    """
    try:
        from core.runner import get_runner_or_none

        runner = get_runner_or_none()
        if runner is None:
            return None
        ctx = runner.new_invocation_context(
            session_id=session_id,
            kind=kind,
            model=model,
            initial_state=initial_state or {},
        )
        ctx.invocation_id = invocation_id
        if load_persisted:
            try:
                await ctx.load_persisted_state()
            except Exception as exc:
                logger.warning("could not load persisted state for %s: %s", session_id, exc)
        return ctx
    except Exception as exc:
        logger.warning("could not build invocation context for %s: %s", session_id, exc)
        return None


# ── TaskState lifecycle ──────────────────────────────────────────────────────

def get_or_create_task_state(
    task_id: str,
    *,
    kind: str,
    label: str,
    parent_id: str | None,
    model: str | None = None,
) -> TaskState:
    """The state the trigger pre-registered, or a fresh one.

    The trigger sets `_tasks[task_id]` before its commit so a subscriber cannot
    race the worker; a job re-claimed after a restart has no such trigger left,
    which is the only path that creates one here.
    """
    state = _tasks.get(task_id)
    if state is not None:
        return state
    state = TaskState(kind=kind, label=label, parent_id=parent_id)  # type: ignore[arg-type]
    _tasks[task_id] = state
    log_task_created(task_id, state, model)
    return state


def finish_task_state(task_id: str, state: TaskState, status: str) -> None:
    """Terminal bookkeeping: log, mark done, wake subscribers, schedule removal."""
    log_task_complete(task_id, state, status)
    state.done = True
    _notify(state)
    loop = asyncio.get_running_loop()
    loop.call_later(TASK_LINGER_SECONDS, lambda tid=task_id: _tasks.pop(tid, None))


# ── Budget + callbacks ───────────────────────────────────────────────────────

def start_budget(state: TaskState, kind: str) -> Any:
    """Create the run's BudgetTracker and attach it to the state."""
    from core.budget import BudgetTracker, get_budget_limits_for_task

    tracker = BudgetTracker(get_budget_limits_for_task(kind), task_state=state)
    state._budget_tracker = tracker
    return tracker


@dataclass
class RunCallbacks:
    """The callback handlers for one run, plus the trackers worth keeping."""

    handlers: list[Any] = field(default_factory=list)
    budget: Any = None
    usage: Any = None
    perf: Any = None


def start_run_callbacks(state: TaskState, kind: str, *, with_perf: bool = False) -> RunCallbacks:
    """Budget tracker + callback handlers for a run, via `build_callbacks`.

    `with_perf` also creates a `PerfTracker` and attaches it to the state; only
    chat persists throughput onto the Message row, so the other kinds leave it
    off rather than pay for the timing.
    """
    from core.runner import build_callbacks, find_handler

    tracker = start_budget(state, kind)

    perf = None
    if with_perf:
        from core.perf import PerfTracker

        perf = PerfTracker(task_state=state)
        state._perf_tracker = perf

    handlers = build_callbacks(tracker, task_state=state, perf_tracker=perf)

    # `build_callbacks` normally supplies both, but a plugin manager is
    # pluggable — assert what this run actually needs rather than assume.
    from core.log_callback import UsageAccumulator

    usage = find_handler(handlers, UsageAccumulator)
    if usage is None:
        usage = UsageAccumulator()
        handlers.append(usage)

    if perf is not None:
        from core.perf import PerfCallbackHandler

        if find_handler(handlers, PerfCallbackHandler) is None:
            handlers.append(PerfCallbackHandler(perf))

    return RunCallbacks(handlers=handlers, budget=tracker, usage=usage, perf=perf)
