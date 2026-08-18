"""PendingApproval — one outstanding human-in-the-loop request.

Backed by the `approvals` table, so unlike the earlier in-memory version this
survives the run that raised it. Not a Relay Node: nothing refetches a single
approval by id (resolving one removes it from the list), so a Node id would be
ceremony without a consumer.
"""

from __future__ import annotations

from datetime import datetime, timezone

import strawberry

from db import models as db_models


def _utc(value: datetime) -> datetime:
    """Force UTC-aware — SQLite hands back naive datetimes even from a
    `DateTime(timezone=True)` column."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@strawberry.type
class PendingApproval:
    id: str
    # "chat" | "workflow" | "automation" | "board_task"
    source: str
    # "approval" (approve/deny) | "input" (free-text answer)
    kind: str
    question: str
    label: str
    tool: str | None
    args_json: str | None
    requested_at: datetime

    # True when approving *is* what performs the operation, because nothing is
    # blocked waiting on it. The UI says "will run on approval" rather than
    # implying something is currently paused.
    deferred: bool

    # Deep-link target for "open where this came from": a conversation id for
    # chat/board, a workflow id for workflow runs. None for a deferred request
    # whose caller did not identify a conversation.
    parent_id: str | None
    board_task_id: str | None

    @classmethod
    def from_db(cls, row: db_models.Approval) -> PendingApproval:
        return cls(
            id=row.id,
            source=row.source,
            kind=row.kind,
            question=row.question,
            label=row.label,
            tool=row.tool,
            args_json=row.args_json,
            requested_at=_utc(row.requested_at),
            deferred=row.action is not None,
            parent_id=row.parent_id,
            board_task_id=row.board_task_id,
        )


@strawberry.type
class ResolveApprovalPayload:
    id: str
    status: str
    # What resolving it produced — a deferred action's outcome, or a note that
    # the answer reached the waiting run. Surfaced so approving a delete tells
    # the human it actually deleted.
    result: str | None
