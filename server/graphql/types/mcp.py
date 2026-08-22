"""MCP tool loader."""

from __future__ import annotations

import json
from typing import Any

import strawberry

from core.mcp import _normalize_servers, load_mode_for


@strawberry.type
class McpTool:
    """One tool exposed by a connected MCP server."""

    name: str
    server: str
    description: str
    # JSON string — only worth selecting when you are about to call the tool.
    input_schema: str = "{}"


@strawberry.type
class McpToolResult:
    """Outcome of one `callMcpTool` invocation."""

    content: str
    is_error: bool = False


@strawberry.type
class McpServer:
    name: str
    config: str  # JSON string of raw connection dict
    transport: str
    command: str | None = None
    url: str | None = None
    tool_count: int = 0
    enabled: bool = True
    # "always" = tools bound to the agent (schemas in every LLM call);
    # "lazy" = reachable only via jarvis.mcp_call.
    load_mode: str = "always"
    tools: list[str] = strawberry.field(default_factory=list)

    @classmethod
    def from_entry(
        cls, name: str, cfg: dict[str, Any], tool_count: int = 0, enabled: bool = True,
        tools: list[str] | None = None,
    ) -> McpServer:
        transport = str(cfg.get("transport", "stdio" if "command" in cfg else "http"))
        cmd = cfg.get("command")
        if isinstance(cmd, list):
            cmd_str = " ".join(str(x) for x in cmd)
        else:
            cmd_str = str(cmd) if cmd else None
        url = cfg.get("url")
        try:
            config_json = json.dumps(cfg)
        except Exception:
            config_json = "{}"
        return cls(
            name=name,
            config=config_json,
            transport=transport,
            command=cmd_str,
            url=str(url) if url else None,
            tool_count=tool_count,
            enabled=enabled,
            load_mode=load_mode_for(cfg),
            tools=list(tools or []),
        )

    @classmethod
    def from_summary(cls, summary: dict[str, Any]) -> McpServer:
        """Build from `McpManager.server_summaries()` — the attributed view."""
        return cls.from_entry(
            summary["name"],
            summary["config"],
            tool_count=summary["tool_count"],
            tools=summary["tools"],
        )
