"""Token budget tracker."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetLimits:
    """Ceilings for a single run. All optional — None means unlimited."""

    max_total_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_llm_calls: int | None = None
    max_tool_calls: int | None = None
    max_duration_seconds: int | None = None
    # Optional: max messages (user+assistant turns) — not enforced yet, placeholder for future
    max_messages: int | None = None

    @classmethod
    def from_env(cls) -> BudgetLimits:
        def _int_env(name: str) -> int | None:
            raw = os.environ.get(name)
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                logger.warning("Invalid int for %s=%r — ignoring", name, raw)
                return None

        return cls(
            max_total_tokens=_int_env("JARVIS_BUDGET_MAX_TOTAL_TOKENS") or _int_env("JARVIS_BUDGET_MAX_TOKENS"),
            max_input_tokens=_int_env("JARVIS_BUDGET_MAX_INPUT_TOKENS"),
            max_output_tokens=_int_env("JARVIS_BUDGET_MAX_OUTPUT_TOKENS"),
            max_llm_calls=_int_env("JARVIS_BUDGET_MAX_LLM_CALLS"),
            max_tool_calls=_int_env("JARVIS_BUDGET_MAX_TOOL_CALLS"),
            max_duration_seconds=_int_env("JARVIS_BUDGET_MAX_DURATION_SECONDS"),
            max_messages=_int_env("JARVIS_BUDGET_MAX_MESSAGES"),
        )

    @classmethod
    def with_defaults(cls, **overrides: Any) -> BudgetLimits:
        """Construct with sane defaults for unset fields, overridable via kwargs."""
        base = cls.from_env()
        # Sensible per-run defaults if nothing in env — generous, not restrictive.
        # Env wins over defaults; explicit kwargs win over both.
        defaults = {
            "max_total_tokens": 500_000,
            "max_input_tokens": None,  # 400k,
            "max_output_tokens": None,  # 100k,
            "max_llm_calls": 200,
            "max_tool_calls": 300,
            "max_duration_seconds": 1800,  # 30min
        }
        merged: dict[str, Any] = {}
        for k, default in defaults.items():
            env_val = getattr(base, k)
            merged[k] = env_val if env_val is not None else default
        # Explicit overrides win
        merged.update({k: v for k, v in overrides.items() if v is not None})
        # If env explicitly set None, respect that? No — defaults above already handle None fallback.
        # But if user wants unlimited, they can set env to "0" (interpreted as unlimited) — we treat 0 as None? Simpler: explicit 0 => None
        for k in list(merged.keys()):
            if merged[k] == 0:
                merged[k] = None
        # Preserve max_messages from env if set
        if base.max_messages is not None:
            merged["max_messages"] = base.max_messages
        return cls(**merged)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class BudgetTracker:
    """Mutable per-run budget tracker. Attached to a TaskState.

    Not thread-safe for concurrent callers; agent loop is sequential except
    tool batch parallelism, but we guard increments with simple ints.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        task_state: Any | None = None,
    ) -> None:
        self.limits = limits
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.llm_calls: int = 0
        self.tool_calls: int = 0
        self.started_at: datetime = datetime.now(timezone.utc)
        self._task_state = task_state
        self._exceeded_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def exceeded_reason(self) -> str | None:
        return self._exceeded_reason

    def record_llm(self, input_tokens: int | None, output_tokens: int | None) -> None:
        it = input_tokens or 0
        ot = output_tokens or 0
        self.input_tokens += it
        self.output_tokens += ot
        self.llm_calls += 1
        self._sync_to_task_state()
        logger.debug("budget: llm call +%d/%d total=%d/%d calls=%d", it, ot, self.input_tokens, self.output_tokens, self.llm_calls)
        self._check_exceeded()

    def record_tool(self, count: int = 1) -> None:
        self.tool_calls += count
        self._sync_to_task_state()
        logger.debug("budget: tool +%d total=%d", count, self.tool_calls)
        self._check_exceeded()

    def _sync_to_task_state(self) -> None:
        if self._task_state is None:
            return
        try:
            self._task_state.input_tokens = self.input_tokens
            self._task_state.output_tokens = self.output_tokens
            self._task_state.llm_calls = self.llm_calls
            self._task_state.tool_calls = self.tool_calls
            if self._exceeded_reason:
                self._task_state.budget_exceeded = True
                self._task_state.budget_reason = self._exceeded_reason
            # Emit live budget update for UI progress bars — throttled by caller
            # (record_llm/record_tool call per LLM/tool, which is already low freq)
            try:
                from core.state import emit_event

                emit_event(
                    self._task_state,
                    "budget_update",
                    input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens,
                    total_tokens=self.total_tokens,
                    llm_calls=self.llm_calls,
                    tool_calls=self.tool_calls,
                    snapshot=self.snapshot(),
                )
            except Exception:
                pass
        except Exception:
            pass

    def _check_exceeded(self) -> bool:
        now = datetime.now(timezone.utc)
        elapsed = (now - self.started_at).total_seconds()
        reason = None
        lim = self.limits
        if lim.max_total_tokens is not None and self.total_tokens > lim.max_total_tokens:
            reason = f"total tokens {self.total_tokens} > limit {lim.max_total_tokens}"
        elif lim.max_input_tokens is not None and self.input_tokens > lim.max_input_tokens:
            reason = f"input tokens {self.input_tokens} > limit {lim.max_input_tokens}"
        elif lim.max_output_tokens is not None and self.output_tokens > lim.max_output_tokens:
            reason = f"output tokens {self.output_tokens} > limit {lim.max_output_tokens}"
        elif lim.max_llm_calls is not None and self.llm_calls > lim.max_llm_calls:
            reason = f"llm calls {self.llm_calls} > limit {lim.max_llm_calls}"
        elif lim.max_tool_calls is not None and self.tool_calls > lim.max_tool_calls:
            reason = f"tool calls {self.tool_calls} > limit {lim.max_tool_calls}"
        elif lim.max_duration_seconds is not None and elapsed > lim.max_duration_seconds:
            reason = f"duration {elapsed:.1f}s > limit {lim.max_duration_seconds}s"

        if reason and not self._exceeded_reason:
            self._exceeded_reason = reason
            logger.warning("budget exceeded: %s", reason)
            if self._task_state is not None:
                try:
                    self._task_state.budget_exceeded = True
                    self._task_state.budget_reason = reason
                    # Lazy emit — avoid import cycle: core.state.emit_event reads task_state only
                    from core.state import emit_event

                    emit_event(self._task_state, "budget_exceeded", reason=reason, snapshot=self.snapshot())
                except Exception:
                    pass
            return True
        return self._exceeded_reason is not None

    def is_exceeded(self) -> tuple[bool, str | None]:
        """Return (exceeded, reason)."""
        if self._exceeded_reason:
            return True, self._exceeded_reason
        # re-check duration even if no new tokens
        self._check_exceeded()
        return (self._exceeded_reason is not None), self._exceeded_reason

    def snapshot(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": int((datetime.now(timezone.utc) - self.started_at).total_seconds()),
            "limits": self.limits.to_dict(),
            "exceeded_reason": self._exceeded_reason,
        }


class BudgetCallbackHandler(BaseCallbackHandler):
    """LangChain callback that feeds BudgetTracker.

    Attach alongside AgentLogger / UsageAccumulator. It reads token usage
    from usage_metadata (same path as UsageAccumulator) and increments
    tool call counts from on_tool_start.

    If budget is exceeded, it sets task_state.cancelled and flag so the
    outer astream loop can stop gracefully.
    """

    def __init__(self, tracker: BudgetTracker, task_state: Any | None = None) -> None:
        self.tracker = tracker
        self._task_state = task_state

    def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
        self.tracker.record_tool(1)
        # If exceeded, propagate cancellation hint
        exceeded, _ = self.tracker.is_exceeded()
        if exceeded and self._task_state is not None:
            try:
                self._task_state.cancelled = True
                self._task_state._stop_event.set()
            except Exception:
                pass

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        input_tokens: int | None = None
        output_tokens: int | None = None
        found = False
        for gen_list in response.generations:
            for gen in gen_list:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if usage:
                    it = usage.get("input_tokens")
                    ot = usage.get("output_tokens")
                    if it is not None or ot is not None:
                        input_tokens = (input_tokens or 0) + (it or 0)
                        output_tokens = (output_tokens or 0) + (ot or 0)
                        found = True
        if not found:
            fb = (response.llm_output or {}).get("token_usage") or {}
            it = fb.get("prompt_tokens") or fb.get("input_tokens")
            ot = fb.get("completion_tokens") or fb.get("output_tokens")
            if it is not None or ot is not None:
                input_tokens = it
                output_tokens = ot

        # Always count as an LLM call even if usage missing (e.g. Ollama no tokens)
        if input_tokens is None and output_tokens is None:
            # Still count call with 0 tokens to track llm_calls ceiling
            self.tracker.record_llm(0, 0)
        else:
            self.tracker.record_llm(input_tokens, output_tokens)

        exceeded, _ = self.tracker.is_exceeded()
        if exceeded and self._task_state is not None:
            try:
                self._task_state.cancelled = True
                self._task_state._stop_event.set()
            except Exception:
                pass


def get_budget_limits_for_task(kind: str = "chat") -> BudgetLimits:
    """Per-kind budget — chat slightly more generous than board_task/automation.

    Tries runner's budget config first (if runner is active), then env overrides.
    """
    try:
        from core.runner import get_runner_or_none

        runner = get_runner_or_none()
        if runner is not None:
            return runner.get_budget_limits(kind)
    except Exception:
        pass

    defaults: dict[str, dict[str, Any]] = {
        "chat": {"max_total_tokens": 500_000, "max_llm_calls": 200, "max_tool_calls": 300, "max_duration_seconds": 1800},
        "automation": {"max_total_tokens": 600_000, "max_llm_calls": 200, "max_tool_calls": 300, "max_duration_seconds": 1800},
        "workflow": {"max_total_tokens": 400_000, "max_llm_calls": 150, "max_tool_calls": 200, "max_duration_seconds": 1800},
        "board_task": {"max_total_tokens": 400_000, "max_llm_calls": 150, "max_tool_calls": 200, "max_duration_seconds": 1800},
    }
    base = defaults.get(kind, defaults["chat"])
    # from_env overrides defaults
    env_limits = BudgetLimits.from_env()
    merged = dict(base)
    for k in base.keys():
        env_v = getattr(env_limits, k)
        if env_v is not None:
            merged[k] = env_v
    return BudgetLimits(**merged)
