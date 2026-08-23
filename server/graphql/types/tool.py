"""AgentTool — one row of the tool inventory, whatever family it comes from.

Not a Relay Node — nothing refetches a single tool by global id. It does carry
a plain `id` (the policy key: `bound:run_cell`, `sdk:create_automation`,
`mcp:github/create_issue`), because that is what lets Relay normalize the
mutation's response onto the same records the page is already rendering.
"""

from __future__ import annotations

import strawberry

from core.tool_policy import ToolInfo


@strawberry.type
class AgentTool:
    # The policy key doubles as the record id so Relay normalizes a tool to one
    # record: `setToolPolicy` returns the whole inventory, and without a stable
    # id the client would hold two unrelated copies and render a stale toggle
    # until the next full fetch.
    id: strawberry.ID
    # Policy key — `<kind>:<name>`. Pass this back to `setToolPolicy`.
    key: str
    # bound | sdk | mcp
    kind: str
    name: str
    description: str
    # MCP server name, SDK category, or "agent" for graph-bound tools.
    group: str
    enabled: bool
    requires_approval: bool
    # True when the tool's schema is sent to the model on every LLM call
    # (bound tools, `always` MCP servers) — i.e. when it costs tokens whether
    # or not it is used.
    in_prompt: bool
    # False when configured but not currently reachable: an MCP server that
    # never connected, or a bound tool whose precondition is missing.
    available: bool
    # Why it is unavailable, or when it is bound — "board runs only", etc.
    detail: str

    @classmethod
    def from_info(cls, info: ToolInfo) -> AgentTool:
        return cls(
            id=strawberry.ID(info.key),
            key=info.key,
            kind=info.kind,
            name=info.name,
            description=info.description,
            group=info.group,
            enabled=info.enabled,
            requires_approval=info.requires_approval,
            in_prompt=info.in_prompt,
            available=info.available,
            detail=info.detail,
        )


@strawberry.type
class ToolApprovalRequest:
    """The durable request a gated SDK call blocks on."""

    id: str
    status: str
