"""Shared fixtures.

Every test runs against a throwaway work_dir. Nothing here may touch
~/.jarvis — the fixtures point WORK_DIR at tmp_path and clear the cached
AppConfig around each test so the redirect actually takes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

# Python's recorded answers, for the tests that diff the Rust server.
pytest_plugins = ["python_golden"]


def _reset_globals() -> None:
    """Drop every process-level singleton a test may have installed."""
    from core import state
    from core.config import _process_default_config
    from core.runner import set_runner
    from db.engine import set_database

    set_runner(None)
    set_database(None)
    _process_default_config.cache_clear()
    state._store = None
    state._http_client = None
    state._queue = None
    state._main_loop = None
    state._tasks.clear()


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated WORK_DIR. Everything (db, checkpoints, artifacts) lands here."""
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CHECKPOINTS_DB", raising=False)
    _reset_globals()
    yield tmp_path
    _reset_globals()


@pytest.fixture
async def database(work_dir: Path):
    """An initialized Database installed as the process default."""
    from db.engine import Database, set_database

    db = Database(f"sqlite+aiosqlite:///{work_dir}/database.db")
    set_database(db)
    await db.init()
    yield db
    await db.close()


@contextlib.asynccontextmanager
async def boot_jarvis() -> AsyncIterator[object]:
    """Minimal analog of server.entrypoint's lifespan: build every resource,
    hand them to a JarvisRunner, install it. Everything downstream
    (get_config / get_database / get_queue / get_store / ...) resolves through
    the runner once it is installed.

    Deliberately does NOT start the scheduler, queue workers, MCP or the bots —
    tests drive job handlers directly so a run is synchronous and observable.
    """
    import httpx

    from core import state
    from core.config import get_config
    from core.queue import SqliteJobQueue
    from core.runner import JarvisRunner, set_runner
    from core.transcript_store import KvStore, import_store_once
    from db import async_session, close_db, get_database, init_db

    cfg = get_config()
    state._main_loop = asyncio.get_running_loop()  # _notify marshals onto this
    await init_db()
    # As the lifespan does: the LangGraph store marker is there from the start.
    async with async_session() as session:
        await import_store_once(session, cfg.checkpoints_db)

    async with httpx.AsyncClient(timeout=30.0) as http:
        runner = JarvisRunner(
            config=cfg,
            store=KvStore(),
            queue=SqliteJobQueue(),
            http_client=http,
            db=get_database(),
        )
        set_runner(runner)
        try:
            yield runner
        finally:
            set_runner(None)
            state._main_loop = None
    await close_db()


@pytest.fixture
async def jarvis(work_dir: Path):
    """A fully booted Jarvis instance scoped to a throwaway work_dir."""
    async with boot_jarvis() as runner:
        yield runner
