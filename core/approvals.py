"""Recording a pause as an `Approval` row, so the inbox can list it and the
request survives the run that raised it. Answering one, and the deferred
actions an agent's writes may need a human for, are the Rust server's
(`edge/src/gql/approval.rs`).
"""

from __future__ import annotations

import logging

from db import ops

logger = logging.getLogger(__name__)


async def record_blocking_request(
    *,
    source: str,
    kind: str,
    question: str,
    label: str,
    task_id: str | None = None,
    interrupt_id: str | None = None,
    parent_id: str | None = None,
    board_task_id: str | None = None,
    tool: str | None = None,
    args_json: str | None = None,
) -> str | None:
    """Persist a pause that is happening right now. Returns the row id.

    Best-effort by design: a failure here must not take down the run that is
    already suspended. The worst case is an approval missing from the inbox
    that the run's own subscriber can still resolve — losing the run itself
    would be strictly worse.
    """
    from db import async_session

    try:
        async with async_session() as session:
            row = await ops.create_approval(
                session,
                source=source,
                kind=kind,
                question=question,
                label=label,
                task_id=task_id,
                interrupt_id=interrupt_id,
                parent_id=parent_id,
                board_task_id=board_task_id,
                tool=tool,
                args_json=args_json,
            )
            return row.id
    except Exception:
        logger.warning("could not persist approval request", exc_info=True)
        return None
