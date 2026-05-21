"""RunningTask GraphQL type — transient (in-memory only), not a Relay Node."""

from __future__ import annotations

from datetime import datetime

import strawberry


@strawberry.type
class RunningTask:
    id: str
    kind: str  # "chat" | "automation" | "workflow"
    label: str
    parent_id: str | None
    started_at: datetime
    has_interrupt: bool
    cancelled: bool
    done: bool


@strawberry.type
class StopRunningTaskPayload:
    ok: bool
    task_id: str
    kind: str
