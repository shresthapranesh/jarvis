"""Unused facts archived (`edge/src/consolidate/stale.rs`) — the edge's own,
so checked against what the design says rather than a recording.

A fact neither retrieved nor confirmed over the user's last 90 active days
(days they wrote themselves — not an automation's or a board task's turn) is
archived: never a `core` or `manual` one, at most a tenth of the live facts a
pass, and only while retrieval is seen working. `restoreMemory` brings one back.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from edge_support import _run_edge, edge_binary  # noqa: F401 — a fixture
from seed import execute, insert, stamp

NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def _ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def _archive(edge_binary: Path, work_dir: Path) -> str:
    env = {**os.environ, "WORK_DIR": str(work_dir), "DATABASE_URL": f"sqlite+aiosqlite:///{work_dir}/database.db",
           "JARVIS_EDGE_LOG": "warn"}
    out = subprocess.run([str(edge_binary), "--maintenance-run", "stale_memories"], env=env, cwd=work_dir,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])["result"]


def _archived(db: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return {i for (i,) in conn.execute("SELECT id FROM memories WHERE archived_at IS NOT NULL")}


def _fact(db: Path, mid: str, created: int, **extra: object) -> None:
    insert(db, "memories", id=mid, text=f"fact {mid}", created_at=_ago(created), updated_at=_ago(created), **extra)


async def test_unused_facts_are_archived(database: Path, work_dir: Path, edge_binary: Path):
    # The user wrote on 100 days, 31 to 130 days ago — so their 90th most
    # recent active day is 120 days ago. An automation ran every day since;
    # counted, it would move that to 90 days ago.
    insert(database, "conversations", id="c-web", title="t", model="m")
    insert(database, "conversations", id="c-auto", title="a", model="m", surface="automation")
    for day in range(31, 131):
        insert(database, "messages", id=f"u{day}", conversation_id="c-web", role="user", content="hi", created_at=_ago(day))
    for day in range(1, 31):
        insert(database, "messages", id=f"a{day}", conversation_id="c-auto", role="user", content="run", created_at=_ago(day))

    for n in range(3):
        _fact(database, f"stale{n}", 200 + n)                                    # unused since long before
    _fact(database, "retrieved", 200, last_retrieved_at=stamp(_ago(10)))
    _fact(database, "confirmed", 200, last_confirmed_at=stamp(_ago(50)))
    _fact(database, "mid", 100)                                               # stale only by the automation's days
    _fact(database, "manual", 200, source="manual")
    _fact(database, "core", 200, kind="core")
    for n in range(30):
        _fact(database, f"new{n}", 5)
    # Retrieval logged on 25 of the 60 active days the log covers (31–90 ago).
    for day in range(31, 56):
        insert(database, "memory_activities", memory_id="retrieved", kind="fact", source="retrieval", accessed_at=_ago(day))

    cutoff = _ago(120).strftime("%Y-%m-%d")
    assert _archive(edge_binary, work_dir) == f"archived 3 of 37 facts, unused since {cutoff}"
    assert _archived(database) == {"stale0", "stale1", "stale2"}
    assert _archive(edge_binary, work_dir) == f"nothing unused since {cutoff}"

    # More stale than a pass may take: the oldest first, a tenth of the live
    # facts (at least 5).
    for n in range(7):
        _fact(database, f"older{n}", 300 + n)
    assert _archive(edge_binary, work_dir) == f"archived 5 of 41 facts, unused since {cutoff}"
    assert _archived(database) - {"stale0", "stale1", "stale2"} == {f"older{n}" for n in range(2, 7)}

    # Retrieval seen on too few of the recent active days: nothing archived.
    execute(database, "DELETE FROM memory_activities")
    assert _archive(edge_binary, work_dir) == "skipped: retrieval logged 0 of the last 60 active days"

    # Archived items are out of `memories` unless asked for, and restoring one
    # confirms it, so the next pass leaves it.
    async with _run_edge(edge_binary, work_dir, database) as client:
        async def gql(query: str) -> dict:
            resp = await client.post("/graphql", json={"query": query})
            body = resp.json()
            assert "errors" not in body, body
            return body["data"]

        live = {m["id"] for m in (await gql("{ memories { id } }"))["memories"]}
        archived = {m["id"] for m in (await gql("{ memories(archived: true) { id archivedAt } }"))["memories"]}
        assert archived == _archived(database) and not live & archived
        restored = (await gql('mutation { restoreMemory(id: "stale0") { id archivedAt lastConfirmedAt } }'))["restoreMemory"]
        assert restored["archivedAt"] is None and restored["lastConfirmedAt"]
        assert "stale0" in {m["id"] for m in (await gql("{ memories { id } }"))["memories"]}
    assert "stale0" not in _archived(database)


async def test_a_small_store_or_a_short_history_is_left_alone(database: Path, work_dir: Path, edge_binary: Path):
    for n in range(29):
        _fact(database, f"f{n}", 400)
    assert _archive(edge_binary, work_dir) == "skipped: 29 live facts, under 30"
    _fact(database, "f29", 400)
    assert _archive(edge_binary, work_dir) == "skipped: fewer than 90 active days"
    assert _archived(database) == set()
