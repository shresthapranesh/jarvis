"""Per-server MCP load modes and the lazy call path.

The bound tool set is billed on every LLM call of every run, so which server
lands in it is the whole point of these tests: `always` binds, `lazy` stays out
of the prompt and is reachable only through `jarvis.mcp_call` (which routes back
here via the API, because the SDK runs in a separate kernel process).
"""

from __future__ import annotations

import pytest

from core.mcp import (
    LOAD_ALWAYS,
    LOAD_LAZY,
    LOAD_MODE_KEY,
    McpManager,
    apply_load_mode_overrides,
    get_default_load_mode,
    load_mode_for,
    normalize_load_mode,
    set_default_load_mode,
    strip_jarvis_keys,
    with_load_mode,
)


@pytest.fixture(autouse=True)
def _default_mode():
    """The module default is process-global — restore it around each test."""
    original = get_default_load_mode()
    yield
    set_default_load_mode(original)


# ── Mode resolution ──────────────────────────────────────────────────────────

def test_declared_mode_wins():
    assert load_mode_for({"command": "x", LOAD_MODE_KEY: "lazy"}) == LOAD_LAZY


def test_missing_mode_falls_back_to_the_default():
    set_default_load_mode(LOAD_LAZY)
    assert load_mode_for({"command": "x"}) == LOAD_LAZY
    set_default_load_mode(LOAD_ALWAYS)
    assert load_mode_for({"command": "x"}) == LOAD_ALWAYS


def test_unknown_mode_degrades_to_the_default_rather_than_raising():
    # A typo in mcp.json must not take MCP down; `always` is the safe read.
    assert normalize_load_mode("sometimes") is None
    assert load_mode_for({"command": "x", LOAD_MODE_KEY: "sometimes"}) == LOAD_ALWAYS


def test_default_mode_is_always():
    assert get_default_load_mode() == LOAD_ALWAYS


# ── The key never reaches the MCP client ─────────────────────────────────────

def test_jarvis_keys_are_stripped_before_connecting():
    # create_session() splats every non-transport key into the transport
    # constructor, so a leaked key is a TypeError at connect time.
    cfg = {"command": "uvx", "args": ["srv"], "transport": "stdio", LOAD_MODE_KEY: "lazy"}
    assert strip_jarvis_keys(cfg) == {"command": "uvx", "args": ["srv"], "transport": "stdio"}


def test_with_load_mode_round_trips():
    cfg = {"command": "x", "transport": "stdio"}
    assert with_load_mode(cfg, "lazy")[LOAD_MODE_KEY] == "lazy"
    # None clears it, so the server falls back to the default again.
    assert LOAD_MODE_KEY not in with_load_mode(with_load_mode(cfg, "lazy"), None)


def test_overrides_apply_without_touching_the_rest_of_the_config():
    servers = {"a": {"command": "x", "transport": "stdio"}, "b": {"url": "u", "transport": "http"}}
    out = apply_load_mode_overrides(servers, {"a": "lazy", "ghost": "lazy"})
    assert load_mode_for(out["a"]) == LOAD_LAZY
    assert load_mode_for(out["b"]) == LOAD_ALWAYS
    assert out["a"]["command"] == "x"  # config preserved, only the mode added
    assert "ghost" not in out  # an override for an absent server is ignored


# ── Manager: attribution, filtering, calling ─────────────────────────────────

class _FakeTool:
    def __init__(self, name: str, result=None, status: str = "success"):
        self.name = name
        self.description = f"{name} does a thing"
        self.args_schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        self._result = result if result is not None else f"{name}-ok"
        self._status = status
        self.calls: list[dict] = []

    async def ainvoke(self, call, *_a, **_kw):
        self.calls.append(call)

        class _Msg:
            content = self._result
            status = self._status

        return _Msg()


class _FakeClient:
    """Stands in for MultiServerMCPClient — records what it was constructed with."""

    last_connections: dict | None = None

    def __init__(self, connections):
        type(self).last_connections = connections
        self.connections = connections

    async def get_tools(self, *, server_name=None):
        if server_name == "broken":
            raise RuntimeError("connection refused")
        return [_FakeTool(f"{server_name}_one"), _FakeTool(f"{server_name}_two")]


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(
        "langchain_mcp_adapters.client.MultiServerMCPClient", _FakeClient, raising=True
    )
    _FakeClient.last_connections = None
    return _FakeClient


async def _manager(connections) -> McpManager:
    mgr = McpManager(connections=connections)
    await mgr.initialize(connections)
    return mgr


async def test_tools_are_attributed_per_server(fake_client):
    mgr = await _manager({
        "alpha": {"command": "a", "transport": "stdio"},
        "beta": {"command": "b", "transport": "stdio"},
    })
    assert [t.name for t in mgr.tools_for_server("alpha")] == ["alpha_one", "alpha_two"]
    assert [t.name for t in mgr.tools_for_server("beta")] == ["beta_one", "beta_two"]
    assert len(mgr.get_tools_sync()) == 4


async def test_only_always_servers_are_bound(fake_client):
    mgr = await _manager({
        "alpha": {"command": "a", "transport": "stdio"},
        "beta": {"command": "b", "transport": "stdio", LOAD_MODE_KEY: "lazy"},
    })
    bound = {t.name for t in mgr.get_bound_tools_sync()}
    assert bound == {"alpha_one", "alpha_two"}
    # Lazy tools are still loaded and callable — just not in the prompt.
    assert len(mgr.get_tools_sync()) == 4


async def test_the_client_never_sees_the_jarvis_key(fake_client):
    await _manager({"alpha": {"command": "a", "transport": "stdio", LOAD_MODE_KEY: "lazy"}})
    assert fake_client.last_connections == {"alpha": {"command": "a", "transport": "stdio"}}


async def test_one_unreachable_server_does_not_sink_the_others(fake_client):
    mgr = await _manager({
        "alpha": {"command": "a", "transport": "stdio"},
        "broken": {"command": "b", "transport": "stdio"},
    })
    assert [t.name for t in mgr.tools_for_server("alpha")] == ["alpha_one", "alpha_two"]
    assert mgr.tools_for_server("broken") == []


async def test_set_load_mode_flips_the_bound_set_in_place(fake_client):
    mgr = await _manager({"alpha": {"command": "a", "transport": "stdio"}})
    assert len(mgr.get_bound_tools_sync()) == 2
    mgr.set_load_mode("alpha", "lazy")
    assert mgr.get_bound_tools_sync() == []
    assert mgr.load_mode("alpha") == LOAD_LAZY


async def test_set_load_mode_rejects_unknown_server_and_mode(fake_client):
    mgr = await _manager({"alpha": {"command": "a", "transport": "stdio"}})
    with pytest.raises(ValueError):
        mgr.set_load_mode("ghost", "lazy")
    with pytest.raises(ValueError):
        mgr.set_load_mode("alpha", "sometimes")


async def test_call_tool_returns_text(fake_client):
    mgr = await _manager({"alpha": {"command": "a", "transport": "stdio"}})
    text, is_error = await mgr.call_tool("alpha", "alpha_one", {"q": "hi"})
    assert text == "alpha_one-ok"
    assert is_error is False
    # Invoked as a ToolCall, which is what makes `status` (and so the error
    # flag) available — a bare dict returns content with no status.
    call = mgr.tools_for_server("alpha")[0].calls[0]
    assert call["type"] == "tool_call" and call["args"] == {"q": "hi"}


async def test_call_tool_surfaces_mcp_errors_as_is_error(fake_client, monkeypatch):
    mgr = await _manager({"alpha": {"command": "a", "transport": "stdio"}})
    failing = _FakeTool("alpha_one", result="boom", status="error")
    monkeypatch.setattr(mgr, "find_tool", lambda *_a, **_k: failing)
    text, is_error = await mgr.call_tool("alpha", "alpha_one")
    assert (text, is_error) == ("boom", True)


async def test_call_tool_flattens_content_blocks(fake_client, monkeypatch):
    mgr = await _manager({"alpha": {"command": "a", "transport": "stdio"}})
    blocks = _FakeTool("alpha_one", result=[{"type": "text", "text": "line one"}, {"type": "image"}])
    monkeypatch.setattr(mgr, "find_tool", lambda *_a, **_k: blocks)
    text, _ = await mgr.call_tool("alpha", "alpha_one")
    assert "line one" in text and "image" in text


async def test_unknown_tool_names_what_is_available(fake_client):
    mgr = await _manager({"alpha": {"command": "a", "transport": "stdio"}})
    with pytest.raises(ValueError, match="alpha_one"):
        mgr.find_tool("alpha", "nope")
    with pytest.raises(ValueError, match="Configured: alpha"):
        mgr.find_tool("ghost", "nope")


# ── Prompt advertisement ─────────────────────────────────────────────────────

def test_lazy_servers_are_advertised_and_always_servers_are_not(monkeypatch):
    from core import agents

    monkeypatch.setattr(agents, "get_mcp_server_summaries", lambda: [
        {"name": "alpha", "load_mode": "always", "tool_count": 2, "tools": ["a1", "a2"],
         "config": {}, "loaded": True},
        {"name": "beta", "load_mode": "lazy", "tool_count": 2, "tools": ["b1", "b2"],
         "config": {}, "loaded": True},
    ])
    segments = agents._mcp_volatile_parts()
    assert len(segments) == 1
    content = segments[0].content
    assert segments[0].name == "mcp_servers" and segments[0].cacheable
    assert "beta" in content and "b1" in content
    # An always server's tools are bound and self-describing; naming it here
    # would be paying twice for the same thing.
    assert "alpha" not in content


def test_no_segment_when_every_server_is_bound(monkeypatch):
    from core import agents

    monkeypatch.setattr(agents, "get_mcp_server_summaries", lambda: [
        {"name": "alpha", "load_mode": "always", "tool_count": 2, "tools": ["a1"],
         "config": {}, "loaded": True},
    ])
    assert agents._mcp_volatile_parts() == []


def test_a_lazy_server_with_no_loaded_tools_is_not_advertised(monkeypatch):
    from core import agents

    monkeypatch.setattr(agents, "get_mcp_server_summaries", lambda: [
        {"name": "beta", "load_mode": "lazy", "tool_count": 0, "tools": [],
         "config": {}, "loaded": False},
    ])
    assert agents._mcp_volatile_parts() == []


def test_mcp_segment_has_a_stability_rank():
    from core.agents import _SEGMENT_STABILITY, _SEGMENT_STABILITY_DEFAULT

    # Without one it sorts last among cached blocks, behind content that
    # changes far more often.
    assert _SEGMENT_STABILITY.get("mcp_servers", _SEGMENT_STABILITY_DEFAULT) < _SEGMENT_STABILITY_DEFAULT
