"""Automation-run subscription event types — discriminated union for
`stream_task_events` output on automation runs.

Reuses TokenEvent and ErrorEvent from chat events since the wire shape is
identical. Other events differ from chat (run_id vs conversation_id, output
vs message), so they get their own types."""

from __future__ import annotations

import json
from typing import Annotated, Union

import strawberry

from .events import ErrorEvent, TokenEvent


@strawberry.type
class AutomationDoneEvent:
    output: str | None
    run_id: str


@strawberry.type
class AutomationStoppedEvent:
    output: str | None
    run_id: str


AutomationEvent = Annotated[
    Union[
        TokenEvent,
        AutomationDoneEvent,
        AutomationStoppedEvent,
        ErrorEvent,
    ],
    strawberry.union("AutomationEvent"),
]


def coerce_automation_event(raw: dict) -> AutomationEvent | None:
    """Convert a raw SSE-shaped event dict into a typed AutomationEvent member."""
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
    if event_name == "done":
        return AutomationDoneEvent(
            output=data.get("output"),
            run_id=data.get("run_id", ""),
        )
    if event_name == "stopped":
        return AutomationStoppedEvent(
            output=data.get("output"),
            run_id=data.get("run_id", ""),
        )
    if event_name == "error":
        return ErrorEvent(error=data.get("error", ""))
    return None
