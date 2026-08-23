"""Per-tool policy — what the agent may call, and what needs a human first.

Three families of tool reach the agent, and until now each had its own (or no)
control surface:

* **bound** — the handful of tools coupled to the agent graph (`core/agents.py`
  `main_tools`). Their schemas are re-sent on every LLM call.
* **sdk** — the `jarvis` SDK functions (`tools/sdk.py` `_CATEGORIES`), discovered
  on demand from inside a `run_cell` kernel. Most of the agent's write surface
  lives here.
* **mcp** — third-party tools from MCP servers. Per *server* load modes existed
  (`always` / `lazy`); per *tool* control did not.

This module is the single inventory across all three plus the policy attached to
each: `enabled` (may the agent call it at all) and `approval` (does a human have
to say yes first — see `core/tool_gate.py` for the blocking half).

**Storage is one `config_settings` row** (`tools.policy`) holding a JSON map of
`key -> {enabled, approval}`, and only *non-default* entries are written, so a
fresh install stores nothing and every tool is enabled and ungated. That keeps
this a settings change rather than a schema migration, and it is readable from
the kernel process over the SDK's existing read-only sqlite connection — which
is the whole reason it isn't a table with an ORM model.

Reads are sync and cached for `_TTL` seconds because they sit on the hot path:
every agent build, every SDK call, every MCP invocation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_KEY = "tools.policy"

KIND_BOUND = "bound"
KIND_SDK = "sdk"
KIND_MCP = "mcp"

# A read is cheap (one row, parsed JSON) but it happens per SDK call, so cache
# it briefly. The TTL is what makes a UI toggle visible to an already-running
# kernel without any cross-process invalidation channel.
_TTL = 2.0

_lock = threading.Lock()
_cache: dict[str, ToolPolicy] | None = None
_cache_at = 0.0


@dataclass(frozen=True)
class ToolPolicy:
    """What a human has decided about one tool. Defaults are permissive."""

    enabled: bool = True
    approval: bool = False

    def is_default(self) -> bool:
        return self.enabled and not self.approval

    def to_json(self) -> dict[str, bool]:
        return {"enabled": self.enabled, "approval": self.approval}


DEFAULT_POLICY = ToolPolicy()


# ── Keys ─────────────────────────────────────────────────────────────────────
# Namespaced so a bound tool and an MCP tool that happen to share a name cannot
# collide, and so the UI can group without a second field.

def bound_key(name: str) -> str:
    return f"{KIND_BOUND}:{name}"


def sdk_key(name: str) -> str:
    return f"{KIND_SDK}:{name}"


def mcp_key(server: str, tool: str) -> str:
    return f"{KIND_MCP}:{server}/{tool}"


def split_key(key: str) -> tuple[str, str]:
    """(kind, remainder). Unknown prefixes come back as kind ""."""
    kind, _, rest = key.partition(":")
    if kind not in (KIND_BOUND, KIND_SDK, KIND_MCP) or not rest:
        return ("", key)
    return (kind, rest)


# ── Reading ──────────────────────────────────────────────────────────────────

def _db_file() -> str:
    from core.config import get_config

    url = get_config().database_url
    return url.rsplit(":///", 1)[-1]


def _read_raw_sync() -> dict[str, Any]:
    """The stored JSON map, or {} for any reason at all.

    Read-only sqlite so this is safe from the kernel process (it cannot take a
    write lock against the server), and tolerant of every failure mode — a
    missing file, a missing table on first boot, malformed JSON. A policy this
    module cannot read must not stop the agent from running; it fails open,
    which matches the shipped default (everything enabled, nothing gated).
    """
    try:
        path = _db_file()
    except Exception:
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        row = conn.execute(
            "SELECT value FROM config_settings WHERE key = ?", (CONFIG_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if row is None:
        return {}
    return _parse(row[0])


def _parse(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("tools.policy is not valid JSON — ignoring it")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce(entry: Any) -> ToolPolicy:
    if not isinstance(entry, dict):
        return DEFAULT_POLICY
    return ToolPolicy(
        enabled=bool(entry.get("enabled", True)),
        approval=bool(entry.get("approval", False)),
    )


def get_policies(*, force: bool = False) -> dict[str, ToolPolicy]:
    """Every non-default policy entry, keyed by tool key."""
    global _cache, _cache_at
    with _lock:
        now = time.monotonic()
        if not force and _cache is not None and (now - _cache_at) < _TTL:
            return _cache
        parsed = {k: _coerce(v) for k, v in _read_raw_sync().items()}
        _cache = parsed
        _cache_at = now
        return parsed


def policy_for(key: str) -> ToolPolicy:
    return get_policies().get(key, DEFAULT_POLICY)


def is_enabled(key: str) -> bool:
    return policy_for(key).enabled


def needs_approval(key: str) -> bool:
    return policy_for(key).approval


def invalidate_cache(entries: dict[str, ToolPolicy] | None = None) -> None:
    """Drop (or replace) the cached map — called right after a write."""
    global _cache, _cache_at
    with _lock:
        _cache = entries
        _cache_at = time.monotonic() if entries is not None else 0.0


# ── Writing (server side — needs the async session) ──────────────────────────

async def load_policies(session) -> dict[str, ToolPolicy]:
    """Refresh the cache from the app DB through the ORM session.

    The sync reader opens its own read-only connection, which is correct for the
    kernel but wasteful in the server where a session is already at hand — and
    it cannot see a write that has not been committed yet.
    """
    from db import ops

    raw = await ops.get_setting(session, CONFIG_KEY)
    entries = {k: _coerce(v) for k, v in _parse(raw).items()}
    invalidate_cache(entries)
    return entries


async def set_tool_policy(
    session, key: str, *, enabled: bool | None = None, approval: bool | None = None,
) -> ToolPolicy:
    """Update one tool's policy and make it take effect immediately.

    Compiled agent graphs bake their bound toolset in via `bind_tools`, so an
    enable/disable is not visible to a cached graph — the same problem MCP load
    modes have, and the same fix: drop the cache so the next run rebuilds.
    In-flight runs keep the graph they started with.
    """
    from core.agents import invalidate_agent_cache
    from db import ops

    kind, rest = split_key(key)
    if not kind or not rest:
        raise ValueError(f"unknown tool key {key!r}")

    stored = _parse(await ops.get_setting(session, CONFIG_KEY))
    current = _coerce(stored.get(key))
    updated = ToolPolicy(
        enabled=current.enabled if enabled is None else bool(enabled),
        approval=current.approval if approval is None else bool(approval),
    )
    if updated.is_default():
        # Don't persist a row that says "the default" — the map stays small and
        # readable, and a tool that never gets touched never appears in it.
        stored.pop(key, None)
    else:
        stored[key] = updated.to_json()
    await ops.set_setting(session, CONFIG_KEY, json.dumps(stored))
    invalidate_cache({k: _coerce(v) for k, v in stored.items()})
    try:
        invalidate_agent_cache()
    except Exception:  # pragma: no cover — agents unimportable in some tests
        logger.debug("could not invalidate agent cache", exc_info=True)
    return updated


# ── Inventory ────────────────────────────────────────────────────────────────

@dataclass
class ToolInfo:
    """One row of the Settings → Tools table."""

    key: str
    kind: str          # bound | sdk | mcp
    name: str
    description: str
    group: str         # MCP server name, SDK category, or "agent" for bound
    enabled: bool
    requires_approval: bool
    # Is the schema in every LLM call? True for bound tools and `always` MCP
    # servers; False for SDK functions and `lazy` servers, which cost nothing
    # until the agent asks for them.
    in_prompt: bool
    # False when the tool is configured but not currently reachable — an MCP
    # server that failed to connect, or a bound tool whose precondition is
    # absent (no embedder → no `remember`).
    available: bool = True
    detail: str = ""


# Bound tools are described here rather than imported, on purpose: the list is
# the *contract* of what the graph binds, it has to survive a tool whose module
# fails to import, and `spawn_workers` has no importable module-level object
# (it is built per agent by `make_spawn_workers`). `core/agents.py` asserts the
# names line up.
_BOUND_TOOLS: tuple[tuple[str, str], ...] = (
    ("run_cell", "Run Python in the conversation's stateful kernel — the door to the jarvis SDK."),
    ("write_artifact", "Save a versioned deliverable (markdown or an on-disk file) to the side panel."),
    ("write_todos", "Replace the run's task list (a graph state delta)."),
    ("set_todo_status", "Advance one todo item (a graph state delta)."),
    ("spawn_workers", "Run parallel role-templated subagents on this conversation's model."),
    ("run_workflow", "Invoke a saved workflow graph as a sub-agent."),
    ("remember", "Write a long-term memory item. Bound only when an embedder is configured."),
    ("complete_task", "Finish the current board task. Bound only inside a board run."),
    ("block_task", "Block the current board task or ask its owner a question. Board runs only."),
)

# Bound tools that are not bound on every surface. Reported as unavailable with
# the reason, instead of silently missing from a list that claims to be the
# full inventory.
_BOUND_CONDITIONAL: dict[str, str] = {
    "remember": "needs an embedding model",
    "complete_task": "board runs only",
    "block_task": "board runs only",
}


def bound_tool_names() -> tuple[str, ...]:
    return tuple(name for name, _ in _BOUND_TOOLS)


def _bound_inventory(policies: dict[str, ToolPolicy]) -> list[ToolInfo]:
    try:
        from core.doc_index import embeddings_available

        has_embedder = bool(embeddings_available())
    except Exception:
        has_embedder = False

    out: list[ToolInfo] = []
    for name, description in _BOUND_TOOLS:
        key = bound_key(name)
        pol = policies.get(key, DEFAULT_POLICY)
        detail = _BOUND_CONDITIONAL.get(name, "")
        available = True
        if name == "remember":
            # Only worth saying when it is the reason the tool is missing —
            # otherwise the note reads as a warning about a tool that works.
            available = has_embedder
            detail = "" if has_embedder else detail
        out.append(ToolInfo(
            key=key,
            kind=KIND_BOUND,
            name=name,
            description=description,
            group="agent",
            enabled=pol.enabled,
            requires_approval=pol.approval,
            in_prompt=True,
            available=available,
            detail=detail,
        ))
    return out


def _sdk_inventory(policies: dict[str, ToolPolicy]) -> list[ToolInfo]:
    import inspect

    try:
        from tools import sdk
    except Exception as exc:  # pragma: no cover — the SDK is always importable
        logger.warning("could not import the jarvis SDK for the tool inventory: %s", exc)
        return []

    out: list[ToolInfo] = []
    for category, (_blurb, funcs) in sdk._CATEGORIES.items():
        for fn in funcs:
            name = fn.__name__
            key = sdk_key(name)
            pol = policies.get(key, DEFAULT_POLICY)
            doc = (inspect.getdoc(fn) or "").strip()
            summary = doc.splitlines()[0] if doc else ""
            out.append(ToolInfo(
                key=key,
                kind=KIND_SDK,
                name=f"jarvis.{name}",
                description=summary,
                group=category,
                enabled=pol.enabled,
                requires_approval=pol.approval,
                in_prompt=False,
            ))
    return out


def _mcp_inventory(policies: dict[str, ToolPolicy]) -> list[ToolInfo]:
    try:
        from core.mcp import get_mcp_manager, get_mcp_server_summaries
    except Exception:
        return []

    mgr = None
    try:
        mgr = get_mcp_manager()
    except Exception:
        pass

    out: list[ToolInfo] = []
    for summary in get_mcp_server_summaries():
        server = summary["name"]
        bound = summary.get("load_mode") != "lazy"
        loaded = bool(summary.get("loaded"))
        tools = list(summary.get("tools") or [])
        if not tools:
            continue
        descriptions: dict[str, str] = {}
        if mgr is not None:
            for t in mgr.tools_for_server(server):
                descriptions[getattr(t, "name", "")] = (getattr(t, "description", "") or "").strip()
        for tool_name in tools:
            key = mcp_key(server, tool_name)
            pol = policies.get(key, DEFAULT_POLICY)
            desc = descriptions.get(tool_name, "")
            out.append(ToolInfo(
                key=key,
                kind=KIND_MCP,
                name=tool_name,
                description=desc.splitlines()[0] if desc else "",
                group=server,
                enabled=pol.enabled,
                requires_approval=pol.approval,
                in_prompt=bound,
                available=loaded,
                detail="" if loaded else "server not connected",
            ))
    return out


def tool_inventory() -> list[ToolInfo]:
    """Every tool the agent can reach, across all three families."""
    policies = get_policies(force=True)
    return _bound_inventory(policies) + _sdk_inventory(policies) + _mcp_inventory(policies)
