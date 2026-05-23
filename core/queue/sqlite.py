"""SQLite-backed JobQueue.

Same DB as the app (`database.db`), so `enqueue(..., session=open_session)`
rides the caller's transaction — the queue row only becomes visible if and
when the caller commits, and the wake signal fires only after that commit.

Wake-up: in-process `asyncio.Event` set by `enqueue()` (or after_commit when
a session was passed in). Workers in the same process get sub-millisecond
pickup. Across processes the same SQLite file would still work but only via
the poll fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session
from db.models import Job as JobModel

from .protocol import Job, JobQueue

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqliteJobQueue(JobQueue):
    def __init__(self) -> None:
        self._wake_event = asyncio.Event()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _signal_wake(self) -> None:
        self._wake_event.set()

    async def _wait_for_signal(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._wake_event.clear()

    # ── Public API ─────────────────────────────────────────────────────────

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        run_at: datetime | None = None,
        max_attempts: int = 3,
        session: AsyncSession | None = None,
    ) -> str:
        job_id = str(uuid4())
        run_at = run_at or _now()
        row = JobModel(
            id=job_id,
            kind=kind,
            payload=json.dumps(payload),
            run_at=run_at,
            max_attempts=max_attempts,
        )

        if session is not None:
            session.add(row)
            # Fire the wake only after the caller's transaction commits. If they
            # roll back, the listener never fires and no wake is sent. `once=True`
            # makes SQLAlchemy auto-detach the listener after the first invocation.
            event.listen(
                session.sync_session,
                "after_commit",
                lambda _s: self._signal_wake(),
                once=True,
            )
            return job_id

        async with async_session() as sess:
            sess.add(row)
            await sess.commit()
        self._signal_wake()
        return job_id

    async def claim(
        self,
        kinds: list[str],
        *,
        worker_id: str,
        ttl_seconds: int = 300,
    ) -> Job | None:
        now = _now()
        async with async_session() as sess:
            stmt = (
                select(JobModel)
                .where(
                    JobModel.kind.in_(kinds),
                    JobModel.status == "pending",
                    JobModel.run_at <= now,
                )
                .order_by(JobModel.run_at.asc())
                .limit(1)
            )
            candidate = (await sess.execute(stmt)).scalar_one_or_none()
            if candidate is None:
                return None

            # Capture id/kind/payload/new-attempts BEFORE the UPDATE, since
            # `session.execute(update)` syncs the in-memory ORM object — the
            # candidate's attribute values shift to post-update after that call.
            job_id_str = candidate.id
            job_kind = candidate.kind
            job_payload_str = candidate.payload
            new_attempts = candidate.attempts + 1
            new_lock_until = now + timedelta(seconds=ttl_seconds)

            # Optimistic concurrency: only succeed if status is still 'pending'.
            # SQLite serializes writers, so the rowcount==0 path is rare but
            # not impossible across processes.
            upd = (
                update(JobModel)
                .where(
                    JobModel.id == job_id_str,
                    JobModel.status == "pending",
                )
                .values(
                    status="running",
                    locked_by=worker_id,
                    locked_until=new_lock_until,
                    attempts=new_attempts,
                )
            )
            result = await sess.execute(upd)
            await sess.commit()
            if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
                return None

            return Job(
                id=job_id_str,
                kind=job_kind,
                payload=json.loads(job_payload_str),
                attempts=new_attempts,
                locked_until=new_lock_until,
            )

    async def extend_lock(
        self, job_id: str, *, worker_id: str, ttl_seconds: int = 300,
    ) -> bool:
        new_lock_until = _now() + timedelta(seconds=ttl_seconds)
        async with async_session() as sess:
            stmt = (
                update(JobModel)
                .where(
                    JobModel.id == job_id,
                    JobModel.status == "running",
                    JobModel.locked_by == worker_id,
                )
                .values(locked_until=new_lock_until)
            )
            result = await sess.execute(stmt)
            await sess.commit()
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def complete(self, job_id: str, *, worker_id: str) -> bool:
        async with async_session() as sess:
            stmt = (
                update(JobModel)
                .where(
                    JobModel.id == job_id,
                    JobModel.status == "running",
                    JobModel.locked_by == worker_id,
                )
                .values(
                    status="done",
                    completed_at=_now(),
                    locked_by=None,
                    locked_until=None,
                )
            )
            result = await sess.execute(stmt)
            await sess.commit()
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def fail(
        self,
        job_id: str,
        error: str,
        *,
        worker_id: str,
        retry_at: datetime | None = None,
    ) -> bool:
        async with async_session() as sess:
            job = await sess.get(JobModel, job_id)
            if job is None:
                return False
            # Refuse to fail a job we no longer own — a reclaimed lock means
            # someone else is now responsible for its outcome.
            if job.status != "running" or job.locked_by != worker_id:
                return False

            if retry_at is not None and job.attempts < job.max_attempts:
                job.status = "pending"
                job.run_at = retry_at
                job.last_error = error
                job.locked_by = None
                job.locked_until = None
                wake_now = retry_at <= _now()
            else:
                job.status = "error"
                job.last_error = error
                job.completed_at = _now()
                job.locked_by = None
                job.locked_until = None
                wake_now = False

            await sess.commit()
        if wake_now:
            self._signal_wake()
        return True

    async def cancel(self, job_id: str) -> None:
        async with async_session() as sess:
            job = await sess.get(JobModel, job_id)
            if job is None:
                return
            if job.status == "pending":
                job.status = "cancelled"
                job.completed_at = _now()
            elif job.status == "running":
                job.cancel_requested = True
            # done/error/cancelled: no-op.
            await sess.commit()

    async def is_cancel_requested(self, job_id: str) -> bool:
        async with async_session() as sess:
            stmt = select(JobModel.cancel_requested).where(JobModel.id == job_id)
            value = (await sess.execute(stmt)).scalar_one_or_none()
            return bool(value)

    async def reap_expired_locks(self) -> int:
        """Flip rows whose lock has expired back to pending so another worker
        can claim them. Call periodically from a background sweeper task."""
        now = _now()
        async with async_session() as sess:
            stmt = (
                update(JobModel)
                .where(
                    JobModel.status == "running",
                    JobModel.locked_until.is_not(None),
                    JobModel.locked_until < now,
                )
                .values(
                    status="pending",
                    locked_by=None,
                    locked_until=None,
                )
            )
            result = await sess.execute(stmt)
            await sess.commit()
            count = result.rowcount or 0  # type: ignore[attr-defined]
        if count > 0:
            self._signal_wake()
        return count
