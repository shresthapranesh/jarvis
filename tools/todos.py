"""Todo list tools — let the agent track its own task list with per-item status.

The list belongs to the run's thread (`ToolContext.thread`, persisted as
`thread_state.todos`); each change is also emitted live as `todos_updated`.
"""

from typing import Literal

from langchain_core.tools import tool

from core.schemas import _normalise_todos
from tools.context import current_ctx


@tool
async def write_todos(todos: list[str]) -> str:
    """Replace your entire task list. Plan multi-step work, then update as you go.

    Shown in your context every turn, and live to the user. New items start
    "pending" — use set_todo_status to advance. Empty list clears.
    """
    ctx = current_ctx()
    items = [{"text": t, "status": "pending"} for t in todos]
    ctx.emit("todos_updated", todos=items)
    if ctx.thread is not None:
        await ctx.thread.set_todos(items)
    return f"Updated todo list ({len(items)} item{'s' if len(items) != 1 else ''})."


@tool
async def set_todo_status(index: int, status: Literal["pending", "in_progress", "done"]) -> str:
    """Mark one todo pending/in_progress/done by its 0-based index.

    Indices come from the list shown in your context each turn.
    """
    ctx = current_ctx()
    todos = _normalise_todos(ctx.thread.todos if ctx.thread is not None else [])
    if index < 0 or index >= len(todos):
        return f"Error: index {index} out of range (have {len(todos)} todos)."
    todos[index] = {"text": todos[index]["text"], "status": status}
    ctx.emit("todos_updated", todos=todos)
    if ctx.thread is not None:
        await ctx.thread.set_todos(todos)
    return f"Set todo {index} to {status!r}."
