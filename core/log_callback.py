"""LangChain callback handler that logs tool calls, LLM calls, and graph nodes."""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

logger = logging.getLogger("jarvis.agent")

# Only these chain names are treated as graph nodes worth logging. Every
# Runnable in LangChain emits on_chain_start/on_chain_end, including tiny
# internal chains; whitelisting keeps the file readable.
_NODE_NAMES = frozenset({
    "model_request",
    "tools",
    "agent",          # worker subgraph node
    "main",           # the compiled top-level graph
    "worker",         # worker subgraph
})

_TRUNCATE_LIMIT = 240


def _shorten(text: str, limit: int = _TRUNCATE_LIMIT) -> str:
    """Collapse whitespace and truncate so a tool arg fits on one log line."""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


class AgentLogger(BaseCallbackHandler):
    """Logs tools, LLM calls, and graph node transitions.

    LangChain calls every callback method with a ``run_id`` UUID and (when
    relevant) a ``parent_run_id``. We use ``run_id`` to correlate start/end
    pairs and compute elapsed time.
    """

    def __init__(self) -> None:
        # run_id → (display_name, started_monotonic)
        self._tools: dict[UUID, tuple[str, float]] = {}
        self._llms: dict[UUID, tuple[str, float, int]] = {}  # name, start, message_count
        self._nodes: dict[UUID, tuple[str, float]] = {}

    # ── Tools ──────────────────────────────────────────────────────────────

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "?"
        self._tools[run_id] = (name, time.monotonic())
        logger.info("tool[%s] start args=%s", name, _shorten(input_str))

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        name, started = self._tools.pop(run_id, ("?", time.monotonic()))
        out_text = output if isinstance(output, str) else str(output)
        logger.info(
            "tool[%s] ok %.0fms (%d chars) %s",
            name, _ms(started), len(out_text), _shorten(out_text),
        )

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        name, started = self._tools.pop(run_id, ("?", time.monotonic()))
        logger.warning("tool[%s] FAIL %.0fms: %s", name, _ms(started), error)

    # ── LLM ────────────────────────────────────────────────────────────────

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        name = self._llm_name(serialized, kwargs)
        msg_count = sum(len(m) for m in messages)
        self._llms[run_id] = (name, time.monotonic(), msg_count)
        logger.info("llm[%s] start messages=%d", name, msg_count)

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        name = self._llm_name(serialized, kwargs)
        self._llms[run_id] = (name, time.monotonic(), len(prompts))
        logger.info("llm[%s] start prompts=%d", name, len(prompts))

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        name, started, _ = self._llms.pop(run_id, ("?", time.monotonic(), 0))
        usage = (response.llm_output or {}).get("token_usage") or {}
        in_tok = usage.get("prompt_tokens") or usage.get("input_tokens")
        out_tok = usage.get("completion_tokens") or usage.get("output_tokens")
        tool_calls = 0
        for gen_list in response.generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                if msg is not None:
                    tool_calls += len(getattr(msg, "tool_calls", []) or [])
        if in_tok is not None or out_tok is not None:
            logger.info(
                "llm[%s] ok %.0fms tokens=%s/%s tool_calls=%d",
                name, _ms(started), in_tok, out_tok, tool_calls,
            )
        else:
            logger.info(
                "llm[%s] ok %.0fms tool_calls=%d",
                name, _ms(started), tool_calls,
            )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        name, started, _ = self._llms.pop(run_id, ("?", time.monotonic(), 0))
        logger.warning("llm[%s] FAIL %.0fms: %s", name, _ms(started), error)

    @staticmethod
    def _llm_name(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
        s = serialized or {}
        # LangChain stuffs the actual model id under "kwargs.model" or
        # "kwargs.model_name" depending on provider; the top-level "name" is
        # often just the class name (e.g. "ChatBedrock").
        kw = s.get("kwargs") or {}
        return (
            kw.get("model")
            or kw.get("model_name")
            or kw.get("model_id")
            or s.get("name")
            or kwargs.get("invocation_params", {}).get("model")
            or "?"
        )

    # ── Graph nodes ────────────────────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or ""
        if name not in _NODE_NAMES:
            return
        self._nodes[run_id] = (name, time.monotonic())
        logger.info("node[%s] start", name)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        info = self._nodes.pop(run_id, None)
        if info is None:
            return
        name, started = info
        extras = ""
        if isinstance(outputs, dict) and "messages" in outputs:
            try:
                extras = f" messages_added={len(outputs['messages'])}"
            except TypeError:
                pass
        logger.info("node[%s] end %.0fms%s", name, _ms(started), extras)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        info = self._nodes.pop(run_id, None)
        if info is None:
            return
        name, started = info
        logger.warning("node[%s] FAIL %.0fms: %s", name, _ms(started), error)


class UsageAccumulator(BaseCallbackHandler):
    """Sums provider-reported token usage across every LLM call in one run.

    Attach a fresh instance per agent invocation alongside ``AgentLogger``;
    callbacks propagate to child runnables, so worker/subagent calls are
    included. Input tokens count the full context sent on each call, so
    per-message totals grow with history.
    """

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self._seen = False

    @property
    def has_usage(self) -> bool:
        return self._seen

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        found = False
        for gen_list in response.generations:
            for gen in gen_list:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if usage:
                    self.input_tokens += usage.get("input_tokens", 0) or 0
                    self.output_tokens += usage.get("output_tokens", 0) or 0
                    found = True
        if not found:
            # Older/odd providers only stuff counts into llm_output.
            fallback = (response.llm_output or {}).get("token_usage") or {}
            in_tok = fallback.get("prompt_tokens") or fallback.get("input_tokens")
            out_tok = fallback.get("completion_tokens") or fallback.get("output_tokens")
            if in_tok is not None or out_tok is not None:
                self.input_tokens += in_tok or 0
                self.output_tokens += out_tok or 0
                found = True
        self._seen = self._seen or found
