"""Centralized tool-call logging middleware."""

import logging

from langchain.agents.middleware import wrap_tool_call

logger = logging.getLogger(__name__)


@wrap_tool_call
async def log_tool_calls(request, handler):
    name = request.tool_call["name"]
    args = request.tool_call["args"]
    logger.info("→ %s %s", name, args)
    result = await handler(request)
    content = result.content if isinstance(result.content, str) else str(result.content)
    logger.info("← %s (%d chars)", name, len(content))
    return result
