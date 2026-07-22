"""Todo list tools — let the agent track its own task list with per-item status.

Event emission goes through the framework-agnostic ``ToolContext`` (``ctx.emit``).
The remaining LangGraph types here — ``Command`` / ``InjectedState`` /
``InjectedToolCallId`` — are the genuine state-mutation mechanism (a tool
writing the shared ``todos`` reducer); they are the residual coupling a future
framework swap must re-map (see ``tools/context.py``).
"""

from typing import Annotated, Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from tools.context import current_ctx


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
    """Update your task list (REPLACES the entire list each call).

    Call at the start of complex multi-step work to plan, and update as you
    complete steps. The list shows in your context every turn and the user
    sees it live. New items start "pending"; use set_todo_status(index,
    status) to mark progress. Pass an empty list to clear.
    """
    items: list[dict] = [{"text": t, "status": "pending"} for t in todos]
    current_ctx().emit("todos_updated", todos=items)
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
    """Mark a single todo item pending/in_progress/done by its 0-based index.

    Signals progress on a long task. Read indices from the list shown in your
    context each turn.
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
    current_ctx().emit("todos_updated", todos=todos)
    ack = f"Set todo {index} to {status!r}."
    return Command(update={
        "todos": todos,
        "messages": [ToolMessage(ack, tool_call_id=tool_call_id)],
    })
