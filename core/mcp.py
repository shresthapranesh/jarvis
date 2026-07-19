"""MCP tool loader."""

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
    """Return the FIRST candidate file that yields a non-empty config —
    candidate files are alternatives, not layers; they are never merged."""
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


MCP_DB_KEY = "mcp.servers"


def _invalidate_agent_graphs() -> None:
    """Drop compiled agent graphs after an MCP toolset change.

    Lazy import — core.agents imports this module at load time.
    """
    try:
        from core.agents import invalidate_agent_cache

        invalidate_agent_cache()
    except Exception as exc:
        logger.warning("could not invalidate agent cache after MCP reload: %s", exc)


def _parse_db_raw(raw: str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return _normalize_servers(data)
    except Exception as exc:
        logger.warning("Failed to parse MCP DB config: %s", exc)
        return {}


def load_mcp_server_configs(work_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load and merge MCP server configs from env + the first config file found.
    Per-server, file wins over env; only one file is read (no cross-file merge)."""
    env_cfg = _load_from_env()
    file_cfg = _load_from_files(work_dir)
    # Merge: file overrides env (same name -> file wins)
    merged = {**env_cfg, **file_cfg}
    if merged:
        logger.info("MCP servers configured: %s", list(merged.keys()))
    else:
        logger.debug("No MCP servers configured")
    return merged


def load_mcp_server_configs_with_db(
    db_cfg: dict[str, dict[str, Any]] | None = None,
    work_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge env + file + DB. DB wins over file wins over env."""
    env_cfg = _load_from_env()
    file_cfg = _load_from_files(work_dir)
    db_normalized = _normalize_servers(db_cfg) if db_cfg else {}
    merged = {**env_cfg, **file_cfg, **db_normalized}
    if merged:
        logger.info("MCP servers configured (env+file+db): %s", list(merged.keys()))
    return merged


# ── Async DB helpers ──────────────────────────────────────────────────────

async def get_mcp_servers_from_db(session) -> dict[str, dict[str, Any]]:
    """Read DB persistence layer (config_settings key MCP_DB_KEY)."""
    try:
        from db.ops import get_setting

        raw = await get_setting(session, MCP_DB_KEY)
        return _parse_db_raw(raw)
    except Exception as exc:
        logger.warning("get_mcp_servers_from_db failed: %s", exc)
        return {}


async def set_mcp_servers_in_db(session, servers: dict[str, dict[str, Any]]) -> None:
    """Persist full dict to DB."""
    from db.ops import set_setting

    await set_setting(session, MCP_DB_KEY, json.dumps(servers))


async def add_mcp_server_to_db(session, name: str, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    servers = await get_mcp_servers_from_db(session)
    # Normalize single entry
    normalized = _normalize_servers({name: cfg})
    if not normalized:
        raise ValueError(f"invalid MCP server config for {name}")
    servers.update(normalized)
    await set_mcp_servers_in_db(session, servers)
    return servers


async def update_mcp_server_in_db(session, name: str, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return await add_mcp_server_to_db(session, name, cfg)


async def remove_mcp_server_from_db(session, name: str) -> dict[str, dict[str, Any]]:
    servers = await get_mcp_servers_from_db(session)
    if name not in servers:
        raise ValueError(f"MCP server {name!r} not found in DB (found: {list(servers.keys())})")
    del servers[name]
    await set_mcp_servers_in_db(session, servers)
    return servers


async def load_mcp_server_configs_async(work_dir: Path | None = None, session=None) -> dict[str, dict[str, Any]]:
    """Async version that includes DB if session provided."""
    env_cfg = _load_from_env()
    file_cfg = _load_from_files(work_dir)
    db_cfg: dict[str, dict[str, Any]] = {}
    if session is not None:
        db_cfg = await get_mcp_servers_from_db(session)
    merged = {**env_cfg, **file_cfg, **db_cfg}
    if merged:
        logger.info("MCP servers configured (async env+file+db): %s", list(merged.keys()))
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
            return await self._initialize_locked(connections)

    async def _initialize_locked(self, connections: dict[str, dict[str, Any]] | None) -> list[Any]:
        if connections is not None:
            if connections != self.connections:
                # New connection set — drop the stale client/tools and reconnect.
                self._client = None
                self._tools = []
                self._initialized = False
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

    async def reload(self, connections: dict[str, dict[str, Any]] | None = None) -> list[Any]:
        """Force reload — clears cache and re-initializes with new connections (or existing)."""
        async with self._lock:
            self._client = None
            self._tools = []
            self._initialized = False
            tools = await self._initialize_locked(connections)
        # Compiled agents bake the MCP toolset in via bind_tools at build time,
        # so a reload must also drop them or it never takes effect.
        _invalidate_agent_graphs()
        return tools

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
