"""Rows seeded into a test database as the ORM inserted them.

Python's ORM (`db/models.py`) filled some columns itself — ids, timestamps,
statuses — and wrote values in its own text: a datetime as
`2026-01-02 03:04:05.000000`, a bool as 0/1. The recordings the edge tests
compare against were made over rows it seeded, so `insert` fills and writes
them the same way. `DEFAULTS` is its column defaults, frozen.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOW = object()
UUID = object()

DEFAULTS: dict[str, dict[str, Any]] = {
    "approvals": {
        "id": UUID,
        "kind": "approval",
        "status": "pending",
        "question": "",
        "label": "",
        "requested_at": NOW,
        "updated_at": NOW,
    },
    "automations": {"enabled": True, "stateful": False, "created_at": NOW, "updated_at": NOW},
    "board_tasks": {
        "status": "todo",
        "priority": 0,
        "created_by": "user",
        "failure_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
    },
    "config_settings": {"updated_at": NOW},
    "jobs": {
        "payload": "{}",
        "status": "pending",
        "run_at": NOW,
        "attempts": 0,
        "max_attempts": 3,
        "cancel_requested": False,
        "created_at": NOW,
        "updated_at": NOW,
    },
    "kv_store": {"created_at": NOW, "updated_at": NOW},
    "memories": {"kind": "fact", "created_at": NOW, "updated_at": NOW},
    "notification_channels": {"created_at": NOW, "updated_at": NOW},
    "projects": {"instructions": "", "memory": "", "created_at": NOW, "updated_at": NOW},
    "skills": {"enabled": True, "created_at": NOW, "updated_at": NOW},
    "thread_messages": {"id": UUID, "created_at": NOW},
    "thread_state": {"updated_at": NOW},
    "transcript_blobs": {"created_at": NOW},
    "workflows": {"definition": "{}", "created_at": NOW, "updated_at": NOW},
    "automation_runs": {"status": "running", "started_at": NOW},
    "board_task_links": {"created_at": NOW},
    "conversations": {"surface": "web", "pinned": False, "ephemeral": False, "created_at": NOW},
    "workflow_runs": {"status": "running", "started_at": NOW},
    "conversation_episodes": {"created_at": NOW},
    "memory_activities": {"id": UUID, "accessed_at": NOW},
    "messages": {"status": "done", "created_at": NOW},
    "artifacts": {"kind": "markdown", "created_at": NOW, "updated_at": NOW},
    "steps": {"created_at": NOW},
    "artifact_versions": {"id": UUID, "created_at": NOW},
}


def stamp(value: datetime) -> str:
    """A datetime as SQLAlchemy stored one in SQLite (any zone dropped)."""
    return f"{value:%Y-%m-%d %H:%M:%S.%f}"


def _value(value: Any) -> Any:
    if value is NOW:
        return stamp(datetime.now(timezone.utc))
    if value is UUID:
        return str(uuid.uuid4())
    if isinstance(value, datetime):
        return stamp(value)
    if isinstance(value, bool):
        return int(value)
    return value


def insert(db: Path | sqlite3.Connection, table: str, **values: Any) -> dict[str, Any]:
    """Insert one row into `table` — the ORM's defaults for whatever isn't
    given — and return what was written."""
    row = {k: _value(v) for k, v in {**DEFAULTS.get(table, {}), **values}.items()}
    sql = f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})"
    if isinstance(db, sqlite3.Connection):
        db.execute(sql, list(row.values()))
    else:
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute(sql, list(row.values()))
            conn.commit()
    return row


def row(db: Path, table: str, key: str, column: str = "id") -> dict[str, Any] | None:
    """The row of `table` whose `column` is `key`, or None."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        found = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (key,)).fetchone()
        return dict(found) if found else None


def execute(db: Path, sql: str, *args: Any) -> None:
    """One write to `db`, committed."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute(sql, args)
        conn.commit()


def set_setting(db: Path, key: str, value: str) -> None:
    """A `config_settings` row, made or replaced (`db.ops.set_setting`)."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        if conn.execute("UPDATE config_settings SET value = ?, updated_at = ? WHERE key = ?",
                        (value, _value(NOW), key)).rowcount == 0:
            insert(conn, "config_settings", key=key, value=value)
        conn.commit()


def put_kv(db: Path, namespace: str, key: str, value: Any) -> None:
    """A `kv_store` entry, made or replaced (`KvStore.aput`)."""
    text = json.dumps(value, ensure_ascii=False)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        if conn.execute("UPDATE kv_store SET value = ?, updated_at = ? WHERE namespace = ? AND key = ?",
                        (text, _value(NOW), namespace, key)).rowcount == 0:
            insert(conn, "kv_store", namespace=namespace, key=key, value=text)
        conn.commit()


def set_todos(db: Path, thread_id: str, todos: Any) -> None:
    """A thread's todo list, stored as JSON (`transcript_store.set_todos`)."""
    text = None if todos is None else json.dumps(todos, ensure_ascii=False)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        if conn.execute("UPDATE thread_state SET todos = ?, updated_at = ? WHERE thread_id = ?",
                        (text, _value(NOW), thread_id)).rowcount == 0:
            insert(conn, "thread_state", thread_id=thread_id, todos=text)
        conn.commit()
