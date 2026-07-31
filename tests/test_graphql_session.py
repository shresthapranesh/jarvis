"""The shared-AsyncSession race, and the extension that fixes it (PR #37).

get_context injects one AsyncSession per request and all ~87 resolvers read it
from info.context, but graphql-core resolves sibling fields concurrently. An
AsyncSession cannot take concurrent operations, so two resolvers racing to
provision its connection blow up and leave the session unusable.

The race needs a COLD pool to be reliable: once a connection is checked in,
provisioning returns fast enough that resolvers rarely interleave. Every test
here builds a fresh Database per operation for that reason -- without it this
suite passes whether or not the fix is present.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

# Six root fields, all async, all hitting the session.
WIDE_QUERY = """{
  projects { id name }
  conversations { id title }
  artifacts { id }
  skills { id }
  automations { id }
  workflows { id }
}"""

RACE_MARKERS = (
    "concurrent operations are not permitted",
    "IllegalStateChange",
    "is already in progress",
    "_connection_for_bind",
)


def _is_race(blob: str) -> bool:
    return any(m in blob for m in RACE_MARKERS)


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _seed(work_dir: Path) -> None:
    from db import async_session, ops

    async with async_session() as s:
        for i in range(3):
            await ops.create_project(s, name=f"proj{i}")
            await ops.get_or_create_conversation(
                s, f"conv{i}", model="test:model", title=f"chat{i}"
            )


async def _run_cold(work_dir: Path, query: str) -> tuple[bool, Any]:
    """Execute `query` on a session from a brand-new (cold-pool) Database.
    Returns (hit_race, result). The race can surface either as GraphQL field
    errors or by corrupting the session so that close() itself throws."""
    from db.engine import Database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/database.db")
    from server.graphql.schema import schema

    hit: bool = False
    result: Any = None
    try:
        async with db.session() as s:
            result = await schema.execute(query, context_value=_context(s))
            if result.errors and _is_race(" ".join(str(e) for e in result.errors)):
                hit = True
    except Exception as exc:
        if _is_race(f"{type(exc).__name__} {exc}"):
            hit = True
        else:
            raise
    finally:
        try:
            await db.close()
        except Exception:
            pass
    return hit, result


async def test_cold_pool_multi_field_query_has_no_race(database, work_dir: Path):
    """The regression. Before the fix this hit 25/25; it must now be 0/25."""
    await _seed(work_dir)
    hits = 0
    for _ in range(25):
        hit, _ = await _run_cold(work_dir, WIDE_QUERY)
        hits += int(hit)
    assert hits == 0, f"{hits}/25 operations hit the shared-session race"


async def test_cold_pool_query_returns_complete_data(database, work_dir: Path):
    """Serializing must not silently drop fields -- every root field resolves."""
    await _seed(work_dir)
    for _ in range(10):
        hit, result = await _run_cold(work_dir, WIDE_QUERY)
        assert not hit
        assert result is not None
        assert not result.errors, result.errors
        assert result.data is not None
        assert len(result.data["projects"]) == 3
        assert len(result.data["conversations"]) == 3
        # Every requested key present, none swallowed.
        assert set(result.data) == {
            "projects", "conversations", "artifacts", "skills", "automations", "workflows",
        }


async def test_mutation_still_works(database, work_dir: Path):
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        result = await schema.execute(
            'mutation { createProject(input: {name: "from-mutation"}) { id name } }',
            context_value=_context(s),
        )
    assert not result.errors, result.errors
    assert result.data is not None
    assert result.data["createProject"]["name"] == "from-mutation"


async def test_nested_object_fields_do_not_deadlock(database, work_dir: Path):
    """A per-resolver lock deadlocks if a parent resolver holds it while its
    children resolve. Nested connections prove the ordering is safe."""
    await _seed(work_dir)
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        result = await asyncio.wait_for(
            schema.execute(
                "{ conversations { id title messages { edges { node { id role } } } } }",
                context_value=_context(s),
            ),
            timeout=30,
        )
    assert not result.errors, result.errors
    assert result.data is not None
    assert len(result.data["conversations"]) == 3


async def test_subscription_streams_without_holding_the_lock(database, work_dir: Path):
    """Subscriptions return async generators rather than awaitables, so the
    extension must pass them through. If it ever locked around one, the stream
    would stall and this times out."""
    from core.state import TaskState, _notify, _tasks
    from db import async_session
    from server.graphql.schema import schema

    state = TaskState(kind="chat", label="sub")
    _tasks["sub-task"] = state

    async def produce():
        for i in range(3):
            await asyncio.sleep(0.05)
            state.events.append({"event": "token", "data": f'{{"text": "t{i}"}}'})
            _notify(state)
        state.events.append(
            {"event": "done", "data": '{"message": "fin", "conversation_id": "c"}'}
        )
        state.done = True
        _notify(state)

    seen: list[str] = []
    try:
        async with async_session() as s:
            stream = await schema.subscribe(
                'subscription { taskEvents(taskId: "sub-task") { __typename } }',
                context_value=_context(s),
            )
            producer = asyncio.create_task(produce())
            async with asyncio.timeout(20):
                async for item in stream:  # type: ignore[union-attr]
                    assert not item.errors, item.errors
                    assert item.data is not None
                    seen.append(item.data["taskEvents"]["__typename"])
                    if seen[-1] == "DoneEvent":
                        break
            await producer
    finally:
        _tasks.pop("sub-task", None)

    assert "DoneEvent" in seen
    assert seen.count("TokenEvent") == 3, seen


async def test_raw_session_still_races_without_the_lock(work_dir: Path):
    """Control: the underlying hazard is real and unchanged. If this ever stops
    raising, the tests above have gone vacuous and the guard is being met some
    other way -- investigate rather than delete."""
    from db.engine import Database
    from sqlalchemy import text

    db = Database(f"sqlite+aiosqlite:///{work_dir}/raw.db")
    await db.init()
    await db.close()

    cold = Database(f"sqlite+aiosqlite:///{work_dir}/raw.db")
    with pytest.raises(Exception) as excinfo:
        async with cold.session() as s:
            await asyncio.gather(
                s.execute(text("SELECT 1")), s.execute(text("SELECT 2"))
            )
    assert _is_race(f"{type(excinfo.value).__name__} {excinfo.value}")
    await cold.close()
