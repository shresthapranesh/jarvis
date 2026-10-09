"""The REST routes the edge serves itself (`edge/src/rest.rs`, `logs.rs`):
raw artifact downloads and the log viewer.

Each answer is diffed against what Python's router answered on a bare
FastAPI app over the same database, recorded while it existed
(`python_golden.py`).

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from edge_support import _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from python_golden import recorded
from seed import insert


@pytest.fixture
async def files(database: Path, work_dir: Path) -> Path:
    """Artifacts with their files, in shapes the headers vary by."""
    d = work_dir / "files"
    d.mkdir()
    (d / "report.md").write_text("# Title\n" + "x" * 300)
    (d / "clip.mp3").write_bytes(bytes(range(256)) * 40)
    (d / "data.bin").write_bytes(b"\0\1\2" * 10)
    # Fixed times: a file's ETag and Last-Modified are made from its mtime.
    for name in ("report.md", "clip.mp3", "data.bin"):
        os.utime(d / name, (1_700_000_000.123456, 1_700_000_000.123456))
    for row in (
        dict(id="a-md", title="Weekly report", filename=str(d / "report.md"), kind="markdown"),
        dict(id="a-audio", title="Café ✓ take 2", filename=str(d / "clip.mp3"), kind="audio", mime_type="audio/mpeg"),
        dict(id="a-untitled", title="", filename=str(d / "data.bin"), kind="file"),
        dict(id="a-gone", title="Gone", filename=str(d / "missing.pdf"), kind="file"),
        dict(id="a-dir", title="Dir", filename=str(d), kind="file"),
    ):
        insert(database, "artifacts", **row)
    return d


@pytest.fixture
async def edge(files, work_dir: Path, edge_binary: Path, tmp_path_factory):
    edge_work = tmp_path_factory.mktemp("edge-work")
    async with _run_edge(edge_binary, edge_work, work_dir / "database.db", {"JARVIS_EDGE_LOG": "info"}) as client:
        yield client


_HEADERS = ("content-type", "content-disposition", "content-length", "content-range", "accept-ranges",
            "last-modified", "etag")


def _shape(resp: httpx.Response) -> tuple:
    return resp.status_code, {h: resp.headers.get(h) for h in _HEADERS}, resp.content


async def _same(edge: httpx.AsyncClient, method: str, url: str, headers: dict | None = None) -> tuple:
    expected = await recorded()
    got = _shape(await edge.request(method, url, headers=headers))
    assert got == expected, (method, url, headers)
    return got


async def test_downloads_answer_as_starlettes_file_response(edge, files):
    for url in ("/artifacts/a-md/raw", "/artifacts/a-audio/raw", "/artifacts/a-untitled/raw", "/artifacts/a-gone/raw",
                "/artifacts/nope/raw"):
        await _same(edge, "GET", url)
    _, whole, _ = await _same(edge, "GET", "/artifacts/a-audio/raw")
    etag, modified = whole["etag"], whole["last-modified"]
    for rng in ("bytes=0-99", "bytes=100-", "bytes=-50", "bytes=10000-", "bytes=9000-99999", "bytes=5-4", "items=0-1",
                "bytes", "bytes=,", "bytes=abc", "BYTES = 3 - 7", "bytes=0-0"):
        await _same(edge, "GET", "/artifacts/a-audio/raw", {"range": rng})
    for if_range in (etag, modified, '"stale"'):
        await _same(edge, "GET", "/artifacts/a-audio/raw", {"range": "bytes=1-2", "if-range": if_range})
    await _same(edge, "HEAD", "/artifacts/a-md/raw")  # FastAPI's 405


async def test_what_starlette_answered_differently(edge, files):
    """Several ranges, or a range number only `int()` reads: the whole file
    (Starlette sent multipart ranges). A path that isn't a file is missing
    (Starlette raised)."""
    whole = await edge.get("/artifacts/a-audio/raw")
    for rng in ("bytes=0-1,5-9", "bytes=+1-2"):
        got = await edge.get("/artifacts/a-audio/raw", headers={"range": rng})
        assert (got.status_code, got.content) == (200, whole.content), rng
    missing = await edge.get("/artifacts/a-dir/raw")
    assert (missing.status_code, missing.json()) == (404, {"error": "file missing"})


async def test_the_log_viewer(edge):
    """The server's own records, as Python's handler shaped them, listed and
    streamed; a cross-origin page is refused."""
    listed = (await edge.get("/server-logs")).json()["logs"]
    assert listed and all(set(r) == {"ts", "level", "logger", "message"} for r in listed)
    assert any(r["logger"].startswith("edge") and r["level"] == "INFO" for r in listed)
    for path in ("/server-logs", "/server-logs/stream"):
        refused = await edge.get(path, headers={"origin": "https://evil.example"})
        assert (refused.status_code, refused.json()) == (403, {"error": "cross-origin not allowed"})
    assert (await edge.get("/server-logs", headers={"origin": "http://localhost:5173"})).status_code == 200
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
    assert backfill[: len(listed)] == listed
