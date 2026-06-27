"""Skill queries."""

from __future__ import annotations

import strawberry

from db.ops import list_skills

from ..types.skill import Skill


@strawberry.type
class SkillQuery:
    @strawberry.field
    async def skills(self, info: strawberry.Info) -> list[Skill]:
        session = info.context["session"]
        rows = await list_skills(session)
        return [Skill.from_db(s) for s in rows]
