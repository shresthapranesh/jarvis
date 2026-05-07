"""Todo list tools — let the agent track its own task list with per-item status."""

from typing import Annotated, Any, Literal

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command


def _normalise(raw: Any) -> list[dict]:
    if not raw or not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            out.append({"text": item, "status": "pending"})
        elif isinstance(item, dict) and "text" in item:
            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "done"):
                status = "pending"
            out.append({"text": str(item["text"]), "status": status})
    return out


@tool
async def write_todos(
    todos: list[str],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Update your task list.

    Call at the start of complex multi-step work to plan, and update as you
    complete steps. The current list is shown in your context on every turn,
    and the user sees it live in their UI. Pass an empty list to clear.

    Each call REPLACES the entire list. New items start as "pending"; use
    `set_todo_status(index, status)` to mark progress without rewriting.

    Example:
      write_todos([
        "Search for Q1 revenue figures",
        "Compare with prior year",
        "Write summary report",
      ])
    """
    items: list[dict] = [{"text": t, "status": "pending"} for t in todos]
    await adispatch_custom_event("todos_updated", {"type": "todos_updated", "todos": items})
    ack = f"Updated todo list ({len(items)} item{'s' if len(items) != 1 else ''})."
    return Command(update={
        "todos": items,
        "messages": [ToolMessage(ack, tool_call_id=tool_call_id)],
    })


@tool
async def set_todo_status(
    index: int,
    status: Literal["pending", "in_progress", "done"],
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Mark a single todo item as pending/in_progress/done by its 0-based index.

    Use to signal progress on a long-running task. The current list is shown
    in your context every turn — read the indices from there.
    """
    todos = _normalise(state.get("todos"))
    if index < 0 or index >= len(todos):
        return Command(update={
            "messages": [ToolMessage(
                f"Error: index {index} out of range (have {len(todos)} todos).",
                tool_call_id=tool_call_id,
            )],
        })
    todos[index] = {"text": todos[index]["text"], "status": status}
    await adispatch_custom_event("todos_updated", {"type": "todos_updated", "todos": todos})
    ack = f"Set todo {index} to {status!r}."
    return Command(update={
        "todos": todos,
        "messages": [ToolMessage(ack, tool_call_id=tool_call_id)],
    })
