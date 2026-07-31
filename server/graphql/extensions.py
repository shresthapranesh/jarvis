"""Schema extensions.

`SerializeSessionResolvers` exists because every resolver in an operation
shares the one `AsyncSession` that `get_context` injects, while graphql-core
resolves sibling fields **concurrently** (it gathers the awaitables that root
and object fields return). An `AsyncSession` does not tolerate concurrent
operations: two resolvers racing to provision the session's connection raise
`IllegalStateChangeError` / "this session is provisioning a new connection;
concurrent operations are not permitted", and the session is left in a state
where even `close()` throws.

This mostly bit the *first* request against a cold pool — once a connection is
checked in, provisioning is fast enough that resolvers rarely interleave — so
it read as a rare flake. It also hid well: GraphQL returns HTTP 200 with the
failure buried in the per-field `errors` array.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from graphql import GraphQLResolveInfo
from strawberry.extensions import SchemaExtension

SESSION_LOCK_KEY = "session_lock"


class SerializeSessionResolvers(SchemaExtension):
    """Run at most one awaitable field resolver at a time per operation.

    The lock is created alongside the session in `get_context`, so it has
    exactly the lifetime of the thing it protects; the fallback covers callers
    that build a context by hand (e.g. `schema.execute` in a test).

    Synchronous resolvers are passed straight through — they cannot interleave
    and never touch the session mid-flight. Subscription resolvers return async
    *generators* rather than awaitables, so they are passed through too and the
    lock is never held across a live stream.

    This serializes resolvers that never touch the database as well, which is
    the cost of not editing 87 call sites. It is cheap here: the resolvers are
    SQLite reads on a local file, and any real parallelism they might have had
    was already bounded by that same single session.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fallback_lock = asyncio.Lock()

    def _lock(self, context: Any) -> asyncio.Lock:
        if isinstance(context, dict):
            lock = context.get(SESSION_LOCK_KEY)
            if isinstance(lock, asyncio.Lock):
                return lock
        return self._fallback_lock

    async def resolve(  # type: ignore[override]
        self,
        _next: Any,
        root: Any,
        info: GraphQLResolveInfo,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = _next(root, info, *args, **kwargs)
        if not inspect.isawaitable(result):
            return result
        async with self._lock(info.context):
            return await result
