"""Workflow-run subscription event types."""

from __future__ import annotations

import json
from typing import Annotated, Any, Union, cast

import strawberry
from strawberry.scalars import JSON


@strawberry.type
class WorkflowNodeStartEvent:
    node_id: str
    node_type: str
    label: str


@strawberry.type
class WorkflowNodeTokenEvent:
    node_id: str
    text: str


@strawberry.type
class WorkflowNodeConditionEvent:
    node_id: str
    verdict: str  # "true" | "false"


@strawberry.type
class WorkflowNodeDoneEvent:
    node_id: str
    output: JSON


@strawberry.type
class WorkflowNodeErrorEvent:
    node_id: str
    error: str


@strawberry.type
class WorkflowMapStartEvent:
    node_id: str
    total: int


@strawberry.type
class WorkflowMapItemDoneEvent:
    node_id: str
    index: int
    result: JSON


@strawberry.type
class WorkflowDoneEvent:
    outputs: JSON
    run_id: str


@strawberry.type
class WorkflowErrorEvent:
    error: str
    run_id: str


@strawberry.type
class WorkflowStoppedEvent:
    run_id: str


@strawberry.type
class WorkflowApprovalRequestEvent:
    tool: str
    reason: str
    args: str
    node_id: str


@strawberry.type
class WorkflowApprovalResolvedEvent:
    tool: str
    approved: bool
    answer: str
    node_id: str


@strawberry.type
class WorkflowInterruptEvent:
    interrupt_id: str
    question: str


@strawberry.type
class WorkflowInterruptResolvedEvent:
    interrupt_id: str


@strawberry.type
class WorkflowBudgetExceededEvent:
    reason: str
    snapshot: str | None = None


WorkflowEvent = Annotated[
    Union[
        WorkflowNodeStartEvent,
        WorkflowNodeTokenEvent,
        WorkflowNodeConditionEvent,
        WorkflowNodeDoneEvent,
        WorkflowNodeErrorEvent,
        WorkflowMapStartEvent,
        WorkflowMapItemDoneEvent,
        WorkflowApprovalRequestEvent,
        WorkflowApprovalResolvedEvent,
        WorkflowInterruptEvent,
        WorkflowInterruptResolvedEvent,
        WorkflowBudgetExceededEvent,
        WorkflowDoneEvent,
        WorkflowErrorEvent,
        WorkflowStoppedEvent,
    ],
    strawberry.union("WorkflowEvent"),
]


def coerce_workflow_event(raw: dict) -> WorkflowEvent | None:
    """Convert a raw SSE-shaped event dict into a typed WorkflowEvent member."""
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

    if event_name == "node_start":
        return WorkflowNodeStartEvent(
            node_id=data.get("node_id", ""),
            node_type=data.get("node_type", ""),
            label=data.get("label", ""),
        )
    if event_name == "node_token":
        return WorkflowNodeTokenEvent(
            node_id=data.get("node_id", ""),
            text=data.get("text", ""),
        )
    if event_name == "node_condition":
        return WorkflowNodeConditionEvent(
            node_id=data.get("node_id", ""),
            verdict=str(data.get("verdict", "")),
        )
    if event_name == "node_done":
        return WorkflowNodeDoneEvent(
            node_id=data.get("node_id", ""),
            output=cast(Any, data.get("output") or {}),
        )
    if event_name == "node_error":
        return WorkflowNodeErrorEvent(
            node_id=data.get("node_id", ""),
            error=data.get("error", ""),
        )
    if event_name == "map_start":
        return WorkflowMapStartEvent(
            node_id=data.get("node_id", ""),
            total=int(data.get("total", 0)),
        )
    if event_name == "map_item_done":
        return WorkflowMapItemDoneEvent(
            node_id=data.get("node_id", ""),
            index=int(data.get("index", 0)),
            result=cast(Any, data.get("result") or {}),
        )
    if event_name == "workflow_done":
        return WorkflowDoneEvent(
            outputs=cast(Any, data.get("outputs") or {}),
            run_id=data.get("run_id", ""),
        )
    if event_name == "workflow_error":
        return WorkflowErrorEvent(
            error=data.get("error", ""),
            run_id=data.get("run_id", ""),
        )
    if event_name == "approval_request":
        return WorkflowApprovalRequestEvent(
            tool=data.get("tool", ""),
            reason=data.get("reason", ""),
            args=data.get("args", "") if isinstance(data.get("args"), str) else json.dumps(data.get("args", {})),
            node_id=data.get("node_id") or data.get("tool", ""),
        )
    if event_name == "approval_resolved":
        return WorkflowApprovalResolvedEvent(
            tool=data.get("tool", ""),
            approved=bool(data.get("approved", False)),
            answer=data.get("answer", ""),
            node_id=data.get("node_id") or data.get("tool", ""),
        )
    if event_name == "interrupt":
        return WorkflowInterruptEvent(
            interrupt_id=data.get("interrupt_id", ""),
            question=data.get("question", ""),
        )
    if event_name == "interrupt_resolved":
        return WorkflowInterruptResolvedEvent(interrupt_id=data.get("interrupt_id", ""))
    if event_name == "budget_exceeded":
        return WorkflowBudgetExceededEvent(
            reason=data.get("reason", ""),
            snapshot=data.get("snapshot") if isinstance(data.get("snapshot"), str) else json.dumps(data.get("snapshot") or {}),
        )
    if event_name == "workflow_stopped":
        return WorkflowStoppedEvent(run_id=data.get("run_id", ""))
    return None
