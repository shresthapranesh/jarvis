"""Task board mutations — create, update, move (status), delete, stop."""

from __future__ import annotations

import strawberry
from strawberry import relay

from core.agents import is_valid_model
from db.ops import (
    create_board_task as db_create_board_task,
    delete_board_task as db_delete_board_task,
    get_board_task,
    replace_board_task_parents,
    update_board_task as db_update_board_task,
)

from ..types.board_task import BoardTask

# Statuses a human can move a card to. "running" is dispatcher-owned.
_MOVABLE_STATUSES = ("todo", "ready", "done", "archived")


@strawberry.input
class BoardTaskInput:
    title: str
    body: str | None = None
    priority: int = 0
    model: str | None = None
    skill: str | None = None
    parent_ids: list[relay.GlobalID] | None = None
    # False parks the new task in "todo" instead of dispatching it.
    start: bool = True


@strawberry.input
class BoardTaskUpdateInput:
    title: str | None = None
    body: str | None = None
    priority: int | None = None
    model: str | None = None
    skill: str | None = None
    # When provided, REPLACES the task's parent links (empty list clears them).
    parent_ids: list[relay.GlobalID] | None = None


async def _kick_dispatch() -> None:
    from server.task_board_runtime import dispatch_board_tasks
    await dispatch_board_tasks()


@strawberry.type
class BoardTaskMutation:
    @strawberry.mutation
    async def create_board_task(
        self,
        info: strawberry.Info,
        input: BoardTaskInput,
    ) -> BoardTask:
        if input.model is not None and not is_valid_model(input.model):
            raise ValueError(f"unknown model {input.model!r}; query `models` for the catalog")
        session = info.context["session"]
        parent_ids = [g.node_id for g in (input.parent_ids or [])]
        task = await db_create_board_task(
            session,
            title=input.title,
            body=input.body,
            status="ready" if input.start else "todo",
            priority=input.priority,
            created_by="user",
            model=input.model,
            skill=input.skill,
            parent_ids=parent_ids,
        )
        if task.status == "ready":
            await _kick_dispatch()
        return BoardTask.from_db(task, parent_ids)

    @strawberry.mutation
    async def update_board_task(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        input: BoardTaskUpdateInput,
    ) -> BoardTask:
        if input.model is not None and not is_valid_model(input.model):
            raise ValueError(f"unknown model {input.model!r}; query `models` for the catalog")
        session = info.context["session"]
        task = await get_board_task(session, id.node_id)
        if task is None:
            raise ValueError("task not found")
        if task.status == "running":
            raise ValueError("task is running — stop it before editing")
        fields = {
            k: v for k, v in (
                ("title", input.title),
                ("body", input.body),
                ("priority", input.priority),
                ("model", input.model),
                ("skill", input.skill),
            ) if v is not None
        }
        task = await db_update_board_task(session, id.node_id, **fields)
        assert task is not None
        parent_ids: list[str] | None = None
        if input.parent_ids is not None:
            parent_ids = [g.node_id for g in input.parent_ids]
            task = await replace_board_task_parents(session, id.node_id, parent_ids)
            assert task is not None
        return BoardTask.from_db(task, parent_ids)

    @strawberry.mutation
    async def set_board_task_status(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        status: str,
    ) -> BoardTask:
        """Move a card: park (todo), queue (ready — also unblocks/re-runs),
        mark done manually, or archive. Running tasks must be stopped first."""
        if status not in _MOVABLE_STATUSES:
            raise ValueError(f"status must be one of {', '.join(_MOVABLE_STATUSES)}")
        session = info.context["session"]
        task = await get_board_task(session, id.node_id)
        if task is None:
            raise ValueError("task not found")
        if task.status == "running":
            raise ValueError("task is running — stop it before moving")
        fields: dict = {"status": status}
        if status in ("todo", "ready"):
            fields["blocked_reason"] = None
            fields["blocked_kind"] = None
            fields["finished_at"] = None
        task = await db_update_board_task(session, id.node_id, **fields)
        assert task is not None
        if status == "ready":
            await _kick_dispatch()
        return BoardTask.from_db(task)

    @strawberry.mutation
    async def decompose_board_task(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> list[BoardTask]:
        """Split a standalone waiting task into LLM-planned subtasks. The
        subtasks become parents of the original, which runs last as the
        synthesis step. Returns the created subtasks."""
        from server.task_board_runtime import decompose_board_task
        subtasks = await decompose_board_task(id.node_id)
        return [BoardTask.from_db(t) for t in subtasks]

    @strawberry.mutation
    async def answer_board_task(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        answer: str,
    ) -> BoardTask:
        """Answer a blocked task's question and resume it. The answer is
        delivered to the resumed run (same conversation thread, so the agent
        keeps the context of what it asked)."""
        if not answer.strip():
            raise ValueError("answer must not be empty")
        session = info.context["session"]
        task = await get_board_task(session, id.node_id)
        if task is None:
            raise ValueError("task not found")
        if task.status != "blocked":
            raise ValueError("only blocked tasks can be answered")
        task = await db_update_board_task(
            session, id.node_id,
            status="ready",
            pending_answer=answer.strip(),
            blocked_reason=None,
            blocked_kind=None,
            finished_at=None,
        )
        assert task is not None
        await _kick_dispatch()
        return BoardTask.from_db(task)

    @strawberry.mutation
    async def delete_board_task(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        task = await get_board_task(session, id.node_id)
        if task is None:
            raise ValueError("task not found")
        if task.status == "running":
            raise ValueError("task is running — stop it before deleting")
        return await db_delete_board_task(session, id.node_id)

    @strawberry.mutation
    async def stop_board_task(self, id: relay.GlobalID) -> bool:
        from server.task_board_runtime import stop_board_task
        stopped = await stop_board_task(id.node_id)
        if not stopped:
            raise ValueError("task is not running")
        return True
