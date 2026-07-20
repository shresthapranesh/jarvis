"""Stateful code-execution tool — `run_cell`, a persistent notebook session.

The agent's sole code surface: code is sent to a long-lived IPython kernel
(see `core/kernels.py`), so variables, imports, and loaded data persist across
calls like cells in a Jupyter notebook.

Kernel scope comes from `ToolContext.code_session_key`: conversation_id when
present (chat / Telegram / Discord), else the LangGraph thread_id (CLI /
automation / workflow), unless an explicit `kernel_key` overrides it — which is
how parallel workers each get their own isolated kernel (see `tools/workers.py`).
The kernel is started lazily on the first cell.
"""

from __future__ import annotations

import logging
import time

from langchain_core.tools import tool

from core.kernels import DEFAULT_CELL_TIMEOUT, get_kernel_registry
from tools.context import current_ctx

logger = logging.getLogger(__name__)


@tool
async def run_cell(code: str) -> str:
    """Run Python in a STATEFUL notebook session and return its output.

    Like a Jupyter cell: variables, imports, and loaded data PERSIST across
    calls within this conversation. Define something in one call and use it in
    the next — no need to re-import or re-fetch.

    Use run_cell when you're building up state iteratively: load a dataset
    once then explore it over several steps, define helpers and reuse them,
    keep an open client/connection between calls, or debug by inspecting
    variables from a previous cell. This is your default tool for all
    computational work — fetching data, running code, testing, analysis.

    Need a clean slate? Don't re-fetch — just rebind the variables you care
    about, or run `%reset -f` in a cell to clear the namespace.

    Output: like a notebook, the value of the last expression is echoed (no
    print needed), alongside anything you print and any traceback. Rich
    outputs (e.g. matplotlib figures) are noted but not rendered as text.

    Timeout: 60s per cell. On timeout the kernel is interrupted but your
    session state (variables, imports) is preserved, so you can continue.

    Preloaded: search(query) / read(url) for the web, plus the `jarvis` SDK
    for platform work — artifacts, documents, automations, board tasks,
    workflows, skills, memory. Run `jarvis.help()` to see the categories and
    `jarvis.help("<category>")` for full signatures.
    """
    ctx = current_ctx()
    key = ctx.code_session_key
    if not key:
        return "No session context — code execution is unavailable here."
    start = time.monotonic()
    logger.info("→ run_cell [%s] (%d chars)", key, len(code))
    # conversation_id (not the kernel key — workers override that) scopes the
    # kernel-preloaded `jarvis` SDK to this conversation; project_id gates its
    # project_memory the same way the bound tool was gated.
    result = await get_kernel_registry().run_cell(
        key,
        code,
        timeout=DEFAULT_CELL_TIMEOUT,
        conversation_id=ctx.conversation_id,
        project_id=ctx.project_id,
    )
    logger.info("← run_cell [%s] (%d chars, %.0fms)", key, len(result), (time.monotonic() - start) * 1000)
    return result
