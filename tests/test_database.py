"""Connection lifecycle — the invariants PR #37 established.

The engine used to be a module global built at import time from get_config(),
with nothing ever disposing it. These lock in the properties that replaced it.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text


def _checked_in(db) -> int:
    """Connections resident in the pool. `checkedin` lives on QueuePool,
    not the Pool base class, so the type checker needs a nudge."""
    return cast(Any, db.engine.pool).checkedin()


async def test_init_creates_schema_and_fts(work_dir: Path):
    from db.engine import Database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/d.db")
    await db.init()
    async with db.session() as s:
        tables = (await s.execute(
            text("SELECT count(*) FROM sqlite_master WHERE type='table'")
        )).scalar()
        fts = (await s.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name LIKE '%_fts'")
        )).scalar()
    assert tables and tables > 20, "expected the full schema"
    assert fts == 2, "memories_fts + document_chunks_fts (hybrid retrieval)"
    await db.close()


async def test_init_is_idempotent(work_dir: Path):
    """Migrations re-run on every boot, so they must be safe to repeat."""
    from db.engine import Database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/d.db")
    await db.init()
    await db.init()
    await db.init()
    async with db.session() as s:
        assert (await s.execute(text("SELECT 1"))).scalar() == 1
    await db.close()


async def test_pragmas_applied_per_connection(work_dir: Path):
    """journal_mode persists in the file, but synchronous/busy_timeout are
    per-connection — so the listener has to fire on every new one."""
    from db.engine import Database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/d.db", pool_size=1, max_overflow=4)
    await db.init()
    for _ in range(3):
        async with db.session() as s:
            assert (await s.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await s.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
    await db.close()


async def test_creates_parent_directory(tmp_path: Path):
    """A tenant db at /data/<uid>/database.db must not require pre-made dirs."""
    from db.engine import Database

    nested = tmp_path / "a" / "b" / "c"
    db = Database(f"sqlite+aiosqlite:///{nested}/d.db")
    assert nested.is_dir()
    await db.init()
    assert (nested / "d.db").exists()
    await db.close()


async def test_close_releases_connections_and_threads(work_dir: Path):
    """aiosqlite runs each connection on its own OS thread; dispose() must
    hand them back, or a Database shorter-lived than the process leaks."""
    from db.engine import Database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/d.db", pool_size=2, max_overflow=8)
    await db.init()
    baseline = threading.active_count()

    async def hold():
        async with db.session() as s:
            await s.execute(text("SELECT 1"))
            await asyncio.sleep(0.2)

    await asyncio.gather(*(hold() for _ in range(10)))
    assert threading.active_count() > baseline - 1  # connections did get made

    await db.close()
    await asyncio.sleep(0.2)
    assert _checked_in(db) == 0, "pool still holds connections after close"


async def test_concurrency_ceiling_is_respected(work_dir: Path):
    """pool_size is the idle footprint; overflow is the ceiling. Both matter:
    too small a ceiling and concurrent handlers deadlock on checkout."""
    from db.engine import Database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/d.db", pool_size=2, max_overflow=13)
    await db.init()

    async def one():
        async with db.session() as s:
            await s.execute(text("SELECT 1"))
            await asyncio.sleep(0.3)

    await asyncio.wait_for(asyncio.gather(*(one() for _ in range(15))), timeout=20)
    await asyncio.sleep(0.2)
    assert _checked_in(db) <= 2, "resident set should collapse to pool_size"
    await db.close()


def test_importing_db_has_no_side_effects(tmp_path: Path):
    """`import db` used to call get_config() and mkdir work_dir at import time,
    which froze the database URL before any caller could choose one. A
    subprocess is the only honest way to test import-time behaviour."""
    home = tmp_path / "home"
    code = (
        "import pathlib, db;"
        f"print((pathlib.Path({str(home)!r}) / '.jarvis').exists())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", "importing db created ~/.jarvis"


async def test_database_is_selectable_after_import(work_dir: Path):
    """The point of the refactor: pick the database at runtime, not import time."""
    from db.engine import Database, async_session, get_database, set_database

    chosen = Database(f"sqlite+aiosqlite:///{work_dir}/chosen.db")
    set_database(chosen)
    await chosen.init()

    assert get_database() is chosen
    async with async_session() as s:
        assert (await s.execute(text("SELECT 1"))).scalar() == 1
    assert (work_dir / "chosen.db").exists()
    await chosen.close()
