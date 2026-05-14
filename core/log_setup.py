"""Process-wide logging setup, shared by the CLI and the server.

Always writes to ``<work_dir>/jarvis.log`` with rotation. Optionally mirrors
to stderr. Silences noisy third-party loggers (SQLAlchemy/SQLite internals,
HTTP transport debug, etc.) so the agent/tool/graph activity stands out.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_FORMAT = "%(asctime)s %(levelname)-5s %(name)-26s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_BACKFILL_CAP = 2000
_SUBSCRIBER_QUEUE_MAX = 500
_MAX_MESSAGE_CHARS = 8192

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


class BroadcastHandler(logging.Handler):
    """Capture log records for the in-app /logs viewer.

    Keeps a bounded deque of recent records for SSE backfill, and fans out
    each new record to all currently connected subscribers. ``emit`` runs on
    whichever thread the logger fires on; the asyncio.Queue dispatch is
    routed through ``loop.call_soon_threadsafe`` so we never touch the queue
    from a non-loop thread (this matches ``core.state._notify``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.backfill: deque[dict[str, Any]] = deque(maxlen=_BACKFILL_CAP)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called by the server lifespan once the event loop is running."""
        self._loop = loop

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.backfill)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if len(message) > _MAX_MESSAGE_CHARS:
                message = message[:_MAX_MESSAGE_CHARS] + "… [truncated]"
            payload = {
                "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        except Exception:
            return
        self.backfill.append(payload)
        loop = self._loop
        if loop is None or not self.subscribers:
            return
        loop.call_soon_threadsafe(self._fanout, payload)

    def _fanout(self, payload: dict[str, Any]) -> None:
        # Drop-oldest backpressure: a slow client should never block logging.
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


_broadcast_handler: BroadcastHandler | None = None


def get_broadcast_handler() -> BroadcastHandler:
    """Return the process-wide BroadcastHandler. Created lazily by setup_logging."""
    if _broadcast_handler is None:
        raise RuntimeError("logging not initialised — call setup_logging first")
    return _broadcast_handler


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
    global _configured, _broadcast_handler
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

    _broadcast_handler = BroadcastHandler()
    root.addHandler(_broadcast_handler)

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(lvl)
