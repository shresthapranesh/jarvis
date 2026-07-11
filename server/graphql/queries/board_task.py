"""Task board queries — full board (with links) + single task."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import get_board_task, list_board_task_links, list_board_tasks

from ..types.board_task import BoardTask


@strawberry.type
class BoardTaskQuery:
    @strawberry.field
    async def board_tasks(
        self,
        info: strawberry.Info,
        include_archived: bool = False,
    ) -> list[BoardTask]:
        session = info.context["session"]
        rows = await list_board_tasks(session, include_archived=include_archived)
        links = await list_board_task_links(session)
        parents: dict[str, list[str]] = {}
        children: dict[str, list[str]] = {}
        for link in links:
            parents.setdefault(link.child_id, []).append(link.parent_id)
            children.setdefault(link.parent_id, []).append(link.child_id)
        return [
            BoardTask.from_db(r, parents.get(r.id), children.get(r.id))
            for r in rows
        ]

    @strawberry.field
    async def board_task(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> BoardTask | None:
        session = info.context["session"]
        row = await get_board_task(session, id.node_id)
        if row is None:
            return None
        links = await list_board_task_links(session)
        parent_ids = [l.parent_id for l in links if l.child_id == row.id]
        child_ids = [l.child_id for l in links if l.parent_id == row.id]
        return BoardTask.from_db(row, parent_ids, child_ids)
