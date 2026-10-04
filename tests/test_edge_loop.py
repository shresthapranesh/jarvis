"""The edge's agent loop against Python's, turn for turn (phase 2d).

Each scenario is one chat turn with a scripted model behind a fake Ollama
server. Python runs it in this process (`register_chat_task` →
`chat_job_handler`); the edge runs it on a twin of the same database, started
with `startTask`. Then everything a user or the next turn could see is
diffed: the events a `taskEvents` subscriber gets, the Step rows, the final
message, the thread, and every request the model received.

Where the edge differs on purpose it says so here, by name.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_harness import Normalizer
from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from test_edge_llm import _intended_ollama, _semantics
from test_edge_runs import CHAT, _python_subscribe

REPO = Path(__file__).resolve().parent.parent
MODEL = "ollama:fake"
START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued } }"""
QUEUE = """mutation($taskId: String!, $query: String!) {
  queueMessage(taskId: $taskId, query: $query) { messageId position } }"""
STOP = "mutation($id: String!) { stopTask(taskId: $id) }"


# ── the model ────────────────────────────────────────────────────────────────


@dataclass
class Reply:
    text: str = ""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class FakeOllama:
    """`/api/chat`, answering the n-th request with `script[n]`; anything else
    (an embedding) is a 404. Records each chat request."""

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[dict[str, Any]] = []
        # Request n waits for `gates[n]` once `arrived[n]` is set.
        self.gates: dict[int, threading.Event] = {}
        self.arrived: dict[int, threading.Event] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server's spelling
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                if self.path != "/api/chat":
                    self.send_response(404)
                    self.end_headers()
                    return
                n = len(fake.requests)
                fake.requests.append(body)
                if n in fake.gates:
                    fake.arrived[n].set()
                    fake.gates[n].wait(30)
                reply = fake.script[n] if n < len(fake.script) else Reply("(unscripted)")
                self.send_response(200)
                self.send_header("content-type", "application/x-ndjson")
                self.end_headers()
                base = {"model": "fake", "created_at": "2026-10-04T00:00:00Z"}
                words = reply.text.split(" ")
                for i, word in enumerate(words):
                    piece = word if i == len(words) - 1 else word + " "
                    if piece:
                        self._line({**base, "message": {"role": "assistant", "content": piece}, "done": False})
                if reply.calls:
                    calls = [{"function": {"name": n, "arguments": a}} for n, a in reply.calls]
                    self._line({**base, "message": {"role": "assistant", "content": "", "tool_calls": calls},
                                "done": False})
                self._line({**base, "message": {"role": "assistant", "content": ""}, "done": True,
                            "done_reason": "stop", "total_duration": 5, "load_duration": 1,
                            "prompt_eval_count": 100 + n, "prompt_eval_duration": 50_000_000,
                            "eval_count": 7, "eval_duration": 70_000_000})

            def _line(self, chunk: dict) -> None:
                self.wfile.write(json.dumps(chunk).encode() + b"\n")
                self.wfile.flush()

            def log_message(self, format, *args):  # noqa: A002 — the parent's name
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self, script: list[Reply], hold: int | None = None) -> None:
        """A new script; with `hold`, that request waits for `release()`."""
        self.script, self.requests = list(script), []
        self.gates = {hold: threading.Event()} if hold is not None else {}
        self.arrived = {hold: threading.Event()} if hold is not None else {}

    async def held(self) -> None:
        """Until the held request has arrived."""
        [arrived] = self.arrived.values()
        assert await asyncio.to_thread(arrived.wait, 30)

    def release(self) -> None:
        for gate in self.gates.values():
            gate.set()


@pytest.fixture
def fake():
    f = FakeOllama()
    yield f
    f.server.shutdown()


# ── the two runtimes ─────────────────────────────────────────────────────────


@dataclass
class Turn:
    task_id: str
    conversation_id: str
    events: list[dict]
    db: Path


@pytest.fixture
async def twins(jarvis, work_dir: Path, tmp_path_factory, fake: FakeOllama, monkeypatch, edge_binary: Path):
    """Python in this process over `work_dir`, and an edge over a copy, both
    pointed at `fake` and at a CDP port nothing listens on."""
    from core import agents
    from db import async_session
    from db.models import ConfigSetting, Project
    from db.ops import hydrate_catalog

    dead = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    monkeypatch.setenv("JARVIS_BROWSER_CDP_URL", dead)
    agents._browser_probe = (0.0, False)
    agents._retrieval_cache.clear()
    agents.invalidate_agent_cache()
    async with async_session() as s:
        s.add(ConfigSetting(key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}])))
        s.add(Project(id="p1", name="Atlas", description="Maps.", instructions="Be brief.", memory="Uses Rust."))
        await s.commit()
        await hydrate_catalog(s)

    edge_dir = tmp_path_factory.mktemp("edge")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(edge_dir / "database.db")) as dst:
        src.backup(dst)
    env = {"JARVIS_AGENT_RUNTIME": "edge", "OLLAMA_HOST": fake.url, "JARVIS_BROWSER_CDP_URL": dead,
           "HOME": str(edge_dir), "JARVIS_APP_DIR": str(REPO)}
    async with _run_edge(edge_binary, edge_dir, edge_dir / "database.db", env) as client:
        yield Twins(jarvis, client, work_dir / "database.db", edge_dir / "database.db", fake)
    agents.invalidate_agent_cache()


class Twins:
    def __init__(self, jarvis: Any, client: Any, python_db: Path, edge_db: Path, fake: FakeOllama):
        self.jarvis, self.client, self.python_db, self.edge_db, self.fake = jarvis, client, python_db, edge_db, fake

    async def python(self, query: str, script: list[Reply], *, hold: int | None = None,
                     during: Any = None, **start: Any) -> tuple[Turn, list[dict]]:
        """`during(task_id)` runs once the held request has arrived, and then
        the request goes on."""
        from db import async_session
        from server.chat_runtime import chat_job_handler, register_chat_task

        self.fake.reset(script, hold)
        async with async_session() as s:
            dispatch = await register_chat_task(s, query=query, model=MODEL, **start)
            await s.commit()
        job = await self.jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=600)
        assert job is not None and job.id == dispatch.task_id
        handler = asyncio.create_task(chat_job_handler(job))
        if during is not None:
            await self.fake.held()
            await during(dispatch.task_id)
            self.fake.release()
        async with asyncio.timeout(60):
            await handler
        await self.jarvis.queue.complete(job.id, worker_id="test")
        events = await _python_subscribe(CHAT, {"id": dispatch.task_id})
        return Turn(dispatch.task_id, dispatch.conversation_id, events, self.python_db), list(self.fake.requests)

    async def edge(self, query: str, script: list[Reply], *, hold: int | None = None,
                   during: Any = None, **start: Any) -> tuple[Turn, list[dict]]:
        self.fake.reset(script, hold)
        # `register_chat_task`'s keywords, as `StartTaskInput` spells them.
        gql = {"projectId" if k == "project_id" else k: v for k, v in start.items()}
        resp = await self.client.post("/graphql", json={"query": START, "variables": {"input": {
            "query": query, "model": MODEL, **gql}}})
        body = resp.json()
        assert "errors" not in body, body
        task_id, conv = body["data"]["startTask"]["taskId"], body["data"]["startTask"]["conversationId"]
        if during is not None:
            await self.fake.held()
            await during(task_id)
            self.fake.release()
        events = await self.subscribe(task_id)
        return Turn(task_id, conv, events, self.edge_db), list(self.fake.requests)

    async def edge_started(self, query: str, script: list[Reply]) -> tuple[Turn, list[dict]]:
        """Start a turn on the edge and wait until its job is no longer the edge's."""
        self.fake.reset(script)
        resp = await self.client.post("/graphql", json={"query": START, "variables": {"input": {
            "query": query, "model": MODEL}}})
        data = resp.json()["data"]["startTask"]
        for _ in range(200):
            [(runtime,)] = _rows(self.edge_db, "SELECT runtime FROM jobs WHERE id = ?", data["taskId"])
            if runtime is None:
                break
            await asyncio.sleep(0.05)
        return Turn(data["taskId"], data["conversationId"], [], self.edge_db), list(self.fake.requests)

    async def subscribe(self, task_id: str) -> list[dict]:
        import websockets

        port = self.client.base_url.port
        async with websockets.connect(f"ws://127.0.0.1:{port}/graphql", subprotocols=["graphql-transport-ws"]) as ws:
            await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
            assert json.loads(await ws.recv())["type"] == "connection_ack"
            await ws.send(json.dumps({"id": "1", "type": "subscribe",
                                      "payload": {"query": CHAT, "variables": {"id": task_id}}}))
            out = []
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
                if msg["type"] == "next":
                    out.append(msg["payload"])
                elif msg["type"] in ("complete", "error"):
                    return out


# ── what a turn left ─────────────────────────────────────────────────────────


def _rows(db: Path, sql: str, *args: Any) -> list[tuple]:
    with contextlib.closing(sqlite3.connect(db)) as c:
        return c.execute(sql, args).fetchall()


def _events(turn: Turn) -> list[dict]:
    """The subscriber's events, without the wall-clock ones, tokens merged."""
    out: list[dict] = []
    for item in turn.events:
        ev = dict(item["data"]["taskEvents"])
        kind = ev.pop("__typename")
        if kind in ("BudgetUpdateEvent", "PerfUpdateEvent"):
            continue
        if kind in ("TokenEvent", "ThinkingTokenEvent") and out and out[-1]["kind"] == kind:
            out[-1]["text"] += ev["text"]
            continue
        out.append({"kind": kind, **ev})
    return out


def _thread(turn: Turn) -> list[dict]:
    """The thread as v1 records, a reply by what it means."""
    out = []
    for (data,) in _rows(turn.db, "SELECT data FROM thread_messages WHERE thread_id = ? AND evicted_at IS NULL "
                                  "ORDER BY seq", turn.conversation_id):
        rec = json.loads(data)
        if rec["role"] == "assistant":
            meaning = _semantics(rec)
            meaning.pop("model")
            # LangChain keeps Ollama's stop reason in its response metadata;
            # the edge records it as the finish reason (test_edge_llm).
            meaning["finish_reason"] = meaning["finish_reason"] or (
                rec.get("extras", {}).get("response_metadata", {}).get("done_reason"))
            rec = {"role": "assistant", "id": rec.get("id"), **meaning}
        out.append(rec)
    return out


def _record(turn: Turn) -> dict[str, Any]:
    norm = Normalizer({turn.conversation_id: "<conversation>", turn.task_id: "<task>"})
    steps = _rows(turn.db, "SELECT seq, node, source, subagent, data FROM steps WHERE message_id = ? ORDER BY seq",
                  turn.task_id)
    [message] = _rows(turn.db, "SELECT content, status, input_tokens, output_tokens FROM messages WHERE id = ?",
                      turn.task_id)
    thread = _thread(turn)
    # AI message ids are each runtime's own; the rest are derived or assigned
    # in the same order.
    for rec in thread:
        if rec["role"] == "assistant":
            rec["id"] = "<ai>"
        elif rec["role"] == "tool":
            rec.pop("id", None)
    return norm.value({
        "events": _events(turn),
        "steps": [list(s) for s in steps],
        "message": list(message),
        "thread": thread,
    })


def _requests(python: list[dict], edge: list[dict]) -> None:
    assert len(edge) == len(python)
    for p, e in zip(python, edge):
        _intended_ollama(p, e)
        assert e["messages"] == p["messages"]
        assert [t["function"]["name"] for t in e.get("tools", [])] == [t["function"]["name"] for t in p.get("tools", [])]
        for et, pt in zip(e.get("tools", []), p.get("tools", [])):
            assert et == pt
        assert {k: v for k, v in e.items() if k != "tools"} == {k: v for k, v in p.items() if k != "tools"}


async def _both(twins: Twins, query: str, script: list[Reply], *, hold: int | None = None,
                python_during: Any = None, edge_during: Any = None, **start: Any) -> tuple[dict, dict]:
    python, python_requests = await twins.python(query, script, hold=hold, during=python_during, **start)
    edge, edge_requests = await twins.edge(query, script, hold=hold, during=edge_during, **start)
    _requests(python_requests, edge_requests)
    return _record(python), _record(edge)


# ── scenarios ────────────────────────────────────────────────────────────────


async def test_a_text_reply(twins):
    python, edge = await _both(twins, "Say hello", [Reply("Hello there, friend.")])
    assert edge == python
    assert python["message"][:2] == ["Hello there, friend.", "done"]


async def test_a_plan_kept_with_the_todo_tools(twins):
    script = [
        Reply("", [("write_todos", {"todos": ["Look", "Answer"]})]),
        Reply("", [("set_todo_status", {"index": 0, "status": "done"})]),
        Reply("All done."),
    ]
    python, edge = await _both(twins, "plan then answer", script)
    assert edge == python
    assert [e["kind"] for e in python["events"]].count("TodosUpdatedEvent") == 3


async def test_an_unknown_tool_and_a_good_one(twins):
    script = [
        Reply("Working. ", [("no_such_tool", {"x": 1}), ("write_todos", {"todos": ["One"]})]),
        Reply("Recovered."),
    ]
    python, edge = await _both(twins, "break things", script)
    assert edge == python


async def test_a_notebook_cell(twins):
    script = [Reply("", [("run_cell", {"code": "6 * 7"})]), Reply("It is 42.")]
    python, edge = await _both(twins, "what is six times seven", script)
    assert edge == python
    assert any("42" in (s[4] or "") for s in python["steps"])


async def test_a_long_request_is_told_to_plan(twins):
    query = "Research the three biggest rivers, compare their lengths, then write a short report about them."
    python, edge = await _both(twins, query, [Reply("Planned.")])
    assert edge == python


async def test_a_project_conversation(twins):
    python, edge = await _both(twins, "what do we use", [Reply("Rust.")], project_id="p1")
    assert edge == python


async def test_a_message_queued_mid_run(twins):
    """Sent while the first model call is out: delivered before the second."""
    from db import async_session
    from server.chat_runtime import queue_chat_message

    async def python_queue(task_id: str) -> None:
        async with async_session() as s:
            await queue_chat_message(s, task_id, "also mention Y")

    async def edge_queue(task_id: str) -> None:
        resp = await twins.client.post("/graphql", json={"query": QUEUE, "variables": {
            "taskId": task_id, "query": "also mention Y"}})
        assert "errors" not in resp.json(), resp.json()

    script = [Reply("", [("write_todos", {"todos": ["X"]})]), Reply("X and Y.")]
    python, edge = await _both(twins, "tell me about X", script, hold=0,
                               python_during=python_queue, edge_during=edge_queue)
    assert edge == python
    kinds = [e["kind"] for e in python["events"]]
    assert kinds.index("QueuedMessageEvent") < kinds.index("QueuedConsumedEvent")
    assert [r["content"] for r in python["thread"] if r["role"] == "user"] == ["tell me about X", "also mention Y"]


async def test_a_stop_while_the_model_is_answering(twins):
    from test_edge_runs import _python

    async def python_stop(task_id: str) -> None:
        assert (await _python(STOP, {"id": task_id}))["data"] == {"stopTask": True}

    async def edge_stop(task_id: str) -> None:
        resp = await twins.client.post("/graphql", json={"query": STOP, "variables": {"id": task_id}})
        assert resp.json()["data"] == {"stopTask": True}

    script = [Reply("Never sent.")]
    python, edge = await _both(twins, "a long job", script, hold=0, python_during=python_stop, edge_during=edge_stop)
    assert edge == python
    assert python["message"][:2] == ["", "stopped"]
    assert python["events"][-1]["kind"] == "StoppedEvent"


# ── the handover ─────────────────────────────────────────────────────────────


async def test_a_call_only_python_runs_hands_the_turn_over(twins):
    """`spawn_workers` is Python's: the edge records the call, then releases
    the job with what the turn carried — for a worker, which this test has
    none of, to go on from."""
    script = [Reply("On it. ", [("spawn_workers", {"tasks": [{"task": "x"}]})])]
    edge, _ = await twins.edge_started("delegate it", script)
    [(status, runtime, payload)] = _rows(twins.edge_db, "SELECT status, runtime, payload FROM jobs WHERE id = ?",
                                         edge.task_id)
    assert (status, runtime) == ("pending", None)
    handoff = json.loads(payload)["handoff"]
    assert handoff["text"] == "On it. " and handoff["step_seq"] == 1 and handoff["steps"] == 1
    assert handoff["usage"] == {"input_tokens": 100, "output_tokens": 7, "llm_calls": 1, "tool_calls": 0}
    thread = _thread(edge)
    assert [r["role"] for r in thread] == ["user", "assistant"]
    assert thread[1]["tool_calls"][0]["name"] == "spawn_workers"
    [(msg_status,)] = _rows(twins.edge_db, "SELECT status FROM messages WHERE id = ?", edge.task_id)
    assert msg_status == "running"


# ── the tool schemas ─────────────────────────────────────────────────────────

TOOLS_JSON = REPO / "edge" / "src" / "agent" / "tools.json"


def test_the_edge_binds_pythons_tool_schemas(monkeypatch):
    """`edge/src/agent/tools.json` is Python's own `convert_to_openai_tool`
    output for every tool the main agent can be bound to. Re-export with
    `JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_edge_loop.py -k schemas`
    after changing a bound tool's signature or docstring, then rebuild the edge."""
    import os

    from langchain_core.utils.function_calling import convert_to_openai_tool

    from core import agents, tool_policy

    monkeypatch.setattr(agents, "embeddings_available", lambda: True)
    monkeypatch.setattr(agents, "get_mcp_tools_sync", lambda: [])
    monkeypatch.setattr(tool_policy, "get_policies", lambda force=False: {})
    agents.invalidate_agent_cache()
    try:
        board = agents._build_agent("google_genai:gemma-4-31b-it", None, board=True)
    finally:
        agents.invalidate_agent_cache()
    python = [convert_to_openai_tool(t)["function"] for t in board.tools]
    if os.environ.get("JARVIS_UPDATE_GOLDEN") == "1":
        TOOLS_JSON.write_text(json.dumps(python, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(TOOLS_JSON.read_text(encoding="utf-8")) == python
