"""Conversation queries — conversations list, single conversation, todos."""

from __future__ import annotations

import json
from datetime import datetime

import strawberry
from strawberry import relay

from core.schemas import _normalise_todos
from db.models import ThreadState
from db.ops import get_conversation_meta, list_conversations

from ..types.conversation import Conversation
from ..types.todo import TodoItem


@strawberry.type
class ConversationQuery:
    # Required by Relay's @refetchable on Node-implementing fragments — Relay
    # generates queries that call `node(id: $id)` to refetch a single record.
    node: relay.Node = relay.node()

    @strawberry.field
    async def conversations(
        self, info: strawberry.Info, surface: str | None = "web",
    ) -> list[Conversation]:
        """List conversations for one surface (default "web", so bot/automation
        threads stay out of the sidebar). Pass surface: null to list all."""
        session = info.context["session"]
        rows = await list_conversations(session, surface=surface)
        # list_conversations returns dicts with an isoformat string for created_at.
        # message_count is resolved on demand via Conversation.message_count (one
        # COUNT(*) per conversation). N+1 in lists; revisit with DataLoader if it bites.
        return [
            Conversation(
                id=r["id"],
                title=r["title"],
                model=r["model"],
                surface=r["surface"],
                pinned=r["pinned"],
                ephemeral=r["ephemeral"],
                project_id=r["project_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    @strawberry.field
    async def conversation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> Conversation | None:
        session = info.context["session"]
        row = await get_conversation_meta(session, id.node_id)
        if row is None:
            return None
        return Conversation.from_db(row)

    @strawberry.field
    async def todos(self, info: strawberry.Info, conversation_id: str) -> list[TodoItem]:
        """The thread's todo list (`thread_state`)."""
        state = await info.context["session"].get(ThreadState, conversation_id)
        raw = json.loads(state.todos) if state is not None and state.todos else []
        return [TodoItem(text=t["text"], status=t["status"]) for t in _normalise_todos(raw)]
