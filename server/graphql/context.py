"""Per-operation GraphQL context — exposes the AsyncSession to every resolver."""

from __future__ import annotations

from typing import Annotated, TypedDict

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session


class GraphQLContext(TypedDict):
    session: AsyncSession


async def get_context(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GraphQLContext:
    return {"session": session}
