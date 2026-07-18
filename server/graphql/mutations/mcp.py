"""MCP tool loader."""

from __future__ import annotations

import json
from typing import Any

import strawberry

from core.mcp import (
    _normalize_servers,
    add_mcp_server_to_db,
    get_mcp_manager,
    get_mcp_servers_from_db,
    load_mcp_server_configs_with_db,
    remove_mcp_server_from_db,
    set_mcp_servers_in_db,
)
from server.graphql.types.mcp import McpServer


@strawberry.type
class McpMutation:
    @strawberry.mutation
    async def addMcpServer(self, info: strawberry.Info, name: str, config_json: str) -> McpServer:
        session = info.context["session"]
        try:
            cfg_raw = json.loads(config_json)
        except Exception as exc:
            raise ValueError(f"config_json must be valid JSON: {exc}")
        if not isinstance(cfg_raw, dict):
            raise ValueError("config_json must be a JSON object (connection dict)")
        normalized = _normalize_servers({name: cfg_raw})
        if name not in normalized:
            raise ValueError(f"invalid config for server {name!r}")

        # Persist to DB
        await add_mcp_server_to_db(session, name, cfg_raw)

        # Reload manager with merged config (env+file+db)
        db_cfg = await get_mcp_servers_from_db(session)
        merged = load_mcp_server_configs_with_db(db_cfg=db_cfg)
        mgr = get_mcp_manager()
        await mgr.reload(merged)

        tools = mgr.get_tools_sync()
        tc = len(tools) if len(merged) == 1 else sum(1 for t in tools if name.lower() in getattr(t, "name", "").lower())
        return McpServer.from_entry(name, normalized[name], tool_count=tc, enabled=True)

    @strawberry.mutation
    async def updateMcpServer(self, info: strawberry.Info, name: str, config_json: str) -> McpServer:
        # Same as add (upsert)
        session = info.context["session"]
        try:
            cfg_raw = json.loads(config_json)
        except Exception as exc:
            raise ValueError(f"config_json must be valid JSON: {exc}")
        if not isinstance(cfg_raw, dict):
            raise ValueError("config_json must be a JSON object")
        normalized = _normalize_servers({name: cfg_raw})
        if name not in normalized:
            raise ValueError(f"invalid config for server {name!r}")

        await add_mcp_server_to_db(session, name, cfg_raw)

        db_cfg = await get_mcp_servers_from_db(session)
        merged = load_mcp_server_configs_with_db(db_cfg=db_cfg)
        mgr = get_mcp_manager()
        await mgr.reload(merged)

        tools = mgr.get_tools_sync()
        tc = len(tools) if len(merged) == 1 else sum(1 for t in tools if name.lower() in getattr(t, "name", "").lower())
        return McpServer.from_entry(name, normalized[name], tool_count=tc, enabled=True)

    @strawberry.mutation
    async def removeMcpServer(self, info: strawberry.Info, name: str) -> bool:
        session = info.context["session"]
        await remove_mcp_server_from_db(session, name)
        db_cfg = await get_mcp_servers_from_db(session)
        merged = load_mcp_server_configs_with_db(db_cfg=db_cfg)
        mgr = get_mcp_manager()
        await mgr.reload(merged)
        return True

    @strawberry.mutation
    async def reloadMcpServers(self, info: strawberry.Info) -> list[McpServer]:
        session = info.context["session"]
        db_cfg = await get_mcp_servers_from_db(session)
        merged = load_mcp_server_configs_with_db(db_cfg=db_cfg)
        mgr = get_mcp_manager()
        await mgr.reload(merged)
        tools = mgr.get_tools_sync()
        out: list[McpServer] = []
        for n, cfg in merged.items():
            tc = len(tools) if len(merged) == 1 else sum(1 for t in tools if n.lower() in getattr(t, "name", "").lower())
            out.append(McpServer.from_entry(n, cfg, tool_count=tc, enabled=True))
        return out
