"""Agent tools for the shared task board (kanban).

Board tasks are durable background work items (db.models.BoardTask) — unlike
write_todos (an in-conversation plan), a board task survives restarts, runs on
its own agent loop, and is visible/manageable in the web UI. create_task /
list_tasks work from any run; complete_task / block_task only make sense
inside a board-task run (they act on the current task via ToolContext).
"""

from __future__ import annotations

import json

from db.engine import async_session
from db.ops import (
    create_board_task as _create,
    list_board_tasks as _list,
    update_board_task,
)
from tools.context import current_ctx


async def create_task(
    title: str,
    body: str,
    priority: int = 0,
    depends_on: str | None = None,
    model: str | None = None,
    skill: str | None = None,
    start: bool = True,
    decompose: bool = False,
) -> str:
    """Create a durable task on the shared task board.

    The task runs in the background on its own agent loop and conversation,
    independent of this chat — use it to queue follow-up work or fan a big job
    into pieces. For an in-conversation checklist use write_todos instead.

    Args:
        title: Short imperative title.
        body: Full instructions for the executing agent.
        priority: Higher runs first. Default 0.
        depends_on: Comma-separated ids of tasks that must finish first; their
                    completion summaries are handed to this task as context.
        model: Model id; None = default.
        skill: Saved skill name the task's agent should follow.
        start: False parks it in todo until a human readies it. Ignored when
               depends_on is set.
        decompose: Have a planner split it into parallel subtasks; the task
                   itself runs last as the synthesis step. Cannot be combined
                   with depends_on.
    """
    parent_ids = [p.strip() for p in (depends_on or "").split(",") if p.strip()]
    if decompose and parent_ids:
        return "Error: decompose cannot be combined with depends_on."
    status = "todo" if decompose else ("ready" if start else "todo")
    try:
        async with async_session() as session:
            task = await _create(
                session,
                title=title,
                body=body,
                status=status,
                priority=priority,
                created_by="agent",
                model=model,
                skill=skill,
                parent_ids=parent_ids,
            )
    except ValueError as exc:
        return f"Error: {exc}"
    # Lazy imports: tools must not import server modules at load time
    # (core.agents imports this file; server imports core.agents).
    if decompose:
        from server.task_board_runtime import decompose_board_task
        try:
            subtasks = await decompose_board_task(task.id)
        except ValueError as exc:
            return (
                f"Created board task '{task.title}' (id={task.id}, status=todo) "
                f"but decomposition failed: {exc}. The task is parked in todo."
            )
        sub_lines = "\n".join(f"  - {s.title} (id={s.id})" for s in subtasks)
        return (
            f"Created board task '{task.title}' (id={task.id}) and split it into "
            f"{len(subtasks)} subtasks that run first:\n{sub_lines}\n"
            "The original task runs last with their results."
        )
    if task.status == "ready":
        from server.task_board_runtime import dispatch_board_tasks
        await dispatch_board_tasks()
    return (
        f"Created board task '{task.title}' (id={task.id}, status={task.status}"
        + (f", depends on {len(parent_ids)} task(s)" if parent_ids else "")
        + ")."
    )


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


async def complete_task(summary: str, metadata: str | None = None) -> str:
    """Mark the board task you are currently executing as done.

    Only valid inside a board-task run. Call this when the task's goal is
    achieved — the summary (and optional metadata) is the handoff that
    dependent tasks receive as context.

    Args:
        summary: Concise handoff: what was done, where the results live.
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


async def block_task(reason: str, needs_input: bool = False) -> str:
    """Mark the board task you are currently executing as blocked.

    Only valid inside a board-task run. Call this when you cannot finish —
    missing input, missing capability, or a decision only a human can make.

    Args:
        reason: What is missing and what would unblock the task. When asking
                the user something, phrase this as the question itself.
        needs_input: Set True when a human answer would unblock the task —
                     the board shows an answer box on the card, and the answer
                     is delivered to you when the task resumes (same
                     conversation, so you keep your context).
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
    return "Task marked blocked. Wrap up with a short final reply explaining the blocker."
