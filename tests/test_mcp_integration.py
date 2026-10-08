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
