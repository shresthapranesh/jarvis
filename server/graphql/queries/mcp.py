"""MCP tool loader."""

from __future__ import annotations

import strawberry

from core.mcp import (
    get_mcp_manager,
    get_mcp_servers_from_db,
    load_mcp_server_configs,
    load_mcp_server_configs_with_db,
)
from server.graphql.types.mcp import McpServer


@strawberry.type
class McpQuery:
    @strawberry.field
    async def mcpServers(self, info: strawberry.Info) -> list[McpServer]:
        session = info.context["session"]
        # Merge env + file + DB (DB wins)
        db_cfg = await get_mcp_servers_from_db(session)
        file_env_cfg = load_mcp_server_configs()
        merged = {**file_env_cfg, **db_cfg}

        # Tool counts from manager (cached)
        mgr = get_mcp_manager()
        # Map tool name -> server? We don't have per-server breakdown from MCP client,
        # so distribute total count or keep 0. For now approximate: total tools if server present.
        tools = mgr.get_tools_sync()
        # If manager not yet initialized with DB config, tool counts may be stale for DB servers.
        # We'll attempt to count per-server via tool's origin if available? Fallback to total per present.

        result: list[McpServer] = []
        for name, cfg in merged.items():
            # tool_count: count tools whose server prefix matches? MCP tools usually have namespaced?
            # For simplicity, if single server, all tools belong to it; else distribute evenly or 0.
            # We'll report len(tools) if name matches any tool's metadata? Best-effort: len(tools) if merged==1 else 0 unless we have more info.
            # To be useful, we show total tool count for each server when manager has tools, else 0.
            # Better: if mgr has connections matching name, assume tools count is total if only one server.
            tc = 0
            if mgr.connections and name in mgr.connections:
                if len(mgr.connections) == 1:
                    tc = len(tools)
                else:
                    # Try to guess by filtering tool names containing server name? Heuristic.
                    tc = sum(1 for t in tools if name.lower() in getattr(t, "name", "").lower()) or 0
            result.append(McpServer.from_entry(name, cfg, tool_count=tc, enabled=True))
        return result

    @strawberry.field
    async def mcpTools(self, info: strawberry.Info) -> list[str]:
        """List loaded MCP tool names."""
        mgr = get_mcp_manager()
        return [getattr(t, "name", str(t)) for t in mgr.get_tools_sync()]
