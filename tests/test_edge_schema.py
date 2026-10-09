"""The edge creates and migrates the schema (`edge/src/schema.rs`).

`edge/src/schema.sql` is the schema a new database is made with: `schema.rs`
creates whichever of its tables is missing, then runs `migrate` — what an
older database lacks — and the one-time LangGraph store import.

Two guarantees:

- Fresh and old databases end as Python's `Database.init` left them — its
  `sqlite_master` and rows, recorded when the schema moved here (Python's
  `db/models.py` + `_migrate` were the source until then).
- An old database, migrated, has the shape of a fresh one: every table's
  columns and every index.

A schema change goes in `schema.sql` (new databases) and a step in
`schema.rs:migrate` (existing ones). The second guarantee checks the two
agree; the first one's recording is updated by hand, in the same commit.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from python_golden import recorded
from tests.edge_support import EDGE_DIR, edge_binary  # noqa: F401 — fixture

SCHEMA_SQL = EDGE_DIR / "src" / "schema.sql"


def _edge_init(edge_binary: Path, db: Path, checkpoints: Path) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}", "WORK_DIR": str(db.parent),
           "CHECKPOINTS_DB": str(checkpoints), "JARVIS_EDGE_LOG": "warn"}
    out = subprocess.run([str(edge_binary), "--init-db"], env=env, cwd=db.parent.parent,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


_STAMP = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(\.\d+)?(\+00:00)?")


def _snapshot(db: Path) -> dict:
    """`sqlite_master`, and every table's rows, with the stamps a run writes
    at the instant it runs masked."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        # By name: `create_all` made a table's indexes in no fixed order.
        master = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables = [n for t, n, _, sql in master if t == "table" and "VIRTUAL" not in (sql or "")
                  and not n.endswith(("_data", "_idx", "_docsize", "_config", "_content"))]
        rows = {}
        for table in tables:
            got = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            rows[table] = [tuple(_STAMP.sub("<now>", v) if isinstance(v, str) and table in ("kv_store", "config_settings")
                                 and ("migrations" in json.dumps(r) or "migration." in json.dumps(r)) else v
                                 for v in r) for r in got]
        fts = {}
        for name in ("memories_fts", "conversation_episodes_fts", "messages_fts"):
            with contextlib.suppress(sqlite3.OperationalError):
                fts[name] = conn.execute(f"SELECT rowid, * FROM {name} ORDER BY rowid").fetchall()
    return {"master": master, "rows": rows, "fts": fts}


def _shape(db: Path) -> dict:
    """Each table's columns (by name: a migration appends them) and each
    index — what a fresh and a migrated database must agree on. Not a
    column's NOT NULL or default: SQLite adds a column to an existing table
    only nullable or with a default, so a migrated one has both."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        tables = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        columns = {t: sorted(conn.execute(f"SELECT name, type, pk FROM pragma_table_info('{t}')")) for t in tables}
        indexes = conn.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
                               "ORDER BY name").fetchall()
    return {"columns": columns, "indexes": indexes}


async def _twins(edge_binary: Path, tmp: Path, start: Path | None, checkpoints: Path | None = None):
    """What Python's init made of the starting database (or none), recorded,
    and what the edge's makes of it."""
    db = tmp / "edge" / "data" / "database.db"  # a directory that isn't there yet
    db.parent.parent.mkdir()
    if start is not None:
        db.parent.mkdir()
        shutil.copy(start, db)
    _edge_init(edge_binary, db, checkpoints or tmp / "edge" / "checkpoints.db")
    return await recorded(), _snapshot(db)


async def test_a_fresh_database(edge_binary, tmp_path):
    python, edge = await _twins(edge_binary, tmp_path, None)
    assert edge["master"] == python["master"]
    assert edge["rows"] == python["rows"]
    assert python["rows"]["kv_store"]  # the store import's marker, copied: 0


async def test_starting_again_changes_nothing(edge_binary, tmp_path):
    db, cp = tmp_path / "database.db", tmp_path / "checkpoints.db"
    _edge_init(edge_binary, db, cp)
    first = _snapshot(db)
    _edge_init(edge_binary, db, cp)
    assert _snapshot(db) == first
    assert await recorded() == first  # and Python found nothing to do either


# ── an older database ────────────────────────────────────────────────────────

# What `_migrate` adds, by table — dropped again to make an old database.
_ADDED = {
    "messages": ["status", "input_tokens", "output_tokens", "ttft_ms", "llm_ms", "prefill_tps", "eval_tps",
                 "duration_ms"],
    "steps": ["subagent"],
    "jobs": ["thread_id", "runtime"],
    "conversations": ["pinned", "surface", "project_id", "ephemeral"],
    "automations": ["notifications", "stateful"],
    "workflows": ["notifications"],
    "board_tasks": ["blocked_kind", "pending_answer"],
    "artifacts": ["mime_type"],
}


def _old_database(path: Path) -> None:
    """The current schema with `_migrate`'s columns, indexes, FTS mirrors and
    backfill marker taken away, and rows each backfill acts on."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_SQL.read_text())
        for table, cols in _ADDED.items():
            (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
            indexes = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                                   "AND sql IS NOT NULL", (table,)).fetchall()
            for col in cols:
                sql, n = re.subn(rf"\n\t{col} [^\n]*?,? \n", "\n", sql)
                assert n == 1, (table, col)
                sql = re.sub(rf",? \n\tFOREIGN KEY\({col}\) REFERENCES [^\n,]*", "", sql)
            sql = re.sub(r",( \n\))$", r"\1", sql).replace(", \n)", "\n)")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(sql)
            for _name, index in indexes:
                if not any(re.search(rf"\b{c}\b", index.split(" ON ", 1)[1]) for c in cols):
                    conn.execute(index)
        for index in ("ix_messages_conv_created", "ix_jobs_kind_status_run_at", "ix_approvals_status_requested",
                      "ix_approvals_task_status", "ix_approvals_board_task_status"):
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.executescript("""
            INSERT INTO conversations (id, title, model, created_at) VALUES
              ('telegram_1', 't', 'm', '2025-01-01 00:00:00.000000'),
              ('discord_2', 'd', 'm', '2025-01-01 00:00:00.000000'),
              ('telegramx', 'w', 'm', '2025-01-01 00:00:00.000000');
            INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES
              ('m1', 'telegram_1', 'assistant', 'first', '2025-01-01 00:00:01.000000'),
              ('m2', 'telegram_1', 'assistant', 'second', '2025-01-01 00:00:03.000000'),
              ('m3', 'telegram_1', 'user', 'a user turn', '2025-01-01 00:00:04.000000');
            INSERT INTO artifacts (id, title, filename, kind, conversation_id, created_at, updated_at) VALUES
              ('a1', 'x', 'a1.md', 'markdown', 'telegram_1', '2025-01-01 00:00:02.000000', '2025-01-01 00:00:02.000000'),
              ('a2', 'y', 'a2.md', 'markdown', 'telegram_1', '2025-01-01 00:00:05.000000', '2025-01-01 00:00:05.000000'),
              ('a3', 'z', 'a3.md', 'markdown', 'telegram_1', '2025-01-01 00:00:00.500000', '2025-01-01 00:00:00.500000');
        """)
        conn.commit()


async def test_an_old_database_is_migrated_the_same(edge_binary, tmp_path):
    start = tmp_path / "old.db"
    _old_database(start)
    python, edge = await _twins(edge_binary, tmp_path, start)
    assert edge["master"] == python["master"]
    assert edge["rows"] == python["rows"]
    assert edge["fts"] == python["fts"]
    # Each backfill did its work.
    with contextlib.closing(sqlite3.connect(tmp_path / "edge" / "data" / "database.db")) as conn:
        assert conn.execute("SELECT id, surface FROM conversations ORDER BY rowid").fetchall() == [
            ("telegram_1", "telegram"), ("discord_2", "discord"), ("telegramx", "web")]
        assert conn.execute("SELECT id, message_id FROM artifacts ORDER BY id").fetchall() == [
            ("a1", "m1"), ("a2", "m2"), ("a3", None)]
        assert {c for (c,) in conn.execute("SELECT name FROM pragma_table_info('messages')")} >= set(_ADDED["messages"])
    assert edge["fts"]["messages_fts"] == [(1, "first"), (2, "second"), (3, "a user turn")]


def test_a_migrated_database_has_the_fresh_shape(edge_binary, tmp_path):
    """`migrate` brings an old database to what `schema.sql` makes new: the
    same columns, indexes and FTS triggers."""
    fresh, old = tmp_path / "fresh" / "database.db", tmp_path / "old" / "database.db"
    for db in (fresh, old):
        db.parent.mkdir()
    _old_database(old)
    for db in (fresh, old):
        _edge_init(edge_binary, db, db.parent / "checkpoints.db")
    assert _shape(old) == _shape(fresh)


# ── the LangGraph store ──────────────────────────────────────────────────────


async def test_the_langgraph_store_is_copied_once(edge_binary, tmp_path):
    cp = tmp_path / "checkpoints.db"
    with contextlib.closing(sqlite3.connect(cp)) as conn:
        conn.execute(
            "CREATE TABLE store (prefix text NOT NULL, key text NOT NULL, value text NOT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "expires_at TIMESTAMP, ttl_minutes REAL, PRIMARY KEY (prefix, key))"
        )
        conn.executemany("INSERT INTO store (prefix, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", [
            ("memory", "AGENTS.md", '{"content": "café"}', "2026-05-03 07:00:24", "2026-05-03 07:00:25"),
            ("memory_consolidation", "state", '{"last_run_at": "x"}', "2026-05-03T07:00:24.5+02:00", "2026-05-03"),
            ("app", "kept", '{"from": "langgraph"}', "2026-05-03 07:00:24", "2026-05-03 07:00:24"),
            ("app", "odd", '{"n": 1}', "not a time", None),
        ])
        conn.commit()
    start = tmp_path / "start.db"
    _old_database(start)
    with contextlib.closing(sqlite3.connect(start)) as conn:
        conn.execute("""INSERT INTO kv_store VALUES ('app', 'kept', '{"from": "kv"}', """
                     "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')")
        conn.commit()
    python, edge = await _twins(edge_binary, tmp_path, start, cp)

    def masked(rows):  # "not a time" and NULL read as now, on each side
        return [tuple("<now>" if r[:2] == ("app", "odd") and i >= 3 else v for i, v in enumerate(r)) for r in rows]

    assert masked(edge["rows"]["kv_store"]) == masked(python["rows"]["kv_store"])
    assert [r[:3] for r in python["rows"]["kv_store"]] == [
        ("app", "kept", '{"from": "kv"}'),
        ("memory", "AGENTS.md", '{"content": "café"}'),
        ("memory_consolidation", "state", '{"last_run_at": "x"}'),
        ("app", "odd", '{"n": 1}'),
        ("jarvis.migrations", "langgraph_store", '{"copied": 3, "at": "<now>"}'),
    ]
    assert python["rows"]["kv_store"][1][3:] == ("2026-05-03 07:00:24.000000", "2026-05-03 07:00:25.000000")
    assert python["rows"]["kv_store"][2][3:] == ("2026-05-03 07:00:24.500000", "2026-05-03 00:00:00.000000")


async def test_store_values_are_kept_as_text(edge_binary, tmp_path):
    """LangGraph's store holds each value as a BLOB; `kv_store` holds text —
    whether the import copies it now or copied it as a BLOB before."""
    cp = tmp_path / "checkpoints.db"
    with contextlib.closing(sqlite3.connect(cp)) as conn:
        conn.execute("CREATE TABLE store (prefix text NOT NULL, key text NOT NULL, value text NOT NULL, "
                     "created_at TIMESTAMP, updated_at TIMESTAMP, PRIMARY KEY (prefix, key))")
        conn.execute("INSERT INTO store VALUES ('memory', 'AGENTS.md', ?, '2026-05-03 07:00:24', '2026-05-03 07:00:24')",
                     ('{"content": "café"}'.encode(),))
        conn.commit()
    db = tmp_path / "data" / "database.db"
    db.parent.mkdir()
    _old_database(db)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO kv_store VALUES ('memory', '/AGENTS.md', ?, "
                     "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')", (b'{"content": "old"}',))
        conn.commit()
    _edge_init(edge_binary, db, cp)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        got = conn.execute("SELECT key, typeof(value), value FROM kv_store WHERE namespace = 'memory' ORDER BY key").fetchall()
    assert got == [("/AGENTS.md", "text", '{"content": "old"}'), ("AGENTS.md", "text", '{"content": "café"}')]
