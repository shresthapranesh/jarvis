"""MCP server GraphQL types — ADK McpToolset dynamic management."""

from __future__ import annotations

import json
from typing import Any

import strawberry

from core.mcp import _normalize_servers


@strawberry.type
class McpServer:
    name: str
    config: str  # JSON string of raw connection dict
    transport: str
    command: str | None = None
    url: str | None = None
    tool_count: int = 0
    enabled: bool = True

    @classmethod
    def from_entry(
        cls, name: str, cfg: dict[str, Any], tool_count: int = 0, enabled: bool = True
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
        )
