"""Pending-approval inbox — every outstanding HITL request, one table.

Approvals reach a human through three unrelated mechanisms (a chat interrupt,
a paused workflow node, a blocked board task) plus a fourth shape that blocks
nothing (a recorded destructive action awaiting sign-off). They now share the
`approvals` table, so this is a single indexed read rather than a merge of
in-memory and durable state — and a request outlives the process that raised
it. `core/approvals.reconcile_startup` is what keeps that honest: rows whose
run cannot be resumed are moved to `expired` at boot rather than listed here as
buttons that resume nothing.
"""

from __future__ import annotations

import strawberry

from db.ops import list_approvals

from ..types.approval import PendingApproval


@strawberry.type
class ApprovalQuery:
    @strawberry.field
    async def pending_approvals(self, info: strawberry.Info) -> list[PendingApproval]:
        """Everything currently awaiting a human, newest request first."""
        session = info.context["session"]
        return [PendingApproval.from_db(row) for row in await list_approvals(session)]
