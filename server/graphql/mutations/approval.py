"""Approval resolution — one entry point for every shape of request.

Deliberately a single mutation rather than four: the caller (the inbox) should
not have to know whether answering means executing a delete, waking an
`asyncio.Future`, or re-queuing a board task. `core/approvals.resolve`
dispatches on the row, so adding a shape does not change the API.

The edge serves this for a tool gate and a board question
(`edge/src/gql/approval.rs`, change both) and defers the other shapes here.

The per-surface mutations (`resumeWorkflowRun`, `resolveWorkflowApproval`,
`answerBoardTask`) stay — they are how the workflow view and the board card
answer in place, and each still closes the durable row. The chat view answers
with this one.
"""

from __future__ import annotations

import strawberry

from core.approvals import resolve

from ..types.approval import ResolveApprovalPayload


@strawberry.type
class ApprovalMutation:
    @strawberry.mutation
    async def resolve_approval(
        self, info: strawberry.Info, id: str, answer: str,
    ) -> ResolveApprovalPayload:
        """Answer a pending approval.

        `answer` is the literal text the request receives. For an approve/deny
        gate it is parsed by `is_affirmative_answer`, which denies on anything
        ambiguous — a reply matching no keyword is usually a question, and
        running a destructive action on that basis is the wrong default.
        """
        if not answer.strip():
            raise ValueError("answer must not be empty")
        row = await resolve(info.context["session"], id, answer.strip())
        return ResolveApprovalPayload(id=row.id, status=row.status, result=row.result)
