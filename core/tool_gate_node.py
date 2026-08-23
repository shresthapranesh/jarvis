"""The agent-graph half of per-tool approval.

Gating happens at the **graph node**, not by wrapping each tool, because the
bound tools are not uniform: `write_todos`/`set_todo_status` take
`InjectedToolCallId`/`InjectedState` and return `Command` state deltas, so a
wrapper that re-declares their schema would either drop the injection or break
the reducer write. The tool objects stay exactly as they are; what changes is
who is allowed to reach `ToolNode`.

The node replaces `ToolNode` in the graph and, per tool call:

* not gated → forwarded to the real `ToolNode` untouched;
* gated → blocks on `core/tool_gate.await_tool_approval` until a human answers;
* denied → answered with a `ToolMessage` saying so, and never executed.

The AI message in history keeps **all** of its tool calls; only the copy handed
to `ToolNode` is narrowed to the approved ones. That asymmetry is deliberate —
every `tool_use` still gets exactly one `tool_result`, so a denial cannot leave
the orphan pairing that Anthropic and Bedrock reject on the next call.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from core.tool_gate import await_tool_approval, denial_message
from core.tool_policy import bound_key, mcp_key, needs_approval

logger = logging.getLogger(__name__)


def _mcp_owner_map() -> dict[str, str]:
    """tool name -> MCP server, for tools that came from one."""
    try:
        from core.mcp import get_mcp_server_summaries

        return {
            tool: summary["name"]
            for summary in get_mcp_server_summaries()
            for tool in (summary.get("tools") or [])
        }
    except Exception:
        return {}


def tool_key_for(name: str, owners: dict[str, str] | None = None) -> str:
    """The policy key for a bound tool call.

    An MCP tool keeps its `mcp:<server>/<tool>` identity even while bound, so
    the Tools page shows one row per tool regardless of the server's load mode
    and a policy survives flipping that mode.
    """
    owners = _mcp_owner_map() if owners is None else owners
    server = owners.get(name)
    return mcp_key(server, name) if server else bound_key(name)


def _find_task_id(conversation_id: str | None) -> str | None:
    """The live run for this conversation, so the row can be closed with it.

    `TaskState` has no id field (the registry key is the id), so this is a
    reverse lookup by `parent_id` — the same shape `core/state.task_id_of` uses.
    """
    if not conversation_id:
        return None
    try:
        from core.state import _tasks

        for task_id, state in _tasks.items():
            if not state.done and state.parent_id == conversation_id:
                return task_id
    except Exception:
        pass
    return None


def make_gated_tool_node(tools: list[Any]):
    """A drop-in replacement for `ToolNode(tools)` that honors tool policy."""
    inner = ToolNode(tools)
    gated_names = {getattr(t, "name", "") for t in tools}

    async def gated_tools(state: dict, config: Optional[RunnableConfig] = None) -> Any:
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        if not isinstance(last, AIMessage) or not calls:
            return await inner.ainvoke(state, config)

        owners = _mcp_owner_map()
        # Resolved once per batch: a policy flip mid-batch would otherwise let
        # two calls in the same AI message disagree about the rules.
        wanted = [
            (call, tool_key_for(call.get("name", ""), owners))
            for call in calls
            if call.get("name") in gated_names
        ]
        gate_list = [(call, key) for call, key in wanted if needs_approval(key)]
        if not gate_list:
            return await inner.ainvoke(state, config)

        from tools.context import current_ctx

        ctx = current_ctx()
        task_id = _find_task_id(ctx.conversation_id)

        denied: list[ToolMessage] = []
        approved_ids: set[Any] = {call.get("id") for call in calls}
        for call, key in gate_list:
            name = call.get("name", "")
            ok, answer = await await_tool_approval(
                tool_key=key,
                tool_name=name,
                args=call.get("args") or {},
                conversation_id=ctx.conversation_id,
                task_id=task_id,
                emit=ctx.emit,
            )
            if not ok:
                approved_ids.discard(call.get("id"))
                denied.append(
                    ToolMessage(
                        denial_message(name, answer),
                        tool_call_id=call.get("id") or "",
                        name=name,
                        status="error",
                    )
                )

        remaining = [c for c in calls if c.get("id") in approved_ids]
        if not remaining:
            # Nothing survived: skip ToolNode entirely rather than handing it an
            # AI message with no tool calls, which it rejects.
            return {"messages": denied}

        narrowed = last.model_copy(update={"tool_calls": remaining})
        result = await inner.ainvoke(
            {**state, "messages": [*messages[:-1], narrowed]}, config
        )
        return _merge(result, denied)

    return gated_tools


def _merge(result: Any, denied: list[ToolMessage]) -> Any:
    """Fold denial messages into whatever `ToolNode` returned.

    `ToolNode` returns a state dict normally, but a *list of `Command`s* when
    any tool returned one (`write_todos` does), so there is no single shape to
    append to.
    """
    if not denied:
        return result
    if isinstance(result, list):
        return [*result, Command(update={"messages": denied})]
    if isinstance(result, Command):
        return [result, Command(update={"messages": denied})]
    if isinstance(result, dict):
        merged = dict(result)
        merged["messages"] = [*(merged.get("messages") or []), *denied]
        return merged
    return result
