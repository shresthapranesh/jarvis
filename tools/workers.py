"""Dynamic parallel worker spawning."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Injected by build_agent at startup — avoids circular imports.
_worker_factory: Any = None


def set_worker_factory(factory) -> None:
    """Register the callable that creates a fresh worker agent.
    Called once from core/agents.py after the agent builder is defined."""
    global _worker_factory
    _worker_factory = factory


@tool
async def spawn_workers(tasks: list[dict]) -> str:
    """Spawn multiple worker agents to run independent tasks concurrently.

    Each task is a dict with:
      "task":    (required) natural-language description of what to do
      "context": (optional) extra background the worker should know

    Workers run in parallel with asyncio and return when all complete.
    Each worker has access to execute(), read_file(), write_file(), list_files().

    Use this when you have independent subtasks that benefit from parallelism:
      - Researching multiple topics simultaneously
      - Processing multiple files at once
      - Running separate analyses on different data sets

    Example:
      spawn_workers([
        {"task": "Find the latest GDP data for the US"},
        {"task": "Find the latest GDP data for China"},
        {"task": "Find the latest GDP data for the EU"},
      ])
    """
    if _worker_factory is None:
        return "Error: worker factory not initialized"

    async def run_one(spec: dict, idx: int) -> tuple[int, str, str | Exception]:
        label = spec.get("task", "")[:80]
        try:
            worker = _worker_factory()
            prompt = spec["task"]
            if ctx := spec.get("context"):
                prompt = f"Context: {ctx}\n\nTask: {prompt}"
            result = await worker.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]}
            )
            answer = result["messages"][-1].content
            logger.info("worker %d done (%d chars): %s", idx, len(answer), label)
            await adispatch_custom_event("worker_done", {
                "type": "worker_done",
                "idx": idx,
                "task": label,
                "result": answer,
            })
            return idx, label, answer
        except Exception as exc:
            logger.warning("worker %d failed: %s — %s", idx, label, exc)
            await adispatch_custom_event("worker_done", {
                "type": "worker_done",
                "idx": idx,
                "task": label,
                "result": f"ERROR: {exc}",
            })
            return idx, label, exc

    jobs = [run_one(spec, i) for i, spec in enumerate(tasks, 1)]
    outcomes = await asyncio.gather(*jobs)

    parts: list[str] = []
    for _, label, result in sorted(outcomes, key=lambda x: x[0]):
        if isinstance(result, Exception):
            parts.append(f"Task: {label}\nERROR: {result}")
        else:
            parts.append(f"Task: {label}\n{result}")

    return "\n\n---\n\n".join(parts)
