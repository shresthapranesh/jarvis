"""Job-queue worker: consume loop + lock heartbeat.

Deliberately minimal. The worker:
- claims jobs from the queue via `queue.stream`,
- runs an async `handler(job)` for each — up to `max_concurrent` at a time
  as tasks on the event loop (handlers are I/O-bound: they spend their time
  awaiting LLM/network/DB, so concurrent runs interleave at await points),
- keeps each job's lock alive with a per-job `extend_lock` heartbeat,
- if a lock is lost (reaper reclaimed because heartbeat starved), cancels
  that handler — letting whichever worker now owns the job run it instead.

What the worker does NOT do: bridge `queue.is_cancel_requested` to any
runtime-specific flag (e.g. TaskState.cancelled). Each runtime has different
cancel semantics (`state.cancelled`, `state._stop_event`, per-node workflow
state); the handler is the right place to spawn that watcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from .protocol import Job, JobQueue

logger = logging.getLogger(__name__)


JobHandler = Callable[[Job], Coroutine[Any, Any, None]]


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        kinds: list[str],
        handler: JobHandler,
        *,
        worker_id: str,
        ttl_seconds: int = 300,
        heartbeat_interval: float | None = None,
        poll_interval: float = 5.0,
        max_concurrent: int = 1,
    ) -> None:
        self.queue = queue
        self.kinds = kinds
        self.handler = handler
        self.worker_id = worker_id
        self.ttl_seconds = ttl_seconds
        # Heartbeat at 1/3 of TTL so a single missed beat doesn't lose the lock.
        self.heartbeat_interval = heartbeat_interval or max(1.0, ttl_seconds / 3)
        self.poll_interval = poll_interval
        self.max_concurrent = max_concurrent

    async def run(self) -> None:
        """Consume jobs forever, running up to `max_concurrent` handlers
        concurrently. Cancelled by the caller (lifespan shutdown)."""
        sem = asyncio.Semaphore(self.max_concurrent)
        inflight: set[asyncio.Task] = set()
        stream = self.queue.stream(
            self.kinds,
            worker_id=self.worker_id,
            ttl_seconds=self.ttl_seconds,
            poll_interval=self.poll_interval,
        )
        try:
            while True:
                # Hold a slot BEFORE claiming (the claim happens inside
                # anext). A job's lock TTL starts ticking at claim time, so a
                # job must never sit claimed-but-unstarted waiting for
                # capacity — the reaper would hand it to another worker while
                # we still hold it.
                await sem.acquire()
                try:
                    job = await anext(stream)
                except BaseException:
                    sem.release()
                    raise
                task = asyncio.create_task(self._run_one(job, sem))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
        finally:
            # Worker is being shut down — cancel in-flight handlers and wait
            # for them so they can still persist state before teardown.
            pending = [t for t in inflight if not t.done()]
            for t in pending:
                t.cancel()
            for t in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    async def _run_one(self, job: Job, sem: asyncio.Semaphore) -> None:
        try:
            await self._process(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker %s: unexpected error processing job %s",
                             self.worker_id, job.id)
        finally:
            sem.release()

    async def _process(self, job: Job) -> None:
        handler_task = asyncio.create_task(self.handler(job))
        heartbeat_task = asyncio.create_task(self._heartbeat(job.id, handler_task))
        lock_lost = False
        try:
            await handler_task
        except asyncio.CancelledError:
            # Either heartbeat detected lock loss and cancelled us, or the
            # whole worker is shutting down. If heartbeat finished first, the
            # cancel was internal — don't propagate; just abandon this job.
            if heartbeat_task.done():
                lock_lost = True
            else:
                raise
        except Exception as exc:
            logger.warning("job %s failed: %s", job.id, exc)
            await self.queue.fail(job.id, str(exc), worker_id=self.worker_id)
            return
        finally:
            if not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

        if lock_lost:
            logger.warning(
                "worker %s abandoning job %s after lock loss", self.worker_id, job.id,
            )
            return

        await self.queue.complete(job.id, worker_id=self.worker_id)

    async def _heartbeat(self, job_id: str, handler_task: asyncio.Task) -> None:
        """Extend the lock every `heartbeat_interval`. Cancel the handler if
        the lock has been lost (another worker now owns the job)."""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            ok = await self.queue.extend_lock(
                job_id, worker_id=self.worker_id, ttl_seconds=self.ttl_seconds,
            )
            if not ok:
                logger.warning(
                    "worker %s lost lock on job %s — cancelling handler",
                    self.worker_id, job_id,
                )
                handler_task.cancel()
                return
