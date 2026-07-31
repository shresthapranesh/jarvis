"""JarvisRunner as the source of truth for resources (PR #38).

The runner used to hold config/checkpointer/store/queue/http that nothing read
— every consumer went to a module global instead. These pin the inversion:
with a runner installed, the accessors must return *its* objects; without one,
they fall back to the process default.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


def _runner_with(cfg, db, *, queue, store, checkpointer, http):
    from core.runner import JarvisRunner

    return JarvisRunner(
        config=cfg, checkpointer=checkpointer, store=store,
        queue=queue, http_client=http, db=db,
    )


async def test_accessors_follow_the_runner(work_dir: Path):
    """Sentinel objects, so a pass cannot be two names for the same thing."""
    from core.config import AppConfig
    from core.runner import set_runner
    from core.state import (
        get_async_checkpointer, get_http_client, get_queue, get_store,
    )
    from db.engine import Database, get_database

    cfg = AppConfig.from_env({
        "work_dir": str(work_dir),
        "database_url": f"sqlite+aiosqlite:///{work_dir}/runner.db",
    })
    db = Database(cfg.database_url)
    queue, store, checkpointer = object(), object(), object()

    async with httpx.AsyncClient() as http:
        set_runner(_runner_with(cfg, db, queue=queue, store=store,
                                checkpointer=checkpointer, http=http))
        from core.config import get_config

        assert get_config() is cfg
        assert get_database() is db
        assert get_queue() is queue
        assert get_store() is store
        assert get_async_checkpointer() is checkpointer
        assert get_http_client() is http
        set_runner(None)

    await db.close()


async def test_accessors_fall_back_without_a_runner(work_dir: Path):
    """The CLI, tests, and anything pre-lifespan run with no runner at all."""
    from core.runner import get_runner_or_none
    from core.state import get_queue
    from db.engine import get_database

    assert get_runner_or_none() is None
    db = get_database()
    assert db is not None, "process default should be built on demand"
    # Resources that only the lifespan provides stay unavailable, loudly.
    with pytest.raises(RuntimeError, match="not initialized"):
        get_queue()
    await db.close()


async def test_runner_config_redirects_the_whole_app(work_dir: Path, tmp_path: Path):
    """The property that makes per-tenant processes work: point a runner at a
    directory and config-derived paths follow it."""
    from core.config import AppConfig, get_config
    from core.runner import set_runner
    from db.engine import Database

    elsewhere = tmp_path / "tenant-b"
    cfg = AppConfig.from_env({"work_dir": str(elsewhere)})
    db = Database(f"sqlite+aiosqlite:///{elsewhere}/database.db")

    async with httpx.AsyncClient() as http:
        set_runner(_runner_with(cfg, db, queue=object(), store=object(),
                                checkpointer=object(), http=http))
        assert Path(get_config().work_dir) == elsewhere
        assert Path(get_config().artifacts_dir).is_relative_to(elsewhere)
        assert Path(get_config().documents_dir).is_relative_to(elsewhere)
        set_runner(None)

    assert Path(get_config().work_dir) != elsewhere, "runner teardown must restore"
    await db.close()


async def test_booted_runner_owns_every_resource(jarvis):
    """Inside the real boot path, each accessor is identity-equal to the
    runner's own reference — no parallel copies."""
    from core.config import get_config
    from core.state import (
        get_async_checkpointer, get_http_client, get_queue, get_store,
    )
    from db.engine import get_database

    assert get_config() is jarvis.config
    assert get_database() is jarvis.db
    assert get_queue() is jarvis.queue
    assert get_store() is jarvis.store
    assert get_async_checkpointer() is jarvis.checkpointer
    assert get_http_client() is jarvis.http_client
