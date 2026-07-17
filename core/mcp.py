"""MCP tool loader — ADK McpToolset analog.

ADK supports Model Context Protocol via McpToolset that loads tools from MCP
servers (stdio, SSE, streamable-http). This module brings the same to Jarvis:

- Reads server configs from multiple sources (env, file, work_dir)
- Manages a MultiServerMCPClient (langchain-mcp-adapters)
- Exposes cached LangChain tools for the main agent (and potentially board/automation)

Config sources (merged, file wins over env, env JSON wins over nothing):
1. Env var JARVIS_MCP_SERVERS: JSON object mapping name -> connection dict, or JSON list
   Example (object):
     {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "transport": "stdio", "env": {"GITHUB_TOKEN": "..."} },
      "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], "transport": "stdio"}}
   Example (list):
     [{"name": "github", "command": "npx", ...}]

2. File ~/.jarvis/mcp.json or $WORK_DIR/mcp.json:
   {
     "mcpServers": {
       "github": {"command": "npx", "args": [...], "transport": "stdio"},
       ...
     }
   }
   or {"servers": {...}} or top-level dict of servers.

3. DB config_settings key "mcp.servers" — read lazily via file? Skipped for now; can be added in runner.

If no config, returns empty — agent works without MCP.

Lifespan integration:
    from core.mcp import get_mcp_manager
    await get_mcp_manager().initialize()

Agent integration:
    from core.mcp import get_mcp_tools_sync
    main_tools += get_mcp_tools_sync()

Design: mirrors ADK Runner's toolset loading but keeps it optional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _expand_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MCP config %s: %s", path, exc)
        return None


def _normalize_servers(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize various config shapes to dict[name -> connection dict]."""
    if not raw:
        return {}
    # Top-level may have mcpServers key (Claude Desktop style)
    if isinstance(raw, dict):
        if "mcpServers" in raw and isinstance(raw["mcpServers"], dict):
            raw = raw["mcpServers"]
        elif "servers" in raw and isinstance(raw["servers"], dict):
            raw = raw["servers"]
        elif "mcp_servers" in raw and isinstance(raw["mcp_servers"], dict):
            raw = raw["mcp_servers"]

    if isinstance(raw, dict):
        # {name: config} shape
        out: dict[str, dict[str, Any]] = {}
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            # Ensure transport defaults to stdio if command present, or http if url present
            if "transport" not in cfg:
                if "command" in cfg:
                    cfg = {**cfg, "transport": "stdio"}
                elif "url" in cfg:
                    cfg = {**cfg, "transport": "http"}
            out[name] = cfg
        return out

    if isinstance(raw, list):
        # [{name, ...}, ...] shape
        out: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("id")
            if not name:
                continue
            cfg = {k: v for k, v in item.items() if k != "name"}
            if "transport" not in cfg:
                if "command" in cfg:
                    cfg["transport"] = "stdio"
                elif "url" in cfg:
                    cfg["transport"] = "http"
            out[str(name)] = cfg
        return out

    return {}


def _load_from_env() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("JARVIS_MCP_SERVERS") or os.environ.get("MCP_SERVERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return _normalize_servers(data)
    except Exception as exc:
        logger.warning("Failed to parse JARVIS_MCP_SERVERS env JSON: %s", exc)
        return {}


def _load_from_files(work_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    candidates: list[Path] = []
    # Work dir candidate (explicit)
    if work_dir:
        candidates.append(work_dir / "mcp.json")
    # Default work dir ~/.jarvis/mcp.json
    default_work = Path.home() / ".jarvis" / "mcp.json"
    candidates.append(default_work)
    # Also check ./mcp.json in current cwd (for dev)
    candidates.append(Path.cwd() / "mcp.json")
    # Also check frontend? No.

    for path in candidates:
        data = _read_json_file(path)
        if data is None:
            continue
        normalized = _normalize_servers(data)
        if normalized:
            logger.info("Loaded MCP config from %s: %s", path, list(normalized.keys()))
            return normalized
    return {}


def load_mcp_server_configs(work_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load and merge MCP server configs from env + files. File wins over env."""
    env_cfg = _load_from_env()
    file_cfg = _load_from_files(work_dir)
    # Merge: file overrides env (same name -> file wins)
    merged = {**env_cfg, **file_cfg}
    if merged:
        logger.info("MCP servers configured: %s", list(merged.keys()))
    else:
        logger.debug("No MCP servers configured")
    return merged


@dataclass
class McpManager:
    """Manages MultiServerMCPClient lifecycle and cached tools."""

    connections: dict[str, dict[str, Any]] = field(default_factory=dict)
    _client: Any = field(default=None, repr=False)
    _tools: list[Any] = field(default_factory=list, repr=False)
    _initialized: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def initialize(self, connections: dict[str, dict[str, Any]] | None = None) -> list[Any]:
        """Initialize client and load tools. Returns tools list (may be empty on failure)."""
        async with self._lock:
            if connections is not None:
                self.connections = connections
            if not self.connections:
                # Try lazy load if empty
                self.connections = load_mcp_server_configs()
            if not self.connections:
                self._initialized = True
                self._tools = []
                return []

            # Already initialized with same connections? Return cached
            if self._initialized and self._tools:
                return self._tools

            try:
                from langchain_mcp_adapters.client import MultiServerMCPClient

                # MultiServerMCPClient accepts dict[name -> connection dict]
                # Example: {"math": {"command": "python", "args": ["/path/to/math_server.py"], "transport": "stdio"}}
                self._client = MultiServerMCPClient(self.connections)  # type: ignore[arg-type]
                tools = await self._client.get_tools()
                self._tools = tools
                self._initialized = True
                logger.info("MCP tools loaded: %d tools from %d servers", len(tools), len(self.connections))
                for t in tools:
                    try:
                        logger.debug("MCP tool: %s - %s", getattr(t, "name", "?"), getattr(t, "description", "")[:120])
                    except Exception:
                        pass
                return tools
            except Exception as exc:
                logger.warning("Failed to initialize MCP clients %s: %s", list(self.connections.keys()), exc, exc_info=True)
                self._tools = []
                self._initialized = True
                return []

    def get_tools_sync(self) -> list[Any]:
        """Sync accessor for agent builder — returns cached tools or empty."""
        return list(self._tools) if self._initialized else []

    async def get_tools(self) -> list[Any]:
        """Async accessor — ensures initialized."""
        if not self._initialized:
            await self.initialize()
        return list(self._tools)

    async def close(self) -> None:
        async with self._lock:
            # MultiServerMCPClient doesn't have explicit close, but we can drop ref
            self._client = None
            self._tools = []
            self._initialized = False


# ── Global singleton ──────────────────────────────────────────────────────────

_mcp_manager: McpManager | None = None


def get_mcp_manager() -> McpManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = McpManager(connections=load_mcp_server_configs())
    return _mcp_manager


def get_mcp_tools_sync() -> list[Any]:
    """Convenience for agents.py — returns cached MCP tools if any."""
    try:
        mgr = get_mcp_manager()
        return mgr.get_tools_sync()
    except Exception as exc:
        logger.warning("get_mcp_tools_sync failed: %s", exc)
        return []


async def initialize_mcp() -> list[Any]:
    """Call from lifespan to warm up MCP."""
    mgr = get_mcp_manager()
    return await mgr.initialize()
