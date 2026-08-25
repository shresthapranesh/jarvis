"""Online retention sweep for checkpoints.db.

LangGraph re-serializes the *entire* graph state on every super-step, so a run
writes O(iterations x history) bytes and nothing ever removes the superseded
snapshots. Measured on a synthetic tool loop: a 48-iteration run over a 302 KB
conversation leaves 99 checkpoint rows totalling 16 MB (53x amplification), and
the amplification grows with run length.

Two distinct kinds of garbage accumulate, and they need different rules:

* **Root namespace** (``checkpoint_ns = ''``) — one row per super-step per
  thread. Only the newest matters: resume-after-restart, ``Command(resume=...)``
  and interrupt handling all read the latest checkpoint for the thread. We keep
  the newest ``keep_per_thread`` as headroom.
* **Subgraph namespaces** (``tools:<uuid>``, worker/tool subgraphs) — the
  namespace embeds a per-invocation uuid, so each is written once and never
  revisited. They are pure garbage once the parent run is over, and they are the
  bulk of the bytes (7.1 of 9.7 MB in the sample DB inspected while writing
  this) because each one snapshots the full parent state.

This runs against the live DB while the server is up, so it is deliberately
conservative — see ``prune_checkpoints`` for the two guards. ``main.py``'s
``maintenance prune-checkpoints`` remains the offline path: it is more
aggressive (keeps exactly one per thread) and VACUUMs, but needs the server
stopped.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Newest N root-namespace checkpoints kept per thread. Resume only ever reads
# the latest; the extra two are cheap insurance against a torn write.
KEEP_PER_THREAD = 3

# A checkpoint younger than this is never touched. This is the guard that keeps
# the sweep away from in-flight runs without having to model every runtime's
# thread-naming scheme.
MIN_AGE_SECONDS = 3600.0

# Number of 100-ns intervals between the UUID epoch (1582-10-15) and the Unix
# epoch. LangGraph mints checkpoint ids with uuid6 (see
# langgraph/checkpoint/base/id.py), so the id itself carries a sortable
# timestamp and we do not need to deserialize the blob to date it.
_UUID_EPOCH_100NS = 0x01B21DD213814000

# Rows deleted per transaction. Keeps the write lock short so a concurrent
# agent super-step waits milliseconds rather than seconds.
_BATCH = 200


def checkpoint_timestamp(checkpoint_id: str) -> float | None:
    """Unix seconds encoded in a uuid6 checkpoint id, or None if unparseable."""
    try:
        u = uuid.UUID(checkpoint_id)
    except (ValueError, AttributeError, TypeError):
        return None
    if u.version != 6:
        return None
    ticks = (u.time_low << 28) | (u.time_mid << 12) | (u.time_hi_version & 0x0FFF)
    return (ticks - _UUID_EPOCH_100NS) / 1e7


def active_thread_ids() -> set[str]:
    """Thread ids that may have a live run, from the in-memory task registry.

    `TaskState.parent_id` is not uniformly the thread id — chat stores the
    conversation id (which *is* the thread), while the board stores the bare
    task id for a `boardtask_{id}` thread and automations use
    `automation_{id}`. Rather than encode a per-kind mapping that would rot the
    moment a runtime changes, we emit every plausible spelling. Over-inclusion
    only means a thread survives one more sweep; under-inclusion could drop a
    live run's resume point, so the asymmetry decides the design.
    """
    try:
        from core.state import _tasks
    except Exception:  # pragma: no cover — import cycle / partial init
        return set()

    out: set[str] = set()
    for task_id, st in list(_tasks.items()):
        for base in (task_id, getattr(st, "parent_id", None)):
            if not base:
                continue
            out.add(base)
            out.add(f"boardtask_{base}")
            out.add(f"automation_{base}")
    return out


def _delete_rows(con: sqlite3.Connection, rows: list[tuple[str, str, str]]) -> tuple[int, int]:
    """Delete (thread_id, checkpoint_ns, checkpoint_id) triples and their writes.

    Returns ``(rows_deleted, payload_bytes_reclaimed)``.
    """
    deleted = 0
    freed = 0
    for i in range(0, len(rows), _BATCH):
        batch = rows[i : i + _BATCH]
        con.execute("BEGIN IMMEDIATE")
        try:
            # Size the payload before it goes, so the sweep can report what it
            # actually reclaimed. The file itself will not shrink — freed pages
            # land on SQLite's freelist for reuse — so a file-size delta would
            # read as 0 and hide the work.
            for tid, ns, cid in batch:
                row = con.execute(
                    "SELECT length(checkpoint) FROM checkpoints "
                    "WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                    (tid, ns, cid),
                ).fetchone()
                if row and row[0]:
                    freed += row[0]
            # `writes` first: an orphaned write row would be resurrected as a
            # pending task if a checkpoint id were ever reused.
            con.executemany(
                "DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                batch,
            )
            cur = con.executemany(
                "DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                batch,
            )
            deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(batch)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return deleted, freed


def _prune_sync(
    db_path: Path,
    *,
    keep_per_thread: int,
    min_age_seconds: float,
    active: set[str],
    dry_run: bool,
) -> dict:
    stats = {
        "root_pruned": 0,
        "subgraph_pruned": 0,
        "bytes_freed": 0,
        "threads_skipped_active": 0,
        "dry_run": dry_run,
    }
    if not db_path.exists():
        return stats

    cutoff = time.time() - min_age_seconds
    con = sqlite3.connect(db_path, timeout=30.0)
    con.isolation_level = None  # explicit transaction control
    try:
        con.execute("PRAGMA busy_timeout=30000")

        def _prunable(thread_id: str, ns: str, cid: str) -> bool:
            if thread_id in active:
                return False
            ts = checkpoint_timestamp(cid)
            # Unparseable id -> unknown age -> leave it alone.
            return ts is not None and ts < cutoff

        skipped_threads: set[str] = set()

        # ── Root namespace: keep the newest `keep_per_thread` per thread ──
        # checkpoint_ids are uuid6, so lexical DESC == newest first. This is the
        # same ordering LangGraph's own `list()` relies on.
        root_candidates: list[tuple[str, str, str]] = []
        seen: dict[str, int] = {}
        for thread_id, cid in con.execute(
            "SELECT thread_id, checkpoint_id FROM checkpoints "
            "WHERE checkpoint_ns = '' ORDER BY thread_id, checkpoint_id DESC"
        ):
            rank = seen.get(thread_id, 0)
            seen[thread_id] = rank + 1
            if rank < keep_per_thread:
                continue
            if thread_id in active:
                skipped_threads.add(thread_id)
                continue
            if _prunable(thread_id, "", cid):
                root_candidates.append((thread_id, "", cid))

        # ── Subgraph namespaces: one-shot, delete outright ────────────────
        subgraph_candidates: list[tuple[str, str, str]] = []
        for thread_id, ns, cid in con.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints "
            "WHERE checkpoint_ns <> ''"
        ):
            if thread_id in active:
                skipped_threads.add(thread_id)
                continue
            if _prunable(thread_id, ns, cid):
                subgraph_candidates.append((thread_id, ns, cid))

        stats["threads_skipped_active"] = len(skipped_threads)

        if dry_run:
            stats["root_pruned"] = len(root_candidates)
            stats["subgraph_pruned"] = len(subgraph_candidates)
            for tid, ns, cid in root_candidates + subgraph_candidates:
                row = con.execute(
                    "SELECT length(checkpoint) FROM checkpoints "
                    "WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                    (tid, ns, cid),
                ).fetchone()
                if row and row[0]:
                    stats["bytes_freed"] += row[0]
            return stats

        # Freed pages go on SQLite's freelist and are reused by subsequent
        # checkpoint writes, so the file plateaus instead of growing without
        # bound. Handing the pages back to the filesystem needs a VACUUM, which
        # takes an exclusive lock — that is the offline CLI's job, not ours.
        root_n, root_bytes = _delete_rows(con, root_candidates)
        sub_n, sub_bytes = _delete_rows(con, subgraph_candidates)
        stats["root_pruned"] = root_n
        stats["subgraph_pruned"] = sub_n
        stats["bytes_freed"] = root_bytes + sub_bytes
    finally:
        con.close()

    return stats


async def prune_checkpoints(
    db_path: Path | str | None = None,
    *,
    keep_per_thread: int = KEEP_PER_THREAD,
    min_age_seconds: float = MIN_AGE_SECONDS,
    dry_run: bool = False,
) -> dict:
    """Drop superseded checkpoints from the live DB. Returns a stats dict.

    Safe to run against a live server. Two guards keep it away from anything
    in use: a thread with an entry in the task registry is skipped entirely,
    and no checkpoint younger than ``min_age_seconds`` is ever removed. Both
    are deliberately over-cautious — a row that survives this sweep is picked
    up by the next one, whereas deleting a live resume point is not
    recoverable.

    Runs the sqlite work on a worker thread; the connection is private to that
    thread and separate from the AsyncSqliteSaver's.
    """
    from core.config import get_config

    path = Path(db_path) if db_path is not None else Path(get_config().checkpoints_db)
    active = active_thread_ids()
    return await asyncio.to_thread(
        _prune_sync,
        path,
        keep_per_thread=keep_per_thread,
        min_age_seconds=min_age_seconds,
        active=active,
        dry_run=dry_run,
    )


def _stats_sync(db_path: Path) -> dict:
    """Size/shape of the checkpoint DB. Read-only, no locks held beyond SELECTs."""
    out = {
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "threads": 0,
        "checkpoints": 0,
        "subgraph_checkpoints": 0,
        "active_threads": len(active_thread_ids()),
    }
    if not out["exists"]:
        return out
    con = sqlite3.connect(db_path, timeout=30.0)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        out["threads"] = con.execute(
            "SELECT count(DISTINCT thread_id) FROM checkpoints"
        ).fetchone()[0]
        out["checkpoints"] = con.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        out["subgraph_checkpoints"] = con.execute(
            "SELECT count(*) FROM checkpoints WHERE checkpoint_ns <> ''"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        # A DB that exists but has no schema yet (server never started) is a
        # legitimate state, not an error worth failing the whole query over.
        logger.debug("checkpoint_stats: %s", exc)
    finally:
        con.close()
    return out


async def checkpoint_stats(db_path: Path | str | None = None) -> dict:
    """What a prune would find, without deleting anything.

    Combines the DB's shape with a dry-run sweep, so a UI can show the same
    numbers the sweep would act on — including the two guards, which is the
    part a raw row count cannot convey ("14 of 900 prunable" is the *point*,
    not a disappointment).
    """
    from core.config import get_config

    path = Path(db_path) if db_path is not None else Path(get_config().checkpoints_db)
    stats = await asyncio.to_thread(_stats_sync, path)
    dry = await prune_checkpoints(path, dry_run=True)
    stats["prunable_root"] = dry["root_pruned"]
    stats["prunable_subgraph"] = dry["subgraph_pruned"]
    stats["reclaimable_bytes"] = dry["bytes_freed"]
    stats["threads_skipped_active"] = dry["threads_skipped_active"]
    return stats
