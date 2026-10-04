"""Shared pieces for the tests that drive the Rust edge (`edge/`).

`edge_binary` builds it once per session; `_run_edge` starts one over a given
database with its backend pointed at a closed port, so anything it proxies
instead of answering fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

EDGE_DIR = Path(__file__).resolve().parent.parent / "edge"


@pytest.fixture(scope="session")
def edge_binary() -> Path:
    if shutil.which("cargo") is None:
        pytest.skip("cargo not installed")
    subprocess.run(
        ["cargo", "build", "--quiet", "--manifest-path", str(EDGE_DIR / "Cargo.toml")],
        check=True,
    )
    return EDGE_DIR / "target" / "debug" / "jarvis-edge"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _run_edge(edge_binary: Path, work_dir: Path, db: Path, extra_env: dict[str, str] | None = None):
    """An edge over `db`, its artifacts under `work_dir`, with a dead backend
    unless `extra_env` names another."""
    port, dead = _free_port(), _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "WORK_DIR": str(work_dir),
        "JARVIS_EDGE_BIND": f"127.0.0.1:{port}",
        # Nothing listens here: a proxied operation fails loudly.
        "JARVIS_BACKEND_URL": f"http://127.0.0.1:{dead}",
        "JARVIS_EDGE_LOG": "warn",
        **(extra_env or {}),
    }
    env.pop("ARTIFACTS_DIR", None)
    # No bot connects, no turn is the edge's, and no model call leaves the
    # machine, unless a test says so.
    for var in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "JARVIS_MCP_SERVERS", "MCP_SERVERS",
                "GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "META_API_KEY", "OLLAMA_HOST"):
        if var not in (extra_env or {}):
            env.pop(var, None)
    # Chat turns are Python's here unless a test hands them to the edge.
    env["JARVIS_AGENT_RUNTIME"] = (extra_env or {}).get("JARVIS_AGENT_RUNTIME", "python")
    # cwd = work_dir so the edge's .env lookup can't find the repo's .env.
    proc = subprocess.Popen([str(edge_binary)], env=env, cwd=work_dir)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("edge did not start")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            yield client
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _gid(type_name: str, raw: str) -> str:
    from strawberry.relay.utils import to_base64

    return to_base64(type_name, raw)


@contextlib.asynccontextmanager
async def fake_worker(client: httpx.AsyncClient):
    """Link a worker that runs nothing, so the edge serves the fields it only
    serves while linked — for diffing them against Python on a twin database,
    where this process's own runs must not be mirrored into the edge."""
    import json

    import websockets

    from core.edge_link import PROTOCOL

    async with websockets.connect(f"ws://127.0.0.1:{client.base_url.port}/internal/worker") as ws:
        await ws.send(json.dumps({"type": "hello", "protocol": PROTOCOL, "instance": "fake", "pid": 0}))
        await ws.send(json.dumps({"type": "snapshot", "tasks": []}))

        async def drain() -> None:
            async for _ in ws:  # wake, adopt_queued: nobody here to do them
                pass

        reader = asyncio.create_task(drain())
        deadline = time.monotonic() + 10
        while (await client.post("/graphql", json={"query": "{ runningTasks { id } }"})).status_code != 200:
            assert time.monotonic() < deadline, "the edge never saw the fake worker"
            await asyncio.sleep(0.05)
        try:
            yield ws
        finally:
            reader.cancel()
