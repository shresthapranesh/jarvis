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
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_harness import Normalizer
from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from test_edge_llm import _intended_ollama, _semantics
from test_edge_runs import AUTOMATION, BOARD, CHAT, _python_subscribe

REPO = Path(__file__).resolve().parent.parent
MODEL = "ollama:fake"
START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued } }"""
QUEUE = """mutation($taskId: String!, $query: String!) {
  queueMessage(taskId: $taskId, query: $query) { messageId position } }"""
STOP = "mutation($id: String!) { stopTask(taskId: $id) }"
TRIGGER = "mutation($id: ID!) { triggerAutomation(id: $id) }"


# ── the model ────────────────────────────────────────────────────────────────


# The fake embedder's vocabulary: a text's vector counts these words, over a
# floor that differs per word — no vector is zero, and stored texts never tie
# (a tie's order is float rounding, which numpy and Rust don't share).
VOCAB = ["colour", "green", "rust", "edge", "lunch", "noon", "tea", "coffee", "deploy", "review", "maps", "river"]


def fake_vector(text: str) -> list[float]:
    import re

    words = re.findall(r"[a-z]+", text.lower())
    return [0.01 * (i + 1) + words.count(w) for i, w in enumerate(VOCAB)]


def fake_blob(text: str) -> bytes:
    import numpy as np

    return np.asarray(fake_vector(text), dtype=np.float32).tobytes()


@dataclass
class Reply:
    text: str = ""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class FakeOllama:
    """`/api/chat`, answering the n-th request with `script[n]`, and
    `/api/embed`. Also the Telegram Bot API: `sendMessage` bodies are
    recorded in `telegram`, and the edge's bot gets an empty inbox."""

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[dict[str, Any]] = []
        # Request n waits for `gates[n]` once `arrived[n]` is set.
        self.gates: dict[int, threading.Event] = {}
        self.arrived: dict[int, threading.Event] = {}
        self.embeds: list[list[str]] = []
        self.telegram: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server's spelling
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                if self.path.startswith("/hook"):
                    fake.hooks.append({"method": "POST", "path": self.path, "body": raw.decode(),
                                       "x-token": self.headers.get("x-token")})
                    self.send_response(201)
                    self.end_headers()
                    self.wfile.write(b'{"received": true}')
                    return
                body = json.loads(raw or b"{}")
                if self.path.startswith("/bot"):
                    method = self.path.rsplit("/", 1)[-1]
                    if method == "sendMessage":
                        fake.telegram.append(body)
                        result: Any = {"message_id": len(fake.telegram)}
                    elif method == "getMe":
                        result = {"id": 1, "is_bot": True, "first_name": "Jarvis", "username": "jarvis_bot"}
                    else:
                        time.sleep(0.5)  # a long poll with nothing in it
                        result = []
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True, "result": result}).encode())
                    return
                if self.path == "/api/embed":
                    fake.embeds.append(body["input"])
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"embeddings": [fake_vector(t) for t in body["input"]]}).encode())
                    return
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
        self.script, self.requests, self.telegram, self.hooks = list(script), [], [], []
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
async def twins(request, jarvis, work_dir: Path, tmp_path_factory, fake: FakeOllama, monkeypatch, edge_binary: Path):
    """Python in this process over `work_dir`, and an edge over a copy, both
    pointed at `fake` and at a CDP port nothing listens on. Parametrized
    indirectly, it sets those environment variables in both."""
    from core import agents, doc_index, memory_store
    from db import async_session
    from db.models import ConfigSetting, Conversation, ConversationEpisode, Memory, Project, Skill
    from db.ops import hydrate_catalog

    dead = f"http://127.0.0.1:{_free_port()}"
    # `{fake}` in a value is the fake server's URL.
    extra = {k: v.replace("{fake}", fake.url) for k, v in (getattr(request, "param", None) or {}).items()}
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    # Python's embedder is Ollama's — the fake — as the edge's is.
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    doc_index._embedder_cache.clear()
    doc_index._query_cache.clear()
    memory_store._core_cache.update(text=None, ts=0.0)
    monkeypatch.setenv("JARVIS_BROWSER_CDP_URL", dead)
    agents._browser_probe = (0.0, False)
    agents._retrieval_cache.clear()
    agents.invalidate_agent_cache()
    async with async_session() as s:
        s.add(ConfigSetting(key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}])))
        s.add(Project(id="p1", name="Atlas", description="Maps.", instructions="Be brief.", memory="Uses Rust."))
        for i, text in enumerate(["The user's favourite colour is green", "The user deploys the rust edge",
                                  "Lunch is at noon"]):
            s.add(Memory(id=f"m{i}", kind="fact", text=text, embedding=fake_blob(text)))
        s.add(Memory(id="core1", kind="core", text="The user is called Sam", embedding=fake_blob("sam")))
        # An episode compacted out of an existing conversation.
        s.add(Conversation(id="c-old", title="Old", model=MODEL))
        s.add(ConversationEpisode(id="e1", conversation_id="c-old", text="We chose green for the river maps.",
                                  embedding=fake_blob("green river maps")))
        await s.commit()
        await hydrate_catalog(s)

    edge_dir = tmp_path_factory.mktemp("edge")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(edge_dir / "database.db")) as dst:
        src.backup(dst)
    env = {"JARVIS_AGENT_RUNTIME": "edge", "OLLAMA_HOST": fake.url, "JARVIS_BROWSER_CDP_URL": dead,
           "JARVIS_BOARD_DISPATCH_EVERY": "1",
           "HOME": str(edge_dir), "JARVIS_APP_DIR": str(REPO), **extra}
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
        gql = {{"project_id": "projectId", "conversation_id": "conversationId"}.get(k, k): v for k, v in start.items()}
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

    # ── automations ──────────────────────────────────────────────────────

    async def automation(self, **fields: Any) -> str:
        """An automation in both databases, under one id."""
        from db import async_session
        from db.models import Automation

        auto = Automation(id=fields.pop("id", "auto-1"), name=fields.pop("name", "Daily"), model=MODEL, **fields)
        async with async_session() as s:
            s.add(auto)
            await s.commit()
        _copy(self.python_db, self.edge_db, "automations", auto.id)
        return auto.id

    async def channel(self, channel_id: str, target: str) -> None:
        """A Telegram notification channel in both databases."""
        from db import async_session
        from db.models import NotificationChannel

        async with async_session() as s:
            s.add(NotificationChannel(id=channel_id, name="phone", type="telegram", target=target))
            await s.commit()
        _copy(self.python_db, self.edge_db, "notification_channels", channel_id)

    async def python_automation(self, auto_id: str, script: list[Reply], *, hold: int | None = None,
                                during: Any = None) -> tuple[Turn, list[dict], list[dict]]:
        """Python runs the automation once: `register_automation_run`, then
        its job. Returns the run, the model's requests and the notifications."""
        from db import async_session
        from server.automation_runtime import automation_job_handler, register_automation_run

        self.fake.reset(script, hold)
        async with async_session() as s:
            run_id = await register_automation_run(s, auto_id)
        job = await self.jarvis.queue.claim(kinds=["automation"], worker_id="test", ttl_seconds=600)
        assert job is not None and job.id == run_id
        handler = asyncio.create_task(automation_job_handler(job))
        if during is not None:
            await self._during(run_id, hold, during)
        async with asyncio.timeout(60):
            await handler
        await self.jarvis.queue.complete(job.id, worker_id="test")
        events = await _python_subscribe(AUTOMATION, {"id": run_id})
        return (Turn(run_id, _automation_thread(self.python_db, auto_id, run_id), events, self.python_db),
                list(self.fake.requests), list(self.fake.telegram))

    async def edge_automation(self, auto_id: str, script: list[Reply], *, hold: int | None = None,
                              during: Any = None) -> tuple[Turn, list[dict], list[dict]]:
        """The edge runs it: `triggerAutomation`, then its events to the end."""
        from edge_support import _gid

        self.fake.reset(script, hold)
        resp = await self.client.post("/graphql", json={"query": TRIGGER, "variables": {"id": _gid("Automation", auto_id)}})
        body = resp.json()
        assert "errors" not in body, body
        run_id = body["data"]["triggerAutomation"]
        if during is not None:
            await self._during(run_id, hold, during)
        events = await self.subscribe(run_id, AUTOMATION)
        # Notifications go out before the run's last event.
        return (Turn(run_id, _automation_thread(self.edge_db, auto_id, run_id), events, self.edge_db),
                list(self.fake.requests), list(self.fake.telegram))

    # ── board tasks ──────────────────────────────────────────────────────

    async def board_task(self, *, both: bool = False, parents: tuple[str, ...] = (), **fields: Any) -> str:
        """A board task in Python's database — and the edge's too when
        `both` (a finished parent); a task to run reaches the edge's only
        when its turn comes (`edge_board`), or its dispatcher would start it
        early."""
        from db import async_session
        from db.models import BoardTask, BoardTaskLink

        task = BoardTask(id=fields.pop("id"), title=fields.pop("title", "Task"), model=MODEL, **fields)
        async with async_session() as s:
            s.add(task)
            for i, parent in enumerate(parents):
                s.add(BoardTaskLink(id=f"{parent}->{task.id}", parent_id=parent, child_id=task.id))
            await s.commit()
        if both:
            _copy(self.python_db, self.edge_db, "board_tasks", task.id)
        return task.id

    async def python_board(self, task_id: str, script: list[Reply], *, hold: int | None = None,
                           during: Any = None) -> tuple[Turn, list[dict]]:
        """Python's dispatcher starts the task, and its handler runs it."""
        from server.task_board_runtime import board_task_job_handler, dispatch_board_tasks

        self.fake.reset(script, hold)
        assert await dispatch_board_tasks() == 1
        job = await self.jarvis.queue.claim(kinds=["board_task"], worker_id="test", ttl_seconds=600)
        assert job is not None
        handler = asyncio.create_task(board_task_job_handler(job))
        if during is not None:
            await self._during(task_id, hold, during)
        async with asyncio.timeout(60):
            await handler
        await self.jarvis.queue.complete(job.id, worker_id="test")
        events = await _python_subscribe(BOARD, {"id": job.id})
        return Turn(job.id, f"boardtask_{task_id}", events, self.python_db), list(self.fake.requests)

    async def edge_board(self, task_id: str, before: tuple, script: list[Reply], *, hold: int | None = None,
                         during: Any = None) -> tuple[Turn, list[dict]]:
        """The task as it was before Python's run (`before`), in the edge's
        database; the edge's dispatcher (every second here) starts it."""
        self.fake.reset(script, hold)
        _sync_board(self.python_db, self.edge_db, task_id, before)
        # The synced row has no job; the dispatch gives it one. A quick run
        # may be over by the next look, so its status isn't waited on.
        run_id = None
        for _ in range(400):
            [(run_id,)] = _rows(self.edge_db, "SELECT job_id FROM board_tasks WHERE id = ?", task_id)
            if run_id:
                break
            await asyncio.sleep(0.05)
        assert run_id, "the edge never dispatched the task"
        if during is not None:
            await self._during(task_id, hold, during)
        events = await self.subscribe(run_id, BOARD)
        return Turn(run_id, f"boardtask_{task_id}", events, self.edge_db), list(self.fake.requests)

    async def _during(self, run_id: str, hold: int | None, during: Any) -> None:
        """`during(run_id)` while the held request waits — or, for a run that
        never calls the model, once it has had a moment to start."""
        if hold is None:
            await asyncio.sleep(1.5)
            await during(run_id)
            return
        await self.fake.held()
        await during(run_id)
        self.fake.release()

    async def subscribe(self, task_id: str, query: str = CHAT) -> list[dict]:
        import websockets

        port = self.client.base_url.port
        async with websockets.connect(f"ws://127.0.0.1:{port}/graphql", subprotocols=["graphql-transport-ws"]) as ws:
            await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
            assert json.loads(await ws.recv())["type"] == "connection_ack"
            await ws.send(json.dumps({"id": "1", "type": "subscribe",
                                      "payload": {"query": query, "variables": {"id": task_id}}}))
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


def _copy(src: Path, dst: Path, table: str, row_id: str) -> None:
    """One row, every column, from one database into the other."""
    with contextlib.closing(sqlite3.connect(src)) as a, contextlib.closing(sqlite3.connect(dst)) as b:
        cur = a.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        cols = [d[0] for d in cur.description]
        b.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", cur.fetchone())
        b.commit()


_BOARD_COLS = ("title", "body", "status", "priority", "created_by", "model", "skill", "pending_answer", "failure_count",
               "summary", "result_metadata", "blocked_reason", "blocked_kind", "created_at")


def _board_row(db: Path, task_id: str) -> tuple:
    [row] = _rows(db, f"SELECT {', '.join(_BOARD_COLS)} FROM board_tasks WHERE id = ?", task_id)
    return row


def _sync_board(src: Path, dst: Path, task_id: str, row: tuple) -> None:
    """The task as `row` has it in the edge's database, with its links."""
    with contextlib.closing(sqlite3.connect(dst)) as b:
        b.execute(f"INSERT OR REPLACE INTO board_tasks (id, {', '.join(_BOARD_COLS)}, updated_at) "
                  f"VALUES (?, {', '.join('?' * len(_BOARD_COLS))}, '2026-10-04 00:00:00.000000')", (task_id, *row))
        for (link_id, parent, child, created) in _rows(src, "SELECT id, parent_id, child_id, created_at FROM "
                                                            "board_task_links WHERE child_id = ?", task_id):
            b.execute("INSERT OR IGNORE INTO board_task_links (id, parent_id, child_id, created_at) VALUES (?, ?, ?, ?)",
                      (link_id, parent, child, created))
        b.commit()


def _automation_thread(db: Path, auto_id: str, run_id: str) -> str:
    """`_is_stateful_prompt` picks the conversation's thread, else the run's."""
    [(input_type, stateful)] = _rows(db, "SELECT input_type, stateful FROM automations WHERE id = ?", auto_id)
    return f"automation_{auto_id}" if input_type == "monitor" or stateful else f"automation_{run_id}"


def _events(turn: Turn) -> list[dict]:
    """The subscriber's events, without the wall-clock ones, tokens merged."""
    out: list[dict] = []
    for item in turn.events:
        [ev] = item["data"].values()
        ev = dict(ev)
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
        elif rec["role"] == "system":
            rec["id"] = "<summary>"
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


# ── retrieval ────────────────────────────────────────────────────────────────


async def test_memories_retrieved_for_the_request(twins):
    python, edge = await _both(twins, "which colour do I like best", [Reply("Green.")])
    assert edge == python
    prompt = twins.fake.requests[0]["messages"]
    assert any("## Relevant Memories" in m["content"] and "favourite colour" in m["content"] for m in prompt)
    activities = _rows(twins.edge_db, "SELECT memory_id, source FROM memory_activities")
    assert activities == _rows(twins.python_db, "SELECT memory_id, source FROM memory_activities")
    assert ("m0", "retrieval") in activities


async def test_a_greeting_retrieves_nothing(twins):
    python, edge = await _both(twins, "hi", [Reply("Hello.")])
    assert edge == python
    assert twins.fake.embeds == []


async def test_remember(twins):
    script = [Reply("", [("remember", {"text": "The user drinks tea"})]),
              Reply("", [("remember", {"text": "The user's favourite colour is green", "kind": "fact"})]),
              Reply("Noted.")]
    python, edge = await _both(twins, "remember I drink tea", script)
    assert edge == python
    memories = "SELECT kind, text FROM memories ORDER BY kind, text"
    assert _rows(twins.edge_db, memories) == _rows(twins.python_db, memories)
    # The colour fact merged into the one already there.
    assert len(_rows(twins.edge_db, "SELECT 1 FROM memories WHERE text LIKE '%colour%'")) == 1


async def test_a_large_skill_catalog_is_ranked(twins):
    from db import async_session
    from db.models import Skill

    names = ["deploy-app", "review-code", "brew-tea", "make-coffee", "draw-maps", "river-facts", "lunch-plan",
             "colour-pick", "rust-help", "edge-ops"]
    rows = [Skill(id=f"s{i}", name=n, description=f"How to {n.replace('-', ' ')}", body="...",
                  embedding=fake_blob(n.replace("-", " "))) for i, n in enumerate(names)]
    async with async_session() as s:
        s.add_all(rows)
        await s.commit()
    with contextlib.closing(sqlite3.connect(twins.edge_db)) as c:
        c.executemany("INSERT INTO skills (id, name, description, body, enabled, embedding, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, 1, ?, '2026-10-04 00:00:00.000000', '2026-10-04 00:00:00.000000')",
                      [(r.id, r.name, r.description, r.body, r.embedding) for r in rows])
        c.commit()
    python, edge = await _both(twins, "how should I brew tea or coffee", [Reply("Steep it.")])
    assert edge == python
    # Uncached (Ollama): the volatile sections are in the one system message.
    system = twins.fake.requests[0]["messages"][0]["content"]
    shortlist = system.split("## Available Skills", 1)[1]
    assert shortlist.count("- **") == 5 and "brew-tea" in shortlist


async def test_an_earlier_episode_is_recalled(twins):
    python, edge = await _both(twins, "what colour did we pick for the river maps", [Reply("Green.")],
                               conversation_id="c-old")
    assert edge == python
    assert "## Earlier in this conversation" in twins.fake.requests[0]["messages"][0]["content"]


# ── summarizing ──────────────────────────────────────────────────────────────


def _note(n: int) -> Reply:
    """A step that says a lot (600 characters, ~150 tokens by the
    heuristic) and keeps the turn going."""
    return Reply(f"Note {n}: " + "lorem " * 98, [("write_todos", {"todos": [f"note {n}"]})])


@pytest.mark.parametrize("twins", [{"JARVIS_COMPACT_TOKEN_THRESHOLD": "300"}], indirect=True)
async def test_a_long_turn_is_summarized_as_it_goes(twins):
    """At 300 tokens the fourth step summarizes the first call away, and the
    fifth the second, merging it into the summary — each summarizer call
    to the turn's own model (requests 4, 6 and 7), each evicted stretch
    kept as an episode."""
    script = [_note(1), _note(2), _note(3), Reply("Earlier: note one."), _note(4),
              Reply("Then: note two."), Reply("Notes one and two."), Reply("All noted.")]
    python, edge = await _both(twins, "take notes as you go", script)
    assert edge == python

    requests = twins.fake.requests
    assert [r["messages"][0]["content"].split(".")[0] for r in (requests[3], requests[5], requests[6])] == [
        "Summarize the following conversation history concisely",
        "Summarize the following conversation history concisely",
        "You have an existing conversation summary and a new chunk summary"]
    assert "tools" not in requests[3]
    # The last call sees the merged summary, and only the newest calls.
    assert "[Conversation summary]\nNotes one and two." in requests[7]["messages"][0]["content"]
    assert "Note 1:" not in json.dumps(requests[7]) and "Note 2:" not in json.dumps(requests[7])
    [summary] = [r for r in python["thread"] if r["role"] == "system"]
    assert summary["content"] == "[Conversation summary]\nNotes one and two."

    episodes = "SELECT text, embedding FROM conversation_episodes WHERE conversation_id != 'c-old' ORDER BY text"
    assert _rows(twins.edge_db, episodes) == _rows(twins.python_db, episodes)
    assert [t for t, _ in _rows(twins.edge_db, episodes)] == ["Earlier: note one.", "Then: note two."]


# ── automations ──────────────────────────────────────────────────────────────


def _automation_record(turn: Turn, auto_id: str) -> dict[str, Any]:
    norm = Normalizer({turn.task_id: "<run>", auto_id: "<automation>"})
    [run] = _rows(turn.db, "SELECT status, output, error, triggered_by FROM automation_runs WHERE id = ?", turn.task_id)
    thread = _thread(turn)
    for rec in thread:
        rec["id"] = f"<{rec['role']}>"
    return norm.value({
        "events": _events(turn),
        "run": list(run),
        "thread": thread,
        "conversation": [list(r) for r in _rows(
            turn.db, "SELECT role, content, status FROM messages WHERE conversation_id = ? ORDER BY created_at",
            f"automation_{auto_id}")],
        "steps": _rows(turn.db, "SELECT count(*) FROM steps WHERE message_id = ?", turn.task_id),
    })


async def _both_automation(twins: Twins, auto_id: str, script: list[Reply], *, hold: int | None = None,
                           python_during: Any = None, edge_during: Any = None) -> tuple[dict, dict, list, list]:
    python, python_requests, python_sent = await twins.python_automation(auto_id, script, hold=hold, during=python_during)
    edge, edge_requests, edge_sent = await twins.edge_automation(auto_id, script, hold=hold, during=edge_during)
    _requests(python_requests, edge_requests)
    [(runtime,)] = _rows(twins.edge_db, "SELECT runtime FROM jobs WHERE id = ?", edge.task_id)
    assert runtime == "edge"
    return _automation_record(python, auto_id), _automation_record(edge, auto_id), python_sent, edge_sent


async def test_a_prompt_automation(twins):
    auto = await twins.automation(input_type="prompt", prompt_text="what is six times seven")
    script = [Reply("", [("run_cell", {"code": "6 * 7"})]), Reply("It is 42.")]
    python, edge, _, _ = await _both_automation(twins, auto, script)
    assert edge == python
    assert python["run"] == ["done", "It is 42.", None, "manual"]
    assert python["steps"] == [(0,)] and python["conversation"] == []


async def test_a_stateful_automation_keeps_its_conversation(twins):
    """Two runs: the second sees the first in its thread, and the
    conversation holds both prompts and both replies."""
    auto = await twins.automation(input_type="prompt", prompt_text="note the weather", stateful=True)
    for answer in ["Sunny.", "Rain now."]:
        python, edge, _, _ = await _both_automation(twins, auto, [Reply(answer)])
        assert edge == python
    assert [r[:2] for r in python["conversation"]] == [
        ["user", "note the weather"], ["assistant", "Sunny."], ["user", "note the weather"], ["assistant", "Rain now."]]
    assert "Sunny." in json.dumps(twins.fake.requests[0]["messages"])


@pytest.mark.parametrize("twins", [{"TELEGRAM_BOT_TOKEN": "t0k", "TELEGRAM_API_URL": "{fake}"}], indirect=True)
async def test_a_monitor_only_speaks_when_something_changed(twins):
    await twins.channel("ch-1", "4242")
    auto = await twins.automation(input_type="monitor", prompt_text="the blog's post count",
                                  notifications=json.dumps([{"id": "ch-1", "on": "both"}]))
    python, edge, python_sent, edge_sent = await _both_automation(twins, auto, [Reply("Baseline: 3 posts.")])
    assert edge == python and edge_sent == python_sent
    assert python_sent == [{"chat_id": "4242", "text": "Daily\n\nBaseline: 3 posts."}]
    assert twins.fake.requests[0]["messages"][-1]["content"].endswith("Target to monitor:\nthe blog's post count")

    python, edge, python_sent, edge_sent = await _both_automation(twins, auto, [Reply("**NO_CHANGE**\nstill 3")])
    assert edge == python
    assert python["run"][0] == "no_change" and python_sent == edge_sent == []


async def test_an_automation_stopped_while_the_model_answers(twins):
    from test_edge_runs import _python

    stop = "mutation($id: String!) { stopAutomationRun(runId: $id) }"

    async def python_stop(run_id: str) -> None:
        assert (await _python(stop, {"id": run_id}))["data"] == {"stopAutomationRun": True}

    async def edge_stop(run_id: str) -> None:
        resp = await twins.client.post("/graphql", json={"query": stop, "variables": {"id": run_id}})
        assert resp.json()["data"] == {"stopAutomationRun": True}

    auto = await twins.automation(input_type="prompt", prompt_text="a long job", stateful=True)
    python, edge, _, _ = await _both_automation(twins, auto, [Reply("Never sent.")], hold=0,
                                                python_during=python_stop, edge_during=edge_stop)
    assert edge == python
    assert python["run"][0] == "stopped" and python["events"][-1]["kind"] == "AutomationStoppedEvent"


async def test_an_automation_hands_over_a_call_only_python_runs(twins):
    from edge_support import _gid

    auto = await twins.automation(input_type="prompt", prompt_text="delegate it", stateful=True)
    twins.fake.reset([Reply("On it. ", [("spawn_workers", {"tasks": [{"task": "x"}]})])])
    resp = await twins.client.post("/graphql", json={"query": TRIGGER, "variables": {"id": _gid("Automation", auto)}})
    run_id = resp.json()["data"]["triggerAutomation"]
    for _ in range(200):
        [(runtime, status, payload)] = _rows(twins.edge_db, "SELECT runtime, status, payload FROM jobs WHERE id = ?", run_id)
        if runtime is None:
            break
        await asyncio.sleep(0.05)
    assert (runtime, status) == (None, "pending")
    assert json.loads(payload)["handoff"]["text"] == "On it. "
    # The prompt is in the conversation once; Python won't write it again.
    assert _rows(twins.edge_db, "SELECT role, content FROM messages WHERE conversation_id = ?",
                 f"automation_{auto}") == [("user", "delegate it")]
    assert _rows(twins.edge_db, "SELECT status FROM automation_runs WHERE id = ?", run_id) == [("running",)]


async def test_a_code_automation(twins):
    """Its output, stderr included, line by line; a failing exit is still
    `done` — Python never looks at the exit code."""
    code = "import sys\nprint('one')\nsys.stdout.flush()\nprint('two', file=sys.stderr)\nraise SystemExit(3)\n"
    auto = await twins.automation(input_type="code", code_text=code)
    python, edge, _, _ = await _both_automation(twins, auto, [])
    assert edge == python
    assert python["run"][:2] == ["done", "one\ntwo"]
    assert [e["text"] for e in python["events"] if e["kind"] == "TokenEvent"] == ["one\ntwo\n"]


async def test_a_code_automation_stopped_mid_run(twins):
    """Terminated where it was: what it printed reached the subscriber, and
    the run ends stopped with no output, as Python's cancelled run does."""
    from test_edge_runs import _python

    stop = "mutation($id: String!) { stopAutomationRun(runId: $id) }"

    async def python_stop(run_id: str) -> None:
        assert (await _python(stop, {"id": run_id}))["data"] == {"stopAutomationRun": True}

    async def edge_stop(run_id: str) -> None:
        resp = await twins.client.post("/graphql", json={"query": stop, "variables": {"id": run_id}})
        assert resp.json()["data"] == {"stopAutomationRun": True}

    auto = await twins.automation(input_type="code", code_text="import time\nprint('started', flush=True)\ntime.sleep(30)\n")
    python, edge, _, _ = await _both_automation(twins, auto, [], python_during=python_stop, edge_during=edge_stop)
    assert edge == python
    assert python["run"][:2] == ["stopped", None]
    assert [e["kind"] for e in python["events"]] == ["TokenEvent", "AutomationStoppedEvent"]


async def test_a_webhook_automation(twins):
    auto = await twins.automation(input_type="webhook", webhook_url=f"{twins.fake.url}/hook?x=1",
                                  webhook_headers=json.dumps({"x-token": "s3cret"}), webhook_body='{"ping": 1}')
    python, edge, _, _ = await _both_automation(twins, auto, [])
    assert edge == python
    assert python["run"][:2] == ["done", 'HTTP 201\n{"received": true}']
    # One call each, the same call.
    assert twins.fake.hooks == [{"method": "POST", "path": "/hook?x=1", "body": '{"ping": 1}', "x-token": "s3cret"}]


# ── board tasks ──────────────────────────────────────────────────────────────


def _board_record(turn: Turn, task_id: str) -> dict[str, Any]:
    norm = Normalizer({turn.task_id: "<run>"})
    [task] = _rows(turn.db, "SELECT status, summary, result_metadata, blocked_reason, blocked_kind, pending_answer, "
                            "failure_count, job_id FROM board_tasks WHERE id = ?", task_id)
    thread = _thread(turn)
    for rec in thread:
        rec["id"] = f"<{rec['role']}>"
    return norm.value({
        "events": _events(turn),
        "task": list(task),
        "thread": thread,
        "conversation": [list(r) for r in _rows(
            turn.db, "SELECT role, content, status FROM messages WHERE conversation_id = ? ORDER BY created_at",
            turn.conversation_id)],
        "questions": _rows(turn.db, "SELECT source, kind, status, question, label, parent_id, board_task_id "
                                    "FROM approvals WHERE board_task_id = ? ORDER BY requested_at", task_id),
    })


async def _both_board(twins: Twins, task_id: str, script: list[Reply], *, before_edge: Any = None,
                      **kw: Any) -> tuple[dict, dict]:
    python_during, edge_during = kw.pop("python_during", None), kw.pop("edge_during", None)
    before = _board_row(twins.python_db, task_id)
    python, python_requests = await twins.python_board(task_id, script, during=python_during, **kw)
    if before_edge is not None:
        before_edge()
    edge, edge_requests = await twins.edge_board(task_id, before, script, during=edge_during, **kw)
    _requests(python_requests, edge_requests)
    [(runtime,)] = _rows(twins.edge_db, "SELECT runtime FROM jobs WHERE id = ?", edge.task_id)
    assert runtime == "edge"
    return _board_record(python, task_id), _board_record(edge, task_id)


async def test_a_board_task_with_handoffs_completes_itself(twins):
    """The prompt carries its skill and the finished parent's handoff; the
    agent's complete_task sets the summary its final reply then doesn't.

    Departure, by name: `metadata` is left out. langchain-ollama parses any
    tool argument that is itself a JSON string, so Python hands
    complete_task a dict, which it rejects; the edge passes the string the
    model wrote."""
    parent = await twins.board_task(id="p1", title="Gather", status="done", summary="Found 3 rivers.",
                                    result_metadata='{"n": 3}', both=True)
    task = await twins.board_task(id="t1", title="Report", body="Write it up.", status="ready", skill="writer",
                                  parents=(parent,))
    script = [Reply("Writing. ", [("complete_task", {"summary": "Report written."})]), Reply("Done.")]
    python, edge = await _both_board(twins, task, script)
    assert edge == python
    assert python["task"][:3] == ["done", "Report written.", None]
    prompt = twins.fake.requests[0]["messages"][1]["content"]
    assert "use_skill('writer')" in prompt and "### Gather\nFound 3 rivers.\nMetadata: {\"n\": 3}" in prompt


async def test_a_board_task_asks_then_resumes_with_the_answer(twins):
    task = await twins.board_task(id="t2", title="Pick", body="Choose a colour.", status="ready")
    ask = [Reply("", [("block_task", {"reason": "Which colour?", "needs_input": True})]), Reply("Asked.")]
    python, edge = await _both_board(twins, task, ask)
    assert edge == python
    assert python["task"][0] == "blocked" and python["task"][4] == "needs_input"
    assert [q[:4] for q in python["questions"]] == [("board_task", "input", "pending", "Which colour?")]

    # The answer, as answerBoardTask leaves it: ready again, answer waiting.
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute("UPDATE board_tasks SET status = 'ready', pending_answer = 'Green' WHERE id = ?", (task,))
            c.commit()
    resume = [Reply("", [("complete_task", {"summary": "Green it is."})]), Reply("Picked.")]
    python, edge = await _both_board(twins, task, resume)
    assert edge == python
    assert python["task"][:2] == ["done", "Green it is."] and python["task"][5] is None
    assert 'The user has answered:\n\nGreen' in twins.fake.requests[0]["messages"][-1]["content"]
    # Done: the question left the inbox.
    assert [q[2] for q in python["questions"]] == ["cancelled"]


async def test_a_board_task_without_its_tools_finishes_with_its_reply(twins):
    task = await twins.board_task(id="t3", title="Note", status="ready")
    python, edge = await _both_board(twins, task, [Reply("Noted it.")])
    assert edge == python
    assert python["task"][:2] == ["done", "Noted it."]


async def test_a_board_task_stopped_through_its_job(twins):
    """Python's stopBoardTask only flags the job for a run it doesn't hold;
    the edge sees the flag within its poll and stops the run."""
    from server.task_board_runtime import stop_board_task

    async def python_stop(task_id: str) -> None:
        assert await stop_board_task(task_id) is True

    async def edge_stop(task_id: str) -> None:
        with contextlib.closing(sqlite3.connect(twins.edge_db)) as c:
            c.execute("UPDATE jobs SET cancel_requested = 1 WHERE kind = 'board_task' AND status = 'running'")
            c.commit()
        await asyncio.sleep(6)  # past the edge's poll

    task = await twins.board_task(id="t4", title="Long", status="ready")
    python, edge = await _both_board(twins, task, [Reply("Never sent.")], hold=0,
                                     python_during=python_stop, edge_during=edge_stop)
    assert edge == python
    assert python["task"][:5] == ["blocked", None, None, "stopped by user", "stopped"]


async def test_a_board_task_stopped_from_the_board(twins):
    """The edge's stopBoardTask on its own run: the turn stops at once, not
    at the next poll of the job, and ends as Python's does."""
    from edge_support import _gid
    from server.task_board_runtime import stop_board_task

    async def python_stop(task_id: str) -> None:
        assert await stop_board_task(task_id) is True

    async def edge_stop(task_id: str) -> None:
        q = "mutation($id: ID!) { stopBoardTask(id: $id) }"
        resp = await twins.client.post("/graphql", json={"query": q, "variables": {"id": _gid("BoardTask", task_id)}})
        assert resp.json() == {"data": {"stopBoardTask": True}}

    task = await twins.board_task(id="t5", title="Long", status="ready")
    python, edge = await _both_board(twins, task, [Reply("Never sent.")], hold=0,
                                     python_during=python_stop, edge_during=edge_stop)
    assert edge == python
    assert python["task"][:5] == ["blocked", None, None, "stopped by user", "stopped"]
    resp = await twins.client.post("/graphql", json={"query": "mutation($id: ID!) { stopBoardTask(id: $id) }",
                                                     "variables": {"id": _gid("BoardTask", task)}})
    assert resp.json()["errors"][0]["message"] == "task is not running"


# ── artifacts ────────────────────────────────────────────────────────────────


def _artifacts(db: Path, norm: Normalizer) -> dict[str, Any]:
    """The artifact rows, version rows and files a turn left, the artifact
    directory named as such."""
    art_dir = db.parent / "artifacts"
    norm = Normalizer({**norm._map, str(art_dir.resolve()): "<dir>", str(art_dir): "<dir>"})
    files = {p.name: p.read_bytes() for p in sorted(art_dir.glob("*"))} if art_dir.exists() else {}
    return norm.value({
        "artifacts": [list(r) for r in _rows(db, "SELECT id, title, filename, kind, mime_type, conversation_id, "
                                                 "message_id FROM artifacts ORDER BY rowid")],
        "versions": [list(r) for r in _rows(db, "SELECT artifact_id, version, title, filename FROM artifact_versions "
                                                "ORDER BY rowid")],
        # Bytes aren't text to the normalizer: name → contents, names normalized.
        "files": {norm.text(name): data.decode("utf-8", "replace") for name, data in files.items()},
    })


async def _both_artifacts(twins: Twins, query: str, script: list[Reply]) -> tuple[dict, dict]:
    python, python_requests = await twins.python(query, script)
    edge, edge_requests = await twins.edge(query, script)
    # Each side mints its own artifact ids, which the model reads back.
    _requests(Normalizer().value(python_requests), Normalizer().value(edge_requests))
    [(runtime,)] = _rows(twins.edge_db, "SELECT runtime FROM jobs WHERE id = ?", edge.task_id)
    assert runtime == "edge", "the edge handed the turn over"
    out = []
    for turn in (python, edge):
        norm = Normalizer({turn.conversation_id: "<conversation>", turn.task_id: "<task>"})
        record = _record(turn)
        out.append({**record, **_artifacts(turn.db, Normalizer(dict(norm._map)))})
    return out[0], out[1]


async def test_a_markdown_artifact(twins):
    body = "# Rivers\n\n" + "The Nile is long. " * 30
    script = [Reply("Writing it up. ", [("write_artifact", {"title": "Rivers", "content": body})]), Reply("Saved.")]
    python, edge = await _both_artifacts(twins, "write a report on rivers", script)
    assert edge == python
    [event] = [e for e in python["events"] if "artifactId" in e]
    assert (event["action"], event["kind"], len(event["preview"])) == ("created", "markdown", 300)
    assert python["artifacts"][0][5:] == ["<conversation>", "<task>"]
    assert sorted(python["files"]) == ["<id1>.md", "<id1>_v1.md"]


async def test_file_artifacts_and_refusals(twins, tmp_path):
    """A file the agent wrote, copied in with its type guessed; and the two
    refusals the tool words itself."""
    song = tmp_path / "theme.MP3"
    song.write_bytes(b"ID3\x04fake audio")
    script = [
        Reply("", [
            ("write_artifact", {"title": "Both", "content": "x", "file_path": str(song)}),
            ("write_artifact", {"title": "Missing", "file_path": str(tmp_path / "gone.png")}),
            ("write_artifact", {"title": "Theme", "file_path": str(song), "content": None}),
        ]),
        Reply("Here it is."),
    ]
    python, edge = await _both_artifacts(twins, "save the theme song", script)
    assert edge == python
    [event] = [e for e in python["events"] if "artifactId" in e]
    assert event["kind"] == "audio" and event["preview"] == "[audio · audio/mpeg · 14 bytes]"
    assert python["artifacts"][0][3:5] == ["audio", "audio/mpeg"]


async def test_an_artifact_from_before_versioning_is_updated(twins):
    """Its file becomes v1 under its old title, the new body v2; an unknown
    id is refused."""
    for db in (twins.python_db, twins.edge_db):
        art_dir = (db.parent / "artifacts").resolve()
        art_dir.mkdir(exist_ok=True)
        (art_dir / "a-old.md").write_bytes(b"old\r\nbody")
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute("INSERT INTO artifacts (id, title, filename, kind, created_at, updated_at) VALUES "
                      "('a-old', 'Draft', ?, 'markdown', '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')",
                      (str(art_dir / "a-old.md"),))
            c.commit()
    script = [
        Reply("", [("write_artifact", {"title": "Final", "content": "new body", "artifact_id": "a-old"}),
                   ("write_artifact", {"title": "x", "content": "y", "artifact_id": "nope"})]),
        Reply("Updated."),
    ]
    python, edge = await _both_artifacts(twins, "finish the draft", script)
    assert edge == python
    assert python["versions"] == [["a-old", 1, "Draft", "<dir>/a-old_v1.md"], ["a-old", 2, "Final", "<dir>/a-old_v2.md"]]
    assert python["files"]["a-old_v1.md"] == "old\nbody"


MIMETYPES_JSON = REPO / "edge" / "src" / "mimetypes.json"


def test_the_edge_guesses_file_types_as_python_does(edge_binary):
    """`edge/src/mimetypes.json` is Python's built-in table (re-export with
    `JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_edge_loop.py -k mimetypes`,
    then rebuild); the edge reads the system's mime.types files on top, as
    Python does."""
    import mimetypes
    import os
    import subprocess

    defaults = {"types": mimetypes._types_map_default, "suffixes": mimetypes._suffix_map_default,
                "encodings": mimetypes._encodings_map_default}
    if os.environ.get("JARVIS_UPDATE_GOLDEN") == "1":
        MIMETYPES_JSON.write_text(json.dumps(defaults, indent=1) + "\n")
    assert json.loads(MIMETYPES_JSON.read_text()) == defaults

    names = ["a.png", "A.PNG", "x.mp3", "clip.MP4", "doc.pdf", "t.csv", "page.html", "a.tar.gz", "a.tgz",
             "a.svgz", "a.Z", "a.gz", "data.json", "notes.md", "s.svg", "w.webp", "noext", ".bashrc", "a.",
             "..x", "f.weird", "img.jpeg", "m.m4a", "o.ogg", "f.flac", "v.mov", "v.webm", "x.xlsx", "x.docx",
             "report:final.pdf", "x.py", "x.yaml", "font.woff2", "a.wasm", "song.aac", "c.heic"]
    out = subprocess.run([str(edge_binary), "--guess-type"], input="\n".join(names) + "\n",
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert dict(zip(names, map(json.loads, out))) == {n: mimetypes.guess_type(n)[0] for n in names}


# ── approvals ────────────────────────────────────────────────────────────────

RESOLVE = "mutation($id: String!, $a: String!) { resolveApproval(id: $id, answer: $a) { id status result } }"
REQUEST = ("mutation($k: String!, $t: String!, $a: String!, $c: String) { requestToolApproval(toolKey: $k, "
           "tool: $t, argsJson: $a, conversationId: $c) { id status } }")
_APPROVAL_COLS = ("source, kind, status, question, label, tool, args_json, task_id, parent_id, board_task_id, "
                  "action, action_payload, result, answer")


async def _python_gql(query: str, variables: dict, *, caller: str = "human", conversation: str | None = None) -> dict:
    from db import async_session
    from server.graphql.extensions import SESSION_LOCK_KEY
    from server.graphql.schema import schema

    async with async_session() as s:
        res = await schema.execute(query, variable_values=variables, context_value={
            "session": s, SESSION_LOCK_KEY: asyncio.Lock(), "caller": caller, "caller_conversation_id": conversation})
    out: dict = {"data": res.data}
    if res.errors:
        out["errors"] = [e.message for e in res.errors]
    return out


async def _edge_gql(twins: Twins, query: str, variables: dict, headers: dict | None = None) -> dict:
    resp = await twins.client.post("/graphql", json={"query": query, "variables": variables}, headers=headers or {})
    assert resp.status_code == 200, f"proxied ({resp.status_code})"
    body = resp.json()
    out: dict = {"data": body.get("data")}
    if body.get("errors"):
        out["errors"] = [e["message"] for e in body["errors"]]
    return out


def _approvals(db: Path, norm: Normalizer) -> list:
    return norm.value([list(r) for r in _rows(db, f"SELECT {_APPROVAL_COLS} FROM approvals ORDER BY rowid")])


async def _gate_run_cell(twins: Twins) -> None:
    """`bound:run_cell` needs a human's yes, in both databases."""
    from core import tool_policy
    from db import async_session

    async with async_session() as s:
        await tool_policy.set_tool_policy(s, "bound:run_cell", approval=True)
    with contextlib.closing(sqlite3.connect(twins.python_db)) as a, contextlib.closing(sqlite3.connect(twins.edge_db)) as b:
        cur = a.execute("SELECT * FROM config_settings WHERE key = 'tools.policy'")
        cols = [d[0] for d in cur.description]
        b.execute(f"INSERT INTO config_settings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", cur.fetchone())
        b.commit()


async def _answer_when_asked(db: Path, answer: Any) -> None:
    """Answer the first gate that opens, as the chat prompt would."""
    async with asyncio.timeout(30):
        while not (rows := _rows(db, "SELECT id FROM approvals WHERE source = 'tool' AND status = 'pending'")):
            await asyncio.sleep(0.05)
    await answer(rows[0][0])


async def _both_gated(twins: Twins, query: str, script: list[Reply], answer: str) -> tuple[dict, dict]:
    async def python_answer(approval_id: str) -> None:
        out = await _python_gql(RESOLVE, {"id": approval_id, "a": answer})
        assert "errors" not in out, out

    async def edge_answer(approval_id: str) -> None:
        out = await _edge_gql(twins, RESOLVE, {"id": approval_id, "a": answer})
        assert "errors" not in out, out

    records = []
    for run, db, answerer in ((twins.python, twins.python_db, python_answer), (twins.edge, twins.edge_db, edge_answer)):
        answering = asyncio.create_task(_answer_when_asked(db, answerer))
        turn, requests = await run(query, script)
        await answering
        norm = Normalizer({turn.conversation_id: "<conversation>", turn.task_id: "<task>"})
        records.append(({**_record(turn), "approvals": _approvals(db, norm)}, requests))
    (python, python_requests), (edge, edge_requests) = records
    _requests(python_requests, edge_requests)
    [(runtime,)] = _rows(twins.edge_db, "SELECT runtime FROM jobs WHERE kind = 'chat' ORDER BY rowid DESC LIMIT 1")
    assert runtime == "edge", "the edge handed the turn over"
    return python, edge


async def test_a_gated_call_runs_once_approved(twins):
    await _gate_run_cell(twins)
    script = [Reply("Checking. ", [("run_cell", {"code": "6 * 7"})]), Reply("It is 42.")]
    python, edge = await _both_gated(twins, "what is six times seven", script, "Approve")
    assert edge == python
    kinds = [e["kind"] for e in python["events"]]
    assert "ApprovalRequestEvent" in kinds and "ApprovalResolvedEvent" in kinds
    [row] = python["approvals"]
    assert row[:3] == ["tool", "approval", "approved"] and row[7] == "<task>" and row[13] == "Approve"
    assert any("42" in (s[4] or "") for s in python["steps"])


async def test_a_denied_call_is_answered_not_run(twins):
    await _gate_run_cell(twins)
    script = [Reply("", [("run_cell", {"code": "open('x', 'w')"})]), Reply("Understood, I won't.")]
    python, edge = await _both_gated(twins, "make a file", script, "no thanks")
    assert edge == python
    denial = [r for r in python["thread"] if r["role"] == "tool"][0]
    assert denial["status"] == "error" and denial["content"].startswith("Denied by a human (no thanks): `run_cell`")


FIXTURES = Path(__file__).parent / "fixtures"
MCP_SERVERS = {
    "echo": {"command": sys.executable, "args": [str(FIXTURES / "echo_mcp_server.py")], "transport": "stdio"},
    "other": {"command": sys.executable, "args": [str(FIXTURES / "other_mcp_server.py")], "transport": "stdio",
              "x-jarvis-load": "lazy"},
}


async def _mcp(twins: Twins, monkeypatch) -> None:
    """`echo` bound (`always`), `other` advertised (`lazy`), on both sides."""
    from core import agents
    from core.mcp import McpManager

    value = json.dumps(MCP_SERVERS)
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute("INSERT INTO config_settings (key, value, updated_at) VALUES ('mcp.servers', ?, "
                      "'2026-01-01 00:00:00')", (value,))
            c.commit()
    mgr = McpManager(connections=MCP_SERVERS)
    await mgr.initialize(MCP_SERVERS)
    monkeypatch.setattr("core.mcp._mcp_manager", mgr)
    agents.invalidate_agent_cache()
    agents._retrieval_cache.clear()
    loaded = await _edge_gql(twins, "mutation { reloadMcpServers { name toolCount } }", {})
    assert loaded["data"]["reloadMcpServers"] == [{"name": "echo", "toolCount": 3}, {"name": "other", "toolCount": 1}]


def _unid(record: Any) -> Any:
    """Without the random ids the adapter gives each content block."""
    import re

    return json.loads(re.sub(r"lc_[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", "lc_<id>", json.dumps(record)))


async def test_mcp_tools_bound_and_advertised(twins, monkeypatch):
    """An `always` server's tools are bound — called, failing on the server,
    answered with the adapter's blocks and structured content — and a `lazy`
    one's are named in the prompt, not callable as tools."""
    await _mcp(twins, monkeypatch)
    script = [
        Reply("Trying. ", [("echo", {"text": "hi"}), ("add", {"a": 2, "b": 3}), ("explode", {})]),
        Reply("", [("ping", {})]),
        Reply("Done."),
    ]
    python, edge = await _both(twins, "use the tools", script)
    assert _unid(edge) == _unid(python)
    results = [r for r in python["thread"] if r["role"] == "tool"]
    assert [r["status"] for r in results] == ["success", "success", "error", "error"]
    assert results[1]["artifact"] == {"structured_content": {"result": 5}}
    [(runtime,)] = _rows(twins.edge_db, "SELECT runtime FROM jobs WHERE kind = 'chat' ORDER BY rowid DESC LIMIT 1")
    assert runtime == "edge", "the edge handed the turn over"
    prompt = twins.fake.requests[0]["messages"][0]["content"]
    assert "## MCP Servers (on demand)" in prompt and "- **other** (1 tools): ping" in prompt


async def test_a_gated_mcp_tool_runs_once_approved(twins, monkeypatch):
    from core import tool_policy
    from db import async_session

    await _mcp(twins, monkeypatch)
    async with async_session() as s:
        await tool_policy.set_tool_policy(s, "mcp:echo/echo", approval=True)
    [(policy,)] = _rows(twins.python_db, "SELECT value FROM config_settings WHERE key = 'tools.policy'")
    with contextlib.closing(sqlite3.connect(twins.edge_db)) as c:
        c.execute("INSERT INTO config_settings (key, value, updated_at) VALUES ('tools.policy', ?, '2026-01-01 00:00:00')",
                  (policy,))
        c.commit()
    script = [Reply("", [("echo", {"text": "gated"})]), Reply("Echoed.")]
    python, edge = await _both_gated(twins, "echo it", script, "approve")
    assert _unid(edge) == _unid(python)
    [row] = python["approvals"]
    assert row[:3] == ["tool", "approval", "approved"]
    assert '{"tool_key": "mcp:echo/echo"}' in row


async def test_the_inbox_answers_through_the_edge(twins):
    """`resolveApproval` on what the edge answers itself — a board task's
    question, a gate with no run behind it — and its refusals, row for row
    with Python."""
    board_slots = [("full-1",), ("full-2",), ("full-3",)]  # no dispatch: the answer is the only change
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.executemany("INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts, cancel_requested, "
                          "run_at, created_at, updated_at) VALUES (?, 'board_task', '{}', 'pending', 0, 3, 0, "
                          "'2099-01-01 00:00:00', '2026-01-01 00:00:00', '2026-01-01 00:00:00')", board_slots)
            c.execute("INSERT INTO board_tasks (id, title, status, priority, created_by, blocked_reason, blocked_kind, "
                      "failure_count, created_at, updated_at) VALUES ('bt', 'Pick', 'blocked', 0, 'user', 'Which?', "
                      "'needs_input', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
            for aid, extra in (("ap-board", "'board_task', 'input', NULL, 'bt'"), ("ap-gate", "'tool', 'approval', 'run_cell', NULL"),
                               ("ap-gate2", "'tool', 'approval', 'run_cell', NULL"), ("ap-done", "'tool', 'approval', 'x', NULL")):
                c.execute(f"INSERT INTO approvals (id, source, kind, tool, board_task_id, status, question, label, "
                          f"requested_at, updated_at) VALUES ('{aid}', {extra}, 'pending', 'q?', 'l', "
                          "'2026-01-01 00:00:00', '2026-01-01 00:00:00')")
            c.execute("UPDATE approvals SET status = 'denied' WHERE id = 'ap-done'")
            c.commit()

    for aid, answer in (("ap-board", "  Green  "), ("ap-gate", "sure thing"), ("ap-gate2", "what is it?"),
                        ("ap-done", "yes"), ("nope", "yes"), ("ap-gate", "  ")):
        python = await _python_gql(RESOLVE, {"id": aid, "a": answer})
        edge = await _edge_gql(twins, RESOLVE, {"id": aid, "a": answer})
        assert edge == python, aid
    for table, cols in (("approvals", _APPROVAL_COLS), ("board_tasks", "status, pending_answer, blocked_reason")):
        python, edge = (_rows(db, f"SELECT {cols} FROM {table} ORDER BY rowid") for db in (twins.python_db, twins.edge_db))
        assert edge == python, table


async def test_deferred_actions_run_through_the_edge(twins):
    """An agent's delete recorded for approval (`core/approvals.py:ACTIONS`):
    approving runs it, in the edge — workflow with its runs, automation with
    its runs and conversation, a skill already gone — and a denial of any
    action, an MCP call's included, only closes the row. An approved MCP call
    runs on the edge's MCP client (`test_edge_mcp.py`); one naming no server
    fails as Python's does, leaving the row pending."""
    ts = "'2026-01-01 00:00:00'"
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute(f"INSERT INTO workflows (id, name, definition, created_at, updated_at) VALUES ('wf', 'flow', '{{}}', {ts}, {ts})")
            c.execute(f"INSERT INTO workflow_runs (id, workflow_id, status, started_at) VALUES ('wr', 'wf', 'done', {ts})")
            c.execute("INSERT INTO automations (id, name, input_type, enabled, stateful, created_at, updated_at) "
                      f"VALUES ('au', 'daily', 'prompt', 1, 1, {ts}, {ts})")
            c.execute("INSERT INTO automation_runs (id, automation_id, status, triggered_by, started_at) "
                      f"VALUES ('ar', 'au', 'done', 'manual', {ts})")
            c.execute("INSERT INTO conversations (id, title, model, surface, pinned, ephemeral, created_at) "
                      f"VALUES ('automation_au', 'daily', 'm', 'automation', 0, 0, {ts})")
            c.execute("INSERT INTO messages (id, conversation_id, role, content, status, created_at) "
                      f"VALUES ('am', 'automation_au', 'user', 'go', 'done', {ts})")
            c.execute(f"INSERT INTO skills (id, name, description, body, enabled, created_at, updated_at) VALUES ('sk', 'kept', 'd', 'b', 1, {ts}, {ts})")
            for aid, action, payload in (
                ("ap-wf", "delete_workflow", {"workflow_id": "wf", "name": "flow"}),
                ("ap-au", "delete_automation", {"automation_id": "au", "name": "daily"}),
                ("ap-gone", "delete_skill", {"skill_id": "nope", "name": "gone"}),
                ("ap-sk", "delete_skill", {"skill_id": "sk", "name": "kept"}),
                ("ap-mcp", "call_mcp_tool", {"server": "s", "tool": "t", "args": {}}),
                ("ap-mcp2", "call_mcp_tool", {"server": "s", "tool": "t", "args": {}}),
                ("ap-odd", "launch", {}),
            ):
                c.execute("INSERT INTO approvals (id, source, kind, status, question, label, action, action_payload, "
                          f"requested_at, updated_at) VALUES (?, 'deferred', 'approval', 'pending', 'q?', 'l', ?, ?, {ts}, {ts})",
                          (aid, action, json.dumps(payload)))
            c.commit()

    for aid, answer in (("ap-wf", "yes"), ("ap-au", "yes"), ("ap-gone", "yes"), ("ap-sk", "what is it?"),
                        ("ap-mcp", "no"), ("ap-odd", "yes"), ("ap-wf", "yes")):
        python = await _python_gql(RESOLVE, {"id": aid, "a": answer})
        edge = await _edge_gql(twins, RESOLVE, {"id": aid, "a": answer})
        assert edge == python, aid
    tables = {"approvals": _APPROVAL_COLS, "workflows": "id", "workflow_runs": "id", "automations": "id",
              "automation_runs": "id", "conversations": "id", "messages": "id", "skills": "id"}
    for table, cols in tables.items():
        python, edge = (_rows(db, f"SELECT {cols} FROM {table} ORDER BY rowid") for db in (twins.python_db, twins.edge_db))
        assert edge == python, table
    assert not _rows(twins.edge_db, "SELECT id FROM automations") and _rows(twins.edge_db, "SELECT id FROM skills")

    python = await _python_gql(RESOLVE, {"id": "ap-mcp2", "a": "yes"})
    assert await _edge_gql(twins, RESOLVE, {"id": "ap-mcp2", "a": "yes"}) == python
    assert "Unknown MCP server 's'" in json.dumps(python)
    for db in (twins.python_db, twins.edge_db):
        assert _rows(db, "SELECT status FROM approvals WHERE id = 'ap-mcp2'") == [("pending",)]


async def test_memory_and_skill_writes_through_the_edge(twins):
    """`addMemory`, `updateMemoryItem`, `createSkill`, `updateSkill`: embedded
    by the same embedder (a new fact merged into its near-duplicate), row for
    row and byte for byte with Python, refusals worded as Python's."""
    import base64
    import re

    def _gid(ty: str, raw: str) -> str:
        return base64.b64encode(f"{ty}:{raw}".encode()).decode()

    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute("INSERT INTO skills (id, name, description, body, enabled, embedding, created_at, updated_at) "
                      "VALUES ('sk1', 'review', 'How to review', 'Read it.', 1, ?, '2026-01-01 00:00:00', "
                      "'2026-01-01 00:00:00')", (fake_blob("review"),))
            c.commit()
    # Each side mints its own ids: numbered by first sight, per side.
    norms = {"python": Normalizer(), "edge": Normalizer()}

    def shape(side: str, out: dict) -> Any:
        # Timestamps by their shape only.
        return json.loads(re.sub(r"\d{2}:\d{2}:\d{2}\.\d+", "<time>", json.dumps(norms[side].value(out))))

    async def both(query: str, variables: dict) -> None:
        python = await _python_gql(query, variables)
        edge = await _edge_gql(twins, query, variables)
        assert shape("edge", edge) == shape("python", python), variables

    add = "mutation($t: String!, $k: String!) { addMemory(text: $t, kind: $k) { id kind text updatedAt useCount } }"
    for text, kind in (("  Tea at noon  ", "fact"), ("The user's favourite colour is green!", "fact"),
                       ("Coffee after lunch", "weird"), ("Sam drinks coffee", "core"), ("   ", "fact")):
        await both(add, {"t": text, "k": kind})
    update = "mutation($id: String!, $t: String!, $k: String) { updateMemoryItem(id: $id, text: $t, kind: $k) { id kind text } }"
    for mid, text, kind in (("m1", " The user reviews the rust edge ", "core"), ("m2", "Lunch is at one", "bogus"),
                            ("m2", "Lunch is at one", None), ("nope", "x", None), ("m1", " ", None)):
        await both(update, {"id": mid, "t": text, "k": kind})

    fields = "name description body enabled"
    create = f"mutation($i: SkillCreateInput!) {{ createSkill(input: $i) {{ {fields} }} }}"
    for i in ({"name": " deploy ", "description": " How to deploy the edge ", "body": " steps "},
              {"name": "deploy", "description": "d", "body": "b"}, {"name": "off", "description": "d", "body": "b", "enabled": False},
              {"name": " ", "description": "d", "body": "b"}, {"name": "x", "description": " ", "body": "b"},
              {"name": "x", "description": "d", "body": "  "}):
        await both(create, {"i": i})
    gid = _gid("Skill", "sk1")
    upd = f"mutation($id: ID!, $i: SkillUpdateInput!) {{ updateSkill(id: $id, input: $i) {{ {fields} }} }}"
    for i in ({"name": "deploy"}, {"name": " "}, {"description": " "}, {"name": " review ", "body": ""},
              {"description": "How to review maps"}, {"enabled": False}, {}):
        await both(upd, {"id": gid, "i": i})
    await both(upd, {"id": _gid("Skill", "nope"), "i": {"name": "x"}})

    for table, cols, order in (("memories", "kind, text, embedding", "text"),
                               ("skills", "name, description, body, enabled, embedding", "name")):
        python, edge = (_rows(db, f"SELECT {cols} FROM {table} ORDER BY {order}") for db in (twins.python_db, twins.edge_db))
        assert edge == python, table
    assert _rows(twins.edge_db, "SELECT embedding FROM skills WHERE name = 'deploy'") == [(fake_blob("How to deploy the edge"),)]
    # The near-duplicate replaced the fact it repeats.
    assert _rows(twins.edge_db, "SELECT text FROM memories WHERE id = 'm0'") == [("The user's favourite colour is green!",)]


async def test_a_board_task_is_decomposed_through_the_edge(twins):
    """`decomposeBoardTask`: the planner's prompt, its reply parsed (prose
    around the object, repeated and boolean indexes), the original parked
    behind its subtasks — and every refusal, before or after the call — row
    for row with Python. The run slots are full, so no dispatch starts one."""
    import base64

    def gid(raw: str) -> str:
        return base64.b64encode(f"BoardTask:{raw}".encode()).decode()

    ts = "'2026-01-01 00:00:00'"
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.executemany("INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts, cancel_requested, "
                          f"run_at, created_at, updated_at) VALUES (?, 'board_task', '{{}}', 'pending', 0, 3, 0, "
                          f"'2099-01-01 00:00:00', {ts}, {ts})", [("full-1",), ("full-2",), ("full-3",)])
            for tid, status, extra in (("dt", "blocked", "'Which?', 'needs_input'"), ("dt2", "todo", "NULL, NULL"),
                                       ("dt-run", "running", "NULL, NULL"), ("dt-kid", "todo", "NULL, NULL")):
                c.execute("INSERT INTO board_tasks (id, title, body, status, priority, created_by, model, blocked_reason, "
                          f"blocked_kind, failure_count, created_at, updated_at) VALUES (?, ?, 'Plan the trip.', ?, 4, "
                          f"'user', ?, {extra}, 0, {ts}, {ts})", (tid, f"Task {tid}", status, MODEL))
            c.execute(f"INSERT INTO board_task_links (id, parent_id, child_id, created_at) VALUES ('l', 'dt2', 'dt-kid', {ts})")
            c.commit()

    plan = {"subtasks": [{"title": " Book flights ", "body": "Find flights.", "depends_on": []},
                         {"title": "Book hotel", "body": "Near the venue.", "depends_on": [0, 0]},
                         {"title": "Itinerary", "body": "Day by day.", "depends_on": [1, True]}]}
    replies = [
        "Not JSON at all",
        json.dumps({"subtasks": [{"title": "only", "body": "one"}]}),
        json.dumps({"subtasks": [{"title": "a", "body": "b", "depends_on": [0]}, {"title": "c", "body": "d"}]}),
        json.dumps({"subtasks": ["a", "b"]}),
        json.dumps({"subtasks": [{"title": "a", "body": ""}, {"title": "c", "body": "d"}]}),
        "Here is the plan:\n" + json.dumps(plan) + "\nGood luck!",
    ]
    twins.fake.script = [Reply(r) for r in replies for _ in ("python", "edge")]

    fields = "title body status priority createdBy model parentIds childIds blockedReason"
    q = f"mutation($id: ID!) {{ decomposeBoardTask(id: $id) {{ {fields} }} }}"
    for tid in ("nope", "dt-run", "dt-kid", "dt", "dt", "dt", "dt", "dt", "dt"):
        python = await _python_gql(q, {"id": gid(tid)})
        edge = await _edge_gql(twins, q, {"id": gid(tid)})
        assert edge == python, tid
    assert edge["data"]["decomposeBoardTask"][2]["title"] == "Itinerary"

    # Each side asked the same thing.
    asked = [[(m["role"], m["content"]) for m in r["messages"]] for r in twins.fake.requests]
    assert len(asked) == 2 * len(replies) and all(a == asked[0] for a in asked)
    assert asked[0][0][1].startswith("You are a planner") and "# Task: Task dt\nPlan the trip." in asked[0][1][1]

    tasks = "SELECT title, body, status, priority, created_by, model, blocked_reason, blocked_kind, finished_at FROM board_tasks ORDER BY title"
    links = ("SELECT p.title, c.title FROM board_task_links l JOIN board_tasks p ON p.id = l.parent_id "
             "JOIN board_tasks c ON c.id = l.child_id ORDER BY l.rowid")
    for sql in (tasks, links):
        assert _rows(twins.edge_db, sql) == _rows(twins.python_db, sql), sql


# ── memory consolidation ─────────────────────────────────────────────────────


def _talk(twins: Twins, conv: str, rows: list[tuple[str, str, str]], *, project: str | None = None,
          title: str | None = "Trip", ephemeral: bool = False, status: dict[int, str] | None = None) -> None:
    """A conversation's (role, content, created_at) rows, in both databases."""
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            if not c.execute("SELECT 1 FROM conversations WHERE id = ?", (conv,)).fetchone():
                c.execute("INSERT INTO conversations (id, title, model, created_at, surface, pinned, project_id, ephemeral) "
                          "VALUES (?, ?, ?, '2026-01-01 00:00:00', 'web', 0, ?, ?)", (conv, title, MODEL, project, ephemeral))
            for i, (role, content, at) in enumerate(rows):
                c.execute("INSERT INTO messages (id, conversation_id, role, content, created_at, status) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (f"{conv}-{at}", conv, role, content, at,
                                                        (status or {}).get(i, "done")))
            c.commit()


def _sql(twins: Twins, sql: str, *args: Any) -> None:
    for db in (twins.python_db, twins.edge_db):
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute(sql, args)
            c.commit()


def _edge_sweep(twins: Twins, edge_binary: Path, task: str) -> dict:
    """One maintenance sweep by the edge, over its database, as its timer runs it."""
    import os
    import subprocess

    env = {**os.environ, "WORK_DIR": str(twins.edge_db.parent), "DATABASE_URL": f"sqlite+aiosqlite:///{twins.edge_db}",
           "OLLAMA_HOST": twins.fake.url, "HOME": str(twins.edge_db.parent), "JARVIS_APP_DIR": str(REPO),
           "JARVIS_EDGE_LOG": "warn"}
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        env.pop(var, None)
    out = subprocess.run([str(edge_binary), "--maintenance-run", task], env=env, cwd=twins.edge_db.parent,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def _asked(twins: Twins, sides: int = 2) -> list:
    """Each side's model requests (Python's first), ids numbered per side."""
    reqs = [[[m["role"], m["content"]] for m in r["messages"]] for r in twins.fake.requests]
    half = len(reqs) // sides
    return [Normalizer().value(reqs[i * half:(i + 1) * half]) for i in range(sides)]


def _kv(db: Path, namespace: str, key: str) -> dict:
    [(raw,)] = _rows(db, "SELECT value FROM kv_store WHERE namespace = ? AND key = ?", namespace, key)
    return json.loads(raw)


async def test_memory_is_consolidated_through_the_edge(twins, edge_binary):
    """`consolidateMemory` and the 6-hourly sweep: the transcript batched
    oldest first (incognito left out, stopping before a reply still being
    written), every operation the model can ask for — row for row with
    Python, embeddings included, and the same watermark."""
    from core.memory_consolidation import consolidate_memory
    from core.state import get_store

    _sql(twins, "INSERT INTO config_settings (key, value, updated_at) VALUES ('default.model', ?, '2026-01-01 00:00:00')", MODEL)
    rows = [("user" if i % 2 == 0 else "assistant", f"{i}: " + "rust edge " * 60, f"2026-01-01 10:{i:02d}:00")
            for i in range(40)]
    rows[39] = (rows[39][0], rows[39][1], "2026-01-01 10:39:00.250000")
    rows.append(("assistant", "still typing", "2026-01-01 10:45:00"))
    _talk(twins, "c-mem", rows, title=None, status={40: "running"})
    _talk(twins, "c-ghost", [("user", "I am incognito", "2026-01-01 10:05:30")], ephemeral=True)

    ops = ("Noted:\n" + json.dumps([
        {"op": "add", "text": " Sam drinks tea ", "kind": "core"},
        {"op": "update", "id": "m2", "text": "Lunch is at one", "kind": "fact"},
        {"op": "delete", "id": "m0", "reason": "contradicted"},
        {"op": "delete", "id": "ghost"},
        {"op": "update", "id": "unknown", "text": "The river maps are green"},
        {"text": "No op is an add", "kind": "odd"},
        {"op": "add", "text": "The user's favourite colour is green!"},
    ]) + "\nDone.")
    replies = [ops, "nothing [] to do"]
    twins.fake.script = [Reply(r) for r in replies * 2]

    memories = "SELECT kind, text, embedding FROM memories ORDER BY kind, text"
    q = "mutation($m: String) { consolidateMemory(model: $m) }"
    python = await _python_gql(q, {"m": MODEL})
    edge = await _edge_gql(twins, q, {"m": MODEL})
    assert edge == python
    assert python["data"]["consolidateMemory"] == "consolidated 40 messages in 2 batch(es) → +4 ~1 -1 (+0 seeded)"
    assert _rows(twins.edge_db, memories) == _rows(twins.python_db, memories)
    py_asked, edge_asked = _asked(twins)
    assert edge_asked == py_asked and len(py_asked) == 2
    assert "I am incognito" not in json.dumps(py_asked) and "still typing" not in json.dumps(py_asked)
    assert "] Untitled | USER: 0: rust edge" in py_asked[0][1][1]
    state = [_kv(db, "memory_consolidation", "state") for db in (twins.python_db, twins.edge_db)]
    assert [s["messages_through"] for s in state] == ["2026-01-01T10:39:00.250000+00:00"] * 2
    assert all(list(s) == ["messages_through", "last_run_at"] for s in state)

    # Nothing past the watermark but the reply still being written.
    twins.fake.reset([])
    assert await _edge_gql(twins, q, {"m": None}) == await _python_gql(q, {"m": None}) == {
        "data": {"consolidateMemory": "skipped: no new messages since last run"}}

    # The timer's pass, on the default model.
    _sql(twins, "UPDATE messages SET status = 'done' WHERE conversation_id = 'c-mem'")
    _talk(twins, "c-mem2", [("user", "Forget the lunch thing", "2026-01-02 09:00:00")])
    twins.fake.script = [Reply(json.dumps([{"op": "delete", "id": "m2", "reason": "user_requested"}]))] * 2
    python = await consolidate_memory(get_store())
    assert _edge_sweep(twins, edge_binary, "memory_consolidation") == {"result": python}
    assert python == "consolidated 2 messages in 1 batch(es) → +0 ~0 -1 (+0 seeded)"
    assert _rows(twins.edge_db, memories) == _rows(twins.python_db, memories)
    assert _asked(twins)[0] == _asked(twins)[1]
    assert _edge_sweep(twins, edge_binary, "nope") == {"error": "unknown maintenance task 'nope'"}


async def test_project_memory_is_consolidated_through_the_edge(twins, edge_binary):
    """`consolidateProjectMemory` and the 30-minute sweep: merge (lines
    already said dropped), nothing new, a due rewrite, a merge that would
    overflow handed to rewrite, the quiet and minimum-material gates — the
    memory, the watermarks and every request as Python's."""
    import base64

    from core.project_memory_consolidation import consolidate_project_memories
    from core.state import get_store

    def gid(raw: str) -> str:
        return base64.b64encode(f"Project:{raw}".encode()).decode()

    _sql(twins, "INSERT INTO config_settings (key, value, updated_at) VALUES ('default.model', ?, '2026-01-01 00:00:00')", MODEL)
    long = "We settled the GraphQL contract. " * 120
    _talk(twins, "c-p", [("user", "  How should the edge serve GraphQL?  ", "2026-01-01 09:00:00"),
                         ("assistant", long, "2026-01-01 09:01:00.500000"),
                         ("user", "And the tests?", "2026-01-01 09:02:00")], project="p1", title="Atlas work")
    _talk(twins, "c-p-ghost", [("user", "secret plans", "2026-01-01 09:03:00")], project="p1", ephemeral=True)

    project = "SELECT memory FROM projects WHERE id = ?"
    q = "mutation($id: ID!, $m: String) { consolidateProjectMemory(id: $id, model: $m) }"

    async def both(pid: str, script: list[str], expect: str) -> None:
        twins.fake.reset([Reply(r) for r in script * 2])
        python = await _python_gql(q, {"id": gid(pid), "m": MODEL})
        edge = await _edge_gql(twins, q, {"id": gid(pid), "m": MODEL})
        assert edge == python
        assert python["data"]["consolidateProjectMemory"] == expect
        assert _rows(twins.edge_db, project, pid) == _rows(twins.python_db, project, pid)
        py_asked, edge_asked = _asked(twins)
        assert edge_asked == py_asked
        metas = [_kv(db, "project_memory_consolidation", pid) for db in (twins.python_db, twins.edge_db)]
        assert metas[0]["messages_through"] == metas[1]["messages_through"]
        assert [list(m) for m in metas] == [["messages_through", "last_rewrite_at"]] * 2

    await both("p1", ["- Uses Rust.\n- The edge serves GraphQL over axum\n## Stack\n- uses rust"],
               "merge: added 1 line(s)")
    assert _rows(twins.python_db, project, "p1") == [("Uses Rust.\n\n- The edge serves GraphQL over axum",)]
    assert "secret plans" not in json.dumps(twins.fake.requests)
    assert " …[truncated]" in twins.fake.requests[0]["messages"][1]["content"]

    _talk(twins, "c-p", [("user", "Thanks, " + "that is all. " * 60, "2026-01-01 09:30:00")], project="p1")
    await both("p1", ["__NO_UPDATE__"], "merge: nothing new (1 messages read)")

    # A day since the last rewrite: this pass may prune.
    _sql(twins, "UPDATE kv_store SET value = json_set(value, '$.last_rewrite_at', '2025-12-01T00:00:00Z') "
                "WHERE namespace = 'project_memory_consolidation'")
    _talk(twins, "c-p", [("assistant", "We dropped axum for hyper. " * 30, "2026-01-01 10:00:00")], project="p1")
    await both("p1", ["- The edge serves GraphQL over hyper\n- Uses Rust."], "rewrite: 47 → 49 chars")
    await both("p1", [], "skipped: no new messages since last run")
    assert (await _python_gql(q, {"id": gid("nope"), "m": None})) == (await _edge_gql(twins, q, {"id": gid("nope"), "m": None}))

    # The sweep: a quiet project whose merge would pass 20 bullets goes to
    # rewrite; one with too little to read is skipped; p1 has nothing new.
    full = "\n".join(f"- Fact number {i} about the beacon" for i in range(19))
    _sql(twins, "INSERT INTO projects (id, name, instructions, memory, created_at, updated_at) VALUES "
                "('p2', 'Beacon', '', ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00'), "
                "('p3', 'Tiny', '', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')", full)
    _talk(twins, "c-b", [("user", "Beacon status? " * 50, "2026-01-01 11:00:00")], project="p2", title="Beacon")
    _talk(twins, "c-t", [("user", "hi", "2026-01-01 11:00:00")], project="p3")
    twins.fake.reset([Reply(r) for r in ["- The beacon ships in March\n- Telemetry goes over LoRa radio", "- Condensed beacon"] * 2])
    python = await consolidate_project_memories(get_store())
    assert _edge_sweep(twins, edge_binary, "project_memory") == {"result": python}
    assert python == f"Beacon: rewrite: {len(full)} → 18 chars"
    for pid in ("p1", "p2", "p3"):
        assert _rows(twins.edge_db, project, pid) == _rows(twins.python_db, project, pid)
    py_asked, edge_asked = _asked(twins)
    assert edge_asked == py_asked and len(py_asked) == 2


async def test_the_sdk_asks_through_the_edge(twins):
    """`requestToolApproval` as the `jarvis` SDK sends it from a kernel."""
    agent = {"X-Jarvis-Caller": "agent", "X-Jarvis-Conversation": "c-old"}
    cases = [
        {"k": "sdk:delete_file", "t": "delete_file", "a": json.dumps({"path": "a.txt", "n": [1, "b"], "x": "é" * 600}), "c": None},
        {"k": "mcp:srv/tool", "t": "tool", "a": "", "c": "c-other"},
        {"k": "nope", "t": "x", "a": "{}", "c": None},
        {"k": "sdk:x", "t": "x", "a": "[1]", "c": None},
    ]
    for case in cases:
        python = await _python_gql(REQUEST, case, caller="agent", conversation="c-old")
        edge = await _edge_gql(twins, REQUEST, case, agent)
        assert Normalizer().value(edge) == Normalizer().value(python), case
    python, edge = (_approvals(db, Normalizer()) for db in (twins.python_db, twins.edge_db))
    assert edge == python and len(python) == 2
    # A human's request is Python's to refuse; the edge doesn't take it.
    resp = await twins.client.post("/graphql", json={"query": REQUEST, "variables": cases[0]})
    assert resp.status_code != 200


BROWSE = "mutation($u: String!, $p: String!, $c: String) { browserActivity(url: $u, phase: $p, conversationId: $c) }"


async def test_a_browse_is_announced_on_the_live_run(twins):
    """`browserActivity` as `tools/browser.py` sends it from a kernel, while
    the run is live (here: waiting on a gate): a `browser_step` in its
    stream, as Python appends one. With no live run it says so; a bad phase
    is refused."""
    await _gate_run_cell(twins)
    # Not a real `read(...)`: the edge's kernel would announce it too, and
    # the Python twin's kernel has no server to announce to.
    script = [Reply("Looking. ", [("run_cell", {"code": "6 * 7"})]), Reply("Done.")]
    url = "https://example.com/" + "é" * 600

    async def announce(db: Path, ask) -> None:
        async with asyncio.timeout(30):
            while not (rows := _rows(db, "SELECT id, parent_id FROM approvals WHERE source = 'tool' AND status = 'pending'")):
                await asyncio.sleep(0.05)
        [(approval_id, conversation)] = rows
        for phase in ("start", "done"):
            assert await ask(BROWSE, {"u": url, "p": phase, "c": None}, conversation) == {"data": {"browserActivity": True}}
        assert (await ask(RESOLVE, {"id": approval_id, "a": "Approve"}, None)).get("errors") is None

    async def python_ask(query: str, variables: dict, conversation: str | None) -> dict:
        return await _python_gql(query, variables, caller="agent" if conversation else "human", conversation=conversation)

    async def edge_ask(query: str, variables: dict, conversation: str | None) -> dict:
        headers = {"X-Jarvis-Caller": "agent", "X-Jarvis-Conversation": conversation} if conversation else {}
        return await _edge_gql(twins, query, variables, headers)

    records = []
    for run, db, ask in ((twins.python, twins.python_db, python_ask), (twins.edge, twins.edge_db, edge_ask)):
        announcing = asyncio.create_task(announce(db, ask))
        turn, _ = await run("read it", script)
        await announcing
        norm = Normalizer({turn.conversation_id: "<conversation>", turn.task_id: "<task>"})
        records.append(norm.value(_record(turn)["events"]))
        # The run is over: nothing to announce onto.
        assert await ask(BROWSE, {"u": url, "p": "start", "c": None}, turn.conversation_id) == {"data": {"browserActivity": False}}
        refused = await ask(BROWSE, {"u": url, "p": "later", "c": None}, turn.conversation_id)
        assert refused["errors"] == ["phase must be one of: start, done, error"]
    python, edge = records
    assert edge == python
    steps = [e for e in python if e["kind"] == "BrowserStepEvent"]
    assert [(e["phase"], len(e["url"])) for e in steps] == [("start", 500), ("done", 500)]
    # A human's is Python's to refuse.
    resp = await twins.client.post("/graphql", json={"query": BROWSE, "variables": {"u": "x", "p": "start"}})
    assert resp.status_code != 200


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
