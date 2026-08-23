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
    """Run Python in this conversation's stateful IPython kernel; returns its output.

    Variables and imports persist across calls, like Jupyter cells. The last
    expression is echoed (no print needed). 60s timeout; state survives it.
    Preloaded: search(query), read(url), jarvis SDK (`jarvis.help()`).
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
    # A cell parked on a tool approval is not a hung cell: without this the
    # 60s timeout would interrupt the kernel out from under a `jarvis` call
    # that is waiting for a human (core/tool_gate.py).
    conversation_id = ctx.conversation_id

    async def _held_on_approval() -> bool:
        from core.tool_gate import has_open_gate

        return await has_open_gate(conversation_id)

    result = await get_kernel_registry().run_cell(
        key,
        code,
        timeout=DEFAULT_CELL_TIMEOUT,
        conversation_id=conversation_id,
        project_id=ctx.project_id,
        hold_check=_held_on_approval,
    )
    logger.info("← run_cell [%s] (%d chars, %.0fms)", key, len(result), (time.monotonic() - start) * 1000)
    return result
