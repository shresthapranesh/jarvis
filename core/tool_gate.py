"""Blocking approval for a gated tool call.

`core/approval.py` blocks by raising a LangGraph **interrupt**, which only works
for a graph-bound tool in a web chat — `_is_headless` auto-approves board tasks,
automations and bots because they have no resume loop. `core/approvals.py`'s
deferred shape blocks nothing at all. Neither covers "no tool may run without a
human's yes, wherever it is called from".

This module makes the durable `Approval` row itself the rendezvous, so the
mechanism no longer depends on the caller's runtime:

* **In-process** (bound tools via the graph's gate node, `callMcpTool` in the
  server) — create the row, then `await` an `asyncio.Event` registered by
  approval id. `core/approvals.resolve` sets it.
* **In the kernel** (the `jarvis` SDK, a separate process where no LangGraph
  runtime and no ORM session exist) — the row is created through the GraphQL
  API and then *polled* over the SDK's read-only sqlite connection. Same row,
  same resolution path, no new transport. See `tools/sdk.py:_await_gate`.

Because nothing here touches the interrupt machinery, a board task or a
scheduled automation blocks and waits exactly like a chat does — which is the
point: an approval requirement that silently auto-approves on four of five
surfaces is not an approval requirement.

The cost is honest and bounded: a waiting run holds its worker slot (and, for
an SDK call, its kernel) until answered or until `GATE_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# `source` on the Approval row. Distinct from "chat"/"workflow"/"board_task"
# because resolution is different: nothing needs to be woken through a
# resume_future, the waiter is watching the row.
GATE_SOURCE = "tool"


def gate_timeout_seconds() -> float:
    """How long a gated call waits before giving up and denying itself.

    30 minutes by default — long enough to walk away from the keyboard, short
    enough that a forgotten approval eventually frees the kernel and worker slot
    it is holding. `run_cell`'s own hold check is derived from this
    (`core/kernels.py`), so raising one without the other just moves the ceiling.
    """
    raw = os.environ.get("JARVIS_TOOL_GATE_TIMEOUT")
    if raw:
        try:
            return max(10.0, float(raw))
        except ValueError:
            logger.warning("JARVIS_TOOL_GATE_TIMEOUT=%r is not a number — using the default", raw)
    return 1800.0


GATE_POLL_SECONDS = 1.5

# approval_id -> (Event, result). The in-process half of the rendezvous; the
# kernel half polls the row instead, so a missing entry here is normal.
_waiters: dict[str, tuple[asyncio.Event, dict[str, Any]]] = {}


def _truncate(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit] + ("…" if len(text) > limit else "")


def describe_call(tool_name: str, args: dict[str, Any] | None) -> str:
    pretty = ""
    if args:
        try:
            pretty = json.dumps(args, default=str, indent=2)
        except Exception:
            pretty = str(args)
        pretty = _truncate(pretty, 1000)
    question = f"Run `{tool_name}`?"
    if pretty:
        question += f"\nArgs: {pretty}"
    return question + "\n\nReply 'approve' to run it, or 'deny' to skip it."


def safe_args(args: dict[str, Any] | None) -> dict[str, str]:
    """Args flattened for display — one gated call must not ship a megabyte of
    base64 into an event payload and a DB row."""
    out: dict[str, str] = {}
    for key, value in (args or {}).items():
        out[str(key)] = _truncate(value)
    return out


# ── Telling the live run ─────────────────────────────────────────────────────

def live_task_id(conversation_id: str | None) -> str | None:
    """The in-flight run for this conversation, or None.

    A reverse lookup by `parent_id` rather than an id field, for the reason
    `core/state.task_id_of` gives. Used both to stamp the row (so the inbox can
    say which run is blocked) and to find the stream to announce on.
    """
    if not conversation_id:
        return None
    try:
        from core.state import _tasks

        for task_id, state in _tasks.items():
            if not state.done and state.parent_id == conversation_id:
                return task_id
    except Exception:
        logger.debug("live task lookup failed", exc_info=True)
    return None


def _emit_to_run(task_id: str | None, event: str, **fields: Any) -> None:
    """Append an event to a live run's stream. No-op when there is no run.

    Written straight onto `TaskState` rather than through `ToolContext.emit`
    because three of the four callers are *not* inside the agent graph: the
    `requestToolApproval` resolver and `callMcpTool` run in the server's request
    path, and resolution runs from whatever answered. A gate that only announced
    itself from the graph is exactly how the SDK and MCP paths ended up blocking
    a run with nothing on screen but a spinner.
    """
    if not task_id:
        return
    try:
        from core.state import _tasks, emit_event

        state = _tasks.get(task_id)
        if state is None or state.done:
            return
        emit_event(state, event, **fields)
    except Exception:
        logger.debug("approval emit failed (%s)", event, exc_info=True)


def announce_request(row) -> None:
    """Put a pending request in front of the user in the conversation it came from.

    The approvals inbox is the durable list; this is the copy that appears where
    the person is actually looking. `approval_id` on the event is what tells the
    chat UI to answer with `resolveApproval` rather than `resumeTask` — there is
    no interrupt to resume, the run is parked inside the call.

    `deferred` is derived from the row (`action is not None`) rather than passed
    in, so it cannot disagree with what the inbox says about the same row. It
    changes what the event *means*: a blocking request is a prompt the run is
    waiting on, a deferred one is a notice that something was recorded and did
    not happen — the run carried on without it.
    """
    try:
        args = json.loads(row.args_json or "{}")
    except Exception:
        args = {}
    deferred = row.action is not None
    _emit_to_run(
        row.task_id or live_task_id(row.parent_id),
        "approval_request",
        tool=row.tool or "",
        reason=(
            f"{row.label or 'This action'} was recorded, not performed \u2014 it runs "
            "only once you approve it."
            if deferred
            else "This tool requires human approval (Settings \u2192 Tools)."
        ),
        args=args if isinstance(args, dict) else {},
        approval_id=row.id,
        deferred=deferred,
    )


def announce_resolved(row, *, approved: bool, answer: str = "") -> None:
    """Clear the inline prompt once the row is answered — from anywhere.

    The inbox, the chat prompt and a timeout all land here, so the conversation
    stops asking regardless of where the answer came from.
    """
    _emit_to_run(
        row.task_id or live_task_id(row.parent_id),
        "approval_resolved",
        tool=row.tool or "",
        approved=approved,
        answer=answer,
    )


# ── Creating the request ─────────────────────────────────────────────────────

async def create_gate_request(
    session,
    *,
    tool_key: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    label: str = "",
):
    """Insert the pending row. Returns the `Approval`.

    Deliberately not deduplicated against an existing open request: two calls to
    the same tool with the same arguments are two operations, and collapsing
    them would run the second on the first's approval.
    """
    from db import ops

    display = safe_args(args)
    # Stamp the run even when the caller could not name it: the SDK asks over
    # HTTP from a kernel process and knows only its conversation.
    task_id = task_id or live_task_id(conversation_id)
    row = await ops.create_approval(
        session,
        source=GATE_SOURCE,
        kind="approval",
        status=ops.APPROVAL_OPEN,
        question=describe_call(tool_name, display),
        label=label or f"Tool: {tool_name}",
        tool=tool_name,
        args_json=json.dumps(display, default=str)[:8000],
        task_id=task_id,
        parent_id=conversation_id,
        # `action` stays NULL: approving does not *execute* anything here, it
        # releases a caller that is already blocked waiting for the answer.
        action=None,
        action_payload=json.dumps({"tool_key": tool_key}),
    )
    announce_request(row)
    return row


# ── Waiting ──────────────────────────────────────────────────────────────────

def _register(approval_id: str) -> tuple[asyncio.Event, dict[str, Any]]:
    event = asyncio.Event()
    result: dict[str, Any] = {}
    _waiters[approval_id] = (event, result)
    return event, result


def notify_resolved(approval_id: str, *, approved: bool, answer: str = "") -> None:
    """Wake an in-process waiter. Safe to call when there isn't one."""
    entry = _waiters.get(approval_id)
    if entry is None:
        return
    event, result = entry
    result["approved"] = approved
    result["answer"] = answer
    try:
        event.set()
    except Exception:  # pragma: no cover — a closed loop, nothing to wake
        logger.debug("could not wake approval waiter %s", approval_id, exc_info=True)


async def _status_of(approval_id: str) -> tuple[str, str]:
    """(status, answer) read fresh. Its own session: the caller's may be inside
    a transaction that predates the resolution we are waiting for."""
    from db.engine import async_session
    from db import ops

    async with async_session() as session:
        row = await ops.get_approval(session, approval_id)
        if row is None:
            return ("expired", "")
        return (row.status, row.answer or "")


async def wait_for_gate(approval_id: str, *, timeout: float | None = None) -> tuple[bool, str]:
    """Block until the row is resolved. Returns (approved, answer).

    The `asyncio.Event` is the fast path; the poll underneath it is what makes
    this correct when the answer arrives from somewhere the event never reaches
    — another process, or a resolution path that forgot to notify.
    """
    limit = gate_timeout_seconds() if timeout is None else timeout
    event, result = _register(approval_id)
    deadline = asyncio.get_running_loop().time() + limit
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _expire(approval_id)
                return (False, "timed out")
            try:
                await asyncio.wait_for(event.wait(), timeout=min(GATE_POLL_SECONDS, remaining))
            except asyncio.TimeoutError:
                pass
            if event.is_set():
                return (bool(result.get("approved")), str(result.get("answer") or ""))
            status, answer = await _status_of(approval_id)
            if status == "pending":
                continue
            return (status == "approved", answer)
    finally:
        _waiters.pop(approval_id, None)


async def _expire(approval_id: str) -> None:
    from db.engine import async_session
    from db import ops

    try:
        async with async_session() as session:
            row = await ops.resolve_approval_row(
                session,
                approval_id,
                status="expired",
                result="No answer before the approval timed out — the call was not run.",
            )
        if row is not None:
            announce_resolved(row, approved=False, answer="timed out")
    except Exception:
        logger.debug("could not expire approval %s", approval_id, exc_info=True)


# ── The whole cycle, for in-process callers ──────────────────────────────────

async def await_tool_approval(
    *,
    tool_key: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> tuple[bool, str]:
    """Record the request, tell the UI, block until answered.

    Announcing is `create_gate_request`'s job, not this function's, so a request
    made anywhere else — the SDK over GraphQL, a resolver — reaches the
    conversation on the same terms. Resolution announces itself for the same
    reason (`core/approvals._resolve_tool_gate`): the answer usually arrives
    from the inbox, where this coroutine is not.
    """
    from db.engine import async_session

    async with async_session() as session:
        row = await create_gate_request(
            session,
            tool_key=tool_key,
            tool_name=tool_name,
            args=args,
            conversation_id=conversation_id,
            task_id=task_id,
        )
        approval_id, question = row.id, row.question

    logger.info("tool gate: waiting on approval %s for %s", approval_id, tool_name)
    approved, answer = await wait_for_gate(approval_id)

    logger.info(
        "tool gate: %s %s (%s)", tool_name, "approved" if approved else "denied", approval_id
    )
    _ = question
    return (approved, answer)


def denial_message(tool_name: str, answer: str) -> str:
    """What the agent sees when a human says no. Phrased so the model treats it
    as a decision to work around, not a transient failure to retry."""
    reason = f" ({answer})" if answer and answer.lower() not in ("no", "deny", "denied") else ""
    return (
        f"Denied by a human{reason}: `{tool_name}` was not run. "
        "Do not retry it — continue without it, or say what you need and why."
    )


# ── Is anything waiting? ─────────────────────────────────────────────────────

async def has_open_gate(conversation_id: str | None) -> bool:
    """True when this conversation has a gated call waiting on a human.

    `core/kernels.py` asks this before interrupting a cell that ran past the
    timeout: an SDK call parked on an approval is not a runaway loop, and
    killing it would make "always blocking" mean "blocks for 60 seconds".
    """
    if not conversation_id:
        return False
    from sqlalchemy import select

    from db import models as db_models
    from db import ops
    from db.engine import async_session

    try:
        async with async_session() as session:
            result = await session.execute(
                select(db_models.Approval.id).where(
                    db_models.Approval.source == GATE_SOURCE,
                    db_models.Approval.status == ops.APPROVAL_OPEN,
                    db_models.Approval.parent_id == conversation_id,
                ).limit(1)
            )
            return result.scalars().first() is not None
    except Exception:
        logger.debug("has_open_gate failed", exc_info=True)
        return False
