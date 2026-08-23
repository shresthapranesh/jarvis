"""Chat subscription event types — discriminated union of everything that flows."""

from __future__ import annotations

import json
from typing import Annotated, Union

import strawberry

from .todo import TodoItem


@strawberry.type
class TokenEvent:
    text: str
    source: str


@strawberry.type
class ThinkingTokenEvent:
    text: str
    source: str


@strawberry.type
class StepEvent:
    node: str
    source: str
    subagent: str | None
    data: str  # JSON-encoded payload (caller deserializes if needed)


@strawberry.type
class BrowserStepEvent:
    thought: str   # JSON-encoded
    actions: str   # JSON-encoded
    source: str


@strawberry.type
class WorkerStartEvent:
    idx: int
    role: str
    task: str


@strawberry.type
class WorkerStepEvent:
    idx: int
    role: str
    node: str
    data: str  # JSON-encoded payload, same shape as StepEvent.data


@strawberry.type
class WorkerTokenEvent:
    idx: int
    text: str


@strawberry.type
class WorkerDoneEvent:
    idx: int
    role: str
    task: str
    status: str  # "done" | "error"
    result: str


@strawberry.type
class ArtifactEvent:
    artifact_id: str
    title: str
    action: str  # "created" | "updated"
    kind: str  # "markdown" | "audio" | "video" | "image" | "binary"
    preview: str | None


@strawberry.type
class TodosUpdatedEvent:
    todos: list[TodoItem]
    source: str


@strawberry.type
class InterruptEvent:
    interrupt_id: str
    question: str


@strawberry.type
class InterruptResolvedEvent:
    interrupt_id: str


@strawberry.type
class DoneEvent:
    message: str
    conversation_id: str


@strawberry.type
class StoppedEvent:
    message: str
    conversation_id: str


@strawberry.type
class ApprovalRequestEvent:
    tool: str
    reason: str
    args: str  # JSON-encoded
    # Set when the request is a durable per-tool gate (core/tool_gate.py) — the
    # run is blocked inside the tool call, not on a LangGraph interrupt, so the
    # chat prompt must answer it with `resolveApproval(id)` rather than
    # `resumeTask`. None for the older interrupt-backed approvals.
    approval_id: str | None = None


@strawberry.type
class ApprovalResolvedEvent:
    tool: str
    approved: bool
    answer: str


@strawberry.type
class WorkflowToolEvent:
    parent_run_id: str
    child_event: str
    data: str  # JSON-encoded remainder


@strawberry.type
class BudgetExceededEvent:
    reason: str
    snapshot: str  # JSON-encoded BudgetTracker snapshot


@strawberry.type
class BudgetUpdateEvent:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    llm_calls: int
    tool_calls: int
    snapshot: str  # JSON-encoded snapshot


@strawberry.type
class PerfUpdateEvent:
    """Throughput so far in this run. Emitted once per LLM call.

    `ttft_ms` is the first call's time-to-first-token (what the user waited for),
    not a rolling one — the aggregate rates cover every call including workers.
    Any field may be null when the split couldn't be measured (non-streaming
    provider, or a fully cache-served prefill).
    """

    ttft_ms: float | None
    llm_ms: float | None
    prefill_tps: float | None
    eval_tps: float | None
    llm_calls: int
    snapshot: str  # JSON-encoded PerfTracker snapshot (includes per-call records)


@strawberry.type
class ErrorEvent:
    error: str


ChatEvent = Annotated[
    Union[
        TokenEvent,
        ThinkingTokenEvent,
        StepEvent,
        BrowserStepEvent,
        WorkerStartEvent,
        WorkerStepEvent,
        WorkerTokenEvent,
        WorkerDoneEvent,
        ArtifactEvent,
        TodosUpdatedEvent,
        InterruptEvent,
        InterruptResolvedEvent,
        ApprovalRequestEvent,
        ApprovalResolvedEvent,
        WorkflowToolEvent,
        BudgetExceededEvent,
        BudgetUpdateEvent,
        PerfUpdateEvent,
        DoneEvent,
        StoppedEvent,
        ErrorEvent,
    ],
    strawberry.union("ChatEvent"),
]


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _safe_float(value: object) -> float | None:
    """None stays None — an unmeasurable rate must not become 0.0 in the UI."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        try:
            return int(str(value).strip() or default)
        except Exception:
            return default


def coerce_chat_event(raw: dict) -> ChatEvent | None:
    """Convert a raw `{"event": <name>, "data": <json string>}` SSE-shaped dict
    from `state.events` into a typed ChatEvent member. Returns None for unknown
    event names so the subscription can silently skip them rather than crash."""
    event_name = raw.get("event") or raw.get("type")
    data_field = raw.get("data")
    if isinstance(data_field, str):
        try:
            data = json.loads(data_field)
        except json.JSONDecodeError:
            data = {}
    elif isinstance(data_field, dict):
        data = data_field
    else:
        data = {}

    if event_name == "token":
        return TokenEvent(text=data.get("text", ""), source=data.get("source", "main"))
    if event_name == "thinking_token":
        return ThinkingTokenEvent(text=data.get("text", ""), source=data.get("source", "main"))
    if event_name == "step":
        return StepEvent(
            node=data.get("node", ""),
            source=data.get("source", ""),
            subagent=data.get("subagent"),
            data=_as_str(data.get("data")),
        )
    if event_name == "browser_step":
        return BrowserStepEvent(
            thought=_as_str(data.get("thought")),
            actions=_as_str(data.get("actions")),
            source=data.get("source", ""),
        )
    if event_name == "worker_start":
        return WorkerStartEvent(
            idx=_safe_int(data.get("idx", 0)),
            role=data.get("role", ""),
            task=data.get("task", ""),
        )
    if event_name == "worker_step":
        return WorkerStepEvent(
            idx=_safe_int(data.get("idx", 0)),
            role=data.get("role", ""),
            node=data.get("node", ""),
            data=_as_str(data.get("data")),
        )
    if event_name == "worker_token":
        return WorkerTokenEvent(
            idx=_safe_int(data.get("idx", 0)),
            text=data.get("text", ""),
        )
    if event_name == "worker_done":
        return WorkerDoneEvent(
            idx=_safe_int(data.get("idx", 0)),
            role=data.get("role", ""),
            task=data.get("task", ""),
            status=data.get("status", "done"),
            result=data.get("result", ""),
        )
    if event_name == "artifact":
        return ArtifactEvent(
            artifact_id=data.get("id", ""),
            title=data.get("title", ""),
            action=data.get("action", ""),
            kind=data.get("kind") or "markdown",
            preview=data.get("preview"),
        )
    if event_name == "todos_updated":
        todos_raw = data.get("todos") or []
        todos: list[TodoItem] = []
        for t in todos_raw:
            if isinstance(t, dict) and "text" in t:
                todos.append(TodoItem(
                    text=str(t["text"]),
                    status=str(t.get("status", "pending")),
                ))
        return TodosUpdatedEvent(todos=todos, source=data.get("source", ""))
    if event_name == "interrupt":
        return InterruptEvent(
            interrupt_id=data.get("interrupt_id", ""),
            question=data.get("question", ""),
        )
    if event_name == "interrupt_resolved":
        return InterruptResolvedEvent(interrupt_id=data.get("interrupt_id", ""))
    if event_name == "approval_request":
        return ApprovalRequestEvent(
            tool=data.get("tool", ""),
            reason=data.get("reason", ""),
            args=_as_str(data.get("args", {})),
            approval_id=data.get("approval_id") or None,
        )
    if event_name == "approval_resolved":
        return ApprovalResolvedEvent(
            tool=data.get("tool", ""),
            approved=bool(data.get("approved", False)),
            answer=data.get("answer", ""),
        )
    if event_name == "workflow_event":
        return WorkflowToolEvent(
            parent_run_id=data.get("parent_run_id", ""),
            child_event=data.get("child_event", ""),
            data=_as_str({k: v for k, v in data.items() if k not in ("parent_run_id", "child_event")}),
        )
    if event_name == "budget_exceeded":
        return BudgetExceededEvent(
            reason=data.get("reason", ""),
            snapshot=_as_str(data.get("snapshot") or {k: v for k, v in data.items() if k != "reason"}),
        )
    if event_name == "budget_update":
        snap = data.get("snapshot")
        return BudgetUpdateEvent(
            input_tokens=_safe_int(data.get("input_tokens", 0)),
            output_tokens=_safe_int(data.get("output_tokens", 0)),
            total_tokens=_safe_int(data.get("total_tokens", 0)),
            llm_calls=_safe_int(data.get("llm_calls", 0)),
            tool_calls=_safe_int(data.get("tool_calls", 0)),
            snapshot=_as_str(snap or data),
        )
    if event_name == "perf_update":
        return PerfUpdateEvent(
            ttft_ms=_safe_float(data.get("ttft_ms")),
            llm_ms=_safe_float(data.get("llm_ms")),
            prefill_tps=_safe_float(data.get("prefill_tps")),
            eval_tps=_safe_float(data.get("eval_tps")),
            llm_calls=_safe_int(data.get("llm_calls", 0)),
            snapshot=_as_str(data.get("snapshot") or data),
        )
    if event_name == "done":
        return DoneEvent(
            message=data.get("message", ""),
            conversation_id=data.get("conversation_id", ""),
        )
    if event_name == "stopped":
        return StoppedEvent(
            message=data.get("message", ""),
            conversation_id=data.get("conversation_id", ""),
        )
    if event_name == "error":
        return ErrorEvent(error=data.get("error", ""))
    return None
