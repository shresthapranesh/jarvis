"""A scripted chat model and a recorder for agent-loop golden tests.

The agent loop is being moved off LangGraph (see core/transcript_format.md).
What the frontend, the edge mirror and the transcript all depend on is what a
run *emits* and *leaves behind*, so `tests/test_agent_golden.py` drives real
chat turns through `chat_job_handler` with this model standing in for the LLM
and compares the outcome to logs recorded from the LangGraph runtime:

- the run's events, normalized (ids → placeholders, coalesced tokens merged);
- the Step rows it persisted and the final assistant Message;
- every request the model received (the shaped prompt, the bound tool names);
- the thread it left (messages + todos).
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


@dataclass
class ModelCall:
    """One request the scripted model received."""

    index: int  # 0-based, across every agent (main and workers) in the test
    messages: list[BaseMessage]
    tools: list[str]

    @property
    def last(self) -> BaseMessage:
        return self.messages[-1]

    def is_worker(self) -> bool:
        return "spawn_workers" not in self.tools


Responder = Callable[[ModelCall], Any]  # -> AIMessage, or an awaitable of one


@dataclass
class Script:
    """Shared by every copy of the model (bind_tools copies it)."""

    responder: Responder
    calls: list[ModelCall] = field(default_factory=list)


def _default_usage(n: int) -> dict[str, int]:
    return {"input_tokens": 100 + n, "output_tokens": 10, "total_tokens": 110 + n}


class ScriptedChatModel(BaseChatModel):
    """Answers each request with whatever `script.responder` returns.

    Streams the answer the way a provider does — text in word-sized chunks,
    list content one block per chunk, tool calls as one final chunk — so both
    LangGraph's `messages` stream mode and a hand-written loop see tokens.
    """

    script: Any
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        names = [getattr(t, "name", None) or t["name"] for t in tools]
        return self.model_copy(update={"tool_names": names})

    def get_num_tokens_from_messages(self, messages: Any, tools: Any = None) -> int:  # type: ignore[override]
        from core.messages import estimate_tokens_heuristic

        return estimate_tokens_heuristic(messages)

    async def _respond(self, messages: list[BaseMessage]) -> AIMessage:
        call = ModelCall(len(self.script.calls), list(messages), list(self.tool_names))
        self.script.calls.append(call)
        out = self.script.responder(call)
        if inspect.isawaitable(out):
            out = await out
        if isinstance(out, str):
            out = AIMessage(content=out)
        if out.usage_metadata is None:
            out.usage_metadata = _default_usage(call.index)  # type: ignore[assignment]
        return out

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # pragma: no cover
        raise NotImplementedError("the agent loop is async")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=await self._respond(messages))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        # langchain-core fires on_llm_new_token for each chunk itself.
        msg = await self._respond(messages)
        for chunk in _chunks(msg):
            yield ChatGenerationChunk(message=chunk)


def _chunks(msg: AIMessage) -> list[AIMessageChunk]:
    out: list[AIMessageChunk] = []
    if isinstance(msg.content, str):
        for word in re.findall(r"\S+\s*|\s+", msg.content):
            out.append(AIMessageChunk(content=word))
    else:
        for i, block in enumerate(msg.content):
            part: dict[str, Any] = dict(block) if isinstance(block, dict) else {"type": "text", "text": block}
            part.setdefault("index", i)
            out.append(AIMessageChunk(content=[part]))
    last = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
            for i, tc in enumerate(msg.tool_calls)
        ],
        usage_metadata=msg.usage_metadata,
        response_metadata={"finish_reason": "STOP", "model_name": "scripted"},
        chunk_position="last",
    )
    out.append(last)
    return out


def tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


# ── normalization ────────────────────────────────────────────────────────────

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RUN_ID = re.compile(r"(?:lc_run--|run-|run--)" + _UUID.pattern + r"(?:-\d+)?")
_HEX_SUFFIX = re.compile(r"::w(\d+)::[0-9a-f]{8}")


class Normalizer:
    """Replaces ids with stable placeholders, numbered in order of first sight,
    so two runs that differ only in their random ids compare equal."""

    def __init__(self, names: dict[str, str] | None = None):
        self._map: dict[str, str] = dict(names or {})
        self._n = 0

    def _sub(self, m: re.Match[str]) -> str:
        key = m.group(0)
        if key not in self._map:
            self._n += 1
            prefix = "run" if not _UUID.fullmatch(key) else "id"
            self._map[key] = f"<{prefix}{self._n}>"
        return self._map[key]

    def text(self, s: str) -> str:
        for raw, name in self._map.items():
            if raw in s and not raw.startswith("<"):
                s = s.replace(raw, name)
        s = _RUN_ID.sub(self._sub, s)
        s = _UUID.sub(self._sub, s)
        return _HEX_SUFFIX.sub(r"::w\1::<hex>", s)

    def value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            return {self.text(k): self.value(x) for k, x in v.items()}
        return v


# Event fields that are wall-clock or machine dependent.
_VOLATILE_FIELDS = {"duration_ms", "elapsed_ms", "perf", "ts", "timestamp", "requested_at"}


_USAGE_EVENTS = ("perf_update", "budget_update")


def normalize_events(events: list[dict], norm: Normalizer) -> list[list[Any]]:
    """`[name, data]` pairs. Token events are merged per (event, source) run:
    the coalescer flushes on a 50 ms timer, so where it splits is timing.

    `perf_update` / `budget_update` come from LLM callbacks, carry wall-clock
    rates, and land in either order — they're dropped here and compared by
    `usage_events` instead."""
    out: list[list[Any]] = []
    for raw in events:
        name = raw.get("event")
        if name in _USAGE_EVENTS:
            continue
        try:
            data = json.loads(raw.get("data") or "{}")
        except (TypeError, ValueError):
            data = {"raw": raw.get("data")}
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in _VOLATILE_FIELDS}
        if name in ("token", "thinking_token", "worker_token") and out and out[-1][0] == name:
            prev = out[-1][1]
            if prev.get("source") == data.get("source") and prev.get("idx") == data.get("idx"):
                prev["text"] += data.get("text", "")
                continue
        out.append([name, data])
    return [[name, norm.value(data)] for name, data in out]


def usage_events(events: list[dict]) -> list[list[Any]]:
    """The callback events as a sorted list of `[name, llm_calls]`."""
    out = []
    for raw in events:
        if raw.get("event") in _USAGE_EVENTS:
            data = json.loads(raw.get("data") or "{}")
            out.append([raw["event"], data.get("llm_calls")])
    return sorted(out)


def _content(content: Any, norm: Normalizer, system_prompt: str) -> Any:
    if isinstance(content, str):
        return norm.text(content.replace(system_prompt, "<SYSTEM_PROMPT>"))
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                part = {k: v for k, v in part.items()}
                if isinstance(part.get("text"), str):
                    part["text"] = part["text"].replace(system_prompt, "<SYSTEM_PROMPT>")
            out.append(norm.value(part))
        return out
    return content


def message_record(m: BaseMessage, norm: Normalizer, *, system_prompt: str = "\0") -> dict[str, Any]:
    rec: dict[str, Any] = {"type": m.type, "content": _content(m.content, norm, system_prompt)}
    if m.id is not None:
        rec["id"] = norm.text(str(m.id))
    if getattr(m, "name", None):
        rec["name"] = m.name
    if isinstance(m, AIMessage):
        if m.tool_calls:
            rec["tool_calls"] = [
                {"name": tc["name"], "args": norm.value(tc["args"]), "id": tc["id"]}
                for tc in m.tool_calls
            ]
        if m.invalid_tool_calls:
            rec["invalid_tool_calls"] = norm.value([dict(tc) for tc in m.invalid_tool_calls])
        if m.usage_metadata:
            rec["usage"] = {k: m.usage_metadata.get(k) for k in ("input_tokens", "output_tokens")}
    if isinstance(m, ToolMessage):
        rec["tool_call_id"] = m.tool_call_id
        rec["status"] = m.status
    return rec


def llm_call_record(call: ModelCall, norm: Normalizer, *, full: bool = True) -> dict[str, Any]:
    from core.agents import _SYSTEM_PROMPT

    rec: dict[str, Any] = {"tools": call.tools, "n_messages": len(call.messages)}
    if full:
        rec["messages"] = [
            message_record(m, norm, system_prompt=_SYSTEM_PROMPT) for m in call.messages
        ]
        # The system message is the one place a misordered cache segment shows.
        rec["system_roles"] = [isinstance(m, SystemMessage) for m in call.messages].count(True)
    return rec
