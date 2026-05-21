"""GraphQL surface for the API — Strawberry schema + FastAPI router."""

from .router import router as graphql_router

__all__ = ["graphql_router"]
