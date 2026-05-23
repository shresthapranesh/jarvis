"""Durable job queue — pluggable backend behind the JobQueue abstract base class.

Today: SQLite (same DB as the app, so enqueue can ride a caller's transaction).
Tomorrow: Redis or another backend dropped in via core/queue/<name>.py and the
JARVIS_QUEUE config flag.

The queue is the *scheduling* layer: "what work needs doing, has the worker
crashed, when should it retry." Ephemeral worker state (SSE event buffers,
streaming waiters, stop flags) stays in-memory keyed by job_id — it is not
durable and does not belong here.
"""

from .protocol import Job, JobQueue
from .sqlite import SqliteJobQueue

__all__ = ["Job", "JobQueue", "SqliteJobQueue"]
