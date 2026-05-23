"""FastAPI server — lifespan, app creation, router wiring, and SPA fallback."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.config import get_config
from core.log_setup import get_broadcast_handler, setup_logging
from core.queue import SqliteJobQueue
from core.safety import configure_judge_model

from core import state
from db import async_session, init_db
from db.ops import get_setting, list_enabled_scheduled_automations
from .graphql import graphql_router
from .routes_artifacts import router as artifacts_router
from .routes_documents import router as documents_router
from .routes_live import router as live_router
from .routes_logs import router as logs_router
from .routes_media import router as media_router
from .routes_uploads import router as uploads_router
from core.scheduler import _register_scheduler_job, _scheduler, register_memory_consolidation_job, register_staging_cleanup_job

def _resource_root() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return pathlib.Path(__file__).resolve().parent.parent


_DIST = _resource_root() / "static" / "dist"


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging(get_config().work_dir, console=bool(os.environ.get("JARVIS_LOG_CONSOLE")))
    state._main_loop = asyncio.get_running_loop()
    get_broadcast_handler().attach_loop(state._main_loop)
    await init_db()
    _scheduler.start()
    async with async_session() as session:
        configure_judge_model(await get_setting(session, "safety.judge_model"))
        automations = await list_enabled_scheduled_automations(session)
        for auto in automations:
            _register_scheduler_job(auto)
    async with (
        AsyncSqliteSaver.from_conn_string(get_config().checkpoints_db) as cp,
        AsyncSqliteStore.from_conn_string(get_config().checkpoints_db) as store,
        httpx.AsyncClient(timeout=30.0) as http,
    ):
        state._async_checkpointer = cp
        state._store = store
        state._http_client = http
        state._queue = _build_queue()
        _reaper_task = asyncio.create_task(_run_lock_reaper(state._queue))
        register_memory_consolidation_job()
        register_staging_cleanup_job()

        _tg_app = None
        _tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if _tg_token:
            from server.telegram_bot import build_application
            _tg_app = build_application(_tg_token)
            await _tg_app.initialize()
            assert _tg_app.updater is not None
            await _tg_app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message"],
            )
            await _tg_app.start()
            state._telegram_bot = _tg_app.bot

        _dc_client = None
        _dc_task: asyncio.Task | None = None
        _dc_token = os.environ.get("DISCORD_BOT_TOKEN")
        if _dc_token:
            from server.discord_bot import build_client
            _dc_client = build_client()
            _dc_task = asyncio.create_task(_dc_client.start(_dc_token))
            state._discord_client = _dc_client

        yield

        if _tg_app is not None:
            assert _tg_app.updater is not None
            await _tg_app.updater.stop()
            await _tg_app.stop()
            await _tg_app.shutdown()
            state._telegram_bot = None

        if _dc_client is not None:
            await _dc_client.close()
            if _dc_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await _dc_task
            state._discord_client = None

        _reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _reaper_task

        state._async_checkpointer = None
        state._store = None
        state._http_client = None
        state._queue = None
    _scheduler.shutdown(wait=False)
    state._main_loop = None


def _build_queue():
    """Construct the JobQueue named by AppConfig.queue_backend.
    Only "sqlite" is wired today; future backends (e.g. "redis://...") slot in here."""
    backend = get_config().queue_backend
    if backend == "sqlite":
        return SqliteJobQueue()
    raise RuntimeError(f"unknown JARVIS_QUEUE backend: {backend!r}")


async def _run_lock_reaper(queue, interval_seconds: float = 60.0) -> None:
    """Periodically flip rows whose lock has expired back to `pending`."""
    import logging
    log = logging.getLogger("jarvis.queue")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            reaped = await queue.reap_expired_locks()
            if reaped:
                log.info("reaped %d expired job locks", reaped)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lock reaper iteration failed")


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Assistant API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(media_router)
app.include_router(live_router)
app.include_router(artifacts_router)
app.include_router(documents_router)
app.include_router(logs_router)
app.include_router(uploads_router)
app.include_router(graphql_router, prefix="/graphql")


# ── SPA fallback — must be last ──────────────────────────────────────────────

if _DIST.exists():
    _INDEX = _DIST / "index.html"

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        file = _DIST / full_path
        if file.is_file():
            return FileResponse(str(file))
        return FileResponse(str(_INDEX))

