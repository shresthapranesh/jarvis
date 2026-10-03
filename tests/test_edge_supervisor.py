"""The edge owning the Python worker (`edge/src/supervisor.rs`), and what
it serves so that an idle UI doesn't need Python: the SPA, `/health`, and the
three queries a page load makes that Python used to answer (`models`, `todos`,
`browserAvailable`). Plus the maintenance gates that keep the timers from
starting Python for nothing.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — a fixture
from test_edge_parity import _assert_same, _edge, _relay_text

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
async def edge(jarvis, work_dir: Path, edge_binary: Path):
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        yield client


async def _set(key: str, value: str) -> None:
    from db import async_session
    from db.ops import set_setting

    async with async_session() as s:
        await set_setting(s, key, value)


# ── the page-load queries ────────────────────────────────────────────────────


async def test_models(edge):
    for op in ("ModelCatalogQuery", "useModelsQuery"):
        await _assert_same(edge, _relay_text(op))
    custom = [
        {"id": "ollama:llama3", "label": "Llama"},
        # provider from the id; label from the id; a window of every shape
        {"id": "openrouter:x/y:free", "context_window": "128_000"},
        {"id": "anthropic:a", "label": "", "provider": "", "context_window": 1.9},
        {"id": "anthropic:b", "context_window": True},
        {"id": "anthropic:c", "context_window": " 12 "},
        {"id": "anthropic:d", "context_window": "1.5"},
        {"id": "anthropic:e", "context_window": -4},
        {"id": "anthropic:f", "context_window": None},
        # skipped: no id, not a dict; deduped: a built-in's id, a repeat
        {"label": "nameless"}, "junk", {"id": ""},
        {"id": "google_genai:gemma-4-31b-it", "label": "shadowed"},
        {"id": "ollama:llama3", "label": "again"},
    ]
    await _set("models.custom", json.dumps(custom))
    await _set("default.model", "ollama:llama3")
    data = await _assert_same(edge, _relay_text("ModelCatalogQuery"))
    assert "ollama:llama3" in [m["id"] for m in data["data"]["models"]["available"]]

    # Not a list: no custom models at all.
    await _set("models.custom", '{"id": "x"}')
    await _assert_same(edge, _relay_text("ModelCatalogQuery"))


async def test_a_custom_model_python_would_reject_is_left_to_python(edge):
    await _set("models.custom", json.dumps([{"id": 7}]))
    resp = await _edge(edge, _relay_text("ModelCatalogQuery"))
    assert resp.status_code == 502  # deferred: proxied to the dead backend


async def test_todos(edge):
    from langchain_core.messages import HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint

    from core.state import get_async_checkpointer

    query = _relay_text("TodoListQuery")
    # No checkpoints.db rows at all yet.
    await _assert_same(edge, query, {"conversationId": "c1"})

    cp = get_async_checkpointer()
    todos = [
        "a legacy string",
        {"text": "doing", "status": "in_progress"},
        {"text": "done", "status": "done"},
        {"text": 42, "status": "bogus"},
        {"status": "done"},
    ]
    for thread, values in (("c1", {"todos": todos}), ("c2", {"todos": "not a list"}), ("c3", {})):
        config: RunnableConfig = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        for step in range(2):  # the newest one wins
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {
                # A message is an extension type the edge skips over.
                "messages": [HumanMessage(content="hi", id=f"h{step}")],
                **({k: (v if step else ["stale"]) for k, v in values.items()}),
            }
            config = await cp.aput(config, checkpoint, {}, {})
    for thread in ("c1", "c2", "c3", "nobody"):
        data = await _assert_same(edge, query, {"conversationId": thread})
    data = await _assert_same(edge, query, {"conversationId": "c1"})
    assert [t["status"] for t in data["data"]["todos"]] == ["pending", "in_progress", "done", "pending"]

    # Once a run has touched a thread its list is in `thread_state`, which
    # wins over the checkpoint — an empty or cleared list included.
    from core.transcript_store import set_todos
    from db import async_session

    async with async_session() as s:
        await set_todos(s, "c1", [{"text": "new", "status": "done"}, "plain", {"text": 3}])
        await set_todos(s, "c2", [])
        await set_todos(s, "c3", None)
    for thread in ("c1", "c2", "c3"):
        data = await _assert_same(edge, query, {"conversationId": thread})
    data = await _assert_same(edge, query, {"conversationId": "c1"})
    assert [t["text"] for t in data["data"]["todos"]] == ["new", "plain", "3"]


@contextlib.contextmanager
def _fake_cdp():
    """A loopback endpoint answering `/json/version` as a browser does."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/json/version" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the base's name
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


async def test_browser_available(edge):
    query = _relay_text("BrowserAvailableQuery")
    await _set("browser.cdp_url", f"http://127.0.0.1:{_free_port()}")  # nothing there
    data = await _assert_same(edge, query)
    assert data["data"]["browserAvailable"] is False
    with _fake_cdp() as url:
        await _set("browser.cdp_url", json.dumps(url + "/"))  # JSON-quoted, trailing slash
        data = await _assert_same(edge, query)
        assert data["data"]["browserAvailable"] is True
    # The edge has no TLS, and says so by leaving it to Python.
    await _set("browser.cdp_url", "https://example.invalid")
    assert (await _edge(edge, query)).status_code == 502


# ── maintenance gates ────────────────────────────────────────────────────────


def _uuid6_at(when: datetime) -> str:
    from unittest import mock

    import langgraph.checkpoint.base.id as ids

    with mock.patch.object(ids.time, "time_ns", return_value=int(when.timestamp() * 1e9)):
        ids._last_v6_timestamp = None
        return str(ids.uuid6())


def _edge_due(edge_binary: Path, work_dir: Path) -> dict[str, Any]:
    env = {**os.environ, "WORK_DIR": str(work_dir), "DATABASE_URL": f"sqlite+aiosqlite:///{work_dir}/database.db",
           "JARVIS_EDGE_LOG": "warn"}
    out = subprocess.run([str(edge_binary), "--maintenance-due"], env=env, cwd=work_dir,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


async def _python_due(monkeypatch) -> dict[str, bool]:
    """Each sweep's own verdict: reaching for an LLM means it found work."""
    from core import project_memory_consolidation as pmc
    from core.checkpoint_retention import prune_checkpoints
    from core.memory_consolidation import _load_watermark, _transcript_block
    from core.state import get_store
    from db import async_session
    from db.models import Project
    from db.ops import get_messages_since
    from sqlalchemy import select

    class Due(Exception):
        pass

    async def due(*_a, **_k):
        raise Due

    monkeypatch.setattr(pmc, "_resolve_llm", due)
    store = get_store()
    async with async_session() as s:
        messages = await get_messages_since(s, since=await _load_watermark(store), limit=200)
        projects = (await s.execute(select(Project.id))).scalars().all()
    project = False
    for pid in projects:
        try:
            assert (await pmc.consolidate_project_memory(store, pid)).startswith("skipped")
        except Due:
            project = True
    stats = await prune_checkpoints(dry_run=True)
    return {
        "memory_consolidation": _transcript_block(messages)[1] is not None,
        "project_memory": project,
        "checkpoint_prune": bool(stats["root_pruned"] or stats["subgraph_pruned"]),
    }


async def test_maintenance_gates_match_the_sweeps(jarvis, work_dir: Path, edge_binary: Path, monkeypatch):
    from core.state import get_async_checkpointer, get_store
    from db import async_session
    from db.models import Conversation, Message, Project

    now = datetime.now(timezone.utc)
    await get_async_checkpointer().setup()
    store = get_store()

    async def check(label: str) -> None:
        edge, python = _edge_due(edge_binary, work_dir), await _python_due(monkeypatch)
        assert edge == python, label

    await check("empty")

    async with async_session() as s:
        s.add_all([
            Project(id="p1", name="P"),
            Conversation(id="c1", title="t", model="m", project_id="p1", created_at=now - timedelta(days=2)),
            Conversation(id="ghost", title="t", model="m", ephemeral=True, project_id="p1"),
        ])
        await s.commit()

    async def say(mid: str, at: datetime, conv: str = "c1", text: str = "x" * 400, status: str = "done") -> None:
        async with async_session() as s:
            s.add(Message(id=mid, conversation_id=conv, role="user", content=text, created_at=at, status=status))
            await s.commit()

    await say("ghost-1", now - timedelta(hours=3), conv="ghost")
    await check("only incognito material")
    await say("m1", now - timedelta(minutes=5))
    await check("new, but too fresh and too short")
    await say("m2", now - timedelta(minutes=4))
    await check("enough, but still active")

    async def age(minutes: int) -> None:
        async with async_session() as s:
            for mid in ("m1", "m2"):
                m = await s.get(Message, mid)
                assert m is not None
                m.created_at -= timedelta(minutes=minutes)
            await s.commit()

    await age(6)
    await check("quiet for ten minutes: still active")
    await age(14)
    await check("quiet")
    await store.aput(("project_memory_consolidation",), "p1",
                     {"messages_through": (now - timedelta(minutes=24, seconds=30)).isoformat()})
    await check("watermark past the first message: too little left")
    await say("m0", now - timedelta(hours=30), text="y" * 300)
    await store.aput(("project_memory_consolidation",), "p1", {"messages_through": "garbage"})
    await check("unreadable watermark counts as none")

    await store.aput(("memory_consolidation",), "state", {"last_run_at": now.isoformat()})
    await check("legacy watermark past everything")
    await store.aput(("memory_consolidation",), "state",
                     {"messages_through": "", "last_run_at": (now - timedelta(hours=1)).isoformat()})
    await check("empty watermark falls back to last_run_at")
    await say("r1", now - timedelta(minutes=30), status="running")
    await check("the first new message is a reply still being written")
    await store.aput(("memory_consolidation",), "state",
                     {"messages_through": (now - timedelta(minutes=30)).replace(tzinfo=None).isoformat()})
    await check("a naive watermark exactly at a message is exclusive")

    db = sqlite3.connect(work_dir / "checkpoints.db")

    def checkpoints(thread: str, ns: str, *ages: timedelta) -> None:
        for age in ages:
            db.execute("INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
                       "VALUES (?, ?, ?, 'msgpack', x'80', '{}')", (thread, ns, _uuid6_at(now - age)))
        db.commit()

    hour = timedelta(hours=1)
    checkpoints("t1", "", 2 * hour, 2 * hour, 2 * hour)
    await check("three old roots: all kept")
    checkpoints("t2", "", timedelta(minutes=5), timedelta(minutes=4), timedelta(minutes=3), timedelta(minutes=2))
    await check("a fourth root, but too young")
    checkpoints("t3", "tools:abc", timedelta(minutes=10))
    await check("a young subgraph")
    checkpoints("t1", "", 3 * hour)
    await check("an old fourth root")
    db.execute("DELETE FROM checkpoints WHERE thread_id = 't1'")
    checkpoints("t3", "tools:def", 2 * hour)
    await check("an old subgraph")
    db.close()


# ── the SPA and the routes it must not shadow ────────────────────────────────


def _python_get_routes() -> list[str]:
    """Every GET route the Python app serves, as a concrete path."""
    from server.entrypoint import app

    paths = []
    for route in app.router.routes:
        inner = getattr(route, "original_router", None)
        prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
        for r in inner.routes if inner else [route]:
            path = prefix + getattr(r, "path", "")
            if "GET" in (getattr(r, "methods", None) or ()) and "{full_path" not in path:
                paths.append(path.replace("{artifact_id}", "a1").replace("{doc_id}", "d1"))
    return paths


async def test_spa_is_served_here_and_python_routes_are_not_shadowed(database, work_dir: Path, edge_binary: Path,
                                                                      monkeypatch):
    app = work_dir / "app"
    (app / "static" / "dist" / "assets").mkdir(parents=True)
    (app / "static" / "dist" / "index.html").write_text("<!doctype html>spa")
    (app / "static" / "dist" / "assets" / "app-1.js").write_text("console.log(1)")
    (work_dir / "secret.txt").write_text("nope")
    monkeypatch.setenv("JARVIS_APP_DIR", str(app))
    routes = _python_get_routes()
    assert {"/health", "/artifacts/a1/raw", "/server-logs/stream", "/graphql", "/docs"} <= set(routes)

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        index = await client.get("/")
        assert index.status_code == 200 and index.text == "<!doctype html>spa"
        assert index.headers["content-type"].startswith("text/html")
        js = await client.get("/assets/app-1.js")
        assert js.text == "console.log(1)" and js.headers["content-type"].startswith("text/javascript")
        head = await client.head("/assets/app-1.js")
        assert head.status_code == 200 and head.content == b""
        # Client-side routes, including ones that share a prefix with Python's.
        for path in ("/c/abc", "/artifacts", "/artifacts/a1", "/documents/d1/raw/x", "/ws/live", "/uploads",
                     "/../secret.txt", "/%2e%2e/secret.txt"):
            resp = await client.get(path)
            assert resp.text == "<!doctype html>spa", path
        # Python's own GET routes are proxied (to the dead backend: 502).
        for path in routes:
            assert (await client.get(path)).status_code == 502, path


# ── the supervisor, end to end ───────────────────────────────────────────────


def _listening(port: int) -> bool:
    with contextlib.suppress(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    return False


async def _until(predicate, timeout: float = 60.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate() if asyncio.iscoroutinefunction(predicate) else predicate():
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


async def test_the_worker_comes_and_goes_with_the_work(database, work_dir: Path, edge_binary: Path):
    from db import async_session
    from db.models import Automation, AutomationRun, Conversation

    async with async_session() as s:
        s.add(Automation(id="a1", name="tick", input_type="code", code_text="print('tick')", enabled=True))
        await s.commit()

    port, backend = _free_port(), _free_port()
    env = {
        **os.environ,
        "WORK_DIR": str(work_dir),
        "DATABASE_URL": f"sqlite+aiosqlite:///{work_dir}/database.db",
        "CHECKPOINTS_DB": str(work_dir / "checkpoints.db"),
        "HOME": str(work_dir),  # no ~/.jarvis/mcp.json servers
        # Set, so the repo's .env can't fill them in.
        "TELEGRAM_BOT_TOKEN": "",
        "DISCORD_BOT_TOKEN": "",
        "JARVIS_EDGE_BIND": f"127.0.0.1:{port}",
        "JARVIS_BACKEND_URL": f"http://127.0.0.1:{backend}",
        "JARVIS_APP_DIR": str(REPO),
        "JARVIS_WORKER_CMD": f"{sys.executable} -m uvicorn server.entrypoint:app --host 127.0.0.1 "
                             "--port $JARVIS_BACKEND_PORT",
        "JARVIS_WORKER_IDLE": "2",
        "JARVIS_EDGE_LOG": "info",
    }
    log = (work_dir / "edge.log").open("w")
    proc = subprocess.Popen([str(edge_binary)], env=env, cwd=work_dir, stdout=log, stderr=subprocess.STDOUT)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
            # Started at boot (the startup sweeps run), then stopped when idle.
            await _until(lambda: _listening(backend), what="the boot start")
            await _until(lambda: not _listening(backend), what="the idle stop")

            # The edge answers a page's queries and /health without it.
            assert (await client.get("/health")).json() == {"status": "ok"}
            for op, variables in (("ConversationListQuery", {}), ("ModelCatalogQuery", {}),
                                  ("TodoListQuery", {"conversationId": "c1"}), ("RunningTasksQuery", {})):
                assert (await _edge(client, _relay_text(op), variables)).status_code == 200, op
            await asyncio.sleep(1)
            assert not _listening(backend)

            # A request only Python can answer starts it, and waits for it.
            assert (await client.get("/openapi.json")).status_code == 200
            assert _listening(backend)
            await _until(lambda: not _listening(backend), what="the second idle stop")

            # An incognito chat open between turns survives the restart.
            async with async_session() as s:
                s.add(Conversation(id="incognito", title="t", model="m", ephemeral=True))
                await s.commit()

            # A run started here, with no worker up: the job brings one up,
            # and the run's row isn't taken for a crashed one's.
            resp = await _edge(client, 'mutation T($id: ID!) { triggerAutomation(id: $id) }',
                               {"id": _gid("Automation", "a1")})
            run_id = resp.json()["data"]["triggerAutomation"]

            async def finished() -> bool:
                async with async_session() as s:
                    run = await s.get(AutomationRun, run_id)
                    return run is not None and run.status not in ("running", "pending")

            await _until(finished, what="the automation run")
            async with async_session() as s:
                run = await s.get(AutomationRun, run_id)
                assert run is not None
                assert (run.status, (run.output or "").strip()) == ("done", "tick"), run.error
                assert await s.get(Conversation, "incognito") is not None
            await _until(lambda: not _listening(backend), what="the idle stop after the run")
    finally:
        proc.terminate()
        proc.wait(timeout=40)
        log.close()
    assert not _listening(backend), "the worker outlived the edge"
    output = (work_dir / "edge.log").read_text()
    assert "worker exited unexpectedly" not in output, output


def _gid(type_name: str, raw: str) -> str:
    from strawberry.relay.utils import to_base64

    return to_base64(type_name, raw)


# ── Python's side ────────────────────────────────────────────────────────────


async def test_a_run_waiting_for_its_first_claim_is_not_a_zombie(database):
    """The edge writes a run's row and its job, then starts Python to claim
    it; that start's sweep must not take the row for a crashed run's."""
    from db import async_session
    from db.models import Automation, AutomationRun, Conversation, Job, Message, Workflow, WorkflowRun
    from db.ops import cleanup_zombie_running_rows

    async with async_session() as s:
        s.add_all([
            Conversation(id="c", title="t", model="m"),
            Automation(id="a", name="a", input_type="code"),
            Workflow(id="w", name="w", definition="{}"),
        ])
        for state, job_status in (("waiting", "pending"), ("crashed", "running")):
            s.add_all([
                Message(id=f"m-{state}", conversation_id="c", role="assistant", content="", status="running"),
                AutomationRun(id=f"a-{state}", automation_id="a", status="running", triggered_by="manual"),
                WorkflowRun(id=f"w-{state}", workflow_id="w", status="running"),
            ])
            s.add_all(Job(id=f"{k}-{state}", kind="chat", payload="{}", status=job_status) for k in "maw")
        await s.commit()
        await cleanup_zombie_running_rows(s)
    async with async_session() as s:
        for model, k in ((Message, "m"), (AutomationRun, "a"), (WorkflowRun, "w")):
            waiting, crashed = await s.get(model, f"{k}-waiting"), await s.get(model, f"{k}-crashed")
            job = await s.get(Job, f"{k}-crashed")
            assert waiting is not None and crashed is not None and job is not None
            assert (waiting.status, crashed.status, job.status) == ("running", "error", "pending"), model.__name__


async def test_drain_waits_out_a_claim_in_flight(database):
    from core.queue import SqliteJobQueue

    queue = SqliteJobQueue()
    await queue.enqueue("chat", {})
    gate = asyncio.Event()
    claim_body = queue._claim

    async def slow_claim(*args, **kwargs):
        await gate.wait()
        return await claim_body(*args, **kwargs)

    queue._claim = slow_claim  # type: ignore[method-assign]
    claiming = asyncio.create_task(queue.claim(["chat"], worker_id="w"))
    await asyncio.sleep(0)
    draining = asyncio.create_task(queue.drain())
    await asyncio.sleep(0.05)
    assert not draining.done()  # the claim that started first finishes first
    gate.set()
    assert (await claiming) is not None
    await draining
    await queue.enqueue("chat", {})
    assert await queue.claim(["chat"], worker_id="w") is None
    queue.undrain()
    assert await queue.claim(["chat"], worker_id="w") is not None
