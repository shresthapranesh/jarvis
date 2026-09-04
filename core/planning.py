"""Planning mode helpers."""

from __future__ import annotations

import os
import re
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

PLANNING_ENV = "JARVIS_PLANNING_MODE"
PLANNING_PREFILL_ENV = "JARVIS_PLANNING_PREFILL"

_COMPLEXITY_KEYWORDS = {
    "research",
    "implement",
    "build",
    "create",
    "analyze",
    "compare",
    "workflow",
    "pipeline",
    "report",
    "refactor",
    "migrate",
    "investigate",
    "plan",
    "design",
    "deploy",
    "test",
    "fix",
    "audit",
    "review",
}

# Thresholds for auto-detect
_AUTO_MIN_CHARS = 80
_AUTO_MIN_LINES = 3


def get_planning_mode() -> str:
    """Return planning mode: auto/always/off from env or config.

    Env JARVIS_PLANNING_MODE wins, default "auto".
    Values: auto, always, off, disabled (alias for off)
    """
    raw = os.environ.get(PLANNING_ENV, "").strip().lower()
    if not raw:
        # Try config file? For now keep env-only and DB later.
        # We will also check config via get_setting lazily in caller if needed.
        raw = "auto"
    if raw in ("disabled", "0", "false", "no"):
        return "off"
    if raw not in ("auto", "always", "off"):
        return "auto"
    return raw


def _keyword_hits(low: str) -> int:
    """Whole-word keyword count — substring matching over-triggers ("fix" in
    "prefix", "test" in "latest")."""
    return sum(1 for kw in _COMPLEXITY_KEYWORDS if re.search(rf"\b{kw}\b", low))


def should_auto_plan(query: str) -> bool:
    """Heuristic: does query look like multi-step work needing todos?

    Deliberately conservative — a single verb like "fix" or "review" in an
    ordinary question is not multi-step work; forcing write_todos on it hijacks
    simple requests. Requires 2+ keywords, an explicit list, sequencing
    language, 3+ lines, or a genuinely long prompt.
    """
    if not query:
        return False
    q = query.strip()
    low = q.lower()
    if len(q) < _AUTO_MIN_CHARS and "\n" not in q:
        # Short single-line query -> not complex unless keyword heavy
        return _keyword_hits(low) >= 2
    # Multi-line or long
    lines = [line for line in q.split("\n") if line.strip()]
    if len(lines) >= _AUTO_MIN_LINES:
        return True
    # If contains numbered list or bullet list, likely multi-step
    if re.search(r"(^|\n)\s*(?:\d+\.\s+|[-*]\s+)", q):
        return True
    if _keyword_hits(low) >= 2:
        return True
    # Contains "and then", "after that", "step", "first", "then"
    if re.search(r"\b(and then|after that|first|then|step \d|phase)\b", low):
        return True
    return len(q) >= 300


def should_plan_for_query(query: str, mode: str | None = None) -> bool:
    """Return True if planning directive should be injected."""
    m = (mode or get_planning_mode()).lower()
    if m == "off":
        return False
    if m == "always":
        return True
    # auto
    return should_auto_plan(query)


def build_planning_directive(query: str) -> str | None:
    """Return a volatile system-prompt injection forcing todo planning, or None.

    Called from model_request_node when query appears complex.
    """
    if not should_plan_for_query(query):
        return None
    return (
        "## Planning Required\n"
        "This task is multi-step. You MUST call `write_todos` as your FIRST action, "
        "before any research, code, or file ops. Break the user request into 3-7 concrete steps. "
        "After that, execute step-by-step calling `set_todo_status(index, 'in_progress')` before each step "
        "and 'done' after. The user sees this list live — it is your status report."
    )


def build_planning_prefill_prompt(query: str) -> str:
    """Prompt for cheap LLM to prefill todos (used if JARVIS_PLANNING_PREFILL=1)."""
    return (
        "You are a planning assistant. Given the user request, produce a JSON array of 3-7 short, "
        "actionable steps (strings) to accomplish it. No explanation, just JSON array. "
        f"User request: {query}\n\nRespond with ONLY the JSON array."
    )


async def prefill_todos_with_llm(query: str, model_id: str | None = None) -> list[str] | None:
    """Optional fast-path: call a cheap LLM to generate initial todos.

    Returns list of strings or None on failure. Only runs if env JARVIS_PLANNING_PREFILL=1.
    """
    if os.environ.get(PLANNING_PREFILL_ENV, "").lower() not in ("1", "true", "yes"):
        return None
    try:
        from core.model_catalog import resolve_model_spec
        from db.ops import resolve_model

        mid = await resolve_model(model_id)
        llm = resolve_model_spec(mid).build_llm()

        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = build_planning_prefill_prompt(query)
        resp = await llm.ainvoke(
            [
                SystemMessage(content="You are a planning assistant. Output only JSON array of steps."),
                HumanMessage(content=prompt),
            ]
        )
        raw = resp.content
        if isinstance(raw, list):
            # Reasoning model blocks
            text = " ".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text").strip()
        else:
            text = str(raw).strip()
        # Try to extract JSON array
        try:
            # Fast path: whole text is JSON
            data = json.loads(text)
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return [s.strip() for s in data if s.strip()][:10]
        except Exception:
            pass
        # Fallback: find first [ ... ]
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                data = json.loads(snippet)
                if isinstance(data, list):
                    return [str(x).strip() for x in data if str(x).strip()][:10]
            except Exception:
                pass
        return None
    except Exception as exc:
        logger.warning("prefill_todos_with_llm failed: %s", exc)
        return None
