"""Centralized tool-call logging middleware."""

import logging
import time

from langchain.agents.middleware import wrap_tool_call

logger = logging.getLogger(__name__)


@wrap_tool_call
async def log_tool_calls(request, handler):
    name = request.tool_call["name"]
    args = request.tool_call["args"]
    start = time.monotonic()
    logger.info("→ %s %s", name, args)
    result = await handler(request)
    elapsed_ms = (time.monotonic() - start) * 1000
    raw = getattr(result, "content", result)
    content = raw if isinstance(raw, str) else str(raw)
    logger.info("← %s (%d chars, %.0fms)", name, len(content), elapsed_ms)
    return result
