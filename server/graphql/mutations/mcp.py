"""MCP mutations — server CRUD, load modes, and on-demand tool calls."""

from __future__ import annotations

import json
from typing import Any

import strawberry

from core.approvals import gate_action
from core.tool_gate import await_tool_approval, denial_message
from core.tool_policy import is_enabled, mcp_key, needs_approval
from core.mcp import (
    LOAD_MODES,
    _normalize_servers,
    add_mcp_server_to_db,
    call_mcp_tool,
    get_mcp_load_modes_from_db,
    get_mcp_manager,
    get_mcp_servers_from_db,
    load_mcp_server_configs_with_db,
    normalize_load_mode,
    remove_mcp_server_from_db,
    set_default_load_mode,
    set_mcp_load_mode_in_db,
    set_mcp_servers_in_db,
    sync_default_load_mode_from_db,
)
from server.graphql.types.mcp import McpServer, McpToolResult


async def _reload_merged(session) -> dict:
    """Re-merge env+file+DB (including mode overrides) and reconnect."""
    await sync_default_load_mode_from_db(session)
    db_cfg = await get_mcp_servers_from_db(session)
    load_modes = await get_mcp_load_modes_from_db(session)
    merged = load_mcp_server_configs_with_db(db_cfg=db_cfg, load_modes=load_modes)
    await get_mcp_manager().reload(merged)
    return merged


def _server_type(name: str, cfg: dict) -> McpServer:
    """Build the GraphQL type from the manager's attributed view."""
    mgr = get_mcp_manager()
    tools = mgr.tools_for_server(name)
    return McpServer.from_entry(
        name, cfg, tool_count=len(tools), tools=[getattr(t, "name", "?") for t in tools]
    )


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
        merged = await _reload_merged(session)
        return _server_type(name, merged.get(name, normalized[name]))

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

        merged = await _reload_merged(session)
        return _server_type(name, merged.get(name, normalized[name]))

    @strawberry.mutation
    async def removeMcpServer(self, info: strawberry.Info, name: str) -> bool:
        session = info.context["session"]
        await remove_mcp_server_from_db(session, name)
        await _reload_merged(session)
        return True

    @strawberry.mutation
    async def reloadMcpServers(self, info: strawberry.Info) -> list[McpServer]:
        session = info.context["session"]
        merged = await _reload_merged(session)
        return [_server_type(n, cfg) for n, cfg in merged.items()]

    @strawberry.mutation
    async def setMcpServerLoadMode(
        self, info: strawberry.Info, name: str, mode: str
    ) -> McpServer:
        """Bind this server's tools to the agent (`always`) or not (`lazy`).

        Applied in place — the tools are already loaded, only the binding
        decision changes — but the compiled agent graphs bake the bound set in,
        so they are dropped (McpManager.set_load_mode) or the flip would not
        take effect until a restart.
        """
        session = info.context["session"]
        normalized = normalize_load_mode(mode, server=name)
        if normalized is None:
            raise ValueError(f"mode must be one of {', '.join(LOAD_MODES)}")
        mgr = get_mcp_manager()
        if name not in mgr.connections:
            raise ValueError(f"MCP server {name!r} is not configured")
        await set_mcp_load_mode_in_db(session, name, normalized)
        mgr.set_load_mode(name, normalized)
        return _server_type(name, mgr.connections[name])

    @strawberry.mutation
    async def setMcpDefaultLoadMode(self, info: strawberry.Info, mode: str) -> str:
        """Fallback mode for servers that don't declare one."""
        session = info.context["session"]
        normalized = normalize_load_mode(mode)
        if normalized is None:
            raise ValueError(f"mode must be one of {', '.join(LOAD_MODES)}")
        from db.ops import set_setting
        from core.mcp import MCP_LOAD_MODE_DB_KEY

        await set_setting(session, MCP_LOAD_MODE_DB_KEY, normalized)
        set_default_load_mode(normalized)
        # Servers resolve their mode through this default, so the bound set may
        # have changed for every one of them.
        await _reload_merged(session)
        return normalized

    @strawberry.mutation
    async def callMcpTool(
        self,
        info: strawberry.Info,
        server: str,
        tool: str,
        args_json: str = "{}",
        timeout_seconds: float = 120.0,
    ) -> McpToolResult:
        """Invoke one MCP tool by (server, tool) — the lazy path's execution end.

        This exists because the `jarvis` SDK runs in a separate kernel process:
        the MCP client (and, for stdio servers, the subprocess handles it owns)
        lives here, so the kernel routes the call through the API rather than
        starting a second client and double-launching every server.
        """
        session = info.context["session"]
        try:
            args = json.loads(args_json) if args_json else {}
        except Exception as exc:
            raise ValueError(f"args_json must be valid JSON: {exc}")
        if not isinstance(args, dict):
            raise ValueError("args_json must be a JSON object")

        if info.context.get("caller") == "agent":
            conversation_id = info.context.get("caller_conversation_id")
            key = mcp_key(server, tool)
            if not is_enabled(key):
                raise ValueError(
                    f"MCP tool {server}.{tool} is switched off in Settings → Tools."
                )
            if needs_approval(key):
                # Blocks this request until a human answers. The kernel-side
                # caller (`jarvis.mcp_call`) waits with a matching client
                # timeout, and its `run_cell` cell is held open meanwhile.
                approved, answer = await await_tool_approval(
                    tool_key=key,
                    tool_name=f"{server}.{tool}",
                    args=args,
                    conversation_id=conversation_id,
                )
                if not approved:
                    return McpToolResult(
                        content=denial_message(f"{server}.{tool}", answer), is_error=True
                    )
            # The blanket `call_mcp_tool` action stays for installs that gate
            # every MCP call through `approval.required_actions`; the per-tool
            # switch above is the finer-grained one and runs first.
            await gate_action(
                session,
                "call_mcp_tool",
                {"server": server, "tool": tool, "args": args},
                source="chat",
                parent_id=conversation_id,
            )

        content, is_error = await call_mcp_tool(server, tool, args, timeout=timeout_seconds)
        return McpToolResult(content=content, is_error=is_error)
