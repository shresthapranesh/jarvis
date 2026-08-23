"""The tool inventory — every tool the agent can reach, and its policy.

One query rather than three, because the question the Tools page asks is "what
can this agent do", and the answer spans families that are otherwise unrelated:
graph-bound tools, the `jarvis` SDK, and MCP servers. `core/tool_policy.py`
owns the merge; this is a thin projection of it.
"""

from __future__ import annotations

import strawberry

from core.tool_policy import tool_inventory

from ..types.tool import AgentTool


@strawberry.type
class ToolQuery:
    @strawberry.field
    async def tools(self, info: strawberry.Info) -> list[AgentTool]:
        """Bound tools, `jarvis` SDK functions and MCP tools, with their policy."""
        # Reads the DB through the policy module's own read-only connection —
        # the inventory is process state (loaded MCP tools, the SDK catalogue),
        # not rows to join against the request's session.
        return [AgentTool.from_info(info_) for info_ in tool_inventory()]
