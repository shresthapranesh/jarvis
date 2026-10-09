"""What a page load gets: the SPA, `/health`, and the queries it makes
(`models`, `todos`, `browserAvailable`) — diffed against Python's answers,
recorded (`python_golden.py`). Plus the maintenance gates that keep the
timers from calling a model for nothing.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — a fixture
from python_golden import recorded
from seed import execute, insert, put_kv, row, set_setting, set_todos, stamp
from test_edge_parity import _assert_same, _edge, _relay_text

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
async def edge(database: Path, work_dir: Path, edge_binary: Path):
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        yield client


async def _set(key: str, value: str) -> None:
    set_setting(Path(os.environ["WORK_DIR"]) / "database.db", key, value)


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

    # OpenAI-compatible endpoints: providers, discoverable, and listed without
    # their keys — every row Python's parse_endpoints skips, the edge skips.
    endpoints = [
        {"name": "groq", "base_url": " https://api.groq.com/openai/v1/ ", "api_key": "sk-secret"},
        {"name": "lmstudio", "base_url": "http://localhost:1234/v1", "api_key": ""},
        {"name": "groq", "base_url": "http://elsewhere"},
        {"name": "ollama", "base_url": "http://x"},
        {"name": "Bad", "base_url": "http://x"},
        {"name": "nourl"},
        {"name": "a" * 33, "base_url": "http://x"},
        "junk",
    ]
    await _set("models.endpoints", json.dumps(endpoints))
    await _set("models.custom", json.dumps([{"id": "groq:llama-3.3-70b", "label": "Llama"}]))
    data = await _assert_same(edge, _relay_text("ModelCatalogQuery"))
    models = data["data"]["models"]
    assert models["endpoints"] == [
        {"name": "groq", "baseUrl": "https://api.groq.com/openai/v1", "hasKey": True},
        {"name": "lmstudio", "baseUrl": "http://localhost:1234/v1", "hasKey": False},
    ]
    assert {"groq", "lmstudio"} <= set(models["providers"]) & set(models["discoverableProviders"])
    assert "sk-secret" not in json.dumps(data)
    for raw in ("not json", '{"name": "x"}', ""):
        await _set("models.endpoints", raw)
        await _assert_same(edge, _relay_text("ModelCatalogQuery"))


async def test_a_custom_model_list_that_cant_be_read_is_an_error(edge):
    await _set("models.custom", json.dumps([{"id": 7}]))
    body = (await _edge(edge, _relay_text("ModelCatalogQuery"))).json()
    assert body["errors"][0]["message"].startswith("the custom models setting (models.custom) can't be read: ")


async def test_todos(edge, database):
    query = _relay_text("TodoListQuery")
    # No `thread_state` row yet.
    await _assert_same(edge, query, {"conversationId": "c1"})

    todos = [
        "a legacy string",
        {"text": "doing", "status": "in_progress"},
        {"text": "done", "status": "done"},
        {"text": 42, "status": "bogus"},
        {"status": "done"},
    ]
    set_todos(database, "c1", todos)
    set_todos(database, "c2", "not a list")
    set_todos(database, "c3", None)
    set_todos(database, "c4", [])
    for thread in ("c1", "c2", "c3", "c4", "nobody"):
        await _assert_same(edge, query, {"conversationId": thread})
    data = await _assert_same(edge, query, {"conversationId": "c1"})
    assert [t["status"] for t in data["data"]["todos"]] == ["pending", "in_progress", "done", "pending"]
    assert [t["text"] for t in data["data"]["todos"]] == ["a legacy string", "doing", "done", "42"]


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
    # An https endpoint is probed here too.
    await _set("browser.cdp_url", f"https://127.0.0.1:{_free_port()}")
    data = await _assert_same(edge, query)
    assert data["data"]["browserAvailable"] is False


# ── maintenance gates ────────────────────────────────────────────────────────


def _edge_due(edge_binary: Path, work_dir: Path) -> dict[str, Any]:
    env = {**os.environ, "WORK_DIR": str(work_dir), "DATABASE_URL": f"sqlite+aiosqlite:///{work_dir}/database.db",
           "JARVIS_EDGE_LOG": "warn"}
    out = subprocess.run([str(edge_binary), "--maintenance-due"], env=env, cwd=work_dir,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


async def _python_due(monkeypatch) -> dict[str, bool]:
    """Each sweep's own verdict: reaching for an LLM means it found work."""
    from core import project_memory_consolidation as pmc
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
    return {
        "memory_consolidation": _transcript_block(messages)[1] is not None,
        "project_memory": project,
    }


async def test_maintenance_gates_match_the_sweeps(database: Path, work_dir: Path, edge_binary: Path, monkeypatch):
    now = datetime.now(timezone.utc)

    async def check(label: str) -> None:
        edge, python = _edge_due(edge_binary, work_dir), await recorded(lambda: _python_due(monkeypatch))
        assert edge == python, label

    await check("empty")

    insert(database, "projects", id="p1", name="P")
    insert(database, "conversations", id="c1", title="t", model="m", project_id="p1", created_at=now - timedelta(days=2))
    insert(database, "conversations", id="ghost", title="t", model="m", ephemeral=True, project_id="p1")

    async def say(mid: str, at: datetime, conv: str = "c1", text: str = "x" * 400, status: str = "done") -> None:
        insert(database, "messages", id=mid, conversation_id=conv, role="user", content=text, created_at=at, status=status)

    await say("ghost-1", now - timedelta(hours=3), conv="ghost")
    await check("only incognito material")
    await say("m1", now - timedelta(minutes=5))
    await check("new, but too fresh and too short")
    await say("m2", now - timedelta(minutes=4))
    await check("enough, but still active")

    async def age(minutes: int) -> None:
        for mid in ("m1", "m2"):
            m = row(database, "messages", mid)
            assert m is not None
            at = datetime.fromisoformat(m["created_at"]) - timedelta(minutes=minutes)
            execute(database, "UPDATE messages SET created_at = ? WHERE id = ?", stamp(at), mid)

    await age(6)
    await check("quiet for ten minutes: still active")
    await age(14)
    await check("quiet")
    put_kv(database, "project_memory_consolidation", "p1",
           {"messages_through": (now - timedelta(minutes=24, seconds=30)).isoformat()})
    await check("watermark past the first message: too little left")
    await say("m0", now - timedelta(hours=30), text="y" * 300)
    put_kv(database, "project_memory_consolidation", "p1", {"messages_through": "garbage"})
    await check("unreadable watermark counts as none")

    put_kv(database, "memory_consolidation", "state", {"last_run_at": now.isoformat()})
    await check("legacy watermark past everything")
    put_kv(database, "memory_consolidation", "state",
           {"messages_through": "", "last_run_at": (now - timedelta(hours=1)).isoformat()})
    await check("empty watermark falls back to last_run_at")
    await say("r1", now - timedelta(minutes=30), status="running")
    await check("the first new message is a reply still being written")
    put_kv(database, "memory_consolidation", "state",
           {"messages_through": (now - timedelta(minutes=30)).replace(tzinfo=None).isoformat()})
    await check("a naive watermark exactly at a message is exclusive")


# ── the SPA and the routes it must not shadow ────────────────────────────────


async def test_spa_is_served_and_the_server_routes_are_not_shadowed(database, work_dir: Path, edge_binary: Path,
                                                                     monkeypatch):
    app = work_dir / "app"
    (app / "static" / "dist" / "assets").mkdir(parents=True)
    (app / "static" / "dist" / "index.html").write_text("<!doctype html>spa")
    (app / "static" / "dist" / "assets" / "app-1.js").write_text("console.log(1)")
    (work_dir / "secret.txt").write_text("nope")
    monkeypatch.setenv("JARVIS_APP_DIR", str(app))
    routes = await recorded()  # every GET route the Python app served
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
        for path in ("/c/abc", "/artifacts", "/artifacts/a1", "/documents/d1/raw", "/uploads",
                     "/../secret.txt", "/%2e%2e/secret.txt"):
            resp = await client.get(path)
            assert resp.text == "<!doctype html>spa", path
        # The routes Python served are never the SPA — except FastAPI's own
        # API docs, which went with it.
        answered = {"/health": 200, "/artifacts/a1/raw": 404, "/server-logs": 200}
        for path in routes:
            if path == "/server-logs/stream":
                continue  # never ends; tests/test_edge_rest.py reads it
            if path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
                continue
            resp = await client.get(path)
            assert resp.text != "<!doctype html>spa", path
            if path in answered:
                assert resp.status_code == answered[path], path
