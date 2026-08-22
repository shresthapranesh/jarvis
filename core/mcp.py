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

# ── Load modes ────────────────────────────────────────────────────────────────
# Every MCP tool schema is re-sent on every LLM call, so a chatty server is
# billed on every iteration of every run (and `should_use_cache()` is only true
# for anthropic/bedrock — elsewhere there is no cache to soften it). `always`
# keeps the historical behaviour: bind the server's tools to the agent. `lazy`
# leaves them unbound and reachable only through `jarvis.mcp_call(...)`, which
# costs a discovery round-trip instead of permanent context.
#
# The mode is a jarvis-only key inside the connection dict, so it round-trips
# through all three config sources (env / file / DB) with no extra plumbing.
# It is stripped before the dict reaches MultiServerMCPClient: create_session
# splats every non-`transport` key into the transport constructor, so an
# unknown key is a TypeError at connect time, not an ignored field.

LOAD_MODE_KEY = "x-jarvis-load"
LOAD_ALWAYS = "always"
LOAD_LAZY = "lazy"
LOAD_MODES = (LOAD_ALWAYS, LOAD_LAZY)

MCP_LOAD_MODE_DB_KEY = "mcp.default_load_mode"

# Jarvis-only keys, stripped before the config is handed to the MCP client.
_JARVIS_KEYS = (LOAD_MODE_KEY, "x_jarvis_load")


def normalize_load_mode(value: Any, *, server: str | None = None) -> str | None:
    """Return a valid mode, or None when unset/unrecognized (caller defaults)."""
    if value is None:
        return None
    mode = str(value).strip().lower()
    if mode in LOAD_MODES:
        return mode
    logger.warning(
        "MCP server %s: unknown %s=%r (expected one of %s) — using the default",
        server or "?", LOAD_MODE_KEY, value, ", ".join(LOAD_MODES),
    )
    return None


_default_load_mode: str = normalize_load_mode(os.environ.get("JARVIS_MCP_DEFAULT_LOAD")) or LOAD_ALWAYS


def get_default_load_mode() -> str:
    """Mode for servers that don't declare one. Defaults to `always`."""
    return _default_load_mode


def set_default_load_mode(mode: Any) -> str:
    """Set the fallback mode (config setting / env). Returns the effective value."""
    global _default_load_mode
    normalized = normalize_load_mode(mode)
    if normalized:
        _default_load_mode = normalized
    return _default_load_mode


def load_mode_for(cfg: dict[str, Any] | None) -> str:
    """Resolve one server's mode from its connection dict."""
    if not cfg:
        return get_default_load_mode()
    for key in _JARVIS_KEYS:
        if key in cfg:
            return normalize_load_mode(cfg[key]) or get_default_load_mode()
    return get_default_load_mode()


def strip_jarvis_keys(cfg: dict[str, Any]) -> dict[str, Any]:
    """Drop jarvis-only keys so the dict is a valid MCP connection config."""
    return {k: v for k, v in cfg.items() if k not in _JARVIS_KEYS}


def with_load_mode(cfg: dict[str, Any], mode: str | None) -> dict[str, Any]:
    """Return a copy of `cfg` carrying `mode` (or with the key removed)."""
    out = strip_jarvis_keys(cfg)
    normalized = normalize_load_mode(mode)
    if normalized:
        out[LOAD_MODE_KEY] = normalized
    return out


def _content_to_text(content: Any) -> str:
    """Flatten MCP tool output (str or content blocks) to text for the kernel."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                try:
                    parts.append(json.dumps(block, default=str))
                except Exception:
                    parts.append(str(block))
        return "\n".join(parts)
    try:
        return json.dumps(content, default=str)
    except Exception:
        return str(content)



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


def apply_load_mode_overrides(
    servers: dict[str, dict[str, Any]],
    overrides: dict[str, str] | None,
) -> dict[str, dict[str, Any]]:
    """Overlay per-server load modes onto connection dicts.

    Kept as a separate map (DB key `mcp.load_modes`) rather than written back
    into the connection dict, so flipping the mode of a server defined in env or
    `mcp.json` doesn't snapshot the rest of that config into the DB — where it
    would win forever and silently ignore later edits to the source file.
    """
    if not overrides:
        return servers
    out = dict(servers)
    for name, mode in overrides.items():
        if name not in out:
            continue
        normalized = normalize_load_mode(mode, server=name)
        if normalized:
            out[name] = with_load_mode(out[name], normalized)
    return out


def load_mcp_server_configs_with_db(
    db_cfg: dict[str, dict[str, Any]] | None = None,
    work_dir: Path | None = None,
    load_modes: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge env + file + DB. DB wins over file wins over env."""
    env_cfg = _load_from_env()
    file_cfg = _load_from_files(work_dir)
    db_normalized = _normalize_servers(db_cfg) if db_cfg else {}
    merged = {**env_cfg, **file_cfg, **db_normalized}
    merged = apply_load_mode_overrides(merged, load_modes)
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


MCP_LOAD_MODES_DB_KEY = "mcp.load_modes"


async def get_mcp_load_modes_from_db(session) -> dict[str, str]:
    """Per-server load-mode overrides (config_settings key `mcp.load_modes`)."""
    try:
        from db.ops import get_setting

        raw = await get_setting(session, MCP_LOAD_MODES_DB_KEY)
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("get_mcp_load_modes_from_db failed: %s", exc)
        return {}


async def set_mcp_load_mode_in_db(session, name: str, mode: str) -> dict[str, str]:
    """Persist one server's load mode. Returns the full override map."""
    from db.ops import set_setting

    normalized = normalize_load_mode(mode, server=name)
    if normalized is None:
        raise ValueError(f"mode must be one of {', '.join(LOAD_MODES)}")
    modes = await get_mcp_load_modes_from_db(session)
    modes[name] = normalized
    await set_setting(session, MCP_LOAD_MODES_DB_KEY, json.dumps(modes))
    return modes


async def sync_default_load_mode_from_db(session) -> str:
    """Apply the `mcp.default_load_mode` config setting to this process."""
    try:
        from db.ops import get_setting

        raw = await get_setting(session, MCP_LOAD_MODE_DB_KEY)
    except Exception as exc:
        logger.warning("sync_default_load_mode_from_db failed: %s", exc)
        return get_default_load_mode()
    if raw:
        set_default_load_mode(raw)
    return get_default_load_mode()


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
    # Per-server attribution. MultiServerMCPClient.get_tools() returns one flat
    # list with no server on the tool (the adapter puts annotations in
    # `metadata`, never the server name), so the only way to know which server
    # owns a tool is to ask per server. Everything downstream — load-mode
    # filtering, honest tool counts, jarvis.mcp_call routing — needs that map.
    _tools_by_server: dict[str, list[Any]] = field(default_factory=dict, repr=False)
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
                self._tools_by_server = {}
                self._initialized = False
            self.connections = connections
        if not self.connections:
            # Try lazy load if empty
            self.connections = load_mcp_server_configs()
        if not self.connections:
            self._initialized = True
            self._tools = []
            self._tools_by_server = {}
            return []

        # Already initialized with same connections? Return cached
        if self._initialized and self._tools:
            return self._tools

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            # MultiServerMCPClient accepts dict[name -> connection dict]
            # Example: {"math": {"command": "python", "args": ["/path/to/math_server.py"], "transport": "stdio"}}
            clean = {n: strip_jarvis_keys(cfg) for n, cfg in self.connections.items()}
            self._client = MultiServerMCPClient(clean)  # type: ignore[arg-type]

            # Per server rather than one flat get_tools(): it is the only source
            # of attribution, and it downgrades one unreachable server from
            # "no MCP tools at all" to "that server has none".
            names = list(clean)
            results = await asyncio.gather(
                *(self._client.get_tools(server_name=n) for n in names),
                return_exceptions=True,
            )
            by_server: dict[str, list[Any]] = {}
            for name, result in zip(names, results):
                if isinstance(result, BaseException):
                    logger.warning("MCP server %r failed to load tools: %s", name, result)
                    by_server[name] = []
                    continue
                by_server[name] = list(result)

            self._tools_by_server = by_server
            self._tools = [t for name in names for t in by_server[name]]
            self._initialized = True
            logger.info(
                "MCP tools loaded: %d tools from %d servers (%s)",
                len(self._tools),
                len(names),
                ", ".join(
                    f"{n}:{len(by_server[n])}/{self.load_mode(n)}" for n in names
                ),
            )
            for t in self._tools:
                try:
                    logger.debug("MCP tool: %s - %s", getattr(t, "name", "?"), getattr(t, "description", "")[:120])
                except Exception:
                    pass
            return self._tools
        except Exception as exc:
            logger.warning("Failed to initialize MCP clients %s: %s", list(self.connections.keys()), exc, exc_info=True)
            self._tools = []
            self._tools_by_server = {}
            self._initialized = True
            return []

    def get_tools_sync(self) -> list[Any]:
        """Every cached tool, regardless of load mode."""
        return list(self._tools) if self._initialized else []

    def get_bound_tools_sync(self) -> list[Any]:
        """Only the tools that should be bound to the agent (`always` servers)."""
        if not self._initialized:
            return []
        out: list[Any] = []
        for name, tools in self._tools_by_server.items():
            if self.load_mode(name) == LOAD_ALWAYS:
                out.extend(tools)
        return out

    async def get_tools(self) -> list[Any]:
        """Async accessor — ensures initialized."""
        if not self._initialized:
            await self.initialize()
        return list(self._tools)

    # ── Per-server view ──────────────────────────────────────────────────────

    def load_mode(self, name: str) -> str:
        return load_mode_for(self.connections.get(name))

    def tools_for_server(self, name: str) -> list[Any]:
        return list(self._tools_by_server.get(name, []))

    def set_load_mode(self, name: str, mode: str) -> str:
        """Flip one server's mode in place.

        No reconnect: the tools are already loaded, only the binding decision
        changes. The compiled agent graphs bake the bound set in, so they must
        still be dropped or the flip takes effect on the next restart.
        """
        if name not in self.connections:
            raise ValueError(f"MCP server {name!r} is not configured")
        normalized = normalize_load_mode(mode, server=name)
        if normalized is None:
            raise ValueError(f"mode must be one of {', '.join(LOAD_MODES)}")
        self.connections[name] = with_load_mode(self.connections[name], normalized)
        _invalidate_agent_graphs()
        return normalized

    def server_summaries(self) -> list[dict[str, Any]]:
        """name / mode / tool count / tool names, for the UI and the SDK."""
        out: list[dict[str, Any]] = []
        for name, cfg in self.connections.items():
            tools = self._tools_by_server.get(name, [])
            out.append({
                "name": name,
                "config": cfg,
                "load_mode": self.load_mode(name),
                "tool_count": len(tools),
                "tools": [getattr(t, "name", "?") for t in tools],
                "loaded": name in self._tools_by_server,
            })
        return out

    def find_tool(self, server: str, tool: str) -> Any:
        """Look up one tool, raising with the available names on a miss."""
        if server not in self.connections:
            raise ValueError(
                f"Unknown MCP server {server!r}. Configured: {', '.join(self.connections) or '(none)'}"
            )
        tools = self._tools_by_server.get(server)
        if tools is None:
            raise ValueError(f"MCP server {server!r} is not loaded — reload MCP servers and retry")
        for t in tools:
            if getattr(t, "name", None) == tool:
                return t
        available = ", ".join(getattr(t, "name", "?") for t in tools) or "(none)"
        raise ValueError(f"MCP server {server!r} has no tool {tool!r}. Available: {available}")

    async def call_tool(
        self, server: str, tool: str, args: dict[str, Any] | None = None, *, timeout: float = 120.0
    ) -> tuple[str, bool]:
        """Invoke one MCP tool and return (text, is_error).

        The adapter opens a fresh MCP session per call (tools are built with
        `session=None, connection=...`), so this costs the same as a bound call
        and there is no session state to thread through the request.

        Invoked with a ToolCall payload rather than a bare dict so the result is
        a ToolMessage: with `handle_tool_errors=True` (the adapter default) an
        MCP-level failure comes back as *content*, and `status` is the only
        thing that distinguishes it from success.
        """
        lc_tool = self.find_tool(server, tool)
        call = {"type": "tool_call", "name": getattr(lc_tool, "name", tool), "args": args or {}, "id": "jarvis-mcp"}
        try:
            message = await asyncio.wait_for(lc_tool.ainvoke(call), timeout=timeout)
        except asyncio.TimeoutError:
            return (f"MCP tool {server}.{tool} timed out after {timeout:g}s", True)
        content = getattr(message, "content", message)
        is_error = getattr(message, "status", None) == "error"
        return (_content_to_text(content), is_error)

    async def reload(self, connections: dict[str, dict[str, Any]] | None = None) -> list[Any]:
        """Force reload — clears cache and re-initializes with new connections (or existing)."""
        async with self._lock:
            self._client = None
            self._tools = []
            self._tools_by_server = {}
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
            self._tools_by_server = {}
            self._initialized = False


# ── Global singleton ──────────────────────────────────────────────────────────

_mcp_manager: McpManager | None = None


def get_mcp_manager() -> McpManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = McpManager(connections=load_mcp_server_configs())
    return _mcp_manager


def get_mcp_tools_sync() -> list[Any]:
    """Tools to BIND to the agent — `always` servers only.

    `lazy` servers are deliberately absent: their schemas would otherwise be
    re-sent on every LLM call. The agent reaches them via `jarvis.mcp_call`.
    """
    try:
        mgr = get_mcp_manager()
        return mgr.get_bound_tools_sync()
    except Exception as exc:
        logger.warning("get_mcp_tools_sync failed: %s", exc)
        return []


def get_all_mcp_tools_sync() -> list[Any]:
    """Every loaded MCP tool, both modes — for introspection, not binding."""
    try:
        return get_mcp_manager().get_tools_sync()
    except Exception as exc:
        logger.warning("get_all_mcp_tools_sync failed: %s", exc)
        return []


def get_mcp_server_summaries() -> list[dict[str, Any]]:
    """Per-server name / mode / tool names. Empty when MCP isn't configured."""
    try:
        return get_mcp_manager().server_summaries()
    except Exception as exc:
        logger.warning("get_mcp_server_summaries failed: %s", exc)
        return []


async def call_mcp_tool(
    server: str, tool: str, args: dict[str, Any] | None = None, *, timeout: float = 120.0
) -> tuple[str, bool]:
    """Invoke one MCP tool by (server, tool). Returns (text, is_error)."""
    mgr = get_mcp_manager()
    if not mgr._initialized:
        await mgr.initialize()
    return await mgr.call_tool(server, tool, args, timeout=timeout)


async def initialize_mcp() -> list[Any]:
    """Call from lifespan to warm up MCP."""
    mgr = get_mcp_manager()
    return await mgr.initialize()
