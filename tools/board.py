"""Agent tools for the shared task board (kanban).

Board tasks are durable background work items (db.models.BoardTask) — unlike
write_todos (an in-conversation plan), a board task survives restarts, runs on
its own agent loop, and is visible/manageable in the web UI. create_task /
list_tasks work from any run; complete_task / block_task only make sense
inside a board-task run (they act on the current task via ToolContext).
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from db.engine import async_session
from db.ops import (
    list_board_tasks as _list,
    update_board_task,
)
from tools.context import current_ctx


async def list_tasks(status: str | None = None) -> str:
    """List tasks on the shared task board.

    Args:
        status: Optional filter — one of "todo", "ready", "running",
                "blocked", "done", "archived". None lists everything except
                archived.
    """
    async with async_session() as session:
        tasks = await _list(session, include_archived=(status == "archived"))
    if status:
        tasks = [t for t in tasks if t.status == status]
    if not tasks:
        return "No board tasks found."
    lines = []
    for t in tasks:
        extra = ""
        if t.status == "blocked" and t.blocked_reason:
            extra = f" | blocked: {t.blocked_reason[:120]}"
        elif t.status == "done" and t.summary:
            extra = f" | summary: {t.summary[:120]}"
        lines.append(f"- id={t.id} | [{t.status}] {t.title} | priority={t.priority}{extra}")
    return "\n".join(lines)


# Bound as tools in a board run (`core/agents.py`); the two above are SDK
# functions. As tools, as LangGraph's ToolNode made them of plain functions.
@tool
async def complete_task(summary: str, metadata: str | None = None) -> str:
    """Mark the board task you are currently executing as done.

    Only valid inside a board-task run. Call when the goal is achieved — the
    summary (and optional metadata) is the handoff dependent tasks receive.

    Args:
        summary: Concise handoff: what was done, where results live.
        metadata: Optional JSON object string with structured results,
                  e.g. '{"artifact": "...", "files": [...]}'.
    """
    ctx = current_ctx()
    if not ctx.board_task_id:
        return "Error: complete_task is only available while executing a board task."
    if metadata is not None:
        try:
            parsed = json.loads(metadata)
            if not isinstance(parsed, dict):
                return "Error: metadata must be a JSON object."
        except json.JSONDecodeError as exc:
            return f"Error: metadata is not valid JSON: {exc}"
    async with async_session() as session:
        task = await update_board_task(
            session, ctx.board_task_id,
            status="done", summary=summary, result_metadata=metadata,
            blocked_reason=None, blocked_kind=None,
        )
    if task is None:
        return "Error: current board task not found."
    return "Task marked done. Wrap up with a short final reply."


@tool
async def block_task(reason: str, needs_input: bool = False) -> str:
    """Mark the board task you are currently executing as blocked.

    Only valid inside a board-task run. Call when you can't finish — missing
    input/capability, or a decision only a human can make.

    Args:
        reason: What is missing and what would unblock it. When asking the
                user, phrase this as the question itself.
        needs_input: True when a human answer would unblock the task — the
                     board shows an answer box, and the answer reaches you on
                     resume (same conversation, context preserved).
    """
    ctx = current_ctx()
    if not ctx.board_task_id:
        return "Error: block_task is only available while executing a board task."
    async with async_session() as session:
        task = await update_board_task(
            session, ctx.board_task_id, status="blocked", blocked_reason=reason,
            blocked_kind="needs_input" if needs_input else "agent",
        )
    if task is None:
        return "Error: current board task not found."
    if needs_input:
        # The block itself is durable (it is a column on the task), but the
        # inbox reads one table, so mirror it there. Failing to write the row
        # must not fail the block — the board card still shows the question.
        from core.approvals import record_blocking_request
        from db.ops import board_task_conversation_id

        await record_blocking_request(
            source="board_task",
            kind="input",
            question=reason,
            label=task.title,
            board_task_id=task.id,
            parent_id=board_task_conversation_id(task.id),
        )
    return "Task marked blocked. Wrap up with a short final reply explaining the blocker."
