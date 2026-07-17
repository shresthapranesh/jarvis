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
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    budget_exceeded: bool = False
    budget_reason: str | None = None


@strawberry.type
class StopRunningTaskPayload:
    ok: bool
    task_id: str
    kind: str
