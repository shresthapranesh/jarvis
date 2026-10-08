"""Shared pieces for the tests that drive the Rust edge (`edge/`).

`edge_binary` builds it once per session; `_run_edge` starts one over a given
database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

EDGE_DIR = Path(__file__).resolve().parent.parent / "edge"
GENERATED = EDGE_DIR.parent / "frontend" / "src" / "__generated__"


def _relay_text(operation: str) -> str:
    """The query text Relay compiled for a frontend operation."""
    src = (GENERATED / f"{operation}.graphql.ts").read_text()
    match = re.search(r'"text": (".*?(?<!\\)")', src, re.S)
    assert match, f"no query text in {operation}"
    return json.loads(match.group(1))


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
    """An edge over `db`, its artifacts under `work_dir`."""
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "WORK_DIR": str(work_dir),
        "JARVIS_EDGE_BIND": f"127.0.0.1:{port}",
        "JARVIS_EDGE_LOG": "warn",
        **(extra_env or {}),
    }
    # Queued runs stay queued unless a test runs them (`JARVIS_RUN_JOBS=1`).
    if "JARVIS_RUN_JOBS" not in (extra_env or {}):
        env["JARVIS_RUN_JOBS"] = "0"
    env.pop("ARTIFACTS_DIR", None)
    # No bot connects and no model call leaves the machine, unless a test
    # says so.
    for var in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "JARVIS_MCP_SERVERS", "MCP_SERVERS",
                "GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "META_API_KEY", "OLLAMA_HOST"):
        if var not in (extra_env or {}):
            env.pop(var, None)
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


async def until(check, timeout: float = 10.0) -> None:
    """Wait for `check()` (async) to hold."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def startup_sweep(edge_binary: Path, work_dir: Path, db: Path) -> None:
    """What a start sweeps up — zombie run rows, approvals no run waits on,
    incognito conversations — done to `db` now, so a copy of it that a
    server then starts on is already as that server would leave it."""
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}", "WORK_DIR": str(work_dir), "JARVIS_EDGE_LOG": "warn"}
    env.pop("ARTIFACTS_DIR", None)
    subprocess.run([str(edge_binary), "--startup-sweep"], env=env, cwd=work_dir, check=True)


def _gid(type_name: str, raw: str) -> str:
    """A Relay global id, as the server mints them."""
    import base64

    return base64.b64encode(f"{type_name}:{raw}".encode()).decode()
