"""Tool policy writes, and the request side of a blocking gate.

`setToolPolicy` is the human's control: switch a tool off, or require an
approval before the agent may use it. `requestToolApproval` is the agent's side
of that second switch — the `jarvis` SDK runs in a kernel process with a
read-only database connection, so it cannot create its own request row and
routes it through here, exactly as it routes its other writes.
"""

from __future__ import annotations

import json

import strawberry

from core.tool_gate import create_gate_request
from core.tool_policy import set_tool_policy, split_key, tool_inventory

from ..types.tool import AgentTool, ToolApprovalRequest


@strawberry.type
class ToolMutation:
    @strawberry.mutation
    async def set_tool_policy(
        self,
        info: strawberry.Info,
        key: str,
        enabled: bool | None = None,
        requires_approval: bool | None = None,
    ) -> list[AgentTool]:
        """Set one tool's policy; returns the whole refreshed inventory.

        The whole list, because a single row cannot express what changed: the
        compiled agent graphs are dropped on every write, and the caller wants
        to re-render against the state the server actually holds.
        """
        session = info.context["session"]
        await set_tool_policy(session, key, enabled=enabled, approval=requires_approval)
        return [AgentTool.from_info(i) for i in tool_inventory()]

    @strawberry.mutation
    async def request_tool_approval(
        self,
        info: strawberry.Info,
        tool_key: str,
        tool: str,
        args_json: str = "{}",
        conversation_id: str | None = None,
    ) -> ToolApprovalRequest:
        """Record a pending approval for a gated call and return its id.

        The caller then blocks until the row leaves `pending` — see
        `tools/sdk.py:_await_gate`. Only the agent should be creating these;
        a human clicking a button in the UI *is* the approval.
        """
        if info.context.get("caller") != "agent":
            raise ValueError("requestToolApproval is only for agent-initiated calls")
        kind, _ = split_key(tool_key)
        if not kind:
            raise ValueError(f"unknown tool key {tool_key!r}")
        try:
            args = json.loads(args_json) if args_json else {}
        except Exception as exc:
            raise ValueError(f"args_json must be valid JSON: {exc}")
        if not isinstance(args, dict):
            raise ValueError("args_json must be a JSON object")

        row = await create_gate_request(
            info.context["session"],
            tool_key=tool_key,
            tool_name=tool,
            args=args,
            conversation_id=conversation_id or info.context.get("caller_conversation_id"),
        )
        return ToolApprovalRequest(id=row.id, status=row.status)
