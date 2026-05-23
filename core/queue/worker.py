"""Job-queue worker: consume loop + lock heartbeat.

Deliberately minimal. The worker:
- claims jobs from the queue via `queue.stream`,
- runs an async `handler(job)` for each,
- keeps the lock alive with periodic `extend_lock`,
- if the lock is lost (reaper reclaimed because heartbeat starved), cancels
  the handler — letting whichever worker now owns the job run it instead.

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
    ) -> None:
        self.queue = queue
        self.kinds = kinds
        self.handler = handler
        self.worker_id = worker_id
        self.ttl_seconds = ttl_seconds
        # Heartbeat at 1/3 of TTL so a single missed beat doesn't lose the lock.
        self.heartbeat_interval = heartbeat_interval or max(1.0, ttl_seconds / 3)
        self.poll_interval = poll_interval

    async def run(self) -> None:
        """Consume jobs forever. Cancelled by the caller (lifespan shutdown)."""
        async for job in self.queue.stream(
            self.kinds,
            worker_id=self.worker_id,
            ttl_seconds=self.ttl_seconds,
            poll_interval=self.poll_interval,
        ):
            try:
                await self._process(job)
            except asyncio.CancelledError:
                # Bubble up — worker is being shut down.
                raise
            except Exception:
                logger.exception("worker %s: unexpected error processing job %s",
                                 self.worker_id, job.id)

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
