"""A chat turn on a model that preserves thinking (`llm::preserves_thinking`):
its history only grows, so each request starts with the one before it,
unchanged, Claude's thinking going back with its calls — against a fake
Anthropic API.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from seed import insert
from test_edge_agent import EDGE_ON, _edge_env, _finished, _start

MODEL = "anthropic:claude-opus-5-5"


class FakeAnthropic:
    """`/v1/messages`, streamed: the agent's request n is answered with
    `script[n]` — a thinking block signed `sig-n`, then a call
    `(name, input)`, a list of them, or text — reporting `input_tokens[n]` (else 100). A summarizer's
    request (compaction) is answered "Earlier work, summarized." and kept
    in `summaries`."""

    def __init__(self, script: list[Any], input_tokens: dict[int, int] | None = None) -> None:
        self.script = script
        self.requests: list[dict[str, Any]] = []
        self.summaries: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server's spelling
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                if "summar" in json.dumps(body.get("system", ""))[:200].lower():
                    fake.summaries.append(body)
                    self._event({"type": "message_start", "message": {"id": "msg_s", "model": "claude-opus-5-5",
                                                                      "usage": {"input_tokens": 10, "output_tokens": 1}}})
                    self._event({"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": "Earlier work, summarized."}})
                    self._event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
                    self._event({"type": "message_stop"})
                    return
                n = len(fake.requests)
                fake.requests.append({"headers": dict(self.headers), "body": body})
                step = fake.script[n] if n < len(fake.script) else "(unscripted)"
                self._event({"type": "message_start", "message": {
                    "id": f"msg_{n}", "model": "claude-opus-5-5",
                    "usage": {"input_tokens": (input_tokens or {}).get(n, 100), "output_tokens": 1},
                    "input_transformations": []}})
                self._event({"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking", "thinking": f"step {n}", "signature": f"sig-{n}"}})
                if isinstance(step, (tuple, list)):
                    calls = step if isinstance(step, list) else [step]
                    blocks = [{"type": "tool_use", "id": f"toolu_{n}_{i}", "name": name, "input": args}
                              for i, (name, args) in enumerate(calls)]
                    stop = "tool_use"
                else:
                    blocks = [{"type": "text", "text": step}]
                    stop = "end_turn"
                for i, block in enumerate(blocks, start=1):
                    self._event({"type": "content_block_start", "index": i, "content_block": block})
                self._event({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 9}})
                self._event({"type": "message_stop"})

            def _event(self, data: dict) -> None:
                self.wfile.write(f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()

            def log_message(self, format, *args):  # noqa: A002 — the parent's name
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _unmarked(value: Any) -> Any:
    """`value` without its cache breakpoints, which may move."""
    if isinstance(value, list):
        return [_unmarked(v) for v in value]
    if isinstance(value, dict):
        return {k: _unmarked(v) for k, v in value.items() if k != "cache_control"}
    return value


@pytest.fixture
def anthropic():
    # The plan is written, then a step marked done, so the turn context
    # changes before every call.
    fake = FakeAnthropic([
        ("write_todos", {"todos": ["survey", "build"]}),
        ("set_todo_status", {"index": 0, "status": "done"}),
        "All done.",
    ])
    yield fake
    fake.server.shutdown()


async def test_each_request_starts_with_the_last(database: Path, work_dir: Path, edge_binary: Path, anthropic):
    insert(database, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Opus"}]))
    env = _edge_env(work_dir, {**EDGE_ON, "ANTHROPIC_BASE_URL": anthropic.url, "ANTHROPIC_API_KEY": "test-key",
                               # Nothing to embed with: retrieval finds nothing.
                               "OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}", "GOOGLE_API_KEY": ""})
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        task_id = await _start(client, query="Research the options and implement the plan", model=MODEL)
        job = await _finished(work_dir / "database.db", task_id)
    assert job["status"] == "done", job

    sent = [r["body"]["messages"] for r in anthropic.requests]
    assert len(sent) == 3
    for before, after in zip(sent, sent[1:]):
        assert _unmarked(after[:len(before)]) == _unmarked(before)
        assert len(after) > len(before)
    # Claude's thinking went back with its call, signature and all.
    assert sent[1][1]["content"][0] == {"type": "thinking", "thinking": "step 0", "signature": "sig-0"}
    assert sent[2][3]["content"][0]["signature"] == "sig-1"
    assert sent[1][1]["content"][1]["id"] == "toolu_0_0"
    # Each turn context where it was sent: the plan directive, then the
    # list as written, then with its first step done.
    contexts = [b["text"] for m in sent[-1] if m["role"] == "user" for b in m["content"]
                if b.get("type") == "text" and b["text"].startswith("<turn_context>")]
    assert len(contexts) == 3
    assert "Planning Required" in contexts[0]
    assert "[ ] survey" in contexts[1] and "[x] survey" in contexts[2]
    for r in anthropic.requests:
        assert r["headers"]["anthropic-beta"] == "context-management-2025-06-27,thinking-binding-controls-2026-08-01"
        assert r["body"]["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}

    # The thread keeps each context on the message it followed.
    with sqlite3.connect(work_dir / "database.db") as conn:
        rows = [json.loads(d) for (d,) in conn.execute(
            "SELECT data FROM thread_messages WHERE thread_id = (SELECT thread_id FROM jobs WHERE id = ?) "
            "AND evicted_at IS NULL ORDER BY seq", (task_id,))]
    carried = [m["role"] for m in rows if "turn_context" in m.get("extras", {})]
    assert carried == ["user", "tool", "tool"]


def _thinking(message: dict) -> list[dict]:
    content = message["content"] if isinstance(message["content"], list) else []
    return [b for b in content if b.get("type") == "thinking"]


async def test_a_compaction_strips_the_kept_turns_thinking_once(database: Path, work_dir: Path, edge_binary: Path):
    """The turns a compaction keeps were thought about with the evicted ones
    before them: they lose their thinking, in the request and the thread, and
    the history goes on growing from there."""
    steps = [("write_todos", {"todos": ["a", "b", "c", "d"]})]
    steps += [("set_todo_status", {"index": i, "status": "done"}) for i in range(4)]
    # The fourth reply says the history passed the threshold: the next call
    # summarizes the two oldest calls and keeps the two newest.
    fake = FakeAnthropic([*steps, "Done."], input_tokens={3: 90_000})
    try:
        insert(database, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Opus"}]))
        env = _edge_env(work_dir, {**EDGE_ON, "ANTHROPIC_BASE_URL": fake.url, "ANTHROPIC_API_KEY": "test-key",
                                   "OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}", "GOOGLE_API_KEY": "",
                                   "JARVIS_COMPACT_TOKEN_THRESHOLD": "50000"})
        async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
            task_id = await _start(client, query="Crunch the numbers", model=MODEL)
            job = await _finished(work_dir / "database.db", task_id, timeout=60)
    finally:
        fake.server.shutdown()
    assert job["status"] == "done", job
    assert len(fake.summaries) == 1, "compacted once"

    sent = [r["body"] for r in fake.requests]
    compacted = next(i for i, b in enumerate(sent) if "Earlier work, summarized." in json.dumps(b["system"]))
    first = sent[compacted]["messages"]
    # What was kept goes without thinking; the reply to this request will have its own.
    assert all(not _thinking(m) for m in first if m["role"] == "assistant")
    # After it, the history only grows again, the new replies' thinking kept.
    for before, after in zip(sent[compacted:], sent[compacted + 1:]):
        assert _unmarked(after["messages"][:len(before["messages"])]) == _unmarked(before["messages"])
        assert _thinking(after["messages"][len(before["messages"])])

    # And the thread was rewritten once: the kept turns have no thinking.
    with sqlite3.connect(work_dir / "database.db") as conn:
        rows = [json.loads(d) for (d,) in conn.execute(
            "SELECT data FROM thread_messages WHERE thread_id = (SELECT thread_id FROM jobs WHERE id = ?) "
            "AND evicted_at IS NULL ORDER BY seq", (task_id,))]
    summary_at = next(i for i, m in enumerate(rows) if m["role"] == "system")
    before_summary = [m for m in rows[:summary_at] if m["role"] == "assistant"]
    assert before_summary and all(not _thinking(m) for m in before_summary)
    assert all(_thinking(m) for m in rows[summary_at:] if m["role"] == "assistant")
