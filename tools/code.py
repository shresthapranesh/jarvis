"""Stateful code-execution tool — `run_cell`, a persistent notebook session.

Companion to `execute` (`tools/execute.py`), which is stateless: every
`execute` call runs in a fresh subprocess. `run_cell` instead sends code to a
long-lived IPython kernel scoped to the conversation (see `core/kernels.py`),
so variables, imports, and loaded data persist across calls like cells in a
Jupyter notebook.

Scoped by conversation_id when present (chat / Telegram / Discord), falling
back to the LangGraph thread_id otherwise (CLI / automation / workflow) so the
session still works there. The kernel is started lazily on the first cell.
"""

from __future__ import annotations

import logging
import time

from langchain_core.tools import tool
from langgraph.config import get_config as _get_lg_config

from core.kernels import DEFAULT_CELL_TIMEOUT, get_kernel_registry

logger = logging.getLogger(__name__)


def _session_key() -> str | None:
    """Session key for the current run: conversation_id, else thread_id."""
    try:
        configurable = _get_lg_config().get("configurable") or {}
    except Exception:
        return None
    key = configurable.get("conversation_id") or configurable.get("thread_id")
    return str(key) if key else None


@tool
async def run_cell(code: str) -> str:
    """Run Python in a STATEFUL notebook session and return its output.

    Like a Jupyter cell: variables, imports, and loaded data PERSIST across
    calls within this conversation. Define something in one call and use it in
    the next — no need to re-import or re-fetch.

    Use run_cell when you're building up state iteratively: load a dataset
    once then explore it over several steps, define helpers and reuse them,
    keep an open client/connection between calls, or debug by inspecting
    variables from a previous cell.

    Use execute() instead for a one-shot, fully self-contained snippet that
    needs a clean slate — run_cell carries over everything you defined before,
    including earlier mistakes.

    Output: like a notebook, the value of the last expression is echoed (no
    print needed), alongside anything you print and any traceback. Rich
    outputs (e.g. matplotlib figures) are noted but not rendered as text.

    Timeout: 60s per cell. On timeout the kernel is interrupted but your
    session state (variables, imports) is preserved, so you can continue.
    """
    key = _session_key()
    if not key:
        return "No session context — run_cell is unavailable here; use execute() instead."
    start = time.monotonic()
    logger.info("→ run_cell [%s] (%d chars)", key, len(code))
    result = await get_kernel_registry().run_cell(key, code, timeout=DEFAULT_CELL_TIMEOUT)
    logger.info("← run_cell [%s] (%d chars, %.0fms)", key, len(result), (time.monotonic() - start) * 1000)
    return result
