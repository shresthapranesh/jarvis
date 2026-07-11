"""Automation + AutomationRun GraphQL types."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import get_automation, get_automation_run


@strawberry.type
class Automation(relay.Node):
    id: relay.NodeID[str]
    name: str
    description: str | None
    input_type: str  # "prompt" | "code" | "webhook"
    prompt_text: str | None
    model: str | None
    code_text: str | None
    webhook_url: str | None
    webhook_method: str | None
    webhook_headers: str | None  # JSON string
    webhook_body: str | None
    schedule: str | None  # cron expression
    enabled: bool
    stateful: bool
    # Conversation backing a stateful automation's shared thread; None otherwise.
    conversation_id: str | None
    notifications: str | None  # JSON string
    created_at: datetime
    updated_at: datetime
    next_run_at: str | None
    # Stats fields — populated by list query, None for single-fetch (matches REST).
    last_run_status: str | None = None
    last_run_at: str | None = None
    success_count_7d: int | None = None
    total_count_7d: int | None = None

    @classmethod
    def from_db(
        cls,
        row: db_models.Automation,
        stats: dict | None = None,
    ) -> Automation:
        # next_run_at depends on schedule + enabled; computed here so it's always populated.
        from server.automation_runtime import _compute_next_run_at
        from db.ops import automation_conversation_id
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            input_type=row.input_type,
            prompt_text=row.prompt_text,
            model=row.model,
            code_text=row.code_text,
            webhook_url=row.webhook_url,
            webhook_method=row.webhook_method,
            webhook_headers=row.webhook_headers,
            webhook_body=row.webhook_body,
            schedule=row.schedule,
            enabled=row.enabled,
            stateful=row.stateful,
            conversation_id=automation_conversation_id(row.id) if row.stateful else None,
            notifications=row.notifications,
            created_at=row.created_at,
            updated_at=row.updated_at,
            next_run_at=_compute_next_run_at(row),
            last_run_status=stats.get("last_run_status") if stats else None,
            last_run_at=stats.get("last_run_at") if stats else None,
            success_count_7d=stats.get("success_count_7d") if stats else None,
            total_count_7d=stats.get("total_count_7d") if stats else None,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> Automation | None:
        session = info.context["session"]
        row = await get_automation(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Automation {node_id} not found")
            return None
        return cls.from_db(row)


@strawberry.type
class AutomationRun(relay.Node):
    id: relay.NodeID[str]
    automation_id: str
    status: str  # "running" | "done" | "error" | "stopped" | "blocked" | "skipped"
    triggered_by: str  # "schedule" | "manual"
    output: str | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_db(cls, row: db_models.AutomationRun) -> AutomationRun:
        return cls(
            id=row.id,
            automation_id=row.automation_id,
            status=row.status,
            triggered_by=row.triggered_by,
            output=row.output,
            error=row.error,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> AutomationRun | None:
        session = info.context["session"]
        row = await get_automation_run(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"AutomationRun {node_id} not found")
            return None
        return cls.from_db(row)
