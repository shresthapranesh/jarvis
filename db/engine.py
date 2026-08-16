import logging
import os
from collections.abc import AsyncGenerator

from sqlalchemy import Connection, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from pathlib import Path

from db.models import Base

logger = logging.getLogger(__name__)

# Pool sizing. aiosqlite runs every connection on its own OS thread, and
# QueuePool keeps `pool_size` of them open for the process lifetime — so
# pool_size is the *idle* footprint, paid whether or not anyone is connected.
# Overflow connections above it are closed on return, so the ceiling costs
# nothing while idle. Keep the ceiling where it was (15) and shrink the
# resident set instead.
_POOL_SIZE = int(os.environ.get("JARVIS_DB_POOL_SIZE", "2"))
_MAX_OVERFLOW = int(os.environ.get("JARVIS_DB_MAX_OVERFLOW", "13"))


# Lexical (BM25) search indexes — the sparse arm of hybrid retrieval for the
# embedded tables (see core/retrieval.py), and the only arm for messages. Each
# entry is (fts table, source table, indexed column). External-content tables
# store no copy of the text; they read it back through `content=` at query
# time, so the only cost is the inverted index.
_FTS_TABLES = (
    ("memories_fts", "memories", "text"),
    ("document_chunks_fts", "document_chunks", "text"),
    # Messages have no embeddings, so this one is not a hybrid arm — it is the
    # whole of `jarvis.search_conversations` (tools/sdk.py).
    ("messages_fts", "messages", "content"),
)


def _ensure_fts(conn: Connection) -> None:
    """Create the FTS5 mirrors + sync triggers, once.

    Best-effort: a SQLite build without FTS5 compiled in degrades to dense-only
    retrieval rather than failing startup. Every write path for these tables
    goes through real INSERT/UPDATE/DELETE (SQLAlchemy emits explicit DELETEs
    even for ORM cascades), so triggers are sufficient to keep the index in
    sync — no application-level bookkeeping.
    """
    for fts, src, col in _FTS_TABLES:
        try:
            already = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
                {"n": fts},
            ).first() is not None

            conn.execute(text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5("
                f"{col}, content='{src}', content_rowid='rowid', "
                "tokenize='porter unicode61 remove_diacritics 2')"
            ))
            # 'delete' rows carry the OLD text: external-content FTS5 can't read
            # it back from the source row that is already gone/changed.
            conn.execute(text(
                f"CREATE TRIGGER IF NOT EXISTS {src}_fts_ai AFTER INSERT ON {src} BEGIN "
                f"INSERT INTO {fts}(rowid, {col}) VALUES (new.rowid, new.{col}); END"
            ))
            conn.execute(text(
                f"CREATE TRIGGER IF NOT EXISTS {src}_fts_ad AFTER DELETE ON {src} BEGIN "
                f"INSERT INTO {fts}({fts}, rowid, {col}) VALUES('delete', old.rowid, old.{col}); END"
            ))
            conn.execute(text(
                f"CREATE TRIGGER IF NOT EXISTS {src}_fts_au AFTER UPDATE OF {col} ON {src} BEGIN "
                f"INSERT INTO {fts}({fts}, rowid, {col}) VALUES('delete', old.rowid, old.{col}); "
                f"INSERT INTO {fts}(rowid, {col}) VALUES (new.rowid, new.{col}); END"
            ))
            if not already:
                # Backfill rows that predate the index.
                conn.execute(text(f"INSERT INTO {fts}({fts}) VALUES('rebuild')"))
                logger.info("built FTS index %s over %s", fts, src)
        except Exception as exc:
            logger.warning(
                "FTS index %s unavailable (%s) — retrieval falls back to dense-only", fts, exc
            )


def _migrate(conn: Connection) -> None:
    """Apply any schema changes not covered by create_all (existing tables)."""
    inspector = inspect(conn)
    msg_cols = {c["name"] for c in inspector.get_columns("messages")}
    if "status" not in msg_cols:
        conn.execute(text("ALTER TABLE messages ADD COLUMN status VARCHAR DEFAULT 'done'"))
    if "input_tokens" not in msg_cols:
        conn.execute(text("ALTER TABLE messages ADD COLUMN input_tokens INTEGER"))
    if "output_tokens" not in msg_cols:
        conn.execute(text("ALTER TABLE messages ADD COLUMN output_tokens INTEGER"))
    # Throughput columns — NULL on every pre-existing row, which is correct:
    # the timings were never measured and cannot be reconstructed after the fact.
    for perf_col in ("ttft_ms", "llm_ms", "prefill_tps", "eval_tps"):
        if perf_col not in msg_cols:
            conn.execute(text(f"ALTER TABLE messages ADD COLUMN {perf_col} FLOAT"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages (conversation_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_conv_created ON messages (conversation_id, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_steps_message_id ON steps (message_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_steps_conversation_id ON steps (conversation_id)"))
    step_cols = {c["name"] for c in inspector.get_columns("steps")}
    if "subagent" not in step_cols:
        conn.execute(text("ALTER TABLE steps ADD COLUMN subagent VARCHAR"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_automation_runs_automation_id ON automation_runs (automation_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_runs_workflow_id ON workflow_runs (workflow_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_kind_status_run_at ON jobs (kind, status, run_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_locked_until ON jobs (locked_until)"))
    conv_cols = {c["name"] for c in inspector.get_columns("conversations")}
    if "pinned" not in conv_cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN pinned BOOLEAN DEFAULT 0"))
    if "surface" not in conv_cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN surface VARCHAR DEFAULT 'web'"))
        # Bot conversations predate the column; their ids are prefixed by surface.
        conn.execute(text("UPDATE conversations SET surface='telegram' WHERE id LIKE 'telegram\\_%' ESCAPE '\\'"))
        conn.execute(text("UPDATE conversations SET surface='discord' WHERE id LIKE 'discord\\_%' ESCAPE '\\'"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_surface ON conversations (surface)"))
    if "project_id" not in conv_cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN project_id VARCHAR REFERENCES projects(id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_project_id ON conversations (project_id)"))
    if "ephemeral" not in conv_cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN ephemeral BOOLEAN DEFAULT 0"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_ephemeral ON conversations (ephemeral)"))
    auto_cols = {c["name"] for c in inspector.get_columns("automations")}
    if "notifications" not in auto_cols:
        conn.execute(text("ALTER TABLE automations ADD COLUMN notifications TEXT"))
    if "stateful" not in auto_cols:
        conn.execute(text("ALTER TABLE automations ADD COLUMN stateful BOOLEAN DEFAULT 0"))
    wf_cols = {c["name"] for c in inspector.get_columns("workflows")}
    if "notifications" not in wf_cols:
        conn.execute(text("ALTER TABLE workflows ADD COLUMN notifications TEXT"))
    doc_cols = {c["name"] for c in inspector.get_columns("documents")}
    if "index_status" not in doc_cols:
        conn.execute(text("ALTER TABLE documents ADD COLUMN index_status VARCHAR"))
        # Anything already carrying chunks was indexed before the column existed;
        # leaving those NULL would make the retrieval tools treat them as
        # never-indexed and refuse to search them.
        conn.execute(text(
            "UPDATE documents SET index_status='indexed' WHERE id IN "
            "(SELECT DISTINCT document_id FROM document_chunks)"
        ))
    bt_cols = {c["name"] for c in inspector.get_columns("board_tasks")}
    if "blocked_kind" not in bt_cols:
        conn.execute(text("ALTER TABLE board_tasks ADD COLUMN blocked_kind VARCHAR"))
    if "pending_answer" not in bt_cols:
        conn.execute(text("ALTER TABLE board_tasks ADD COLUMN pending_answer TEXT"))
    art_cols = {c["name"] for c in inspector.get_columns("artifacts")}
    if "mime_type" not in art_cols:
        conn.execute(text("ALTER TABLE artifacts ADD COLUMN mime_type VARCHAR"))
    _ensure_fts(conn)

def _set_sqlite_pragmas(dbapi_conn: object, _record: object) -> None:
    """journal_mode is persisted in the file; synchronous and busy_timeout are
    per-connection, so this has to run on every new DBAPI connection."""
    cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _sqlite_file(url: str) -> Path | None:
    """The on-disk path behind a file-backed sqlite URL, else None (`:memory:`)."""
    if "sqlite" not in url or ":///" not in url:
        return None
    path = url.rsplit(":///", 1)[-1]
    return None if not path or path == ":memory:" else Path(path)


class Database:
    """One database: engine + session factory + schema lifecycle.

    Owning these as an object instead of module globals means the connection
    pool has an explicit lifetime — `close()` actually releases the aiosqlite
    threads — and the URL is a constructor argument rather than something
    frozen from the environment at import time.
    """

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = _POOL_SIZE,
        max_overflow: int = _MAX_OVERFLOW,
        echo: bool = False,
    ) -> None:
        self.url = url
        file = _sqlite_file(url)
        if file is not None:
            file.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            url, echo=echo, pool_size=pool_size, max_overflow=max_overflow
        )
        event.listen(self.engine.sync_engine, "connect", _set_sqlite_pragmas)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create missing tables and apply migrations. Idempotent."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_migrate)

    async def close(self) -> None:
        """Dispose the pool, closing every connection and its aiosqlite thread."""
        await self.engine.dispose()

    def __repr__(self) -> str:
        return f"Database({self.url!r})"


# ── Database resolution ──────────────────────────────────────────────────────
# Everything still reaches the DB through the module-level `async_session()`
# below, which resolves through get_database() on every call. That in turn
# prefers the active JarvisRunner's database, falling back to a process default
# for the CLI, tests, and anything running before the server lifespan. So the
# runner is the source of truth without a single call site having to know.

_database: Database | None = None


def set_database(db: Database | None) -> Database | None:
    """Install the process-default Database. Returns the one it replaced."""
    global _database
    previous, _database = _database, db
    return previous


def _process_default_database() -> Database:
    """The fallback Database, built from AppConfig on first use."""
    global _database
    if _database is None:
        from core.config import get_config

        cfg = get_config()
        # Preserved from the old import-time side effect: work_dir must exist
        # even when DATABASE_URL points somewhere else entirely.
        Path(cfg.work_dir).mkdir(parents=True, exist_ok=True)
        _database = Database(cfg.database_url)
    return _database


def get_database() -> Database:
    """The active Database — the runner's if one is installed, else the default."""
    from core.runner import get_runner_or_none

    runner = get_runner_or_none()
    if runner is not None and runner.db is not None:
        return runner.db
    return _process_default_database()


def async_session() -> AsyncSession:
    """A new session on the process-default Database.

    Kept as a module-level callable so the ~130 `async with async_session()`
    call sites (many of which import the name at module scope) keep working
    while the Database it resolves to becomes swappable.
    """
    return get_database().session()


async def init_db() -> None:
    await get_database().init()


async def close_db() -> None:
    """Dispose the process-default Database, if one was ever built."""
    db = set_database(None)
    if db is not None:
        await db.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
