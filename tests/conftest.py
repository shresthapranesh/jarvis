"""Shared fixtures.

Every test runs against a throwaway work_dir. Nothing here may touch
~/.jarvis — the fixtures point WORK_DIR at tmp_path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from edge_support import edge_binary, fresh_db  # noqa: F401 — edge_binary is a fixture

# Python's recorded answers, for the tests that diff the Rust server.
pytest_plugins = ["python_golden"]


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated WORK_DIR. Everything (db, artifacts) lands here."""
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CHECKPOINTS_DB", raising=False)
    monkeypatch.delenv("ARTIFACTS_DIR", raising=False)
    yield tmp_path


@pytest.fixture
def database(work_dir: Path, edge_binary: Path) -> Path:
    """`work_dir/database.db`, new, as the server's first start leaves it."""
    db = work_dir / "database.db"
    fresh_db(edge_binary, db)
    return db
