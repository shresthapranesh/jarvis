"""Dynamic parallel worker spawning with role-typed worker pool.

The parent agent calls `spawn_workers` to fan out independent subtasks. Each
task can request a `role` — `researcher`, `coder`, `writer`, or `general`
(the default for back-compat) — and gets a worker built with a role-specific
prompt and tool subset. All workers run concurrently via `asyncio.gather`.

The tool is built per agent via `make_spawn_workers(role_factories)` in
`core/agents.py`, so each agent's workers run on that agent's own model —
a process-global registry here would make whichever model built its agent
last own the workers of every conversation.

Each worker surfaces live progress to the parent stream via the ToolContext
event sink: `worker_start` when it spins up, `worker_step` per node
transition, coalesced `worker_token` text, and `worker_done` (status
done|error) at the end. `core/streaming.py` forwards these to the UI.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from uuid import uuid4

from langchain_core.tools import tool

from core.kernels import get_kernel_registry
from tools.context import current_ctx

logger = logging.getLogger(__name__)

DEFAULT_ROLE = "general"


def _chunk_text(content: Any) -> str:
    """Flatten one streamed AI chunk's content to plain text.

    Reasoning models stream content as a list of typed blocks; take only
    `text` blocks (thinking stays out of the worker's visible tail).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class _TokenTail:
    """Coalesces a worker's streamed text into `worker_token` events.

    Same motivation as core.streaming.TokenCoalescer: a verbose worker emits
    thousands of tiny chunks, and each emitted event wakes every stream
    subscriber. Buffer until `max_chars`, plus explicit flushes at step
    boundaries so ordering against `worker_step` events is preserved.
    """

    def __init__(self, emit: Callable[..., None], idx: int, max_chars: int = 120):
        self._emit = emit
        self._idx = idx
        self._max = max_chars
        self._buf: list[str] = []
        self._len = 0

    def add(self, text: str) -> None:
        if not text:
            return
        self._buf.append(text)
        self._len += len(text)
        if self._len >= self._max:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf, self._len = [], 0
        self._emit("worker_token", idx=self._idx, text=text)


def make_spawn_workers(role_factories: dict[str, Callable[[], Any]]):
    """Build a `spawn_workers` tool bound to one agent's worker factories.

    `role_factories` maps role name → zero-arg callable returning a fresh
    compiled worker graph (closing over that agent's LLM and tool subset).
    """

    @tool
    async def spawn_workers(tasks: list[dict]) -> str:
        """Spawn worker agents to run independent tasks concurrently; returns when all complete.

        Each task is a dict:
          "task":    (required) what to do
          "context": (optional) extra background for the worker
          "role":    (optional) "researcher", "coder", "writer", or "general"
                     (default) — tunes the worker's prompt and tool subset

        Workers are separate agents and can't see your kernel variables — put
        everything each one needs in its task/context.
        """

        # Propagate the parent's conversation_id so worker-side tools (e.g.
        # list_artifacts) scope to the same conversation as the main agent.
        # Captured once here, then used from the gathered child tasks below.
        tctx = current_ctx()
        parent_conv_id = tctx.conversation_id

        async def run_one(spec: dict, idx: int) -> tuple[int, str, str, str | Exception]:
            label = spec.get("task", "")[:80]
            role = spec.get("role") or DEFAULT_ROLE
            factory = role_factories.get(role)
            if factory is None:
                available = ", ".join(sorted(role_factories))
                err = f"Unknown role '{role}' (available: {available})"
                tctx.emit("worker_done", idx=idx, role=role, task=label, status="error", result=f"ERROR: {err}")
                return idx, role, label, err
            tctx.emit("worker_start", idx=idx, role=role, task=label)
            # Each worker gets its OWN kernel via a unique kernel_key so that
            # concurrent workers' run_cell sessions stay isolated (they'd
            # otherwise collide on the parent conversation's single kernel).
            # conversation_id is still propagated for artifact/document scoping.
            worker_key = f"{parent_conv_id or 'worker'}::w{idx}::{uuid4().hex[:8]}"
            try:
                worker = factory()
                prompt = spec["task"]
                if ctx := spec.get("context"):
                    prompt = f"Context: {ctx}\n\nTask: {prompt}"
                from core.log_callback import AgentLogger
                # Workers need a generous recursion budget — the langgraph default
                # of 25 is half a dozen tool round-trips, often not enough for a
                # multi-step subtask. Keep it lower than the main agent (100) so a
                # runaway worker doesn't dominate.
                configurable: dict[str, Any] = {"kernel_key": worker_key}
                if parent_conv_id:
                    configurable["conversation_id"] = parent_conv_id
                worker_config: dict[str, Any] = {
                    "callbacks": [AgentLogger()],
                    "recursion_limit": 50,
                    "configurable": configurable,
                }
                # Stream the worker instead of ainvoke so its progress is
                # visible live. `updates` gives node transitions (→ worker_step),
                # `messages` gives LLM tokens (→ coalesced worker_token), and
                # `custom` catches events the worker's own tools emit (e.g.
                # write_artifact) — re-dispatched upward through the parent's
                # sink, which they'd otherwise never reach.
                from core.streaming import _extract_step_data
                tail = _TokenTail(tctx.emit, idx)
                final_msg: Any = None
                async for mode, chunk in worker.astream(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config=worker_config,
                    stream_mode=["updates", "messages", "custom"],
                ):
                    if mode == "messages":
                        token, _meta = chunk
                        if getattr(token, "type", "") in ("ai", "AIMessageChunk"):
                            tail.add(_chunk_text(getattr(token, "content", "")))
                    elif mode == "custom":
                        if isinstance(chunk, dict) and chunk.get("type"):
                            tail.flush()
                            tctx.emit(chunk["type"], **{k: v for k, v in chunk.items() if k != "type"})
                    elif mode == "updates" and isinstance(chunk, dict):
                        for node_name, node_data in chunk.items():
                            if not node_name or node_name.startswith("__"):
                                continue
                            tail.flush()
                            # Worker graphs name their model node "agent"; normalise
                            # to "model_request" so the step extractor and the
                            # frontend's describeStep() treat worker steps exactly
                            # like main-agent ones.
                            step_node = "model_request" if node_name == "agent" else node_name
                            data_dict = node_data if isinstance(node_data, dict) else {}
                            tctx.emit(
                                "worker_step",
                                idx=idx, role=role, node=step_node,
                                data=_extract_step_data(step_node, data_dict),
                            )
                            if node_name == "agent" and (msgs := data_dict.get("messages")):
                                final_msg = msgs[-1]
                tail.flush()
                raw_content = getattr(final_msg, "content", "") if final_msg is not None else ""
                # Thinking models (Anthropic extended thinking, Bedrock) return
                # content as a list of typed blocks, not a plain string.  Extract
                # the text portion so downstream consumers (the main agent's
                # ToolMessage, len(), worker_done event) get a string.
                if isinstance(raw_content, list):
                    answer = " ".join(
                        b.get("text", "") for b in raw_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip() or str(raw_content)
                else:
                    answer = str(raw_content)
                logger.info("worker %d (%s) done (%d chars): %s", idx, role, len(answer), label)
                tctx.emit("worker_done", idx=idx, role=role, task=label, status="done", result=answer)
                return idx, role, label, answer
            except Exception as exc:
                logger.warning("worker %d (%s) failed: %s — %s", idx, role, label, exc)
                tctx.emit("worker_done", idx=idx, role=role, task=label, status="error", result=f"ERROR: {exc}")
                return idx, role, label, exc
            finally:
                # Free the worker's kernel promptly — unique keys would otherwise
                # accumulate idle kernels and evict the main conversation's.
                try:
                    await get_kernel_registry().shutdown(worker_key)
                except Exception as exc:
                    logger.debug("worker kernel shutdown failed for %s: %s", worker_key, exc)

        jobs = [run_one(spec, i) for i, spec in enumerate(tasks, 1)]
        outcomes = await asyncio.gather(*jobs)

        parts: list[str] = []
        for _, role, label, result in sorted(outcomes, key=lambda x: x[0]):
            prefix = f"Task ({role}): {label}"
            if isinstance(result, Exception):
                parts.append(f"{prefix}\nERROR: {result}")
            else:
                parts.append(f"{prefix}\n{result}")

        return "\n\n---\n\n".join(parts)

    return spawn_workers
