"""APScheduler integration for scheduled automations."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import state

logger = logging.getLogger(__name__)


# ── Timezone ─────────────────────────────────────────────────────────────────
# Cron expressions are written by humans ("0 9 * * 1" means Monday 9am to the
# person who typed it), so every schedule is interpreted in the machine's local
# timezone, not UTC. Override with the `scheduler.timezone` config setting or
# the JARVIS_TIMEZONE env var when the process runs somewhere (a UTC container)
# whose local time isn't the user's.

_timezone: tzinfo | None = None


def _resolve_timezone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.warning("invalid timezone %r (%s) — falling back to local time", name, exc)
        return None


def get_scheduler_timezone() -> tzinfo:
    """The timezone every cron expression is interpreted in.

    Resolution order: an explicit override (`set_scheduler_timezone`, i.e. the
    `scheduler.timezone` setting) → `JARVIS_TIMEZONE` → the machine's local
    timezone → UTC as a last resort.
    """
    global _timezone
    if _timezone is None:
        _timezone = _resolve_timezone(os.environ.get("JARVIS_TIMEZONE"))
    if _timezone is None:
        try:
            from tzlocal import get_localzone  # noqa: PLC0415

            _timezone = get_localzone()
        except Exception as exc:
            logger.warning("could not detect local timezone (%s) — using UTC", exc)
            _timezone = ZoneInfo("UTC")
    return _timezone


def set_scheduler_timezone(name: str | None) -> None:
    """Apply the `scheduler.timezone` config setting. Must be called before
    `_scheduler.start()` — APScheduler won't reconfigure a running scheduler."""
    resolved = _resolve_timezone(name)
    if resolved is None:
        return
    global _timezone
    _timezone = resolved
    _scheduler.configure(timezone=resolved)


# ── Day-of-week normalization ────────────────────────────────────────────────
# Unix cron numbers weekdays 0=Sunday..6=Saturday (7 also means Sunday).
# APScheduler numbers them 0=Monday..6=Sunday — and `CronTrigger.from_crontab`
# passes the field through verbatim without remapping, so every *numeric*
# day-of-week silently fires one day late ("0 9 * * 1" ran Tuesday, not Monday)
# and "0 9 * * 1-5" meant Tue-Sat rather than the weekdays it reads as.
# Weekday *names* are unambiguous in APScheduler, so we expand the field to an
# explicit name list and let it parse that.

_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # indexed by Unix number
_DOW_NUMS = {name: num for num, name in enumerate(_DOW_NAMES)}


def _dow_atom(token: str) -> int | None:
    """A single Unix day-of-week value as 0=Sunday..6=Saturday, or None if the
    token isn't one (unrecognized syntax is left for APScheduler to handle)."""
    token = token.strip().lower()
    if token in _DOW_NUMS:
        return _DOW_NUMS[token]
    if token.isdigit():
        value = int(token)
        # Unix cron accepts both 0 and 7 for Sunday.
        return value % 7 if value <= 7 else None
    return None


def _normalize_dow_field(field: str) -> str | None:
    """Rewrite a Unix-cron day-of-week field as explicit APScheduler weekday
    names. Returns None if the field uses syntax we don't model, in which case
    the caller passes it through untouched."""
    days: set[int] = set()
    for token in field.split(","):
        token = token.strip()
        base, _, step_raw = token.partition("/")
        if step_raw:
            if not step_raw.isdigit() or int(step_raw) < 1:
                return None
            step = int(step_raw)
        else:
            step = 1

        if base in ("*", "?"):
            lo, hi = 0, 6
        elif "-" in base.strip("-"):
            start_raw, _, end_raw = base.partition("-")
            lo, hi = _dow_atom(start_raw), _dow_atom(end_raw)
            if lo is None or hi is None:
                return None
        else:
            single = _dow_atom(base)
            if single is None:
                return None
            # A bare `a/n` means `a-6/n` in Unix cron; a bare `a` is just itself.
            lo, hi = single, (6 if step_raw else single)

        # Ranges wrap (`fri-mon` == Fri, Sat, Sun, Mon), and the step counts
        # from the start of the range, not from Sunday.
        span = (hi - lo) % 7
        days.update((lo + offset) % 7 for offset in range(0, span + 1, step))

    if not days:
        return None
    if len(days) == 7:
        return "*"
    # Emit in APScheduler's own Mon..Sun order purely so the trigger reads well.
    return ",".join(_DOW_NAMES[day] for day in sorted(days, key=lambda d: (d - 1) % 7))


def normalize_crontab(expr: str) -> str:
    """Translate a standard Unix crontab expression into the dialect
    `CronTrigger.from_crontab` actually implements. Only the day-of-week field
    differs; everything else is passed through untouched."""
    fields = expr.split()
    if len(fields) != 5:
        return expr  # let APScheduler raise its own "wrong number of fields"
    normalized = _normalize_dow_field(fields[4])
    if normalized is None:
        return expr
    return " ".join((*fields[:4], normalized))


def _cron(expr: str) -> CronTrigger:
    """Build a cron trigger bound to the scheduler timezone, with the expression
    read as standard Unix cron. Always go through this — a bare
    `CronTrigger.from_crontab(expr)` both silently picks its own timezone rather
    than inheriting the scheduler's, and misreads numeric weekdays."""
    return CronTrigger.from_crontab(normalize_crontab(expr), timezone=get_scheduler_timezone())


_scheduler = BackgroundScheduler(timezone=get_scheduler_timezone())


def _register_scheduler_job(auto) -> None:
    """Register (or replace) a cron job for the given automation."""
    try:
        _scheduler.add_job(
            func=_run_scheduled_automation,
            trigger=_cron(auto.schedule),
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
        trigger=_cron(cron_expr),
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
        trigger=_cron(cron_expr),
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
        trigger=_cron(cron_expr),
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
    """Register daily prune for memory_activities (default 04:00 local time)."""
    _scheduler.add_job(
        func=_prune_memory_activities_job,
        trigger=_cron(cron_expr),
        id="memory_activity_prune",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("memory_activity prune scheduled: %s", cron_expr)


def _prune_checkpoints_job() -> None:
    """Drop superseded LangGraph checkpoints — runs on the main loop.

    Every graph super-step rewrites the whole state, so checkpoints.db grows
    quadratically with run length and nothing else reclaims it. See
    core/checkpoint_retention.py for the retention rules and guards.
    """
    if state._main_loop is None:
        return
    from core.checkpoint_retention import prune_checkpoints  # noqa: PLC0415

    future = asyncio.run_coroutine_threadsafe(prune_checkpoints(), state._main_loop)
    try:
        stats = future.result(timeout=300)
        pruned = stats["root_pruned"] + stats["subgraph_pruned"]
        if pruned:
            logger.info(
                "checkpoint prune: removed %d checkpoints (%d root, %d subgraph), "
                "freed %.1f MB, skipped %d active thread(s)",
                pruned,
                stats["root_pruned"],
                stats["subgraph_pruned"],
                stats["bytes_freed"] / 1e6,
                stats["threads_skipped_active"],
            )
    except Exception:
        logger.exception("checkpoint prune failed")


def register_checkpoint_prune_job(cron_expr: str = "20 * * * *") -> None:
    """Register the checkpoint retention sweep. Hourly at :20 by default —
    off the hour so it doesn't stack with the staging cleanup."""
    _scheduler.add_job(
        func=_prune_checkpoints_job,
        trigger=_cron(cron_expr),
        id="checkpoint_prune",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("checkpoint prune scheduled: %s", cron_expr)
