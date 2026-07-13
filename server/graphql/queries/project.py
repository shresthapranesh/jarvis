"""Project queries."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import get_project, list_projects

from ..types.project import Project


@strawberry.type
class ProjectQuery:
    @strawberry.field
    async def projects(self, info: strawberry.Info) -> list[Project]:
        session = info.context["session"]
        rows = await list_projects(session)
        return [Project.from_db(p) for p in rows]

    @strawberry.field
    async def project(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> Project | None:
        session = info.context["session"]
        row = await get_project(session, id.node_id)
        if row is None:
            return None
        return Project.from_db(row)
