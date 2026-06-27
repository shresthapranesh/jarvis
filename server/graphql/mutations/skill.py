"""Skill mutations — create, update, delete."""

from __future__ import annotations

import strawberry
from strawberry import relay

from core.skill_store import save_new_skill, save_skill_update
from db.ops import (
    delete_skill as db_delete_skill,
    get_skill,
    get_skill_by_name,
)

from ..types.skill import Skill


@strawberry.input
class SkillCreateInput:
    name: str
    description: str
    body: str
    enabled: bool = True


@strawberry.input
class SkillUpdateInput:
    name: str | None = None
    description: str | None = None
    body: str | None = None
    enabled: bool | None = None


@strawberry.type
class SkillMutation:
    @strawberry.mutation
    async def create_skill(
        self,
        info: strawberry.Info,
        input: SkillCreateInput,
    ) -> Skill:
        name = input.name.strip()
        if not name:
            raise ValueError("name required")
        if not input.description.strip():
            raise ValueError("description required")
        if not input.body.strip():
            raise ValueError("body required")
        session = info.context["session"]
        if await get_skill_by_name(session, name) is not None:
            raise ValueError(f"a skill named '{name}' already exists")
        skill = await save_new_skill(
            session,
            name=name,
            description=input.description.strip(),
            body=input.body,
            enabled=input.enabled,
        )
        return Skill.from_db(skill)

    @strawberry.mutation
    async def update_skill(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        input: SkillUpdateInput,
    ) -> Skill:
        session = info.context["session"]
        existing = await get_skill(session, id.node_id)
        if existing is None:
            raise ValueError("skill not found")

        name = input.name.strip() if input.name is not None else None
        if name is not None:
            if not name:
                raise ValueError("name required")
            clash = await get_skill_by_name(session, name)
            if clash is not None and clash.id != existing.id:
                raise ValueError(f"a skill named '{name}' already exists")

        description = input.description.strip() if input.description is not None else None
        if description is not None and not description:
            raise ValueError("description required")

        skill = await save_skill_update(
            session, id.node_id,
            name=name,
            description=description,
            body=input.body,
            enabled=input.enabled,
        )
        assert skill is not None
        return Skill.from_db(skill)

    @strawberry.mutation
    async def delete_skill(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        existing = await get_skill(session, id.node_id)
        if existing is None:
            raise ValueError("skill not found")
        await db_delete_skill(session, id.node_id)
        return True
