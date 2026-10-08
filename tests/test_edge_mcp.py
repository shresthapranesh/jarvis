"""The edge's MCP manager and API (`edge/src/mcp/`, `edge/src/gql/mcp.rs`)
diffed against `core/mcp.py` and its resolvers.

Both sides talk to real MCP servers — the stdio fixtures, and the same tools
over Streamable HTTP, HTTP+SSE and websocket — because the contract is the
wire protocol and what the adapter makes of it, and a stand-in would agree
with a wrong assumption about either. The edge runs on a copy of the test
database, and each operation is diffed on its answer and on the settings it
wrote against Python's, recorded while it existed (`python_golden.py`).
Skipped when `cargo` isn't installed.

Where the edge differs on purpose: a deleted `mcp.default_load_mode` falls
back at once (Python keeps the last one it synced until it restarts); a
tool's structured output isn't validated against its output schema (Python's
SDK lists the tools again on every call to do that); a listing gives up
after five minutes (Python waits forever).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import _free_port, _relay_text, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from python_golden import RECORD, portable, recorded

pytest.importorskip("mcp.server.fastmcp")

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = {"X-Jarvis-Caller": "agent", "X-Jarvis-Conversation": "c1"}


def _stdio(script: str, **extra: Any) -> dict[str, Any]:
    return {"command": sys.executable, "args": [str(FIXTURES / script)], "transport": "stdio", **extra}


SERVERS = {
    "echo": _stdio("echo_mcp_server.py", **{"x-jarvis-load": "lazy"}),
    "other": _stdio("other_mcp_server.py"),
}


@pytest.fixture
def isolated(monkeypatch):
    """Only the `mcp.servers` setting configures anything, on either side."""
    monkeypatch.setattr("core.mcp._load_from_files", lambda *_a, **_k: {})
    monkeypatch.setattr("core.mcp._load_from_env", lambda *_a, **_k: {})
    monkeypatch.setattr("core.mcp._default_load_mode", "always")
    monkeypatch.delenv("JARVIS_EDGE_URL", raising=False)
    monkeypatch.delenv("JARVIS_MCP_DEFAULT_LOAD", raising=False)


def _settings(db: Path) -> list[tuple[str, str]]:
    with contextlib.closing(sqlite3.connect(db)) as c:
        return c.execute("SELECT key, value FROM config_settings ORDER BY key").fetchall()


def _put(db: Path, key: str, value: str) -> None:
    with contextlib.closing(sqlite3.connect(db)) as c:
        c.execute("INSERT OR REPLACE INTO config_settings (key, value, updated_at) VALUES (?, ?, '2026-01-01 00:00:00')",
                  (key, value))
        c.commit()


async def _python(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    from db import async_session
    from server.graphql.extensions import SESSION_LOCK_KEY
    from server.graphql.schema import schema

    async with async_session() as s:
        res = await schema.execute(
            query, variable_values=variables,
            context_value={"session": s, SESSION_LOCK_KEY: asyncio.Lock(), "caller": "human"},
        )
    out: dict[str, Any] = {"data": res.data}
    if res.errors:
        out["errors"] = [{"message": e.message, "path": e.path} for e in res.errors]
    return out


class McpTwin:
    """The edge on a copy of the test database; Python's answers recorded."""

    def __init__(self, edge: httpx.AsyncClient, a_dir: Path, b_dir: Path):
        self.edge, self.a_dir, self.b_dir = edge, a_dir, b_dir

    def put(self, key: str, value: str) -> None:
        for d in (self.a_dir, self.b_dir):
            _put(d / "database.db", key, value)

    async def run(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async def python_side() -> Any:
            answer = await _python(query, variables)
            return portable(answer), portable(_settings(self.a_dir / "database.db"))

        python, settings = await recorded(python_side)
        resp = await self.edge.post("/graphql", json={"query": query, "variables": variables or {}})
        assert resp.status_code == 200
        body = resp.json()
        edge: dict[str, Any] = {"data": body.get("data")}
        if body.get("errors"):
            edge["errors"] = [{"message": e["message"], "path": e.get("path")} for e in body["errors"]]
        assert portable(edge) == python, query
        assert portable(_settings(self.b_dir / "database.db")) == settings, query
        return python


async def _python_manager(monkeypatch) -> Any:
    """Python's manager, loaded from the merged config as the lifespan loads it."""
    from core.mcp import (
        McpManager,
        get_mcp_load_modes_from_db,
        get_mcp_servers_from_db,
        load_mcp_server_configs_with_db,
    )
    from db import async_session

    async with async_session() as s:
        merged = load_mcp_server_configs_with_db(
            db_cfg=await get_mcp_servers_from_db(s), load_modes=await get_mcp_load_modes_from_db(s)
        )
    mgr = McpManager(connections=merged)
    await mgr.initialize(merged)
    monkeypatch.setattr("core.mcp._mcp_manager", mgr)
    return mgr


@contextlib.asynccontextmanager
async def _twin(work_dir: Path, b_dir: Path, edge_binary: Path, monkeypatch, servers: dict[str, Any], **env: str):
    _put(work_dir / "database.db", "mcp.servers", json.dumps(servers))
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(b_dir / "database.db")) as dst:
        src.backup(dst)
    # Python's side is only run when recording it.
    mgr = await _python_manager(monkeypatch) if RECORD else None
    # HOME and the edge's working directory hold no mcp.json.
    async with _run_edge(edge_binary, b_dir, b_dir / "database.db", {"HOME": str(b_dir), **env}) as client:
        client.timeout = httpx.Timeout(120)
        yield McpTwin(client, work_dir, b_dir)
    if mgr is not None:
        await mgr.close()


@pytest.fixture
async def twin(database, work_dir: Path, tmp_path_factory, edge_binary: Path, isolated, monkeypatch):
    async with _twin(work_dir, tmp_path_factory.mktemp("twin"), edge_binary, monkeypatch, SERVERS) as t:
        yield t


# ── reads ────────────────────────────────────────────────────────────────────


async def test_servers_and_tools(twin):
    data = await twin.run(_relay_text("McpServersQuery"))
    by_name = {s["name"]: s for s in data["data"]["mcpServers"]}
    assert (by_name["echo"]["loadMode"], by_name["echo"]["toolCount"]) == ("lazy", 3)
    assert by_name["other"]["tools"] == ["ping"]
    await twin.run("{ mcpTools { name server description inputSchema } }")
    await twin.run('{ mcpTools(server: "echo") { name description } }')
    await twin.run('{ mcpTools(server: "nope") { name } }')


async def test_tool_inventory_lists_mcp_tools(twin):
    data = await twin.run(_relay_text("ToolsQuery"))
    mcp = [t for t in data["data"]["tools"] if t["kind"] == "mcp"]
    assert {(t["group"], t["name"], t["inPrompt"]) for t in mcp} == {
        ("echo", "echo", False), ("echo", "add", False), ("echo", "explode", False), ("other", "ping", True),
    }
    policy = _relay_text("SetToolPolicyMutation")
    await twin.run(policy, {"key": "mcp:echo/add", "enabled": False})
    await twin.run(policy, {"key": "mcp:other/ping", "requiresApproval": True})


@pytest.mark.parametrize(
    "server, tool, args",
    [
        ("echo", "echo", '{"text": "via graphql"}'),
        ("echo", "add", '{"a": 2, "b": 3}'),
        ("echo", "explode", "{}"),               # the server's failure is a result
        ("echo", "echo", '{"wrong": 1}'),         # so is its validation error
        ("other", "ping", ""),
        ("nope", "echo", "{}"),
        ("echo", "nope", "{}"),
        ("echo", "echo", "[1]"),
    ],
)
async def test_call_mcp_tool(twin, server, tool, args):
    q = ("mutation($s: String!, $t: String!, $a: String!) {"
         " callMcpTool(server: $s, tool: $t, argsJson: $a) { content isError } }")
    await twin.run(q, {"s": server, "t": tool, "a": args})


# ── writes ───────────────────────────────────────────────────────────────────


SERVER_FIELDS = "name config transport command url toolCount enabled loadMode tools"


async def test_server_mutations(twin):
    add = f"mutation($n: String!, $c: String!) {{ addMcpServer(name: $n, configJson: $c) {{ {SERVER_FIELDS} }} }}"
    update = f"mutation($n: String!, $c: String!) {{ updateMcpServer(name: $n, configJson: $c) {{ {SERVER_FIELDS} }} }}"
    ping = json.dumps({"command": sys.executable, "args": [str(FIXTURES / "other_mcp_server.py")]})
    await twin.run(add, {"n": "second", "c": ping})
    await twin.run(add, {"n": "broken", "c": json.dumps({"command": "/nonexistent/mcp", "args": []})})
    await twin.run(add, {"n": "x", "c": "[1]"})
    await twin.run(update, {"n": "x", "c": '"s"'})
    await twin.run(add, {"n": "servers", "c": ping})  # _normalize_servers unwraps a "servers" key
    await twin.run(update, {"n": "second", "c": json.dumps({**json.loads(ping), "x-jarvis-load": "lazy"})})
    await twin.run(f"mutation {{ reloadMcpServers {{ {SERVER_FIELDS} }} }}")
    await twin.run('mutation { removeMcpServer(name: "broken") }')
    await twin.run('mutation { removeMcpServer(name: "never") }')
    await twin.run(_relay_text("McpServersQuery"))


async def test_load_modes(twin):
    mode = f"mutation($n: String!, $m: String!) {{ setMcpServerLoadMode(name: $n, mode: $m) {{ {SERVER_FIELDS} }} }}"
    await twin.run(mode, {"n": "echo", "m": " ALWAYS "})
    await twin.run(mode, {"n": "other", "m": "lazy"})
    await twin.run(mode, {"n": "echo", "m": "sometimes"})
    await twin.run(mode, {"n": "nope", "m": "lazy"})
    await twin.run(_relay_text("McpServersQuery"))
    default = "mutation($m: String!) { setMcpDefaultLoadMode(mode: $m) }"
    await twin.run(default, {"m": "lazy"})
    await twin.run(default, {"m": "never"})
    await twin.run(_relay_text("McpServersQuery"))


async def test_mcp_settings_are_applied(twin):
    q = "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v, allowManaged: true) { note setting { key value } } }"
    await twin.run(q, {"k": "mcp.load_modes", "v": json.dumps({"other": "lazy"})})
    await twin.run(_relay_text("McpServersQuery"))
    await twin.run('mutation { deleteSetting(key: "mcp.load_modes", allowManaged: true) { note } }')
    # Not JSON: refused with the parser's reason.
    body = (await twin.edge.post("/graphql", json={"query": q, "variables": {"k": "mcp.servers", "v": "{"}})).json()
    assert body["errors"][0]["message"].startswith("mcp.servers must be valid JSON: ")


async def test_an_approved_deferred_call_runs_on_the_edge(database, work_dir, tmp_path_factory, edge_binary, isolated,
                                                          monkeypatch):
    payload = json.dumps({"server": "echo", "tool": "echo", "args": {"text": "approved"}})
    rows = [("ap-ok", payload), ("ap-err", json.dumps({"server": "echo", "tool": "explode"})),
            ("ap-gone", json.dumps({"server": "echo", "tool": "nope"}))]
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
        for id_, p in rows:
            c.execute(
                "INSERT INTO approvals (id, source, kind, status, question, label, tool, action, action_payload, "
                "requested_at, updated_at) VALUES (?, 'chat', 'approval', 'pending', 'q', 'l', 'call_mcp_tool', "
                "'call_mcp_tool', ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (id_, p),
            )
        c.commit()
    # The edge answers approvals while it runs turns itself.
    async with _twin(work_dir, tmp_path_factory.mktemp("twin"), edge_binary, monkeypatch, SERVERS,
                     JARVIS_RUN_JOBS="1") as twin:
        q = "mutation($id: String!) { resolveApproval(id: $id, answer: \"yes\") { status result } }"
        ok = await twin.run(q, {"id": "ap-ok"})
        assert ok["data"]["resolveApproval"] == {"status": "approved", "result": "echo: approved"}
        await twin.run(q, {"id": "ap-err"})
        await twin.run(q, {"id": "ap-gone"})  # an error, and the row stays pending


# ── an agent's call ──────────────────────────────────────────────────────────


async def test_an_agents_call_follows_its_tool_policy(twin):
    q = 'mutation { callMcpTool(server: "echo", tool: "echo", argsJson: "{\\"text\\": \\"x\\"}") { content isError } }'
    twin.put("tools.policy", json.dumps({"mcp:echo/echo": {"enabled": False}}))
    body = (await twin.edge.post("/graphql", json={"query": q}, headers=AGENT)).json()
    assert body["errors"][0]["message"] == "MCP tool echo.echo is switched off in Settings → Tools."
    # A human's call isn't the agent's to be refused.
    body = (await twin.edge.post("/graphql", json={"query": q})).json()
    assert body["data"]["callMcpTool"] == {"content": "echo: x", "isError": False}
    # With every MCP call approval-gated, the request is recorded for a human.
    twin.put("tools.policy", "{}")
    twin.put("approval.required_actions", "call_mcp_tool")
    body = (await twin.edge.post("/graphql", json={"query": q}, headers=AGENT)).json()
    assert body["errors"][0]["message"].startswith('Approval required: Call MCP tool echo.echo with {"text": "x"}? ')


@pytest.mark.parametrize("answer, expected", [
    ("yes", {"content": "echo: gated", "isError": False}),
    ("no, not now", {"content": "Denied by a human (no, not now): `echo.echo` was not run. "
                                "Do not retry it — continue without it, or say what you need and why.",
                     "isError": True}),
])
async def test_an_agents_gated_call_waits_for_a_human(database, work_dir, tmp_path_factory, edge_binary, isolated,
                                                     monkeypatch, answer, expected):
    async with _twin(work_dir, tmp_path_factory.mktemp("twin"), edge_binary, monkeypatch, SERVERS,
                     JARVIS_RUN_JOBS="1") as twin:
        twin.put("tools.policy", json.dumps({"mcp:echo/echo": {"approval": True}}))
        q = 'mutation { callMcpTool(server: "echo", tool: "echo", argsJson: "{\\"text\\": \\"gated\\"}") { content isError } }'
        call = asyncio.create_task(twin.edge.post("/graphql", json={"query": q}, headers=AGENT))
        deadline = time.monotonic() + 10
        while True:
            pending = (await twin.edge.post("/graphql", json={"query": "{ pendingApprovals { id tool } }"})).json()
            if pending["data"]["pendingApprovals"]:
                break
            assert time.monotonic() < deadline, "no approval was requested"
            await asyncio.sleep(0.1)
        row = pending["data"]["pendingApprovals"][0]
        assert row["tool"] == "echo.echo"
        resolve = "mutation($id: String!, $a: String!) { resolveApproval(id: $id, answer: $a) { status } }"
        await twin.edge.post("/graphql", json={"query": resolve, "variables": {"id": row["id"], "a": answer}})
        assert (await call).json()["data"]["callMcpTool"] == expected


# ── transports ───────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _http_server(transport: str):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FIXTURES / "http_mcp_server.py"), transport, str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import socket

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail(f"{transport} server did not start")
    try:
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.parametrize("transport, cfg", [
    ("streamable-http", lambda p: {"url": f"http://127.0.0.1:{p}/mcp"}),
    ("streamable-http-json", lambda p: {"url": f"http://127.0.0.1:{p}/mcp", "transport": "streamable_http"}),
    ("sse", lambda p: {"url": f"http://127.0.0.1:{p}/sse", "transport": "sse"}),
    ("websocket", lambda p: {"url": f"ws://127.0.0.1:{p}/ws", "transport": "websocket"}),
])
async def test_network_transports(database, work_dir, tmp_path_factory, edge_binary, isolated, monkeypatch, transport, cfg):
    with _http_server(transport) as port:
        async with _twin(work_dir, tmp_path_factory.mktemp("twin"), edge_binary, monkeypatch, {"web": cfg(port)}) as twin:
            data = await twin.run('{ mcpTools(server: "web") { name inputSchema } }')
            assert {t["name"] for t in data["data"]["mcpTools"]} == {"echo", "explode"}
            q = "mutation($t: String!, $a: String!) { callMcpTool(server: \"web\", tool: $t, argsJson: $a) { content isError } }"
            done = await twin.run(q, {"t": "echo", "a": '{"text": "over the wire"}'})
            assert done["data"]["callMcpTool"] == {"content": "echo: over the wire", "isError": False}
            await twin.run(q, {"t": "explode", "a": "{}"})


# ── conversions ──────────────────────────────────────────────────────────────


def test_schemas_convert_as_langchain_converts_them():
    """`edge/src/mcp/mod.rs:llm_tool` is covered by its own unit tests; this
    pins the LangChain behaviour those tests were written from."""
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    async def noop(**_: Any) -> str:
        return ""

    schema = {"type": "object", "description": "own", "properties": {
        "title": {"type": "string", "title": "Title"},
        "node": {"$ref": "#/$defs/Node", "description": "n"},
        "list": {"anyOf": [{"title": "kept in lists", "type": "null"}]}},
        "$defs": {"Node": {"type": "object", "title": "Node", "properties": {"next": {"$ref": "#/$defs/Node"}}}}}
    tool = StructuredTool(name="t", description="", args_schema=schema, coroutine=noop)
    assert convert_to_openai_tool(tool)["function"] == {
        "name": "t", "description": "own", "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "node": {"type": "object", "properties": {"next": {}}, "description": "n"},
            "list": {"anyOf": [{"title": "kept in lists", "type": "null"}]}}},
    }
