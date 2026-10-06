"""The edge's scheduler (`edge/src/cron.rs`, `edge/src/schedule.rs`).

Schedules are a promise in two places — the UI shows `nextRunAt`, the
scheduler keeps it — so the cron engine is diffed against the one it
replaces: APScheduler's `CronTrigger`, built the way `core/scheduler.py:_cron`
builds it, over every expression shape the app accepts, in zones with every
kind of DST (none, 1 h, 30 min, southern-hemisphere, negative), at instants
around each 2026 transition.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import json
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from edge_support import edge_binary  # noqa: F401 — a fixture

EXPRESSIONS = [
    "* * * * *", "*/15 * * * *", "*/30 * * * *", "0 */6 * * *", "20 * * * *", "0 4 * * *",
    # the hours DST moves
    "30 2 * * *", "0 2 * * *", "15 1 * * *", "45 1 * * *", "0 3 * * *", "0,30 1,2,3 * * *",
    # weekdays, Unix-numbered
    "0 9 * * 1", "0 9 * * 1-5", "0 9 * * 0", "0 9 * * 7", "30 8 * * 1-5", "0 9 * * fri-mon",
    "0 9 * * */2", "0 9 * * 1/2", "0 9 * * 1/", "0 9 * * MON", "0 0 * * 6,0", "0 9 * * mon-fri/2",
    # day of month AND day of week, month ends, leap days, names
    "0 9 1 * 1", "0 9 1-7 * 1", "0 0 31 * *", "0 0 29 2 *", "0 0 last * *", "59 23 31 12 *",
    "0 12 * jan-mar mon", "0 0 1 */3 *", "1 2 3 4 5", "5-55/10 * * * *", "*/7 */5 * * *", "0-0/5 * * * *",
    # refused
    "* * * *", "60 * * * *", "*/0 * * * *", "0 0 0 * *", "0 0 * 13 *", "5-1 * * * *", "a b c d e",
    "0 9 * * 8", "0 25 * * *", "*/60 * * * *", "0 0 L * *", "0 0 lastx * *", "", "0 0 * dec-jan *",
]

ZONES = [
    "UTC", "America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London", "Europe/Dublin",
    "Europe/Berlin", "Australia/Sydney", "Australia/Lord_Howe", "Asia/Kolkata", "Pacific/Chatham",
    "America/Santiago", "Asia/Tehran", "Pacific/Apia",
]

CHAIN = 6


def _transitions(zone: str, year: int = 2026) -> list[datetime]:
    tz = ZoneInfo(zone)
    out, t = [], datetime(year, 1, 1, tzinfo=timezone.utc)
    prev = t.astimezone(tz).utcoffset()
    while t.year == year:
        t2 = t + timedelta(hours=1)
        off = t2.astimezone(tz).utcoffset()
        if off != prev:
            out.append(t2)
        prev, t = off, t2
    return out


def _instants(zone: str, rng: random.Random) -> list[datetime]:
    out = []
    for at in _transitions(zone):
        for delta in (-3 * 3600, -3600, -1800, -1, 0, 1, 1800, 3600, 86400 - 1800):
            out.append(at + timedelta(seconds=delta))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for _ in range(6):
        stamp = start + rng.random() * 365 * 86400
        out.append(datetime.fromtimestamp(round(stamp, 6), timezone.utc))
    return out


def _apscheduler(expr: str, zone: str, now: datetime) -> list[str] | None:
    from apscheduler.triggers.cron import CronTrigger

    from core.scheduler import normalize_crontab

    try:
        trigger = CronTrigger.from_crontab(normalize_crontab(expr), timezone=ZoneInfo(zone))
    except Exception:
        return None
    fires, prev, when = [], None, now.astimezone(ZoneInfo(zone))
    for _ in range(CHAIN):
        nxt = trigger.get_next_fire_time(prev, when)
        if nxt is None:
            break
        fires.append(nxt.isoformat())
        prev = when = nxt
    return fires


def test_next_fire_times_match_apscheduler(edge_binary: Path):
    rng = random.Random(20261002)
    cases = [
        {"expr": expr, "tz": zone, "now": now.isoformat(), "count": CHAIN}
        for zone in ZONES
        for now in _instants(zone, rng)
        for expr in EXPRESSIONS
    ]
    out = subprocess.run(
        [str(edge_binary), "--cron-next"], input="\n".join(json.dumps(c) for c in cases) + "\n",
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(out) == len(cases)
    mismatches = []
    for case, line in zip(cases, out):
        expected = _apscheduler(case["expr"], case["tz"], datetime.fromisoformat(case["now"]))
        if json.loads(line) != expected:
            mismatches.append((case, expected, json.loads(line)))
    assert not mismatches, f"{len(mismatches)} of {len(cases)} differ; first: {mismatches[:3]}"
    assert len(cases) > 10_000


# ── the board dispatcher ─────────────────────────────────────────────────────

import asyncio  # noqa: E402
import contextlib  # noqa: E402

from edge_support import _run_edge, fake_worker  # noqa: E402
from test_edge_parity import _dump, _mask  # noqa: E402

RUNNING = "{ runningTasks { id kind label parentId } }"


def _at(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


async def _seed_board() -> None:
    from db import async_session
    from db.models import BoardTask, BoardTaskLink, Job

    def task(id_: str, status: str, priority: int = 0, minute: int = 0, job_id: str | None = None) -> BoardTask:
        return BoardTask(id=id_, title=f"Task {id_}", status=status, priority=priority, job_id=job_id,
                         created_at=_at(2026, 9, 1, 12, minute), updated_at=_at(2026, 9, 1, 12, minute))

    async with async_session() as s:
        s.add_all([
            task("p1", "done"), task("p2", "done"), task("p3", "ready", minute=1),
            task("joins", "todo", minute=2),            # both parents done → promoted
            task("waits", "todo", minute=3),            # one parent not done → stays
            task("parked", "todo", minute=4),           # no parents → never promotes
            task("high-old", "ready", priority=5, minute=5),
            task("high-new", "ready", priority=5, minute=6),
            task("low", "ready", minute=0),
            task("busy", "ready", priority=9, job_id="j-live"),  # previous run still alive
        ])
        s.add_all([
            BoardTaskLink(id="l1", parent_id="p1", child_id="joins", created_at=_at(2026, 9, 1)),
            BoardTaskLink(id="l2", parent_id="p2", child_id="joins", created_at=_at(2026, 9, 1)),
            BoardTaskLink(id="l3", parent_id="p1", child_id="waits", created_at=_at(2026, 9, 1)),
            BoardTaskLink(id="l4", parent_id="p3", child_id="waits", created_at=_at(2026, 9, 1)),
            Job(id="j-live", kind="chat", payload="{}", status="running"),
            # One board run already in flight: two slots left of three.
            Job(id="j-board", kind="board_task", payload='{"task_id": "x"}', status="pending"),
        ])
        await s.commit()


@contextlib.asynccontextmanager
async def _board_twin(work_dir: Path, tmp_path_factory, edge_binary: Path):
    import sqlite3

    await _seed_board()
    b_dir = tmp_path_factory.mktemp("twin")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(b_dir / "database.db")) as dst:
        src.backup(dst)
    async with _run_edge(edge_binary, b_dir, b_dir / "database.db") as client, fake_worker(client) as ws:
        yield client, ws, b_dir


def _jobs(db: Path) -> int:
    import sqlite3

    with contextlib.closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


async def test_dispatch_matches_python(jarvis, work_dir: Path, tmp_path_factory, edge_binary: Path):
    """Promotion, the cap, priority order, and the live-previous-run guard —
    a pass through Python's dispatcher on one database and a requested pass
    through the edge's on a copy leave the same rows."""
    import sqlite3

    from server.task_board_runtime import dispatch_board_tasks

    async with _board_twin(work_dir, tmp_path_factory, edge_binary) as (client, ws, b_dir):
        since = datetime.now(timezone.utc).replace(microsecond=0)
        dirs = (str(work_dir), str(b_dir))

        async def both_pass() -> None:
            await dispatch_board_tasks()
            want = _jobs(work_dir / "database.db")
            await ws.send(json.dumps({"type": "dispatch"}))
            for _ in range(200):
                if _jobs(b_dir / "database.db") == want:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)  # let the pass's commit settle
            a, b = _dump(work_dir / "database.db"), _dump(b_dir / "database.db")
            for table in ("board_tasks", "jobs"):
                assert _mask(b[table], since, dirs) == _mask(a[table], since, dirs), table

        await both_pass()   # promotes "joins", starts the two highest priority
        await both_pass()   # at the cap: nothing more
        for db in (work_dir / "database.db", b_dir / "database.db"):
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE jobs SET status = 'done' WHERE kind = 'board_task'")
                conn.commit()
        await both_pass()   # room again: the rest, by priority then age

        # Each run the edge started is mirrored, pending, for its subscribers.
        running = (await client.post("/graphql", json={"query": RUNNING})).json()["data"]["runningTasks"]
        assert sorted(r["label"] for r in running) == sorted(
            f"Task {t}" for t in ("high-old", "high-new", "joins", "low", "p3")
        )


async def test_a_queued_board_run_stopped_matches_python(jarvis, work_dir: Path, tmp_path_factory, edge_binary: Path):
    """stopBoardTask on a run no worker has claimed: the job is cancelled
    and the card blocked as stopped — the end no handler is left to write.
    The edge's run ends for whoever watches it."""
    from edge_support import _gid
    from server.task_board_runtime import dispatch_board_tasks, stop_board_task
    from test_edge_runs import _until

    async with _board_twin(work_dir, tmp_path_factory, edge_binary) as (client, ws, b_dir):
        since = datetime.now(timezone.utc).replace(microsecond=0)
        await dispatch_board_tasks()
        await ws.send(json.dumps({"type": "dispatch"}))
        async def same_jobs() -> bool:
            return _jobs(b_dir / "database.db") == _jobs(work_dir / "database.db")

        await _until(same_jobs)
        await asyncio.sleep(0.1)

        assert await stop_board_task("high-old") is True
        q = "mutation($id: ID!) { stopBoardTask(id: $id) }"
        resp = await client.post("/graphql", json={"query": q, "variables": {"id": _gid("BoardTask", "high-old")}})
        assert resp.json() == {"data": {"stopBoardTask": True}}
        for task in ("parked", "nope"):
            resp = await client.post("/graphql", json={"query": q, "variables": {"id": _gid("BoardTask", task)}})
            assert resp.json()["errors"][0]["message"] == "task is not running"

        a, b = _dump(work_dir / "database.db"), _dump(b_dir / "database.db")
        dirs = (str(work_dir), str(b_dir))
        for table in ("board_tasks", "jobs"):
            assert _mask(b[table], since, dirs) == _mask(a[table], since, dirs), table
        [run] = [t for t in b["board_tasks"] if t["id"] == "high-old"]
        assert (run["status"], run["blocked_kind"]) == ("blocked", "stopped")
        # Finished in the mirror (it lingers a moment, as Python's does).
        q = "{ runningTasks { label cancelled done } }"
        running = (await client.post("/graphql", json={"query": q})).json()["data"]["runningTasks"]
        assert {"label": "Task high-old", "cancelled": True, "done": True} in running


async def test_python_behind_the_edge_asks_it_to_dispatch(jarvis, work_dir: Path, edge_binary: Path, monkeypatch):
    from core import edge_link, scheduler
    from core.edge_link import EdgeLink
    from db.models import Automation
    from server.task_board_runtime import dispatch_board_tasks
    from test_edge_runs import _edge_owns_runs, _until

    await _seed_board()
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        link = EdgeLink(f"ws://127.0.0.1:{client.base_url.port}/internal/worker")
        link.start()
        monkeypatch.setenv("JARVIS_EDGE_URL", f"http://127.0.0.1:{client.base_url.port}")
        monkeypatch.setattr(edge_link, "_link", link)
        try:
            await _until(lambda: _edge_owns_runs(client))
            # Python's own pass would claim cards itself; behind the edge it asks.
            assert await dispatch_board_tasks() == 0

            async def edge_dispatched() -> bool:
                running = (await client.post("/graphql", json={"query": RUNNING})).json()["data"]["runningTasks"]
                return len(running) == 2

            await _until(edge_dispatched)
            # And schedules are the edge's to keep: nothing registers here.
            scheduler._register_scheduler_job(Automation(id="a1", schedule="* * * * *", enabled=True))
            assert scheduler._scheduler.get_job("auto_a1") is None
        finally:
            await link.stop()


async def test_maintenance_jobs_run_the_python_sweeps(jarvis):
    from core.queue import Job
    from core.scheduler import MAINTENANCE_TASKS, maintenance_job_handler

    # The edge's timers.
    assert set(MAINTENANCE_TASKS) == {"memory_consolidation", "project_memory"}
    import pytest

    with pytest.raises(ValueError, match="unknown maintenance task"):
        await maintenance_job_handler(Job(id="j", kind="maintenance", payload={"task": "nope"},
                                          attempts=1, locked_until=datetime.now(timezone.utc)))
