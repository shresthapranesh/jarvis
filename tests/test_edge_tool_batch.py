"""A batch of tool calls (`agent/turn.rs:tool_step`): the calls that touch
nothing the turn holds — bound MCP tools, `remember` — run at the same
time, and every result is still written in call order.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from seed import insert
from test_edge_agent import EDGE_ON, _edge_env, _finished, _start
from test_edge_preserved_thinking import MODEL, FakeAnthropic

SLOW = Path(__file__).resolve().parent / "fixtures" / "slow_mcp_server.py"


async def test_independent_calls_run_together(database: Path, work_dir: Path, edge_binary: Path):
    fake = FakeAnthropic([
        [("wait", {"label": "first", "seconds": 1.5}), ("write_todos", {"todos": ["x"]}),
         ("wait", {"label": "second", "seconds": 1.5})],
        "Done.",
    ])
    try:
        insert(database, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Opus"}]))
        insert(database, "config_settings", key="mcp.servers", value=json.dumps(
            {"slow": {"command": sys.executable, "args": [str(SLOW)], "transport": "stdio"}}))
        env = _edge_env(work_dir, {**EDGE_ON, "ANTHROPIC_BASE_URL": fake.url, "ANTHROPIC_API_KEY": "test-key",
                                   "OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}", "GOOGLE_API_KEY": ""})
        async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
            loaded = await client.post("/graphql", json={"query": "mutation { reloadMcpServers { name toolCount } }"})
            assert loaded.json()["data"]["reloadMcpServers"] == [{"name": "slow", "toolCount": 1}]
            task_id = await _start(client, query="Wait twice", model=MODEL)
            job = await _finished(work_dir / "database.db", task_id, timeout=60)
    finally:
        fake.server.shutdown()
    assert job["status"] == "done", job

    with sqlite3.connect(work_dir / "database.db") as conn:
        rows = [json.loads(d) for (d,) in conn.execute(
            "SELECT data FROM thread_messages WHERE thread_id = (SELECT thread_id FROM jobs WHERE id = ?) "
            "AND evicted_at IS NULL ORDER BY seq", (task_id,))]
    results = [m for m in rows if m["role"] == "tool"]
    # In call order, whatever order they finished in.
    assert [r["tool_call_id"] for r in results] == ["toolu_0_0", "toolu_0_1", "toolu_0_2"]
    spans = {}
    for r in (results[0], results[2]):
        text = r["content"] if isinstance(r["content"], str) else "".join(b.get("text", "") for b in r["content"])
        label, start, end = text.split()
        spans[label] = (float(start), float(end))
    # Each slept 1.5s; run one after the other, the second would start after the first ended.
    assert spans["second"][0] < spans["first"][1] and spans["first"][0] < spans["second"][1], spans
