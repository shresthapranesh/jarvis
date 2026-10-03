"""SQLite-backed JobQueue."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import event, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session
from db.models import Job as JobModel

from .protocol import Job, JobQueue

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _thread_free():
    """No other job is running on this job's thread — its lease is free.

    The partial unique index `ux_jobs_thread_lease` enforces the same rule for
    every writer; this keeps a claim from tripping it, and lets the claim pass
    over a held thread to the next due job."""
    holder = aliased(JobModel)
    return JobModel.thread_id.is_(None) | ~exists().where(
        holder.thread_id == JobModel.thread_id,
        holder.status == "running",
        holder.id != JobModel.id,
    )


class SqliteJobQueue(JobQueue):
    def __init__(self) -> None:
        self._wake_event = asyncio.Event()
        # `drain()`: claims refused, and how many are mid-flight.
        self._draining = False
        self._claims = 0
        self._no_claims = asyncio.Event()
        self._no_claims.set()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _signal_wake(self) -> None:
        self._wake_event.set()

    def wake(self) -> None:
        self._signal_wake()

    async def drain(self) -> None:
        self._draining = True
        await self._no_claims.wait()

    def undrain(self) -> None:
        self._draining = False
        self._signal_wake()

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
        job_id: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = 3,
        session: AsyncSession | None = None,
        thread_id: str | None = None,
    ) -> str:
        jid = job_id or str(uuid4())
        run_at = run_at or _now()
        row = JobModel(
            id=jid,
            kind=kind,
            payload=json.dumps(payload),
            run_at=run_at,
            max_attempts=max_attempts,
            thread_id=thread_id,
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
            return jid

        async with async_session() as sess:
            sess.add(row)
            await sess.commit()
        self._signal_wake()
        return jid

    async def claim(
        self,
        kinds: list[str],
        *,
        worker_id: str,
        ttl_seconds: int = 300,
    ) -> Job | None:
        # No await between the check and the count, so a drain either sees
        # this claim in flight or this claim sees the drain.
        if self._draining:
            return None
        self._claims += 1
        self._no_claims.clear()
        try:
            return await self._claim(kinds, worker_id=worker_id, ttl_seconds=ttl_seconds)
        finally:
            self._claims -= 1
            if not self._claims:
                self._no_claims.set()

    async def _claim(self, kinds: list[str], *, worker_id: str, ttl_seconds: int) -> Job | None:
        now = _now()
        async with async_session() as sess:
            stmt = (
                select(JobModel)
                .where(
                    JobModel.kind.in_(kinds),
                    JobModel.status == "pending",
                    JobModel.run_at <= now,
                    _thread_free(),
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
            job_created_at = candidate.created_at
            job_cancel_requested = bool(candidate.cancel_requested)
            job_thread_id = candidate.thread_id
            new_attempts = candidate.attempts + 1
            new_lock_until = now + timedelta(seconds=ttl_seconds)

            # Optimistic concurrency: only succeed if status is still 'pending'
            # and the thread is still free. SQLite serializes writers, so the
            # rowcount==0 path is rare but not impossible across processes.
            upd = (
                update(JobModel)
                .where(
                    JobModel.id == job_id_str,
                    JobModel.status == "pending",
                    _thread_free(),
                )
                .values(
                    status="running",
                    locked_by=worker_id,
                    locked_until=new_lock_until,
                    attempts=new_attempts,
                )
            )
            try:
                result = await sess.execute(upd)
                await sess.commit()
            except IntegrityError:
                # Another process took the thread's lease between our check
                # and our write; the unique index refused the second holder.
                await sess.rollback()
                return None
            if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
                return None

            return Job(
                id=job_id_str,
                kind=job_kind,
                payload=json.loads(job_payload_str),
                attempts=new_attempts,
                locked_until=new_lock_until,
                # SQLite hands back naive datetimes; every stamp here is UTC.
                created_at=(
                    job_created_at.replace(tzinfo=timezone.utc)
                    if job_created_at is not None and job_created_at.tzinfo is None
                    else job_created_at
                ),
                cancel_requested=job_cancel_requested,
                thread_id=job_thread_id,
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
            done = (result.rowcount or 0) > 0  # type: ignore[attr-defined]
        if done:
            # The job's thread lease is free: a turn queued behind it on the
            # same thread is claimable now, not at the next poll.
            self._signal_wake()
        return done

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

            # Either way the job stops running, which frees its thread lease
            # for the next job waiting on it — so wake.
            if retry_at is not None and job.attempts < job.max_attempts:
                job.status = "pending"
                job.run_at = retry_at
                job.last_error = error
                job.locked_by = None
                job.locked_until = None
            else:
                job.status = "error"
                job.last_error = error
                job.completed_at = _now()
                job.locked_by = None
                job.locked_until = None

            await sess.commit()
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
