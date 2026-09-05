"""Durable human-in-the-loop approvals.

`core/approval.py` (singular) is the *mechanism* for one pause: it raises a
LangGraph interrupt and blocks the calling tool until an answer arrives. This
module is the *record*: every request becomes an `Approval` row, so a request
survives the run that raised it and the inbox can list requests it never saw
happen.

Two shapes, because one mechanism cannot cover every caller:

* **Blocking** — a run is suspended right now. Chat interrupts, workflow
  approval/human_input nodes, board tasks blocked on a question. Resolving
  hands the answer to the waiting run.
* **Deferred** — nobody is waiting; the operation was *recorded* instead of
  performed, and approving is what executes it. This exists because the
  `jarvis` SDK runs in a **separate kernel process** (jupyter_client spawns it),
  where `current_ctx()` finds no LangGraph runtime and the interrupt mechanism
  cannot reach. Its writes arrive as ordinary GraphQL requests, so the gate has
  to live in the resolver and cannot block anything.

Deferred is the better shape even where blocking is possible: a human may take
hours, and a blocked run holds a worker slot and a kernel the whole time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.tool_gate import GATE_SOURCE, announce_request
from db import models as db_models
from db import ops

logger = logging.getLogger(__name__)


# ── Deferred actions ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActionSpec:
    """A destructive operation that may be recorded now and run on approval."""

    label: str
    # Rendered into the approval question. Kept separate from `label` so the
    # inbox can say *which* workflow, not just "delete a workflow".
    describe: Callable[[dict[str, Any]], str]
    execute: Callable[[AsyncSession, dict[str, Any]], Awaitable[str]]


async def _exec_delete_workflow(session: AsyncSession, payload: dict[str, Any]) -> str:
    deleted = await ops.delete_workflow(session, payload["workflow_id"])
    return "Deleted." if deleted else "Workflow no longer exists."


async def _exec_delete_automation(session: AsyncSession, payload: dict[str, Any]) -> str:
    deleted = await ops.delete_automation(session, payload["automation_id"])
    if deleted:
        # Same obligation as the mutation: a scheduled job that outlives its
        # row keeps firing against nothing.
        try:
            from core.scheduler import _remove_scheduler_job

            _remove_scheduler_job(payload["automation_id"])
        except Exception:  # pragma: no cover - scheduler absent in tests/CLI
            logger.debug("could not unregister scheduler job", exc_info=True)
    return "Deleted." if deleted else "Automation no longer exists."


async def _exec_delete_skill(session: AsyncSession, payload: dict[str, Any]) -> str:
    deleted = await ops.delete_skill(session, payload["skill_id"])
    return "Deleted." if deleted else "Skill no longer exists."


async def _exec_call_mcp_tool(session: AsyncSession, payload: dict[str, Any]) -> str:
    """Run the MCP tool the agent asked for once a human approves it."""
    from core.mcp import call_mcp_tool

    content, is_error = await call_mcp_tool(
        payload["server"], payload["tool"], payload.get("args") or {}
    )
    prefix = "MCP tool failed: " if is_error else ""
    return f"{prefix}{content}"[:4000]


def _describe_mcp_call(payload: dict[str, Any]) -> str:
    args = payload.get("args") or {}
    try:
        rendered = json.dumps(args, default=str)
    except Exception:
        rendered = str(args)
    if len(rendered) > 300:
        rendered = rendered[:300] + "…"
    return f"Call MCP tool {payload.get('server')}.{payload.get('tool')} with {rendered}?"


ACTIONS: dict[str, ActionSpec] = {
    "delete_workflow": ActionSpec(
        label="Delete workflow",
        describe=lambda p: f"Delete workflow {p.get('name') or p.get('workflow_id')}? This cannot be undone.",
        execute=_exec_delete_workflow,
    ),
    "delete_automation": ActionSpec(
        label="Delete automation",
        describe=lambda p: f"Delete automation {p.get('name') or p.get('automation_id')}? This cannot be undone.",
        execute=_exec_delete_automation,
    ),
    "delete_skill": ActionSpec(
        label="Delete skill",
        describe=lambda p: f"Delete skill {p.get('name') or p.get('skill_id')}? This cannot be undone.",
        execute=_exec_delete_skill,
    ),
    # An MCP server is third-party code doing arbitrary work; a lazy call from
    # the kernel can't route through the blocking in-process approval helper
    # (no LangGraph runtime there), so this is the gate for it. Off unless the
    # operator opts in, like everything else in ACTIONS.
    "call_mcp_tool": ActionSpec(
        label="Call MCP tool",
        describe=_describe_mcp_call,
        execute=_exec_call_mcp_tool,
    ),
}

# Which actions actually require a human.
#
# **Nothing, by default.** The framework is fully wired — every gated call site
# resolves an ActionSpec, checks this set, and would record a durable request —
# but with no opt-in every action passes straight through. That is deliberate:
# turning gating on changes what the agent can do on its own, and that decision
# belongs to whoever runs the install, not to a default. The plumbing being
# live means enabling it is a config write, not a deploy.
#
# Opt in at runtime, no code change:
#     config set approval.required_actions "all"
#     config set approval.required_actions "delete_workflow,delete_automation"
#     config set approval.required_actions "none"     # back to pass-through
_DEFAULT_REQUIRED: frozenset[str] = frozenset()
_CONFIG_KEY = "approval.required_actions"


async def required_actions(session: AsyncSession) -> frozenset[str]:
    raw = await ops.get_setting(session, _CONFIG_KEY)
    if raw is None:
        return _DEFAULT_REQUIRED
    names = {part.strip() for part in raw.split(",") if part.strip()}
    if not names or names == {"none"}:
        return frozenset()
    if names == {"all"}:
        # Spelled out rather than left implicit: an operator who adds a new
        # ActionSpec later should get it gated without editing config again.
        return frozenset(ACTIONS)
    unknown = names - set(ACTIONS)
    if unknown:
        logger.warning("%s lists unknown actions: %s", _CONFIG_KEY, ", ".join(sorted(unknown)))
    return frozenset(names & set(ACTIONS))


class ApprovalRequired(Exception):
    """Raised by a resolver instead of performing a gated action.

    Carries the row so the caller can tell the agent what to wait for. It is an
    exception rather than a return value because it must abort the mutation —
    a resolver that recorded the approval *and* returned success would have
    performed the very operation it was gating.
    """

    def __init__(self, approval: db_models.Approval) -> None:
        self.approval = approval
        super().__init__(
            f"Approval required: {approval.question} "
            f"(approval id {approval.id}). It is now pending in /approvals; "
            "the action runs only once a human approves it."
        )


async def gate_action(
    session: AsyncSession,
    action: str,
    payload: dict[str, Any],
    *,
    source: str = "chat",
    label: str = "",
    parent_id: str | None = None,
) -> None:
    """Record `action` for approval and abort, unless it is not gated.

    Returns normally when the caller may proceed. Raises `ApprovalRequired`
    otherwise — including an already-pending duplicate, so an agent that
    retries the same delete in a loop does not fill the inbox with copies.
    """
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"unknown gated action {action!r}")
    if action not in await required_actions(session):
        # Auto-approved: the call site is wired, gating is simply not enabled
        # for this action. Logged so the path is observable when someone is
        # deciding whether to turn it on.
        logger.debug("approval not required for %s — proceeding (%s)", action, _CONFIG_KEY)
        return

    existing = await _find_duplicate(session, action, payload)
    if existing is not None:
        raise ApprovalRequired(existing)

    row = await ops.create_approval(
        session,
        source=source,
        kind="approval",
        question=spec.describe(payload),
        label=label or spec.label,
        tool=action,
        args_json=json.dumps(payload, default=repr)[:2000],
        action=action,
        action_payload=json.dumps(payload, default=repr),
        parent_id=parent_id,
    )
    # Nothing is blocked, but the conversation that asked for this deserves to
    # be told it did not happen — otherwise the only trace is the agent
    # mentioning it in prose, and the request lives solely in /approvals.
    announce_request(row)
    raise ApprovalRequired(row)


async def _find_duplicate(
    session: AsyncSession, action: str, payload: dict[str, Any],
) -> db_models.Approval | None:
    for row in await ops.list_approvals(session, status=ops.APPROVAL_OPEN):
        if row.action != action or not row.action_payload:
            continue
        try:
            if json.loads(row.action_payload) == payload:
                return row
        except json.JSONDecodeError:
            continue
    return None


# ── Blocking requests ────────────────────────────────────────────────────────

async def record_blocking_request(
    *,
    source: str,
    kind: str,
    question: str,
    label: str,
    task_id: str | None = None,
    interrupt_id: str | None = None,
    parent_id: str | None = None,
    board_task_id: str | None = None,
    tool: str | None = None,
    args_json: str | None = None,
) -> str | None:
    """Persist a pause that is happening right now. Returns the row id.

    Best-effort by design: a failure here must not take down the run that is
    already suspended. The worst case is an approval missing from the inbox
    that the run's own subscriber can still resolve — losing the run itself
    would be strictly worse.
    """
    from db import async_session

    try:
        async with async_session() as session:
            row = await ops.create_approval(
                session,
                source=source,
                kind=kind,
                question=question,
                label=label,
                task_id=task_id,
                interrupt_id=interrupt_id,
                parent_id=parent_id,
                board_task_id=board_task_id,
                tool=tool,
                args_json=args_json,
            )
            return row.id
    except Exception:
        logger.warning("could not persist approval request", exc_info=True)
        return None


# ── Resolution ───────────────────────────────────────────────────────────────

async def resolve(
    session: AsyncSession, approval_id: str, answer: str,
) -> db_models.Approval:
    """Answer one approval, whatever shape it is.

    Dispatch order matters: a deferred row is executed here and now, while a
    blocking row only hands the answer to whoever is waiting. The row is
    closed *after* the effect succeeds, so a failure leaves it pending and
    answerable rather than silently consumed.
    """
    from core.approval import is_affirmative_answer

    row = await ops.get_approval(session, approval_id)
    if row is None:
        raise ValueError("approval not found")
    if row.status != ops.APPROVAL_OPEN:
        raise ValueError(f"approval already {row.status}")

    approved = True
    if row.kind == "approval":
        parsed = is_affirmative_answer(answer)
        # Ambiguous denies, matching core/approval.py: a reply that matches no
        # keyword is usually a question, and running a destructive action on
        # that basis is the wrong default.
        approved = parsed is True

    if row.action:
        return await _resolve_deferred(session, row, answer, approved)
    if row.source == GATE_SOURCE:
        return await _resolve_tool_gate(session, row, answer, approved)
    if row.board_task_id:
        return await _resolve_board(session, row, answer)
    return await _resolve_blocking(session, row, answer, approved)


async def _resolve_tool_gate(
    session: AsyncSession, row: db_models.Approval, answer: str, approved: bool,
) -> db_models.Approval:
    """A per-tool gate (`core/tool_gate.py`): the caller is blocked on the row.

    Nothing is executed here and no future is woken through the run — closing
    the row *is* the answer, because the waiter is watching the row. That is
    what lets the same gate work for a chat, a board task and a call made from
    the kernel process, which have nothing else in common.
    """
    from core.tool_gate import announce_resolved, notify_resolved

    status = "approved" if approved else "denied"
    resolved = await ops.resolve_approval_row(
        session,
        row.id,
        status=status,
        answer=answer,
        result="Released the waiting call." if approved else "The call was not run.",
    )
    assert resolved is not None
    # After the commit: an in-process waiter that wakes early would otherwise
    # re-read the row and still see `pending`.
    notify_resolved(row.id, approved=approved, answer=answer)
    # The answer usually arrives from the inbox, so the conversation that is
    # showing the prompt has to be told to stop showing it.
    announce_resolved(resolved, approved=approved, answer=answer)
    return resolved


async def _resolve_deferred(
    session: AsyncSession, row: db_models.Approval, answer: str, approved: bool,
) -> db_models.Approval:
    if not approved:
        resolved = await ops.resolve_approval_row(
            session, row.id, status="denied", answer=answer, result="Not executed.",
        )
        assert resolved is not None
        return resolved

    spec = ACTIONS.get(row.action or "")
    if spec is None:
        raise ValueError(f"approval references unknown action {row.action!r}")
    payload = json.loads(row.action_payload or "{}")
    # Runs before the row is closed: if it raises, the approval stays pending
    # and the human can retry instead of losing the request.
    result = await spec.execute(session, payload)
    resolved = await ops.resolve_approval_row(
        session, row.id, status="approved", answer=answer, result=result,
    )
    assert resolved is not None
    return resolved


async def _resolve_board(
    session: AsyncSession, row: db_models.Approval, answer: str,
) -> db_models.Approval:
    from server.task_board_runtime import answer_board_task

    # Delegates the close as well: `answer_board_task` already closes every
    # open row for the task, and resolving here too would hit the
    # already-resolved guard. Re-read to return the committed row.
    await answer_board_task(session, row.board_task_id or "", answer)
    refreshed = await ops.get_approval(session, row.id)
    assert refreshed is not None
    return refreshed


async def _resolve_blocking(
    session: AsyncSession, row: db_models.Approval, answer: str, approved: bool,
) -> db_models.Approval:
    from core.state import _tasks, emit_event

    state = _tasks.get(row.task_id or "")
    if state is None or state.resume_future is None or state.resume_future.done():
        # The run is gone (restart, crash, or it moved on). Say so instead of
        # reporting success for an answer nobody received.
        await ops.resolve_approval_row(
            session, row.id, status="expired", answer=answer,
            result="The run was no longer waiting; the answer was not delivered.",
        )
        raise ValueError("the run this approval belongs to is no longer waiting")

    state.resume_future.set_result(answer)
    emit_event(state, "interrupt_resolved", interrupt_id=row.interrupt_id)
    state.clear_interrupt()
    status = "answered" if row.kind == "input" else ("approved" if approved else "denied")
    resolved = await ops.resolve_approval_row(
        session, row.id, status=status, answer=answer, result="Delivered to the run.",
    )
    assert resolved is not None
    return resolved


# ── Startup reconciliation ───────────────────────────────────────────────────

async def reconcile_startup() -> dict[str, int]:
    """Make the inbox honest about what a restart just destroyed.

    Durability of the *record* is free; durability of the *pause* is not, and
    it differs per source:

    * **deferred** — nothing was waiting, so nothing was lost. Stays pending.
    * **board** — the block is a column on the task, so it survives outright.
      Rows are backfilled here for tasks blocked before this table existed.
    * **chat** — LangGraph checkpointed the interrupt, so the run can be
      resumed once its job is re-claimed. Kept pending only while a job row
      still exists to re-claim it.
    * **workflow / automation** — the workflow engine holds its entire run
      state in memory (BFS frontier, asyncio futures) and checkpoints nothing,
      so a restart re-runs the graph from the start and asks again. The old
      request is dead; marking it `expired` is the honest outcome.
    * **tool gate** (`core/tool_gate.py`) — the waiter was an in-flight call, in
      this process or in a kernel that died with it. Nothing is left to release,
      so it expires with the workflow rows rather than becoming a button that
      unblocks a caller that no longer exists.
    """
    from db import async_session

    counts = {"expired": 0, "backfilled": 0, "kept": 0}
    async with async_session() as session:
        for row in await ops.list_approvals(session, status=ops.APPROVAL_OPEN):
            if row.action or row.board_task_id:
                counts["kept"] += 1
                continue
            if row.source == "chat" and row.task_id and await _job_exists(session, row.task_id):
                counts["kept"] += 1
                continue
            await ops.resolve_approval_row(
                session, row.id, status="expired",
                result="The run was lost when the server restarted.",
            )
            counts["expired"] += 1

        counts["backfilled"] = await _backfill_board(session)

    if counts["expired"] or counts["backfilled"]:
        logger.info(
            "approvals reconciled: %d expired, %d backfilled, %d kept",
            counts["expired"], counts["backfilled"], counts["kept"],
        )
    return counts


async def _job_exists(session: AsyncSession, task_id: str) -> bool:
    job = await session.get(db_models.Job, task_id)
    return job is not None and job.status in ("pending", "running")


async def _backfill_board(session: AsyncSession) -> int:
    """Give every already-blocked board task a row, so tasks that were waiting
    before this table existed still appear in the inbox."""
    created = 0
    for task in await ops.list_board_tasks_awaiting_input(session):
        existing = await ops.list_approvals(session, status=ops.APPROVAL_OPEN)
        if any(row.board_task_id == task.id for row in existing):
            continue
        await ops.create_approval(
            session,
            source="board_task",
            kind="input",
            question=task.blocked_reason or "The task is waiting on an answer.",
            label=task.title,
            board_task_id=task.id,
            parent_id=ops.board_task_conversation_id(task.id),
        )
        created += 1
    return created
