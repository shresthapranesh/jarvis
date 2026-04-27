"""Todo list tool — lets the agent track its own task list."""

from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command


@tool
def write_todos(
    todos: list[str],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Update your task list.

    Call at the start of complex multi-step work to plan, and update as you
    complete steps. The current list is shown in your context on every turn.
    Pass an empty list to clear.

    Example:
      write_todos([
        "Search for Q1 revenue figures",
        "Compare with prior year",
        "Write summary report",
      ])
    """
    ack = f"Updated todo list ({len(todos)} item{'s' if len(todos) != 1 else ''})."
    return Command(update={
        "todos": todos,
        "messages": [ToolMessage(ack, tool_call_id=tool_call_id)],
    })
