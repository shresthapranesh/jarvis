"""Process-wide logging setup, shared by the CLI and the server.

Always writes to ``<work_dir>/jarvis.log`` with rotation. Optionally mirrors
to stderr. Silences noisy third-party loggers (SQLAlchemy/SQLite internals,
HTTP transport debug, etc.) so the agent/tool/graph activity stands out.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-5s %(name)-26s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that flood the file with low-signal noise. SQLite/DB
# internals are explicitly silenced per the user's request.
_NOISY_LOGGERS: dict[str, int] = {
    "sqlalchemy":                  logging.WARNING,
    "sqlalchemy.engine":           logging.WARNING,
    "sqlalchemy.pool":             logging.WARNING,
    "aiosqlite":                   logging.WARNING,
    "langgraph.checkpoint.sqlite": logging.WARNING,
    "langgraph.store.sqlite":      logging.WARNING,
    "httpx":                       logging.WARNING,
    "httpcore":                    logging.WARNING,
    "urllib3":                     logging.WARNING,
    "google":                      logging.WARNING,
    "google.auth":                 logging.WARNING,
    "google_genai":                logging.WARNING,
    "google_genai.models":         logging.WARNING,
    "watchfiles":                  logging.WARNING,
    "fsspec":                      logging.WARNING,
    "asyncio":                     logging.WARNING,
    "uvicorn.access":              logging.WARNING,
    "apscheduler":                 logging.WARNING,
    "telegram":                    logging.WARNING,
    "httpcore.http11":             logging.WARNING,
    "httpcore.connection":         logging.WARNING,
    "openai":                      logging.WARNING,
    "anthropic":                   logging.WARNING,
}

_configured = False


def setup_logging(
    work_dir: Path | str | None,
    *,
    level: int = logging.INFO,
    console: bool = False,
) -> None:
    """Configure the root logger. Idempotent — calling it twice is a no-op.

    ``work_dir`` is the directory where ``jarvis.log`` lives. Pass ``None`` to
    skip the file handler (rare; handy for tests). ``console=True`` mirrors
    output to stderr — used by ``main.py run --debug``.
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    if work_dir is not None:
        wd = Path(work_dir)
        wd.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            wd / "jarvis.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if console:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root.addHandler(stderr_handler)

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(lvl)
