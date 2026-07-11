"""BoardTask GraphQL type — one card on the shared task board."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import board_task_conversation_id, get_board_task


@strawberry.type
class BoardTask(relay.Node):
    id: relay.NodeID[str]
    title: str
    body: str | None
    status: str  # "todo" | "ready" | "running" | "blocked" | "done" | "archived"
    priority: int
    created_by: str  # "user" | "agent"
    model: str | None
    skill: str | None
    blocked_reason: str | None
    # "needs_input" | "agent" | "error" | "safety" | "stopped" | None
    blocked_kind: str | None
    failure_count: int
    summary: str | None
    result_metadata: str | None  # JSON object string
    # Conversation holding the task's run transcript (exists once it has run).
    conversation_id: str
    # Job id of the current/most recent dispatch — key for boardTaskEvents.
    run_id: str | None
    # Raw ids (not global ids) of linked tasks; populated by the list query.
    parent_ids: list[str]
    child_ids: list[str]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_db(
        cls,
        row: db_models.BoardTask,
        parent_ids: list[str] | None = None,
        child_ids: list[str] | None = None,
    ) -> BoardTask:
        return cls(
            id=row.id,
            title=row.title,
            body=row.body,
            status=row.status,
            priority=row.priority,
            created_by=row.created_by,
            model=row.model,
            skill=row.skill,
            blocked_reason=row.blocked_reason,
            blocked_kind=row.blocked_kind,
            failure_count=row.failure_count,
            summary=row.summary,
            result_metadata=row.result_metadata,
            conversation_id=board_task_conversation_id(row.id),
            run_id=row.job_id,
            parent_ids=parent_ids or [],
            child_ids=child_ids or [],
            created_at=row.created_at,
            updated_at=row.updated_at,
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
    ) -> BoardTask | None:
        session = info.context["session"]
        row = await get_board_task(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"BoardTask {node_id} not found")
            return None
        return cls.from_db(row)
