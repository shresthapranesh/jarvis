"""The edge's kernels (`edge/src/kernels/`) diffed against `core/kernels.py`.

Each case runs the same cells through Python's `KernelRegistry` and through
the edge's `/internal/kernels/run`, and compares what the agent would read.
Both start real `ipykernel` processes from this venv. Skipped when `cargo`
isn't installed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.kernels import KernelRegistry
from edge_support import _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture

KERNEL_ENV = {"JARVIS_KERNEL_PYTHON": sys.executable, "JARVIS_APP_DIR": str(Path(__file__).resolve().parent.parent)}
JSON = {"content-type": "application/json"}


@pytest.fixture
async def edge(database, work_dir: Path, edge_binary: Path):
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", KERNEL_ENV) as client:
        client.timeout = httpx.Timeout(120)
        yield client


@pytest.fixture
async def py_kernels():
    registry = KernelRegistry()
    yield registry
    await registry.shutdown_all()


async def _edge_run(client: httpx.AsyncClient, key: str, code: str, **kw: Any) -> str:
    r = await client.post("/internal/kernels/run", json={"key": key, "code": code, **kw})
    assert r.status_code == 200, r.text
    return r.json()["output"]


async def _py_run(registry: KernelRegistry, key: str, code: str, timeout: float = 60, **kw: Any) -> str:
    """As `tools/code.py:run_cell` calls it."""
    from core.tool_gate import has_open_gate

    async def held() -> bool:
        return await has_open_gate(kw.get("conversation_id"))

    return await registry.run_cell(key, code, timeout=timeout, hold_check=held, **kw)


async def _both(edge: httpx.AsyncClient, registry: KernelRegistry, key: str, cells: list) -> tuple[list, list]:
    """Each `(code, kwargs)` through Python's registry, then the edge."""
    python = [await _py_run(registry, key, code, **kw) for code, kw in cells]
    got = [await _edge_run(edge, key, code, **kw) for code, kw in cells]
    return python, got


CELLS: list[tuple[str, dict[str, Any]]] = [
    ("x = 41", {}),
    ("x + 1", {}),
    ("import sys\nprint('out')\nprint('err', file=sys.stderr)\nx", {}),
    ("from IPython.display import display, HTML\ndisplay(HTML('<b>hi</b>'))\ndisplay(1)", {}),
    ("undefined_name", {}),
    ("def f():\n    raise ValueError('boom')\nf()", {}),
    ("print('é' * 30010)", {}),
    ("None", {}),
    ("for i in range(3): print(i)", {}),
    ('"""two\nlines"""', {}),
    ("search.__module__, read.__module__, jarvis.__name__", {}),
    # The SDK is scoped to the cell's conversation, and again when it joins a project.
    ("(jarvis._conversation_id, jarvis._project_id)", {"conversation_id": "conv-1"}),
    ("(jarvis._conversation_id, jarvis._project_id)", {"conversation_id": "conv-1", "project_id": "p1"}),
]


async def test_cells_read_the_same(edge, py_kernels):
    python, got = await _both(edge, py_kernels, "k", CELLS)
    assert got == python
    assert got[1] == "42" and "NameError" in got[4] and got[-1] == "('conv-1', 'p1')"
    # 30,010 characters and a newline, trimmed.
    assert got[6].endswith("\n... [truncated 10 chars]")
    assert "\x1b[" not in got[5]


async def test_a_cell_past_its_timeout_is_interrupted(edge, py_kernels):
    cells = [
        ("x = 1", {}),
        ("import time\nprint('started', flush=True)\ntime.sleep(30)", {"timeout": 1}),
        # …and the session survives it.
        ("x", {}),
    ]
    python, got = await _both(edge, py_kernels, "k", cells)
    assert got == python
    assert got[1].startswith("started\n") and "KeyboardInterrupt" in got[1]
    assert got[1].endswith("[execution timed out after 1s — kernel interrupted; session state is preserved]")
    assert got[2] == "1"


async def test_an_open_approval_holds_the_timeout(edge, py_kernels):
    from db import ops
    from db.engine import async_session

    async with async_session() as session:
        await ops.create_approval(session, source="tool", status="pending", parent_id="conv-h", question="ok?")
    cell = ("import time\ntime.sleep(3)\n'done'", {"timeout": 1, "conversation_id": "conv-h"})
    python, got = await _both(edge, py_kernels, "k", [cell])
    assert got == python == ["'done'"]


async def test_shutdown_forgets_the_session(edge, py_kernels):
    await _py_run(py_kernels, "k", "x = 1")
    await _edge_run(edge, "k", "x = 1")
    await py_kernels.shutdown("k")
    r = await edge.post("/internal/kernels/shutdown", json={"key": "k"})
    assert r.json() == {"ok": True}
    python, got = await _both(edge, py_kernels, "k", [("x", {})])
    assert got == python and "NameError" in got[0]


async def test_a_dead_kernel_is_restarted(edge, py_kernels):
    cells = [("x = 1", {}), ("import os\nos._exit(1)", {"timeout": 2}), ("x", {})]
    python, got = await _both(edge, py_kernels, "k", cells)
    assert got == python
    assert "NameError" in got[2]


async def test_input_fails_at_once(edge, py_kernels):
    """Intended: the edge never offers stdin, so `input()` raises at once.
    Python's client offered it with nobody to answer, so the cell hung until
    its timeout."""
    python = await _py_run(py_kernels, "k", "input('name? ')", timeout=2)
    assert python.endswith("[execution timed out after 2s — kernel interrupted; session state is preserved]")
    loop = asyncio.get_running_loop()
    started = loop.time()
    got = await _edge_run(edge, "k", "input('name? ')", timeout=30)
    assert "StdinNotImplementedError" in got and loop.time() - started < 10


async def test_a_caller_that_goes_away_interrupts_the_cell(edge):
    """…and the next cell waits for that interrupt to land. Python's cancel
    path doesn't, and a cell sent before the kernel has raised is aborted:
    no output at all."""
    await _edge_run(edge, "k", "x = 1")
    with pytest.raises(httpx.ReadTimeout):
        await edge.post("/internal/kernels/run", json={"key": "k", "code": "import time\ntime.sleep(60)"}, timeout=2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await _edge_run(edge, "k", "x") == "1"
    assert loop.time() - started < 15


@pytest.mark.parametrize("headers", [
    {"origin": "https://example.com", **JSON},
    {"content-type": "text/plain"},
])
async def test_a_browser_cannot_run_code(edge, headers):
    body = '{"key": "k", "code": "1"}'
    for path in ("run", "shutdown"):
        r = await edge.post(f"/internal/kernels/{path}", content=body, headers=headers)
        assert r.status_code == 403


async def test_python_behind_the_edge_runs_cells_there(edge, monkeypatch):
    """`get_kernel_registry()` behind the edge: cells run in the edge's
    kernels, a cancelled one is interrupted there, and no kernel holds this
    process up."""
    import core.kernels as kernels
    from core.edge_link import current_holds

    monkeypatch.setenv("JARVIS_EDGE_URL", f"http://127.0.0.1:{edge.base_url.port}")
    monkeypatch.setattr(kernels, "_registry", None)
    registry = kernels.get_kernel_registry()
    assert isinstance(registry, kernels.EdgeKernels)

    assert await registry.run_cell("conv", "y = 6 * 7") == "(no output)"
    assert await _edge_run(edge, "conv", "y") == "42"
    run = asyncio.create_task(registry.run_cell("conv", "import time\ntime.sleep(60)"))
    await asyncio.sleep(2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert await asyncio.wait_for(registry.run_cell("conv", "y"), 15) == "42"
    assert current_holds() == []
    await registry.shutdown_all()
    assert await registry.run_cell("conv", "y") == "42"
    await registry.shutdown("conv")
    assert "NameError" in await registry.run_cell("conv", "y")
    await registry.shutdown("conv")
