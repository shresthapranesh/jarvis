"""The per-thread lease on jobs (`Job.thread_id`, `core/queue/sqlite.py`).

Jobs that share a transcript thread run one at a time, whichever process
claims them: a claim passes over a job whose thread has a job running, and
the database refuses a second running job on a thread outright.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.queue.sqlite import SqliteJobQueue


@pytest.fixture
async def queue(database) -> SqliteJobQueue:
    return SqliteJobQueue()


async def test_a_second_job_on_a_thread_waits_for_the_first(queue: SqliteJobQueue):
    first = await queue.enqueue("chat", {}, thread_id="conv-a")
    second = await queue.enqueue("chat", {}, thread_id="conv-a")

    claimed = await queue.claim(["chat"], worker_id="w1")
    assert claimed is not None and claimed.id == first
    assert claimed.thread_id == "conv-a"
    assert await queue.claim(["chat"], worker_id="w2") is None

    assert await queue.complete(first, worker_id="w1")
    claimed = await queue.claim(["chat"], worker_id="w2")
    assert claimed is not None and claimed.id == second


async def test_a_held_thread_does_not_block_other_jobs(queue: SqliteJobQueue):
    held = await queue.enqueue("chat", {}, thread_id="conv-a")
    await queue.enqueue("chat", {}, thread_id="conv-a")
    other = await queue.enqueue("chat", {}, thread_id="conv-b")
    loose = await queue.enqueue("chat", {})

    assert (await queue.claim(["chat"], worker_id="w")).id == held  # type: ignore[union-attr]
    # Passed over, not stuck behind: the waiting conv-a job is older.
    assert (await queue.claim(["chat"], worker_id="w")).id == other  # type: ignore[union-attr]
    assert (await queue.claim(["chat"], worker_id="w")).id == loose  # type: ignore[union-attr]
    assert await queue.claim(["chat"], worker_id="w") is None


@pytest.mark.parametrize("release", ["fail", "reap"])
async def test_every_way_a_job_stops_running_frees_the_thread(
    queue: SqliteJobQueue, work_dir: Path, release: str,
):
    first = await queue.enqueue("chat", {}, thread_id="conv-a")
    second = await queue.enqueue("chat", {}, thread_id="conv-a")
    assert (await queue.claim(["chat"], worker_id="w1")).id == first  # type: ignore[union-attr]

    if release == "fail":
        assert await queue.fail(first, "boom", worker_id="w1")
        expect = second
    else:
        with sqlite3.connect(work_dir / "database.db") as conn:
            conn.execute("UPDATE jobs SET locked_until = '2000-01-01 00:00:00' WHERE id = ?", (first,))
        assert await queue.reap_expired_locks() == 1
        # Back to pending and older, so the reaped job runs first again.
        expect = first
    assert (await queue.claim(["chat"], worker_id="w2")).id == expect  # type: ignore[union-attr]


async def test_finishing_a_job_wakes_the_one_waiting_on_its_thread(queue: SqliteJobQueue):
    first = await queue.enqueue("chat", {}, thread_id="conv-a")
    second = await queue.enqueue("chat", {}, thread_id="conv-a")
    stream = queue.stream(["chat"], worker_id="w", poll_interval=60)
    assert (await anext(stream)).id == first

    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await queue.complete(first, worker_id="w")
    # Woken by the finish — the poll is a minute away.
    assert (await asyncio.wait_for(waiting, 2)).id == second


async def test_the_database_refuses_a_second_running_job_on_a_thread(
    queue: SqliteJobQueue, work_dir: Path,
):
    """What holds against a writer that skips the claim's check — the edge's
    own claim, once it runs turns, or a race between processes."""
    first = await queue.enqueue("chat", {}, thread_id="conv-a")
    second = await queue.enqueue("chat", {}, thread_id="conv-a")
    await queue.enqueue("chat", {})
    await queue.enqueue("chat", {})
    assert (await queue.claim(["chat"], worker_id="w1")).id == first  # type: ignore[union-attr]

    with sqlite3.connect(work_dir / "database.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (second,))
        # Jobs without a thread hold no lease.
        conn.execute("UPDATE jobs SET status = 'running' WHERE thread_id IS NULL")
