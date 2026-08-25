"""Maintenance over GraphQL — `maintenance prune-checkpoints` and `download-voice`.

The checkpoint half is a thin wrapper over `core.checkpoint_retention`, which
has its own guards; what is tested here is that the *reported* numbers are the
post-guard ones a UI would act on, and that a dry run deletes nothing.

The voice half is the interesting one: the repo path is derived from the voice
*name*, so an unparseable name has to fail loudly rather than 404 halfway
through a 60 MB transfer, and a partial file must never be left where `exists`
would call it done.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _exec(query: str, variables: dict[str, Any] | None = None) -> Any:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(query, variable_values=variables, context_value=_context(s))


# ── Checkpoints ──────────────────────────────────────────────────────────────

STATS = """
{ checkpointStats { dbPath exists sizeBytes threads checkpoints
                    subgraphCheckpoints prunableRoot prunableSubgraph
                    reclaimableBytes activeThreads } }
"""


async def test_stats_on_a_missing_db_are_zeros_not_an_error(database, work_dir):
    # A server that has never run a graph has no checkpoints.db. That is a
    # legitimate state for a settings page to render, not a failure.
    res = await _exec(STATS)
    assert not res.errors, res.errors
    s = res.data["checkpointStats"]
    assert s["checkpoints"] == 0
    assert s["prunableRoot"] == 0


async def test_dry_run_deletes_nothing(database, work_dir):
    from core.config import get_config

    db_path = Path(get_config().checkpoints_db)
    _seed_checkpoints(db_path, threads=2, per_thread=6)
    before = _count(db_path)

    res = await _exec(
        "mutation($d: Boolean!) { pruneCheckpoints(dryRun: $d)"
        " { rootPruned subgraphPruned bytesFreed dryRun note } }",
        {"d": True},
    )
    assert not res.errors, res.errors
    assert res.data["pruneCheckpoints"]["dryRun"] is True
    assert _count(db_path) == before, "a dry run must not touch the DB"


async def test_prune_removes_superseded_rows_and_keeps_the_newest(database, work_dir):
    from core.checkpoint_retention import KEEP_PER_THREAD
    from core.config import get_config

    db_path = Path(get_config().checkpoints_db)
    _seed_checkpoints(db_path, threads=2, per_thread=6)

    res = await _exec(
        "mutation { pruneCheckpoints { rootPruned subgraphPruned note } }"
    )
    assert not res.errors, res.errors
    assert res.data["pruneCheckpoints"]["rootPruned"] == 2 * (6 - KEEP_PER_THREAD)
    assert _count(db_path) == 2 * KEEP_PER_THREAD

    # The note must say the file will not shrink — freed pages go on the
    # freelist, and a UI that implied otherwise would send people looking for
    # megabytes that never come back.
    assert "vacuum" in res.data["pruneCheckpoints"]["note"].lower()


# ── Voice ────────────────────────────────────────────────────────────────────

VOICE = "{ voiceStatus { voice directory ready error files { name exists } } }"


async def test_voice_status_reports_missing_without_touching_the_network(database, work_dir):
    res = await _exec(VOICE)
    assert not res.errors, res.errors
    v = res.data["voiceStatus"]
    assert v["ready"] is False
    assert v["error"] == ""
    # Two files: the model and its config. /tts needs both.
    assert [f["name"] for f in v["files"]] == [
        "en_US-hfc_female-medium.onnx",
        "en_US-hfc_female-medium.onnx.json",
    ]


def test_unparseable_voice_name_fails_before_any_request(work_dir):
    from core.voice import voice_status

    s = voice_status("voices/nonsense.onnx", work_dir)
    assert s.ready is False
    assert "cannot parse" in s.error.lower()
    assert s.files == []


def test_ready_when_both_files_are_present(work_dir):
    from core.voice import voice_status

    d = work_dir / "voices"
    d.mkdir(parents=True, exist_ok=True)
    (d / "en_US-hfc_female-medium.onnx").write_bytes(b"model")
    (d / "en_US-hfc_female-medium.onnx.json").write_text("{}")

    s = voice_status("voices/en_US-hfc_female-medium.onnx", work_dir)
    assert s.ready is True
    assert all(f.exists for f in s.files)


def test_a_failed_download_leaves_no_partial_file(work_dir, monkeypatch):
    """`exists` is all the status check can cheaply do, so a truncated .onnx
    would read as ready forever. The download writes to `.part` and renames."""
    import httpx

    from core import voice

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "Client", lambda **kw: _Boom())

    with pytest.raises(httpx.ConnectError):
        voice.download_voice("voices/en_US-hfc_female-medium.onnx", work_dir)

    d = work_dir / "voices"
    assert not (d / "en_US-hfc_female-medium.onnx").exists()
    assert list(d.glob("*.part")) == []


# ── helpers ──────────────────────────────────────────────────────────────────

def _seed_checkpoints(db_path: Path, *, threads: int, per_thread: int) -> None:
    """Write root-ns checkpoints old enough for the sweep to consider them.

    Ids are uuid6, which is where the sweep reads a checkpoint's age from —
    backdating the timestamp is what gets past the minimum-age guard without
    having to wait an hour.
    """
    import time
    import uuid

    from core.checkpoint_retention import _UUID_EPOCH_100NS

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS checkpoints ("
        " thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, parent_checkpoint_id TEXT,"
        " type TEXT, checkpoint BLOB, metadata BLOB,"
        " PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS writes ("
        " thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT, idx INTEGER,"
        " channel TEXT, type TEXT, value BLOB,"
        " PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))"
    )
    old = time.time() - 86400
    for t in range(threads):
        for i in range(per_thread):
            cid = _uuid6_at(old + i, _UUID_EPOCH_100NS)
            con.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                (f"thread-{t}", "", cid, None, "msgpack", b"x" * 512, b"{}"),
            )
    con.commit()
    con.close()


def _uuid6_at(unix_seconds: float, epoch_offset: int) -> str:
    """A uuid6 string whose embedded timestamp is `unix_seconds`."""
    import uuid

    ts = int(unix_seconds * 1e7) + epoch_offset
    time_high = (ts >> 28) & 0xFFFFFFFF
    time_mid = (ts >> 12) & 0xFFFF
    time_low = ts & 0x0FFF
    node = uuid.uuid4().int & 0xFFFFFFFFFFFF
    clock_seq = uuid.uuid4().int & 0x3FFF
    value = (
        (time_high << 96)
        | (time_mid << 80)
        | (0x6 << 76)
        | (time_low << 64)
        | (0x2 << 62)
        | (clock_seq << 48)
        | node
    )
    return str(uuid.UUID(int=value))


def _count(db_path: Path) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
    finally:
        con.close()
