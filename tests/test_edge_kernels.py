"""The edge's kernels (`edge/src/kernels/`) diffed against `core/kernels.py`.

Each case runs the same cells through Python's `KernelRegistry` and through
the edge's kernels (`jarvis-edge --kernel-cells`, one JSON command per line),
and compares what the agent would read. Both start real `ipykernel`
processes from this venv. Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from core.kernels import KernelRegistry
from edge_support import edge_binary  # noqa: F401 — edge_binary is a fixture
from python_golden import recorded

KERNEL_ENV = {"JARVIS_KERNEL_PYTHON": sys.executable, "JARVIS_APP_DIR": str(Path(__file__).resolve().parent.parent)}


class EdgeKernels:
    """`jarvis-edge --kernel-cells` over the test database."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    async def ask(self, command: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write((json.dumps(command) + "\n").encode())
        await self.proc.stdin.drain()
        return json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 120))


@pytest.fixture
async def edge(database, work_dir: Path, edge_binary: Path):
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{work_dir / 'database.db'}", "WORK_DIR": str(work_dir),
           "JARVIS_EDGE_LOG": "warn", **KERNEL_ENV}
    proc = await asyncio.create_subprocess_exec(str(edge_binary), "--kernel-cells", env=env, cwd=work_dir,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    yield EdgeKernels(proc)
    assert proc.stdin
    proc.stdin.close()
    await asyncio.wait_for(proc.wait(), 30)


@pytest.fixture
async def py_kernels():
    registry = KernelRegistry()
    yield registry
    await registry.shutdown_all()


async def _edge_run(edge: EdgeKernels, key: str, code: str, **kw: Any) -> str:
    out = await edge.ask({"key": key, "code": code, **kw})
    assert "output" in out, out
    return out["output"]


async def _py_run(registry: KernelRegistry, key: str, code: str, timeout: float = 60, **kw: Any) -> str:
    """As `tools/code.py:run_cell` calls it."""
    from core.tool_gate import has_open_gate

    async def held() -> bool:
        return await has_open_gate(kw.get("conversation_id"))

    return await registry.run_cell(key, code, timeout=timeout, hold_check=held, **kw)


async def _both(edge: EdgeKernels, registry: KernelRegistry, key: str, cells: list) -> tuple[list, list]:
    """Each `(code, kwargs)` through Python's registry, then the edge."""
    async def python_side() -> list[str]:
        return [await _py_run(registry, key, code, **kw) for code, kw in cells]

    python = await recorded(python_side)
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
    async def python_side() -> list[str]:
        await _py_run(py_kernels, "k", "x = 1")
        await py_kernels.shutdown("k")
        return [await _py_run(py_kernels, "k", "x")]

    python = await recorded(python_side)
    await _edge_run(edge, "k", "x = 1")
    assert await edge.ask({"shutdown": "k"}) == {"ok": True}
    got = [await _edge_run(edge, "k", "x")]
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
    python = await recorded(lambda: _py_run(py_kernels, "k", "input('name? ')", timeout=2))
    assert python.endswith("[execution timed out after 2s — kernel interrupted; session state is preserved]")
    loop = asyncio.get_running_loop()
    started = loop.time()
    got = await _edge_run(edge, "k", "input('name? ')", timeout=30)
    assert "StdinNotImplementedError" in got and loop.time() - started < 10
