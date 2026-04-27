"""Todo list tool — lets the agent track its own task list."""

from langchain_core.tools import tool
from langgraph.types import Command


@tool
def write_todos(todos: list[str]) -> Command:
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
    return Command(update={"todos": todos})
