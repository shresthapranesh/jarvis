"""The edge creates and migrates the schema (`edge/src/schema.rs`).

`edge/src/schema.sql` is what `Base.metadata.create_all` makes, captured
from a fresh database in creation order; `schema.rs` creates whichever of
its tables is missing, then runs its port of `db/engine.py:_migrate` and
the one-time LangGraph store import. Both runtimes, from the same starting
database, must end with the same `sqlite_master` and the same rows.

After a change to `db/models.py`, re-capture the file with
`JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_edge_schema.py`.
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

import pytest

from python_golden import recorded
from tests.edge_support import EDGE_DIR, edge_binary  # noqa: F401 — fixture

SCHEMA_SQL = EDGE_DIR / "src" / "schema.sql"

HEADER = """\
-- What `Base.metadata.create_all` (db/models.py) makes, captured from a fresh
-- database in creation order: each table, then its indexes by name. `schema.rs`
-- creates every table missing here (and its indexes), then migrates.
-- Generated: JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_edge_schema.py
"""


def _create_all_ddl(tmp: Path) -> str:
    from sqlalchemy import create_engine

    from db.models import Base

    path = tmp / "create_all.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY rowid").fetchall()
        statements = []
        for name, sql in tables:
            # A table's indexes come out of a set, in no fixed order: by name.
            indexes = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                                   "AND sql IS NOT NULL ORDER BY name", (name,)).fetchall()
            statements += [sql, *(i for (i,) in indexes)]
    return HEADER + "\n" + "".join(f"{sql};\n\n" for sql in statements)


def test_schema_sql_is_what_create_all_makes(tmp_path):
    want = _create_all_ddl(tmp_path)
    if os.environ.get("JARVIS_UPDATE_GOLDEN"):
        SCHEMA_SQL.write_text(want)
    assert SCHEMA_SQL.read_text() == want, "db/models.py changed: re-capture edge/src/schema.sql (see the docstring)"


# ── both runtimes over the same database ─────────────────────────────────────


def _edge_init(edge_binary: Path, db: Path, checkpoints: Path) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}", "WORK_DIR": str(db.parent),
           "CHECKPOINTS_DB": str(checkpoints), "JARVIS_EDGE_LOG": "warn"}
    out = subprocess.run([str(edge_binary), "--init-db"], env=env, cwd=db.parent.parent,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


async def _python_init(db: Path, checkpoints: Path) -> None:
    from core.transcript_store import import_store_once
    from db.engine import Database

    database = Database(f"sqlite+aiosqlite:///{db}")
    try:
        await database.init()
        async with database.session() as session:
            await import_store_once(session, str(checkpoints))
    finally:
        await database.close()


_STAMP = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(\.\d+)?(\+00:00)?")


def _snapshot(db: Path) -> dict:
    """`sqlite_master`, and every table's rows, with the
    stamps the two runs write at different instants masked."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        # By name: `create_all` makes a table's indexes in no fixed order.
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


async def _twins(edge_binary: Path, tmp: Path, start: Path | None, checkpoints: Path | None = None):
    """The same starting database (or none) brought up by each runtime."""
    snaps = []
    for side in ("python", "edge"):
        d = tmp / side
        d.mkdir()
        db = d / "data" / "database.db"  # a directory that isn't there yet
        if start is not None:
            db.parent.mkdir()
            shutil.copy(start, db)
        cp = checkpoints or d / "checkpoints.db"
        if side == "python":
            async def python(db: Path = db, cp: Path = cp) -> dict:
                db.parent.mkdir(exist_ok=True)  # Database() makes it; the edge must too
                await _python_init(db, cp)
                return _snapshot(db)

            snaps.append(await recorded(python))
        else:
            _edge_init(edge_binary, db, cp)
            snaps.append(_snapshot(db))
    return snaps


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

    async def python() -> dict:
        await _python_init(db, cp)
        return _snapshot(db)

    assert await recorded(python) == first  # and Python found nothing to do either


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
