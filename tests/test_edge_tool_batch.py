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


async def test_workers_run_on_the_worker_model(database: Path, work_dir: Path, edge_binary: Path):
    """`workers.model` names a model in the catalog: the workers run on it,
    the run itself on its own."""
    haiku = "anthropic:claude-haiku-5-5"
    fake = FakeAnthropic([("spawn_workers", {"tasks": [{"task": "Look it up", "role": "researcher"}]}),
                          "Found it.", "Done."])
    try:
        insert(database, "config_settings", key="models.custom",
               value=json.dumps([{"id": MODEL, "label": "Opus"}, {"id": haiku, "label": "Haiku"}]))
        insert(database, "config_settings", key="workers.model", value=haiku)
        env = _edge_env(work_dir, {**EDGE_ON, "ANTHROPIC_BASE_URL": fake.url, "ANTHROPIC_API_KEY": "test-key",
                                   "OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}", "GOOGLE_API_KEY": ""})
        async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
            task_id = await _start(client, query="Find it", model=MODEL)
            job = await _finished(work_dir / "database.db", task_id, timeout=60)
    finally:
        fake.server.shutdown()
    assert job["status"] == "done", job
    assert [r["body"]["model"] for r in fake.requests] == ["claude-opus-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
    assert "You are a research worker." in json.dumps(fake.requests[1]["body"]["system"])


async def test_more_workers_than_pool_connections(database: Path, work_dir: Path, edge_binary: Path):
    """Six workers on a pool of four connections: the run keeps them
    going while it writes what they tell it, or a worker waiting on a
    connection holds up the very write that would free one (`db.rs`)."""
    workers = 6
    tasks = [{"task": f"Look up {i}", "role": "researcher"} for i in range(workers)]
    fake = FakeAnthropic([("spawn_workers", {"tasks": tasks}), *["Found it."] * workers, "Done."])
    try:
        insert(database, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Opus"}]))
        env = _edge_env(work_dir, {**EDGE_ON, "ANTHROPIC_BASE_URL": fake.url, "ANTHROPIC_API_KEY": "test-key",
                                   "OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}", "GOOGLE_API_KEY": ""})
        async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
            task_id = await _start(client, query="Look them all up", model=MODEL)
            job = await _finished(work_dir / "database.db", task_id, timeout=60)
    finally:
        fake.server.shutdown()
    assert job["status"] == "done", job
    assert len(fake.requests) == workers + 2
    with sqlite3.connect(work_dir / "database.db") as conn:
        # The job is done even when the run failed; the reply says which.
        (status,) = conn.execute("SELECT status FROM messages WHERE id = ?", (task_id,)).fetchone()
        done = conn.execute("SELECT COUNT(*) FROM steps WHERE message_id = ? AND node = 'worker_done'",
                            (task_id,)).fetchone()[0]
    assert status == "done"
    assert done == workers
