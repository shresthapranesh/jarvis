"""Project mutations — create, update, delete, and conversation membership."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import (
    create_project,
    delete_project as db_delete_project,
    get_project,
    set_conversation_project as db_set_conversation_project,
    update_project as db_update_project,
)

from ..types.conversation import Conversation
from ..types.project import Project


@strawberry.input
class ProjectCreateInput:
    name: str
    description: str | None = None
    instructions: str = ""


@strawberry.input
class ProjectUpdateInput:
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    memory: str | None = None


@strawberry.type
class ProjectMutation:
    @strawberry.mutation
    async def create_project(
        self,
        info: strawberry.Info,
        input: ProjectCreateInput,
    ) -> Project:
        name = input.name.strip()
        if not name:
            raise ValueError("name required")
        session = info.context["session"]
        proj = await create_project(
            session,
            name=name,
            description=input.description,
            instructions=input.instructions,
        )
        return Project.from_db(proj)

    @strawberry.mutation
    async def update_project(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        input: ProjectUpdateInput,
    ) -> Project:
        session = info.context["session"]
        existing = await get_project(session, id.node_id)
        if existing is None:
            raise ValueError("project not found")

        fields: dict = {}
        if input.name is not None:
            name = input.name.strip()
            if not name:
                raise ValueError("name required")
            fields["name"] = name
        if input.description is not None:
            fields["description"] = input.description
        if input.instructions is not None:
            fields["instructions"] = input.instructions
        if input.memory is not None:
            fields["memory"] = input.memory
        if not fields:
            raise ValueError("nothing to update")

        proj = await db_update_project(session, id.node_id, **fields)
        assert proj is not None
        return Project.from_db(proj)

    @strawberry.mutation
    async def consolidate_project_memory(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        model: str | None = None,
    ) -> str:
        """Run the memory consolidation pass for this project now.

        Skips the quiet-period wait the scheduled sweep observes, so a user who
        just finished a session doesn't have to wait for the next tick.
        """
        from core.model_catalog import is_valid_model
        from core.project_memory_consolidation import consolidate_project_memory
        from core.state import get_store
        from db.ops import resolve_model

        session = info.context["session"]
        if await get_project(session, id.node_id) is None:
            raise ValueError("project not found")
        model_id = await resolve_model(model)
        if not is_valid_model(model_id):
            raise ValueError(f"unknown model {model_id!r}; query `models` for the catalog")
        return await consolidate_project_memory(
            get_store(), id.node_id, model_id=model_id, force=True
        )

    @strawberry.mutation
    async def delete_project(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        """Delete a project. Its conversations are kept (membership cleared)."""
        session = info.context["session"]
        deleted = await db_delete_project(session, id.node_id)
        if not deleted:
            raise ValueError("project not found")
        return True

    @strawberry.mutation
    async def set_conversation_project(
        self,
        info: strawberry.Info,
        conversation_id: relay.GlobalID,
        project_id: relay.GlobalID | None = None,
    ) -> Conversation:
        """Assign a conversation to a project, or remove it (projectId: null).

        A dedicated mutation (rather than an updateConversation arg) because
        that mutation's None-means-unchanged convention can't express "clear".
        """
        session = info.context["session"]
        try:
            conv = await db_set_conversation_project(
                session,
                conversation_id.node_id,
                project_id.node_id if project_id is not None else None,
            )
        except ValueError as exc:
            raise ValueError(str(exc))
        if conv is None:
            raise ValueError("conversation not found")
        return Conversation.from_db(conv)
