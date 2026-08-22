"""MCP queries — configured servers and the tools they expose."""

from __future__ import annotations

import json

import strawberry

from core.mcp import (
    get_mcp_load_modes_from_db,
    get_mcp_manager,
    get_mcp_servers_from_db,
    load_mcp_server_configs_with_db,
    sync_default_load_mode_from_db,
)
from server.graphql.types.mcp import McpServer, McpTool


def _tool_type(tool, server: str) -> McpTool:
    # The adapter passes the server's raw JSON Schema straight through as
    # args_schema; older//converted tools carry a pydantic model instead, whose
    # `.args` is the equivalent properties dict.
    schema = getattr(tool, "args_schema", None)
    if not isinstance(schema, dict):
        try:
            schema = {"properties": tool.args}
        except Exception:
            schema = {}
    try:
        schema_json = json.dumps(schema, default=str)
    except Exception:
        schema_json = "{}"
    return McpTool(
        name=getattr(tool, "name", "?"),
        server=server,
        description=(getattr(tool, "description", "") or ""),
        input_schema=schema_json,
    )


@strawberry.type
class McpQuery:
    @strawberry.field
    async def mcpServers(self, info: strawberry.Info) -> list[McpServer]:
        session = info.context["session"]
        # Applied before load_mode_for() resolves any server that doesn't
        # declare a mode of its own.
        await sync_default_load_mode_from_db(session)
        db_cfg = await get_mcp_servers_from_db(session)
        load_modes = await get_mcp_load_modes_from_db(session)
        merged = load_mcp_server_configs_with_db(db_cfg=db_cfg, load_modes=load_modes)

        # Tool counts come from the manager's per-server index — real
        # attribution, not the substring guess this used to do. A server present
        # in config but absent from the index simply hasn't loaded (never
        # connected, or added since the last reload) and reports 0.
        mgr = get_mcp_manager()
        by_server = {s["name"]: s for s in mgr.server_summaries()}

        result: list[McpServer] = []
        for name, cfg in merged.items():
            summary = by_server.get(name)
            result.append(
                McpServer.from_entry(
                    name,
                    cfg,
                    tool_count=summary["tool_count"] if summary else 0,
                    tools=summary["tools"] if summary else [],
                )
            )
        # Union, not just the config: a server the manager is connected to but
        # that config no longer names is still live and still billing tools —
        # listing only the config would hide it until the next restart.
        for name, summary in by_server.items():
            if name in merged:
                continue
            result.append(
                McpServer.from_entry(
                    name,
                    summary["config"],
                    tool_count=summary["tool_count"],
                    tools=summary["tools"],
                )
            )
        return result

    @strawberry.field
    async def mcpTools(self, info: strawberry.Info, server: str | None = None) -> list[McpTool]:
        """Loaded MCP tools, optionally narrowed to one server.

        Select `inputSchema` only when you intend to call the tool — it is the
        expensive half, and keeping it out of the listing is the whole point of
        the lazy path.
        """
        mgr = get_mcp_manager()
        if server is not None:
            return [_tool_type(t, server) for t in mgr.tools_for_server(server)]
        out: list[McpTool] = []
        for summary in mgr.server_summaries():
            out.extend(_tool_type(t, summary["name"]) for t in mgr.tools_for_server(summary["name"]))
        return out
