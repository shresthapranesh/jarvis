"""Dynamic parallel worker spawning with role-typed worker pool.

The parent agent calls `spawn_workers` to fan out independent subtasks. Each
task can request a `role` — `researcher`, `coder`, `writer`, or `general`
(the default for back-compat) — and gets a worker built with a role-specific
prompt and tool subset. All workers run concurrently via `asyncio.gather`.

The tool is built per agent via `make_spawn_workers(role_factories)` in
`core/agents.py`, so each agent's workers run on that agent's own model —
a process-global registry here would make whichever model built its agent
last own the workers of every conversation.
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


def make_spawn_workers(role_factories: dict[str, Callable[[], Any]]):
    """Build a `spawn_workers` tool bound to one agent's worker factories.

    `role_factories` maps role name → zero-arg callable returning a fresh
    compiled worker graph (closing over that agent's LLM and tool subset).
    """

    @tool
    async def spawn_workers(tasks: list[dict]) -> str:
        """Spawn multiple worker agents to run independent tasks concurrently.

        Each task is a dict with:
          "task":    (required) natural-language description of what to do
          "context": (optional) extra background the worker should know
          "role":    (optional) one of "researcher", "coder", "writer", "general"
                     — defaults to "general"

        Roles tune the worker's prompt and tool subset:
          - researcher → run_cell() + read_file. Best for finding/verifying facts.
          - coder      → full toolset. Best for writing/editing code.
          - writer     → read_file + write_file (no code). Best for prose.
          - general    → full toolset, generic prompt. Fallback.

        Workers run in parallel and return when all complete.

        Example:
          spawn_workers([
            {"role": "researcher", "task": "Find current US/China/EU GDP"},
            {"role": "researcher", "task": "Find current US/China/EU population"},
            {"role": "writer", "task": "Draft a one-paragraph comparison"},
          ])
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
                tctx.emit("worker_done", idx=idx, role=role, task=label, result=f"ERROR: {err}")
                return idx, role, label, err
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
                result = await worker.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config=worker_config,
                )
                raw_content = result["messages"][-1].content
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
                tctx.emit("worker_done", idx=idx, role=role, task=label, result=answer)
                return idx, role, label, answer
            except Exception as exc:
                logger.warning("worker %d (%s) failed: %s — %s", idx, role, label, exc)
                tctx.emit("worker_done", idx=idx, role=role, task=label, result=f"ERROR: {exc}")
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
