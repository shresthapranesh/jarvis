import logging
from collections.abc import AsyncGenerator

from sqlalchemy import Connection, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pathlib import Path

from core.config import get_config

logger = logging.getLogger(__name__)

_cfg = get_config()
Path(_cfg.work_dir).mkdir(parents=True, exist_ok=True)
DATABASE_URL = _cfg.database_url
from db.models import Base


# Lexical (BM25) side of hybrid retrieval — see core/retrieval.py. Each entry is
# (fts table, source table, indexed column). External-content tables store no
# copy of the text; they read it back through `content=` at query time, so the
# only cost is the inverted index.
_FTS_TABLES = (
    ("memories_fts", "memories", "text"),
    ("document_chunks_fts", "document_chunks", "text"),
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
    bt_cols = {c["name"] for c in inspector.get_columns("board_tasks")}
    if "blocked_kind" not in bt_cols:
        conn.execute(text("ALTER TABLE board_tasks ADD COLUMN blocked_kind VARCHAR"))
    if "pending_answer" not in bt_cols:
        conn.execute(text("ALTER TABLE board_tasks ADD COLUMN pending_answer TEXT"))
    art_cols = {c["name"] for c in inspector.get_columns("artifacts")}
    if "mime_type" not in art_cols:
        conn.execute(text("ALTER TABLE artifacts ADD COLUMN mime_type VARCHAR"))
    _ensure_fts(conn)

engine = create_async_engine(DATABASE_URL, echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn: object, _record: object) -> None:
    cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
