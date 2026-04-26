"""Strip thinking blocks from conversation history before sending to Bedrock.

Anthropic's extended thinking produces ``thinking`` content blocks with a
``signature`` field in every assistant response. When these are stored in the
LangGraph checkpoint and replayed on subsequent turns, serialization can
lose the signature — causing Bedrock to reject the request with:

    messages.N.content.0.thinking.signature: Field required

Anthropic explicitly allows omitting thinking blocks from prior assistant
turns. This middleware strips them from all messages *except the most recent
assistant message* so the API never sees a stale/incomplete thinking block.

Usage::

    from core.strip_thinking_middleware import StripThinkingMiddleware

    agent = create_deep_agent(
        ...
        middleware=[StripThinkingMiddleware(), ...],
    )
"""

from __future__ import annotations

import logging


from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, AnyMessage

logger = logging.getLogger(__name__)

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _strip_thinking_from_message(msg: AIMessage) -> AIMessage:
    """Return a copy of the AIMessage with thinking blocks removed from content."""
    content = msg.content
    if not isinstance(content, list):
        # String content — nothing to strip
        return msg

    filtered = [
        block for block in content
        if not (isinstance(block, dict) and block.get("type") in _THINKING_TYPES)
    ]

    if len(filtered) == len(content):
        # No thinking blocks found — return unchanged
        return msg

    # Ensure we don't produce an empty content list
    if not filtered:
        filtered = [{"type": "text", "text": ""}]

    new_msg = msg.model_copy(update={"content": filtered})

    # Also strip thinking from additional_kwargs if present
    if "thinking" in (new_msg.additional_kwargs or {}):
        new_kwargs = {k: v for k, v in new_msg.additional_kwargs.items() if k != "thinking"}
        new_msg = new_msg.model_copy(update={"additional_kwargs": new_kwargs})

    return new_msg


class StripThinkingMiddleware(AgentMiddleware):
    """Remove thinking/signature blocks from historical messages.

    Runs before every model call.  Strips thinking blocks from all AI messages
    in the conversation except the very last one (which is about to be extended).
    """

    def wrap_model_call(self, request: ModelRequest, handler):
        request = _clean_request(request)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler):
        request = _clean_request(request)
        return await handler(request)


def _clean_request(request: ModelRequest) -> ModelRequest:
    """Strip thinking blocks from all but the last AIMessage."""
    messages = request.messages
    if not messages:
        return request

    # Find the index of the last AIMessage
    last_ai_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            last_ai_idx = i
            break

    cleaned: list[AnyMessage] = []
    changed = False
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage) and i != last_ai_idx:
            stripped = _strip_thinking_from_message(msg)
            if stripped is not msg:
                changed = True
            cleaned.append(stripped)
        else:
            cleaned.append(msg)

    if not changed:
        return request

    logger.debug("Stripped thinking blocks from %d historical AI messages", sum(
        1 for i, m in enumerate(messages)
        if isinstance(m, AIMessage) and i != last_ai_idx
        and isinstance(m.content, list)
        and any(isinstance(b, dict) and b.get("type") in _THINKING_TYPES for b in m.content)
    ))
    return request.override(messages=cleaned)
