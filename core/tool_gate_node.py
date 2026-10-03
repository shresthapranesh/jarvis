"""The agent-loop half of per-tool approval.

Gating happens in the loop, between the model's reply and the tool batch, not
by wrapping each tool: a wrapper would have to re-declare every tool's schema,
and the tool objects stay exactly as they are this way. What changes is which
calls are allowed to reach them.

Per tool call:

* not gated → runs;
* gated → blocks on `core/tool_gate.await_tool_approval` until a human answers;
* denied → answered with a `ToolMessage` saying so, and never executed.

The AI message in history keeps **all** of its tool calls; a denied call gets
its denial as its result. Every `tool_use` still gets exactly one
`tool_result`, so a denial cannot leave the orphan pairing that Anthropic and
Bedrock reject on the next call.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import ToolMessage

from core.tool_gate import await_tool_approval, denial_message, live_task_id
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


def make_tool_gate(tools: list[Any]):
    """The gate for an agent bound to `tools` (`core/agent_loop.ToolGate`)."""
    bound = {getattr(t, "name", "") for t in tools}

    async def gate(run: Any, calls: list[dict[str, Any]]) -> dict[str, ToolMessage]:
        owners = _mcp_owner_map()
        # Resolved once per batch: a policy flip mid-batch would otherwise let
        # two calls in the same AI message disagree about the rules.
        gated = [
            (call, key)
            for call in calls
            if call.get("name") in bound
            and needs_approval(key := tool_key_for(call.get("name", ""), owners))
        ]
        if not gated:
            return {}

        from tools.context import current_ctx

        ctx = current_ctx()
        task_id = live_task_id(ctx.conversation_id)

        denied: dict[str, ToolMessage] = {}
        for call, key in gated:
            name = call.get("name", "")
            ok, answer = await await_tool_approval(
                tool_key=key,
                tool_name=name,
                args=call.get("args") or {},
                conversation_id=ctx.conversation_id,
                task_id=task_id,
            )
            if not ok:
                denied[call.get("id") or ""] = ToolMessage(
                    denial_message(name, answer),
                    tool_call_id=call.get("id") or "",
                    name=name,
                    status="error",
                )
        return denied

    return gate
