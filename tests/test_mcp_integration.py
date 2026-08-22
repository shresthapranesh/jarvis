"""End-to-end against real stdio MCP servers.

Everything the lazy path relies on is a contract with langchain-mcp-adapters —
per-server tool loading, a fresh session per call, ToolCall-shaped invocation
carrying `status` — and a mock would happily agree with a wrong assumption
about any of it. These spawn the real thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.mcp import LOAD_MODE_KEY, McpManager

_FIXTURES = Path(__file__).parent / "fixtures"

pytest.importorskip("mcp.server.fastmcp")


def _stdio(script: str, **extra) -> dict:
    return {
        "command": sys.executable,
        "args": [str(_FIXTURES / script)],
        "transport": "stdio",
        **extra,
    }


@pytest.fixture
async def manager() -> McpManager:
    connections = {
        "echo": _stdio("echo_mcp_server.py", **{LOAD_MODE_KEY: "lazy"}),
        "other": _stdio("other_mcp_server.py"),
    }
    mgr = McpManager(connections=connections)
    await mgr.initialize(connections)
    yield mgr
    await mgr.close()


async def test_tools_load_attributed_to_their_own_server(manager: McpManager):
    assert sorted(t.name for t in manager.tools_for_server("echo")) == ["add", "echo", "explode"]
    assert [t.name for t in manager.tools_for_server("other")] == ["ping"]


async def test_only_the_always_server_is_bound(manager: McpManager):
    # echo is lazy: 3 tools loaded and callable, 0 tools in the prompt.
    assert [t.name for t in manager.get_bound_tools_sync()] == ["ping"]
    assert len(manager.get_tools_sync()) == 4


async def test_call_a_lazy_tool(manager: McpManager):
    text, is_error = await manager.call_tool("echo", "echo", {"text": "hello"})
    assert text == "echo: hello" and is_error is False


async def test_call_coerces_and_returns_non_string_output(manager: McpManager):
    text, is_error = await manager.call_tool("echo", "add", {"a": 2, "b": 3})
    assert text == "5" and is_error is False


async def test_server_side_failure_comes_back_as_is_error(manager: McpManager):
    # handle_tool_errors=True (the adapter default) turns this into content, so
    # `status` is the ONLY thing separating it from a successful call.
    text, is_error = await manager.call_tool("echo", "explode")
    assert is_error is True
    assert "boom from the server" in text


async def test_bad_arguments_do_not_raise_into_the_kernel(manager: McpManager):
    text, is_error = await manager.call_tool("echo", "echo", {"wrong": "arg"})
    assert is_error is True
    assert text  # the server's own validation message, for the agent to correct


async def test_input_schema_is_available_for_mcp_help(manager: McpManager):
    tool = manager.find_tool("echo", "add")
    schema = tool.args_schema
    assert isinstance(schema, dict)
    assert set(schema["properties"]) == {"a", "b"}


async def test_summaries_report_mode_and_tool_names(manager: McpManager):
    by_name = {s["name"]: s for s in manager.server_summaries()}
    assert by_name["echo"]["load_mode"] == "lazy"
    assert by_name["echo"]["tool_count"] == 3
    assert by_name["other"]["load_mode"] == "always"
    assert "ping" in by_name["other"]["tools"]


async def test_calls_are_repeatable(manager: McpManager):
    # Each call opens its own session; a stale/closed session would surface here.
    first, _ = await manager.call_tool("echo", "echo", {"text": "one"})
    second, _ = await manager.call_tool("echo", "echo", {"text": "two"})
    assert (first, second) == ("echo: one", "echo: two")


# ── Through the API — the transport the kernel actually uses ─────────────────
# jarvis.mcp_call runs in a separate process and reaches MCP only through these
# resolvers, so the GraphQL layer is part of the call path, not a wrapper.

def _context(session):
    import asyncio

    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


@pytest.fixture
def isolated_config(monkeypatch):
    """Keep the developer's own ~/.jarvis/mcp.json out of these assertions."""
    monkeypatch.setattr("core.mcp._load_from_files", lambda *_a, **_k: {})
    monkeypatch.setattr("core.mcp._load_from_env", lambda *_a, **_k: {})


@pytest.fixture
async def api(manager, isolated_config, database, monkeypatch):
    """The real manager installed as the process singleton, plus a session."""
    monkeypatch.setattr("core.mcp._mcp_manager", manager)
    from db import async_session
    from server.graphql.schema import schema

    async def run(query: str, variables: dict | None = None):
        async with async_session() as session:
            result = await schema.execute(
                query, variable_values=variables or {}, context_value=_context(session)
            )
            assert not result.errors, result.errors
            return result.data

    return run


async def test_mcp_servers_query_reports_modes_and_counts(api):
    data = await api("{ mcpServers { name loadMode toolCount tools } }")
    by_name = {s["name"]: s for s in data["mcpServers"]}
    assert by_name["echo"]["loadMode"] == "lazy"
    assert by_name["echo"]["toolCount"] == 3
    assert by_name["other"]["loadMode"] == "always"
    assert by_name["other"]["tools"] == ["ping"]


async def test_mcp_tools_query_is_scoped_to_one_server(api):
    data = await api(
        "query($s: String) { mcpTools(server: $s) { name server description } }", {"s": "echo"}
    )
    names = {t["name"] for t in data["mcpTools"]}
    assert names == {"echo", "add", "explode"}
    assert all(t["server"] == "echo" for t in data["mcpTools"])
    assert any("Add two integers" in t["description"] for t in data["mcpTools"])


async def test_input_schema_is_only_paid_for_when_selected(api):
    # The listing above carries no schemas; this is the second, deliberate hop
    # that jarvis.mcp_help makes.
    data = await api(
        'query { mcpTools(server: "echo") { name inputSchema } }'
    )
    schemas = {t["name"]: t["inputSchema"] for t in data["mcpTools"]}
    assert '"a"' in schemas["add"] and '"b"' in schemas["add"]


async def test_call_mcp_tool_mutation_round_trip(api):
    data = await api(
        "mutation($s: String!, $t: String!, $a: String!) {"
        " callMcpTool(server: $s, tool: $t, argsJson: $a) { content isError } }",
        {"s": "echo", "t": "echo", "a": '{"text": "via graphql"}'},
    )
    assert data["callMcpTool"] == {"content": "echo: via graphql", "isError": False}


async def test_call_mcp_tool_reports_failure_without_erroring_the_mutation(api):
    data = await api(
        "mutation { callMcpTool(server: \"echo\", tool: \"explode\", argsJson: \"{}\") "
        "{ content isError } }"
    )
    # A failing MCP tool is a result the agent must read and act on, not a
    # transport error that should blow up the request.
    assert data["callMcpTool"]["isError"] is True
    assert "boom from the server" in data["callMcpTool"]["content"]


async def test_set_load_mode_mutation_flips_what_gets_bound(api, manager):
    assert [t.name for t in manager.get_bound_tools_sync()] == ["ping"]
    data = await api(
        'mutation { setMcpServerLoadMode(name: "echo", mode: "always") { name loadMode } }'
    )
    assert data["setMcpServerLoadMode"]["loadMode"] == "always"
    assert len(manager.get_bound_tools_sync()) == 4

    await api('mutation { setMcpServerLoadMode(name: "other", mode: "lazy") { name loadMode } }')
    assert [t.name for t in manager.get_bound_tools_sync()] != ["ping"]


async def test_load_mode_survives_as_a_db_override(api, manager):
    from core.mcp import get_mcp_load_modes_from_db
    from db import async_session

    await api('mutation { setMcpServerLoadMode(name: "echo", mode: "always") { name } }')
    async with async_session() as session:
        assert (await get_mcp_load_modes_from_db(session))["echo"] == "always"
