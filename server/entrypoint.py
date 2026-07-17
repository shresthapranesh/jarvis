"""FastAPI server — lifespan, app creation, router wiring, and SPA fallback."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.config import get_config
from core.doc_index import configure_embedding_model
from core.log_setup import get_broadcast_handler, setup_logging
from core.model_catalog import load_custom_models
from core.queue import SqliteJobQueue, Worker

from core import state
from core.runner import JarvisRunner, set_runner
from db import async_session, init_db
from db.ops import cleanup_zombie_running_rows, get_custom_models, get_setting, list_enabled_scheduled_automations
from .graphql import graphql_router
from .routes_artifacts import router as artifacts_router
from .routes_documents import router as documents_router
from .routes_live import router as live_router
from .routes_logs import router as logs_router
from .routes_media import router as media_router
from .routes_uploads import router as uploads_router
from core.scheduler import (
    _register_scheduler_job,
    _scheduler,
    register_board_dispatch_job,
    register_kernel_reaper_job,
    register_memory_activity_prune_job,
    register_memory_consolidation_job,
    register_staging_cleanup_job,
)

_DIST = pathlib.Path(__file__).resolve().parent.parent / "static" / "dist"

logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging(get_config().work_dir, console=bool(os.environ.get("JARVIS_LOG_CONSOLE")))
    state._main_loop = asyncio.get_running_loop()
    get_broadcast_handler().attach_loop(state._main_loop)
    await init_db()
    _scheduler.start()
    async with async_session() as session:
        sweep = await cleanup_zombie_running_rows(session)
        if any(sweep.values()):
            logger.info("startup zombie sweep: %s", sweep)
        configure_embedding_model(await get_setting(session, "embedding.model"))
        load_custom_models(await get_custom_models(session))
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

        # ── MCP (ADK McpToolset analog) ─────────────────────────────────
        # Warm up MCP client so tools are cached before first agent build.
        # Merge env + file + DB (DB wins) so runtime-added servers are active on boot.
        try:
            from core.mcp import get_mcp_manager, load_mcp_server_configs_with_db

            db_cfg: dict = {}
            try:
                from core.mcp import get_mcp_servers_from_db
                from db import async_session as _async_session

                async with _async_session() as _sess:
                    db_cfg = await get_mcp_servers_from_db(_sess)
            except Exception:
                db_cfg = {}

            merged = load_mcp_server_configs_with_db(db_cfg=db_cfg)
            mcp_tools = await get_mcp_manager().initialize(merged if merged else None)
            logger.info("MCP initialized: %d tools from %d servers", len(mcp_tools), len(merged))
        except Exception as exc:
            logger.warning("MCP init failed: %s", exc, exc_info=True)

        # ── Runner (ADK analog) ───────────────────────────────────────
        # Centralizes checkpointer/store/queue/config and cache config.
        try:
            set_runner(
                JarvisRunner(
                    config=get_config(),
                    checkpointer=cp,
                    store=store,
                    queue=state._queue,
                    http_client=http,
                )
            )
            logger.info("runner initialized: %s", get_config().queue_backend)
        except Exception as exc:
            logger.warning("runner init failed: %s", exc)

        _reaper_task = asyncio.create_task(_run_lock_reaper(state._queue))
        _automation_worker_task = asyncio.create_task(_build_automation_worker(state._queue).run())
        _workflow_worker_task = asyncio.create_task(_build_workflow_worker(state._queue).run())
        _chat_worker_task = asyncio.create_task(_build_chat_worker(state._queue).run())
        _board_worker_task = asyncio.create_task(_build_board_worker(state._queue).run())
        register_memory_consolidation_job()
        register_staging_cleanup_job()
        register_kernel_reaper_job()
        register_board_dispatch_job()
        register_memory_activity_prune_job()

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

        # Stop workers BEFORE the queue/checkpointer/etc. tear down so any
        # in-flight handler can still update its run via async_session.
        _workers = (_automation_worker_task, _workflow_worker_task, _chat_worker_task, _board_worker_task)
        for _t in _workers:
            _t.cancel()
        for _t in _workers:
            with contextlib.suppress(asyncio.CancelledError):
                await _t

        _reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _reaper_task

        # Tear down any live run_cell kernels (workers are already stopped).
        from core.kernels import get_kernel_registry
        with contextlib.suppress(Exception):
            await get_kernel_registry().shutdown_all()
        # Close MCP clients
        try:
            from core.mcp import get_mcp_manager
            await get_mcp_manager().close()
        except Exception:
            pass

        state._async_checkpointer = None
        state._store = None
        state._http_client = None
        state._queue = None
        set_runner(None)
    _scheduler.shutdown(wait=False)
    state._main_loop = None


def _build_queue():
    """Construct the JobQueue named by AppConfig.queue_backend.
    Only "sqlite" is wired today; future backends (e.g. "redis://...") slot in here."""
    backend = get_config().queue_backend
    if backend == "sqlite":
        return SqliteJobQueue()
    raise RuntimeError(f"unknown JARVIS_QUEUE backend: {backend!r}")


# Per-kind concurrency caps. Handlers are I/O-bound (awaiting LLM tokens),
# so these jobs interleave on the event loop — the caps exist to bound
# SQLite writer contention (checkpointer + queue heartbeats all share one
# file), not CPU. Keep them modest.
_CHAT_CONCURRENCY = 5
_AUTOMATION_CONCURRENCY = 3
_WORKFLOW_CONCURRENCY = 3
_BOARD_CONCURRENCY = 3


def _build_automation_worker(queue) -> Worker:
    """Worker that consumes 'automation' jobs. TTL is sized for long agent
    runs (prompt-type automations can take many minutes); the Worker's
    heartbeat refreshes the lock at TTL/3, so a healthy worker keeps the
    job indefinitely. The window only bites if the worker hangs."""
    # Lazy import to break the entrypoint <-> automation_runtime cycle —
    # automation_runtime imports core.state, which imports core.queue.
    from server.automation_runtime import automation_job_handler  # noqa: PLC0415
    return Worker(
        queue,
        kinds=["automation"],
        handler=automation_job_handler,
        worker_id=f"automation-{os.getpid()}",
        ttl_seconds=600,
        max_concurrent=_AUTOMATION_CONCURRENCY,
    )


def _build_workflow_worker(queue) -> Worker:
    """Worker that consumes 'workflow' jobs. Workflow nodes can include
    long agent runs, so TTL is sized the same as automations. Map nodes
    already fan out sub-runs internally, so the cap stays low."""
    from server.workflow_runtime import workflow_job_handler  # noqa: PLC0415
    return Worker(
        queue,
        kinds=["workflow"],
        handler=workflow_job_handler,
        worker_id=f"workflow-{os.getpid()}",
        ttl_seconds=600,
        max_concurrent=_WORKFLOW_CONCURRENCY,
    )


def _build_chat_worker(queue) -> Worker:
    """Worker that consumes 'chat' jobs. Same TTL as automations — long
    agent loops with tool calls. On restart, the LangGraph checkpointer
    (thread_id == conv_id) lets the agent resume from the last node
    boundary rather than restarting from the user's original prompt."""
    from server.chat_runtime import chat_job_handler  # noqa: PLC0415
    return Worker(
        queue,
        kinds=["chat"],
        handler=chat_job_handler,
        worker_id=f"chat-{os.getpid()}",
        ttl_seconds=600,
        max_concurrent=_CHAT_CONCURRENCY,
    )


def _build_board_worker(queue) -> Worker:
    """Worker that consumes 'board_task' jobs — full agent loops, so the TTL
    matches the other agent-run kinds. The dispatcher additionally caps how
    many board jobs are in flight (MAX_IN_PROGRESS in task_board_runtime),
    so this concurrency just needs to be >= that cap."""
    from server.task_board_runtime import board_task_job_handler  # noqa: PLC0415
    return Worker(
        queue,
        kinds=["board_task"],
        handler=board_task_job_handler,
        worker_id=f"board-{os.getpid()}",
        ttl_seconds=600,
        max_concurrent=_BOARD_CONCURRENCY,
    )


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

