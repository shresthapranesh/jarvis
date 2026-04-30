"""FastAPI server — lifespan, app creation, router wiring, and SPA fallback."""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.config import get_config
from core.log_setup import setup_logging

from core import state
from db import async_session, init_db
from db.ops import list_enabled_scheduled_automations
from .routes_automations import router as automations_router
from .routes_chat import router as chat_router
from .routes_live import router as live_router
from .routes_media import router as media_router
from .routes_memory import router as memory_router
from .routes_workflows import router as workflows_router
from core.scheduler import _register_scheduler_job, _scheduler, register_memory_consolidation_job

_DIST = pathlib.Path(__file__).parent.parent / "static" / "dist"


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging(get_config().work_dir, console=bool(os.environ.get("JARVIS_LOG_CONSOLE")))
    state._main_loop = asyncio.get_running_loop()
    await init_db()
    _scheduler.start()
    async with async_session() as session:
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
        register_memory_consolidation_job()

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

        yield

        if _tg_app is not None:
            assert _tg_app.updater is not None
            await _tg_app.updater.stop()
            await _tg_app.stop()
            await _tg_app.shutdown()
            state._telegram_bot = None

        state._async_checkpointer = None
        state._store = None
        state._http_client = None
    _scheduler.shutdown(wait=False)
    state._main_loop = None


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Assistant API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(media_router)
app.include_router(chat_router)
app.include_router(automations_router)
app.include_router(live_router)
app.include_router(memory_router)
app.include_router(workflows_router)


# ── SPA fallback — must be last ──────────────────────────────────────────────

if _DIST.exists():
    _INDEX = _DIST / "index.html"

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        file = _DIST / full_path
        if file.is_file():
            return FileResponse(str(file))
        return FileResponse(str(_INDEX))

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
