"""The REST routes the edge serves itself (`edge/src/rest.rs`, `logs.rs`):
raw artifact and document downloads, upload staging, and the log viewer.

Python's routers run on a bare FastAPI app over the same database; the edge
runs with a dead backend, so a request it hands to Python comes back 502 —
"served by the edge" is asserted, not assumed.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import pytest

from edge_support import _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture


@pytest.fixture
async def python(database):
    from fastapi import FastAPI

    from server import routes_artifacts, routes_documents, routes_uploads

    app = FastAPI()
    for module in (routes_artifacts, routes_documents, routes_uploads):
        app.include_router(module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as client:
        yield client


@pytest.fixture
async def files(database, work_dir: Path) -> Path:
    """Artifacts and documents with their files, in shapes the headers vary by."""
    from db import async_session
    from db.models import Artifact, Document

    d = work_dir / "files"
    d.mkdir()
    (d / "report.md").write_text("# Title\n" + "x" * 300)
    (d / "clip.mp3").write_bytes(bytes(range(256)) * 40)
    (d / "data.bin").write_bytes(b"\0\1\2" * 10)
    (d / "notes.txt").write_text("plain notes")
    os.utime(d / "clip.mp3", (1_700_000_000.123456, 1_700_000_000.123456))
    async with async_session() as s:
        s.add_all([
            Artifact(id="a-md", title="Weekly report", filename=str(d / "report.md"), kind="markdown"),
            Artifact(id="a-audio", title="Café ✓ take 2", filename=str(d / "clip.mp3"), kind="audio", mime_type="audio/mpeg"),
            Artifact(id="a-untitled", title="", filename=str(d / "data.bin"), kind="file"),
            Artifact(id="a-gone", title="Gone", filename=str(d / "missing.pdf"), kind="file"),
            Artifact(id="a-dir", title="Dir", filename=str(d), kind="file"),
            Document(id="d-1", conversation_id="c", filename="notes v1.txt", mime_type="text/plain", path=str(d / "notes.txt"),
                     size=11),
            Document(id="d-gone", conversation_id="c", filename="x.pdf", mime_type="application/pdf", path=str(d / "nope.pdf"),
                     size=1),
        ])
        await s.commit()
    return d


@pytest.fixture
async def edge(files, work_dir: Path, edge_binary: Path, tmp_path_factory):
    staging = tmp_path_factory.mktemp("edge-work")
    async with _run_edge(edge_binary, staging, work_dir / "database.db", {"JARVIS_EDGE_LOG": "info"}) as client:
        client.work_dir = staging
        yield client


_HEADERS = ("content-type", "content-disposition", "content-length", "content-range", "accept-ranges",
            "last-modified", "etag")


def _shape(resp: httpx.Response) -> tuple:
    return resp.status_code, {h: resp.headers.get(h) for h in _HEADERS}, resp.content


async def _same(python: httpx.AsyncClient, edge: httpx.AsyncClient, method: str, url: str, headers: dict | None = None):
    expected = await python.request(method, url, headers=headers)
    got = await edge.request(method, url, headers=headers)
    assert got.status_code != 502, f"edge proxied {method} {url} {headers}"
    assert _shape(got) == _shape(expected), (method, url, headers)
    return expected


async def test_downloads_answer_as_starlettes_file_response(python, edge, files):
    for url in ("/artifacts/a-md/raw", "/artifacts/a-audio/raw", "/artifacts/a-untitled/raw", "/artifacts/a-gone/raw",
                "/artifacts/nope/raw", "/documents/d-1/raw", "/documents/d-gone/raw", "/documents/nope/raw"):
        await _same(python, edge, "GET", url)
    whole = await python.get("/artifacts/a-audio/raw")
    etag, modified = whole.headers["etag"], whole.headers["last-modified"]
    for rng in ("bytes=0-99", "bytes=100-", "bytes=-50", "bytes=10000-", "bytes=9000-99999", "bytes=5-4", "items=0-1",
                "bytes", "bytes=,", "bytes=abc", "BYTES = 3 - 7", "bytes=0-0"):
        await _same(python, edge, "GET", "/artifacts/a-audio/raw", {"range": rng})
    for if_range in (etag, modified, '"stale"'):
        await _same(python, edge, "GET", "/artifacts/a-audio/raw", {"range": "bytes=1-2", "if-range": if_range})


async def test_what_the_edge_leaves_to_python(edge, files):
    """Several ranges, a range number only `int()` reads, a path that isn't a
    file, a HEAD (FastAPI's 405), an urlencoded upload: Python's to answer
    (here, the dead backend's 502)."""
    assert (await edge.head("/artifacts/a-md/raw")).status_code == 502
    for url, headers in (("/artifacts/a-audio/raw", {"range": "bytes=0-1,5-9"}),
                         ("/artifacts/a-audio/raw", {"range": "bytes=+1-2"}),
                         ("/artifacts/a-dir/raw", None)):
        assert (await edge.get(url, headers=headers)).status_code == 502, (url, headers)
    assert (await edge.post("/uploads", data={"file": "x"})).status_code == 502  # urlencoded


def _staged(d: Path) -> list[tuple[str, bytes, dict]]:
    """Each staged upload: its bytes and its meta, ids and stamps masked."""
    out = []
    for meta in sorted(d.glob("*.meta.json")):
        body = json.loads(meta.read_text())
        raw = meta.read_text()
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00", body.pop("created_at"))
        out.append((re.sub(r'"created_at": "[^"]*"', '"created_at": <now>', raw), (d / meta.name.removesuffix(".meta.json")).read_bytes(), body))
    assert not [p for p in d.iterdir() if p.name.startswith(".")], "a partial upload was left behind"
    return sorted(out, key=lambda r: (r[2]["filename"], r[1]))


async def test_uploads_are_staged_as_python_stages_them(python, edge, work_dir: Path):
    from core.config import get_config

    cases = [
        {"files": {"file": ("notes.txt", b"hello", "text/plain")}},
        {"files": {"file": ("Café ✓ résumé.pdf", b"%PDF-1", "application/pdf")}},
        {"files": {"file": ("raw.bin", b"\0\1", "")}},
        {"files": [("file", ("one.txt", b"1", "text/plain")), ("file", ("two.txt", b"22", "text/x"))]},
        {"files": {"other": ("a.txt", b"x", "text/plain")}},
        {"files": {"file": (None, b"just text")}},
        {"content": b"abc", "headers": {"content-type": "text/plain"}},
        {"files": {"file": ("", b"xy", "text/plain")}},
        {"content": b"--xx\r\nbroken", "headers": {"content-type": "multipart/form-data; boundary=xx"}},
    ]
    mask = lambda body: {**body, "uploadId": "<id>"} if "uploadId" in body else body  # noqa: E731
    for case in cases:
        expected = await python.post("/uploads", **case)
        got = await edge.post("/uploads", **case)
        assert (got.status_code, mask(got.json())) == (expected.status_code, mask(expected.json())), case
        if "uploadId" in got.json():
            assert (edge.work_dir / "staging" / got.json()["uploadId"]).exists()
    assert _staged(edge.work_dir / "staging") == _staged(get_config().staging_dir)


async def test_an_oversized_upload_is_refused(edge):
    big = b"\0" * (100 * 1024 * 1024 + 1)
    got = await edge.post("/uploads", files={"file": ("big.bin", big, "application/octet-stream")}, timeout=60)
    assert (got.status_code, got.json()) == (413, {"error": "upload exceeds 100 MiB limit"})
    staging = edge.work_dir / "staging"
    assert not list(staging.iterdir()) if staging.exists() else True


async def test_the_log_viewer_shows_the_edge_and_a_linked_worker(edge, monkeypatch):
    """The edge's own records, and what a linked Python logs, in one stream;
    a cross-origin page is refused."""
    import logging

    from core import edge_link, log_setup
    from core.edge_link import EdgeLink
    from test_edge_runs import _edge_owns_runs, _until

    listed = (await edge.get("/server-logs")).json()["logs"]
    assert listed and all(set(r) == {"ts", "level", "logger", "message"} for r in listed)
    assert any(r["logger"].startswith("edge") and r["level"] == "INFO" for r in listed)
    for path in ("/server-logs", "/server-logs/stream"):
        refused = await edge.get(path, headers={"origin": "https://evil.example"})
        assert (refused.status_code, refused.json()) == (403, {"error": "cross-origin not allowed"})
    assert (await edge.get("/server-logs", headers={"origin": "http://localhost:5173"})).status_code == 200

    handler = log_setup.BroadcastHandler()
    handler.attach_loop(asyncio.get_running_loop())
    monkeypatch.setattr(log_setup, "_broadcast_handler", handler)
    probe = logging.getLogger("jarvis.test.probe")
    probe.addHandler(handler)
    probe.setLevel(logging.INFO)
    probe.info("before the link — é")
    link = EdgeLink(f"ws://127.0.0.1:{edge.base_url.port}/internal/worker")
    link.start()
    monkeypatch.setattr(edge_link, "_link", link)
    try:
        await _until(lambda: _edge_owns_runs(edge))
        probe.warning("after the link")

        async def arrived() -> bool:
            logs = (await edge.get("/server-logs")).json()["logs"]
            return {"before the link — é", "after the link"} <= {r["message"] for r in logs}

        await _until(arrived)
        mine = [r for r in (await edge.get("/server-logs")).json()["logs"] if r["logger"] == "jarvis.test.probe"]
        assert [(r["level"], r["message"]) for r in mine] == [("INFO", "before the link — é"), ("WARNING", "after the link")]
        # The stream opens with the backfill, Python's `json.dumps` of it.
        async with edge.stream("GET", "/server-logs/stream") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            head = ""
            async for chunk in resp.aiter_text():
                head += chunk
                if "\n\n" in head:
                    break
        event, data = head.split("\n")[:2]
        assert event == "event: backfill"
        backfill = json.loads(data.removeprefix("data: "))
        assert "before the link \\u2014 \\u00e9" in data  # ensure_ascii, as json.dumps
        assert {"before the link — é", "after the link"} <= {r["message"] for r in backfill}
    finally:
        probe.removeHandler(handler)
        await link.stop()
