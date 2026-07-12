from collections.abc import AsyncGenerator

from sqlalchemy import Connection, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pathlib import Path

from core.config import get_config

_cfg = get_config()
Path(_cfg.work_dir).mkdir(parents=True, exist_ok=True)
DATABASE_URL = _cfg.database_url
from db.models import Base


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
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_automation_runs_automation_id ON automation_runs (automation_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_runs_workflow_id ON workflow_runs (workflow_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_kind_status_run_at ON jobs (kind, status, run_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_locked_until ON jobs (locked_until)"))
    conv_cols = {c["name"] for c in inspector.get_columns("conversations")}
    if "surface" not in conv_cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN surface VARCHAR DEFAULT 'web'"))
        # Bot conversations predate the column; their ids are prefixed by surface.
        conn.execute(text("UPDATE conversations SET surface='telegram' WHERE id LIKE 'telegram\\_%' ESCAPE '\\'"))
        conn.execute(text("UPDATE conversations SET surface='discord' WHERE id LIKE 'discord\\_%' ESCAPE '\\'"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_surface ON conversations (surface)"))
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
