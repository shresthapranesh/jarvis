"""APScheduler integration for scheduled automations.

Owns the BackgroundScheduler instance and the helpers to register/remove
cron jobs. The lifespan in server.py starts and stops the scheduler.
"""

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
    """Called from BackgroundScheduler thread — submits the run onto the main
    FastAPI event loop so all TaskState waiters, SSE futures, and the shared
    AsyncSqliteSaver stay on a single loop.
    """
    # Lazy import to break the scheduler <-> routes_automations cycle.
    from http_server.routes_automations import _execute_automation_bg  # noqa: PLC0415

    if state._main_loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(
        _execute_automation_bg(automation_id, triggered_by="schedule"),
        state._main_loop,
    )
    try:
        future.result()
    except Exception:
        pass


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
