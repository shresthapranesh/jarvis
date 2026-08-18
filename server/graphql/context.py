"""Per-operation GraphQL context — exposes the AsyncSession to every resolver."""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session

from .extensions import SESSION_LOCK_KEY


class GraphQLContext(TypedDict):
    session: AsyncSession
    # Guards `session`: graphql-core resolves sibling fields concurrently and
    # an AsyncSession cannot take concurrent operations. Held by
    # SerializeSessionResolvers around every awaitable resolver.
    session_lock: asyncio.Lock
    # "agent" when the request came from the `jarvis` SDK, else "human".
    # Destructive mutations gate on this: a person clicking Delete in the UI is
    # the approval, so making them approve themselves would be absurd — the
    # gate exists for writes the agent makes on its own initiative.
    #
    # This is an *ergonomic* boundary, not a security one. The header is
    # self-asserted and anyone who can reach /graphql can set it; the SDK runs
    # in a kernel the agent already controls, so there is no boundary here to
    # defend. Deployment isolation is the actual control (see CLAUDE.md).
    caller: str
    # Conversation the SDK call belongs to, so a recorded approval can say
    # which chat asked for it. None outside the SDK.
    caller_conversation_id: str | None


CALLER_HEADER = "x-jarvis-caller"
CONVERSATION_HEADER = "x-jarvis-conversation"


async def get_context(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GraphQLContext:
    caller = (request.headers.get(CALLER_HEADER) or "human").strip().lower()
    return {
        "session": session,
        SESSION_LOCK_KEY: asyncio.Lock(),
        "caller": "agent" if caller == "agent" else "human",
        "caller_conversation_id": request.headers.get(CONVERSATION_HEADER) or None,
    }
