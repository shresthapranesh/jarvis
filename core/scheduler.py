"""APScheduler integration for scheduled automations."""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import state

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler(timezone="UTC")


def _register_scheduler_job(auto) -> None:
    """Register (or replace) a cron job for the given automation."""
    try:
        _scheduler.add_job(
            func=_run_scheduled_automation,
            trigger=CronTrigger.from_crontab(auto.schedule),
            id=f"auto_{auto.id}",
            args=[auto.id],
            replace_existing=True,
            misfire_grace_time=60,
        )
    except Exception as exc:
        logger.warning(
            "failed to register automation %s (schedule=%r): %s",
            auto.id, auto.schedule, exc,
        )


def _remove_scheduler_job(automation_id: str) -> None:
    job_id = f"auto_{automation_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


def _run_scheduled_automation(automation_id: str) -> None:
    """Called from BackgroundScheduler thread — enqueues a queue job on the
    main loop and returns immediately. The automation Worker consumes the job
    on the same loop; we don't block the scheduler thread on the actual run."""
    if state._main_loop is None or state._queue is None:
        return
    queue = state._queue

    async def _enqueue() -> None:
        await queue.enqueue(
            "automation",
            {"automation_id": automation_id, "triggered_by": "schedule"},
        )

    future = asyncio.run_coroutine_threadsafe(_enqueue(), state._main_loop)
    try:
        future.result(timeout=10.0)
    except Exception:
        logger.exception("failed to enqueue scheduled automation %s", automation_id)


def _run_board_dispatch() -> None:
    """Called from BackgroundScheduler thread — runs one board-dispatch pass
    (promote todo→ready, enqueue ready board tasks) on the main loop."""
    from server.task_board_runtime import dispatch_board_tasks  # noqa: PLC0415

    if state._main_loop is None or state._queue is None:
        return
    future = asyncio.run_coroutine_threadsafe(dispatch_board_tasks(), state._main_loop)
    try:
        future.result(timeout=30)
    except Exception:
        logger.exception("board dispatch tick failed")


def register_board_dispatch_job(interval_seconds: int = 15) -> None:
    """Register the task-board dispatcher interval job. Called once from the
    server lifespan. Mutations/tools also kick dispatch directly on create;
    this tick catches promotions and anything those kicks missed."""
    from apscheduler.triggers.interval import IntervalTrigger

    _scheduler.add_job(
        func=_run_board_dispatch,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id="board_dispatch",
        replace_existing=True,
        misfire_grace_time=30,
    )
    logger.info("board dispatch scheduled: every %ss", interval_seconds)


def _run_memory_consolidation() -> None:
    """Called from BackgroundScheduler thread — submits memory consolidation onto the main loop."""
    from core.memory_consolidation import consolidate_memory  # noqa: PLC0415

    if state._main_loop is None:
        return
    store = state._store
    if store is None:
        return
    future = asyncio.run_coroutine_threadsafe(
        consolidate_memory(store),
        state._main_loop,
    )
    try:
        result = future.result(timeout=300)
        logger.info("memory consolidation: %s", result)
    except Exception:
        logger.exception("memory consolidation failed")


def register_memory_consolidation_job(cron_expr: str = "0 */6 * * *") -> None:
    """Register the memory consolidation cron job. Called once from server lifespan."""
    _scheduler.add_job(
        func=_run_memory_consolidation,
        trigger=CronTrigger.from_crontab(cron_expr),
        id="memory_consolidation",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("memory consolidation scheduled: %s", cron_expr)


def _cleanup_staged_uploads(max_age_seconds: int = 3600) -> None:
    """Delete staged upload files (and their .meta.json sidecars) older than
    `max_age_seconds`. Files that were claimed by a successful startTask are
    deleted at claim time; this catches abandoned uploads."""
    import time  # noqa: PLC0415
    from core.config import get_config  # noqa: PLC0415

    cfg = get_config()
    if not cfg.staging_dir.exists():
        return
    cutoff = time.time() - max_age_seconds
    for entry in cfg.staging_dir.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("staging cleanup: failed to unlink %s: %s", entry, e)


def register_staging_cleanup_job(cron_expr: str = "0 * * * *") -> None:
    """Register the staged-uploads cleanup cron job. Defaults to hourly."""
    _scheduler.add_job(
        func=_cleanup_staged_uploads,
        trigger=CronTrigger.from_crontab(cron_expr),
        id="staging_cleanup",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("staging cleanup scheduled: %s", cron_expr)


def _run_kernel_reaper() -> None:
    """Called from BackgroundScheduler thread — reaps idle run_cell kernels on the main loop."""
    from core.kernels import get_kernel_registry  # noqa: PLC0415

    if state._main_loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(
        get_kernel_registry().reap_idle(),
        state._main_loop,
    )
    try:
        future.result(timeout=120)
    except Exception:
        logger.exception("kernel reaper failed")


def register_kernel_reaper_job(cron_expr: str = "*/10 * * * *") -> None:
    """Register the idle-kernel reaper cron job. Defaults to every 10 minutes."""
    _scheduler.add_job(
        func=_run_kernel_reaper,
        trigger=CronTrigger.from_crontab(cron_expr),
        id="kernel_reaper",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("kernel reaper scheduled: %s", cron_expr)


def _prune_memory_activities_job() -> None:
    """Prune memory_activities older than 90 days — runs on main loop."""
    if state._main_loop is None:
        return
    from db.engine import async_session
    from db.ops import prune_memory_activities

    async def _prune() -> None:
        async with async_session() as session:
            count = await prune_memory_activities(session, older_than_days=90)
            if count:
                logger.info("pruned %d old memory_activities", count)

    future = asyncio.run_coroutine_threadsafe(_prune(), state._main_loop)
    try:
        future.result(timeout=60)
    except Exception:
        logger.exception("memory_activities prune failed")


def register_memory_activity_prune_job(cron_expr: str = "0 4 * * *") -> None:
    """Register daily prune for memory_activities (default 04:00 UTC)."""
    _scheduler.add_job(
        func=_prune_memory_activities_job,
        trigger=CronTrigger.from_crontab(cron_expr),
        id="memory_activity_prune",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("memory_activity prune scheduled: %s", cron_expr)
