"""Artifacts record the assistant message that produced them.

The chat UI renders each artifact as a card under its own message, so the
`artifacts.message_id` stamp is what makes an artifact reachable at all — the
card is the only entry point to the side panel. Two paths have to hold: the
write stamps it from the run config, and the one-shot migration attributes
rows written before the stamp existed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path


from db import async_session
from db.models import Artifact, Conversation, Message, _now


async def _clear_backfill_marker() -> None:
    from sqlalchemy import text

    from db.engine import _ARTIFACT_BACKFILL_KEY

    async with async_session() as session:
        await session.execute(
            text("DELETE FROM config_settings WHERE key = :k"),
            {"k": _ARTIFACT_BACKFILL_KEY},
        )
        await session.commit()


async def _artifact(session, **kw) -> Artifact:
    row = Artifact(id=kw.pop("id"), title="t", filename="", kind="markdown", **kw)
    session.add(row)
    await session.commit()
    return row


async def test_write_artifact_stamps_message_id(database, monkeypatch):
    """`write_artifact` takes message_id from the run's ToolContext."""
    from tools import artifacts as artifacts_tool
    from tools.context import ToolContext

    monkeypatch.setattr(
        artifacts_tool,
        "current_ctx",
        lambda: ToolContext(conversation_id="conv-1", message_id="msg-42"),
    )

    result = await artifacts_tool.write_artifact.ainvoke(
        {"title": "Report", "content": "# hello"}
    )
    art_id = __import__("json").loads(result)["id"]

    async with async_session() as session:
        row = await session.get(Artifact, art_id)
    assert row is not None
    assert row.message_id == "msg-42"
    assert row.conversation_id == "conv-1"


async def test_write_artifact_without_a_message_stays_null(database, monkeypatch):
    """Off-chat runs (automation, board, CLI) have no message to point at."""
    from tools import artifacts as artifacts_tool
    from tools.context import ToolContext

    monkeypatch.setattr(
        artifacts_tool, "current_ctx", lambda: ToolContext(conversation_id="conv-1")
    )
    result = await artifacts_tool.write_artifact.ainvoke({"title": "x", "content": "y"})
    art_id = __import__("json").loads(result)["id"]

    async with async_session() as session:
        row = await session.get(Artifact, art_id)
    assert row is not None and row.message_id is None


async def test_migration_backfills_unattached_artifacts(work_dir: Path):
    """Pre-existing rows attach to the newest assistant message at or before
    their creation — and the backfill runs once, not on every boot."""
    from db.engine import Database, set_database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/database.db")
    set_database(db)
    await db.init()
    try:
        base = _now()
        async with async_session() as session:
            session.add(Conversation(id="conv-1", title="c", model="google_genai:gemma-4-31b-it"))
            session.add(Message(
                id="msg-1", conversation_id="conv-1", role="assistant",
                content="a", created_at=base,
            ))
            session.add(Message(
                id="msg-2", conversation_id="conv-1", role="assistant",
                content="b", created_at=base + timedelta(minutes=10),
            ))
            # A later user message must never win — only assistant rows own artifacts.
            session.add(Message(
                id="msg-3", conversation_id="conv-1", role="user",
                content="c", created_at=base + timedelta(minutes=11),
            ))
            await session.commit()
            # Written during msg-1's run…
            await _artifact(
                session, id="art-1", conversation_id="conv-1",
                created_at=base + timedelta(minutes=1),
            )
            # …and during msg-2's.
            await _artifact(
                session, id="art-2", conversation_id="conv-1",
                created_at=base + timedelta(minutes=12),
            )
            # No conversation at all — nothing to attribute it to.
            await _artifact(session, id="art-3", created_at=base)

        # A DB that predates the stamp has no marker row — drop the one this
        # fixture's own init wrote, then restart into the migration.
        await _clear_backfill_marker()
        await db.init()  # re-run migrations, as a restart would

        async with async_session() as session:
            rows = {a: await session.get(Artifact, a) for a in ("art-1", "art-2", "art-3")}
        assert rows["art-1"].message_id == "msg-1"
        assert rows["art-2"].message_id == "msg-2"
        assert rows["art-3"].message_id is None

        # The marker makes it one-shot: a row deliberately cleared afterwards
        # (an artifact whose message was deleted) must not be re-guessed.
        async with async_session() as session:
            row = await session.get(Artifact, "art-1")
            row.message_id = None
            await session.commit()
        await db.init()
        async with async_session() as session:
            row = await session.get(Artifact, "art-1")
        assert row.message_id is None
    finally:
        await db.close()
        set_database(None)
