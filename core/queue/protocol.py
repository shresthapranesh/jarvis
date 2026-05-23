"""JobQueue abstract base + Job dataclass.

A backend implementation provides the five abstract storage operations
(`enqueue`, `claim`, `extend_lock`, `complete`, `fail`, `cancel`,
`is_cancel_requested`, `reap_expired_locks`). The base class composes them
into a `stream()` async-iterator that workers consume.

Backends with real wake-up primitives (Redis pub/sub, BLPOP) override
`_wait_for_signal`; the default just sleeps for the poll interval.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class Job:
    """The handle returned by `claim()` and yielded by `stream()`. Holds only
    what the worker needs to do its work and report back — durable state lives
    in the backend, not here."""
    id: str
    kind: str
    payload: dict
    attempts: int
    locked_until: datetime


class JobQueue(abc.ABC):
    """Abstract scheduling layer. See core/queue/__init__.py for framing."""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        job_id: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = 3,
        session: AsyncSession | None = None,
    ) -> str:
        """Insert a new job and return its id.

        If `job_id` is provided, it is used verbatim — useful when the caller
        wants the job id to equal a domain-specific UUID (e.g. AutomationRun.id)
        so cancellation and status lookups don't need a join. Must be unique;
        passing an id that already exists raises an integrity error.

        If `session` is provided, the row is added to that session and the
        wake signal fires only after the caller commits. If the caller rolls
        back, no wake fires (and no job exists). If `session` is None, the
        queue opens its own session, commits, and fires the wake immediately.
        """

    @abc.abstractmethod
    async def claim(
        self,
        kinds: list[str],
        *,
        worker_id: str,
        ttl_seconds: int = 300,
    ) -> Job | None:
        """Atomically transition one due pending job to running and return it.
        Returns None if no job is currently due.

        The lock holds for `ttl_seconds`. If the worker hasn't `extend_lock`'d
        or finished by then, the reaper will reclaim it for another worker.
        Default `ttl_seconds=300` paired with the reaper's ~60s sweep means a
        crashed worker is detected within ~6 minutes — callers running LLM
        loops that can exceed that should either bump TTL at claim time or
        call `extend_lock` periodically."""

    @abc.abstractmethod
    async def extend_lock(
        self, job_id: str, *, worker_id: str, ttl_seconds: int = 300,
    ) -> bool:
        """Push `locked_until` forward. Returns False if this worker is no
        longer the lock holder (the lock was reaped and someone else claimed
        the job, or the job has reached a terminal state). A False return
        means the worker MUST abort its work — continuing would race with
        whichever worker now owns the job."""

    @abc.abstractmethod
    async def complete(self, job_id: str, *, worker_id: str) -> bool:
        """Mark the job done. Returns False if this worker no longer holds
        the lock — in which case the job stays as-is (the new lock holder is
        running it). Caller must treat False as "I lost the race; do not
        emit side effects as if this job succeeded.\""""

    @abc.abstractmethod
    async def fail(
        self,
        job_id: str,
        error: str,
        *,
        worker_id: str,
        retry_at: datetime | None = None,
    ) -> bool:
        """Mark the job errored. Returns False if this worker no longer holds
        the lock — see `complete`. If `retry_at` is given and attempts remain,
        the job is requeued; otherwise it transitions to terminal `error`."""

    @abc.abstractmethod
    async def cancel(self, job_id: str) -> None:
        """If pending → cancelled (will not be claimed). If running → set
        cancel_requested=True for the worker to observe and exit cleanly."""

    @abc.abstractmethod
    async def is_cancel_requested(self, job_id: str) -> bool:
        """Workers poll this between steps to support /stop-style cancellation."""

    @abc.abstractmethod
    async def reap_expired_locks(self) -> int:
        """Flip rows whose `locked_until` has passed back to `pending`. Returns
        the number of rows reaped. Intended to be called periodically by a
        background sweeper, not from the hot path."""

    # ── Stream loop (concrete; backends override _wait_for_signal) ────────

    async def _wait_for_signal(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for a new-job signal. Default impl is
        a plain sleep; backends with pub/sub override."""
        await asyncio.sleep(timeout)

    async def stream(
        self,
        kinds: list[str],
        *,
        worker_id: str,
        ttl_seconds: int = 300,
        poll_interval: float = 5.0,
    ) -> AsyncIterator[Job]:
        """Yield jobs as they become available. Loops forever; cancel by
        cancelling the consuming task."""
        while True:
            job = await self.claim(kinds, worker_id=worker_id, ttl_seconds=ttl_seconds)
            if job is not None:
                yield job
            else:
                await self._wait_for_signal(poll_interval)
