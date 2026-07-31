"""Per-operation GraphQL context — exposes the AsyncSession to every resolver."""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session

from .extensions import SESSION_LOCK_KEY


class GraphQLContext(TypedDict):
    session: AsyncSession
    # Guards `session`: graphql-core resolves sibling fields concurrently and
    # an AsyncSession cannot take concurrent operations. Held by
    # SerializeSessionResolvers around every awaitable resolver.
    session_lock: asyncio.Lock


async def get_context(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GraphQLContext:
    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}
