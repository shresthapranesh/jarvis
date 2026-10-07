//! The database schema: the edge creates and migrates it, at start and before
//! a command-line command, as `db/engine.py:Database.init` does for Python
//! alone.
//!
//! `schema.sql` is what `Base.metadata.create_all` makes, captured from a fresh
//! database (`tests/test_edge_schema.py` re-captures it): every table missing
//! from the file is created with its indexes, then `migrate` — a port of
//! `_migrate` — adds what older databases lack. Then, once, the store
//! LangGraph left in `checkpoints.db` is copied into `kv_store`
//! (`core/transcript_store.py:import_store_once`). A change to any of these is
//! made in both; the test diffs the two runtimes over fresh and old databases.

use std::collections::HashSet;
use std::path::Path;
use std::str::FromStr;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, Utc};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool};
use sqlx::{Connection, Sqlite, SqliteConnection, Transaction};

const SCHEMA: &str = include_str!("schema.sql");

/// One `create_all` table: its `CREATE TABLE` and its indexes.
struct Table {
    name: &'static str,
    statements: Vec<&'static str>,
}

fn tables() -> Vec<Table> {
    let mut out: Vec<Table> = vec![];
    for chunk in SCHEMA.split(";\n\n") {
        let statement = chunk.lines().skip_while(|l| l.starts_with("--") || l.is_empty()).collect::<Vec<_>>();
        if statement.is_empty() {
            continue;
        }
        // Back to a slice of SCHEMA, so the text is exactly as captured.
        let start = chunk.find(statement[0]).expect("a statement");
        let statement = chunk[start..].trim_end();
        if let Some(rest) = statement.strip_prefix("CREATE TABLE ") {
            let name = rest.split_whitespace().next().expect("a table name");
            out.push(Table { name, statements: vec![statement] });
        } else {
            let on = statement.split(" ON ").nth(1).and_then(|s| s.split_whitespace().next());
            let table = out.iter_mut().rev().find(|t| Some(t.name) == on).expect("an index after its table");
            table.statements.push(statement);
        }
    }
    out
}

/// `Database.init`: the missing tables, then the migrations, in one
/// transaction. Makes the database's directory first, as `Database()` does.
pub async fn init(pool: &SqlitePool, db_path: &Path) -> Result<(), String> {
    if let Some(dir) = db_path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    }
    let mut tx = crate::db::write_tx(pool).await.map_err(|e| e.to_string())?;
    create_missing(&mut tx).await.map_err(|e| format!("creating tables: {e}"))?;
    migrate(&mut tx).await.map_err(|e| format!("migrating: {e}"))?;
    tx.commit().await.map_err(|e| e.to_string())
}

async fn create_missing(tx: &mut Transaction<'_, Sqlite>) -> sqlx::Result<()> {
    let existing: HashSet<String> =
        sqlx::query_scalar("SELECT name FROM sqlite_master WHERE type = 'table'").fetch_all(&mut **tx).await?.into_iter().collect();
    for table in tables() {
        if existing.contains(table.name) {
            continue;
        }
        for statement in table.statements {
            sqlx::query(statement).execute(&mut **tx).await?;
        }
    }
    Ok(())
}

async fn columns(tx: &mut Transaction<'_, Sqlite>, table: &str) -> sqlx::Result<HashSet<String>> {
    Ok(sqlx::query_scalar("SELECT name FROM pragma_table_info(?)").bind(table).fetch_all(&mut **tx).await?.into_iter().collect())
}

async fn exec(tx: &mut Transaction<'_, Sqlite>, sql: &str) -> sqlx::Result<()> {
    sqlx::query(sql).execute(&mut **tx).await.map(drop)
}

/// `_migrate`, statement for statement — the text of an `ALTER TABLE` ends up
/// in `sqlite_master`, so it is Python's exactly.
async fn migrate(tx: &mut Transaction<'_, Sqlite>) -> sqlx::Result<()> {
    let msg = columns(tx, "messages").await?;
    if !msg.contains("status") {
        exec(tx, "ALTER TABLE messages ADD COLUMN status VARCHAR DEFAULT 'done'").await?;
    }
    if !msg.contains("input_tokens") {
        exec(tx, "ALTER TABLE messages ADD COLUMN input_tokens INTEGER").await?;
    }
    if !msg.contains("output_tokens") {
        exec(tx, "ALTER TABLE messages ADD COLUMN output_tokens INTEGER").await?;
    }
    for perf in ["ttft_ms", "llm_ms", "prefill_tps", "eval_tps", "duration_ms"] {
        if !msg.contains(perf) {
            exec(tx, &format!("ALTER TABLE messages ADD COLUMN {perf} FLOAT")).await?;
        }
    }
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages (conversation_id)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_messages_conv_created ON messages (conversation_id, created_at)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_steps_message_id ON steps (message_id)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_steps_conversation_id ON steps (conversation_id)").await?;
    if !columns(tx, "steps").await?.contains("subagent") {
        exec(tx, "ALTER TABLE steps ADD COLUMN subagent VARCHAR").await?;
    }
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_automation_runs_automation_id ON automation_runs (automation_id)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_workflow_runs_workflow_id ON workflow_runs (workflow_id)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_jobs_kind_status_run_at ON jobs (kind, status, run_at)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_jobs_locked_until ON jobs (locked_until)").await?;
    let jobs = columns(tx, "jobs").await?;
    if !jobs.contains("thread_id") {
        exec(tx, "ALTER TABLE jobs ADD COLUMN thread_id VARCHAR").await?;
    }
    if !jobs.contains("runtime") {
        exec(tx, "ALTER TABLE jobs ADD COLUMN runtime VARCHAR").await?;
    }
    exec(
        tx,
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_thread_lease ON jobs (thread_id) \
         WHERE status = 'running' AND thread_id IS NOT NULL",
    )
    .await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_approvals_status_requested ON approvals (status, requested_at)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_approvals_task_status ON approvals (task_id, status)").await?;
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_approvals_board_task_status ON approvals (board_task_id, status)").await?;
    let conv = columns(tx, "conversations").await?;
    if !conv.contains("pinned") {
        exec(tx, "ALTER TABLE conversations ADD COLUMN pinned BOOLEAN DEFAULT 0").await?;
    }
    if !conv.contains("surface") {
        exec(tx, "ALTER TABLE conversations ADD COLUMN surface VARCHAR DEFAULT 'web'").await?;
        // Bot conversations predate the column; their ids are prefixed by surface.
        exec(tx, r"UPDATE conversations SET surface='telegram' WHERE id LIKE 'telegram\_%' ESCAPE '\'").await?;
        exec(tx, r"UPDATE conversations SET surface='discord' WHERE id LIKE 'discord\_%' ESCAPE '\'").await?;
    }
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_conversations_surface ON conversations (surface)").await?;
    if !conv.contains("project_id") {
        exec(tx, "ALTER TABLE conversations ADD COLUMN project_id VARCHAR REFERENCES projects(id)").await?;
    }
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_conversations_project_id ON conversations (project_id)").await?;
    if !conv.contains("ephemeral") {
        exec(tx, "ALTER TABLE conversations ADD COLUMN ephemeral BOOLEAN DEFAULT 0").await?;
    }
    exec(tx, "CREATE INDEX IF NOT EXISTS ix_conversations_ephemeral ON conversations (ephemeral)").await?;
    let auto = columns(tx, "automations").await?;
    if !auto.contains("notifications") {
        exec(tx, "ALTER TABLE automations ADD COLUMN notifications TEXT").await?;
    }
    if !auto.contains("stateful") {
        exec(tx, "ALTER TABLE automations ADD COLUMN stateful BOOLEAN DEFAULT 0").await?;
    }
    if !columns(tx, "workflows").await?.contains("notifications") {
        exec(tx, "ALTER TABLE workflows ADD COLUMN notifications TEXT").await?;
    }
    let board = columns(tx, "board_tasks").await?;
    if !board.contains("blocked_kind") {
        exec(tx, "ALTER TABLE board_tasks ADD COLUMN blocked_kind VARCHAR").await?;
    }
    if !board.contains("pending_answer") {
        exec(tx, "ALTER TABLE board_tasks ADD COLUMN pending_answer TEXT").await?;
    }
    if !columns(tx, "artifacts").await?.contains("mime_type") {
        exec(tx, "ALTER TABLE artifacts ADD COLUMN mime_type VARCHAR").await?;
    }
    backfill_artifact_message_ids(tx).await?;
    ensure_fts(tx).await;
    Ok(())
}

/// `_backfill_artifact_message_ids`: once (a marker setting), each artifact
/// without a message gets the newest assistant message at or before it.
async fn backfill_artifact_message_ids(tx: &mut Transaction<'_, Sqlite>) -> sqlx::Result<()> {
    const KEY: &str = "migration.artifact_message_ids";
    let done: Option<i64> =
        sqlx::query_scalar("SELECT 1 FROM config_settings WHERE key = ?").bind(KEY).fetch_optional(&mut **tx).await?;
    if done.is_some() {
        return Ok(());
    }
    exec(
        tx,
        "UPDATE artifacts SET message_id = (  SELECT m.id FROM messages m  WHERE m.conversation_id = artifacts.conversation_id    \
         AND m.role = 'assistant'    AND m.created_at <= artifacts.created_at  ORDER BY m.created_at DESC LIMIT 1) \
         WHERE message_id IS NULL AND conversation_id IS NOT NULL",
    )
    .await?;
    sqlx::query("INSERT INTO config_settings (key, value, updated_at) VALUES (?, '1', CURRENT_TIMESTAMP)")
        .bind(KEY)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

/// `_FTS_TABLES`: (fts table, source table, indexed column).
const FTS_TABLES: [(&str, &str, &str); 3] = [
    ("memories_fts", "memories", "text"),
    ("conversation_episodes_fts", "conversation_episodes", "text"),
    ("messages_fts", "messages", "content"),
];

/// `_ensure_fts`: each FTS5 mirror and its sync triggers, built from the
/// source rows the first time. One that fails leaves retrieval dense-only.
async fn ensure_fts(tx: &mut Transaction<'_, Sqlite>) {
    for (fts, src, col) in FTS_TABLES {
        if let Err(e) = ensure_one_fts(tx, fts, src, col).await {
            tracing::warn!("FTS index {fts} unavailable ({e}) — retrieval falls back to dense-only");
        }
    }
}

async fn ensure_one_fts(tx: &mut Transaction<'_, Sqlite>, fts: &str, src: &str, col: &str) -> sqlx::Result<()> {
    let already: Option<i64> = sqlx::query_scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?")
        .bind(fts)
        .fetch_optional(&mut **tx)
        .await?;
    exec(
        tx,
        &format!(
            "CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5({col}, content='{src}', content_rowid='rowid', \
             tokenize='porter unicode61 remove_diacritics 2')"
        ),
    )
    .await?;
    exec(
        tx,
        &format!(
            "CREATE TRIGGER IF NOT EXISTS {src}_fts_ai AFTER INSERT ON {src} BEGIN \
             INSERT INTO {fts}(rowid, {col}) VALUES (new.rowid, new.{col}); END"
        ),
    )
    .await?;
    exec(
        tx,
        &format!(
            "CREATE TRIGGER IF NOT EXISTS {src}_fts_ad AFTER DELETE ON {src} BEGIN \
             INSERT INTO {fts}({fts}, rowid, {col}) VALUES('delete', old.rowid, old.{col}); END"
        ),
    )
    .await?;
    exec(
        tx,
        &format!(
            "CREATE TRIGGER IF NOT EXISTS {src}_fts_au AFTER UPDATE OF {col} ON {src} BEGIN \
             INSERT INTO {fts}({fts}, rowid, {col}) VALUES('delete', old.rowid, old.{col}); \
             INSERT INTO {fts}(rowid, {col}) VALUES (new.rowid, new.{col}); END"
        ),
    )
    .await?;
    if already.is_none() {
        exec(tx, &format!("INSERT INTO {fts}({fts}) VALUES('rebuild')")).await?;
        tracing::info!("built FTS index {fts} over {src}");
    }
    Ok(())
}

// ── the store LangGraph left ─────────────────────────────────────────────────

/// `import_store_once`: the LangGraph store's items copied into `kv_store`
/// (keys already there kept), the first time only — afterwards `kv_store` is
/// the store, and a key deleted from it must not come back. Returns how many
/// were copied, or None when it had already run.
pub async fn import_store_once(pool: &SqlitePool, checkpoints_db: &Path) -> Result<Option<u64>, String> {
    let mut tx = crate::db::write_tx(pool).await.map_err(|e| e.to_string())?;
    let done: Option<i64> =
        sqlx::query_scalar("SELECT 1 FROM kv_store WHERE namespace = 'jarvis.migrations' AND key = 'langgraph_store'")
            .fetch_optional(&mut *tx)
            .await
            .map_err(|e| e.to_string())?;
    if done.is_some() {
        return Ok(None);
    }
    let rows = if checkpoints_db.exists() { store_rows(checkpoints_db).await.map_err(|e| e.to_string())? } else { vec![] };
    let mut copied = 0;
    for (prefix, key, value, created_at, updated_at) in rows {
        copied += sqlx::query(
            "INSERT INTO kv_store (namespace, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?) \
             ON CONFLICT (namespace, key) DO NOTHING",
        )
        .bind(prefix)
        .bind(key)
        .bind(value)
        .bind(stamp(created_at.as_deref()))
        .bind(stamp(updated_at.as_deref()))
        .execute(&mut *tx)
        .await
        .map_err(|e| e.to_string())?
        .rows_affected();
    }
    let now = crate::gql::codec::now_stored();
    sqlx::query("INSERT INTO kv_store (namespace, key, value, created_at, updated_at) VALUES ('jarvis.migrations', 'langgraph_store', ?, ?, ?)")
        .bind(format!(r#"{{"copied": {copied}, "at": "{}"}}"#, isoformat(&Utc::now().fixed_offset())))
        .bind(&now)
        .bind(&now)
        .execute(&mut *tx)
        .await
        .map_err(|e| e.to_string())?;
    tx.commit().await.map_err(|e| e.to_string())?;
    Ok(Some(copied))
}

type StoreRow = (String, String, String, Option<String>, Option<String>);

/// The `store` table's rows, read-only; none when there is no such table.
async fn store_rows(path: &Path) -> sqlx::Result<Vec<StoreRow>> {
    let opts = SqliteConnectOptions::from_str(&format!("sqlite://{}", path.display()))?.read_only(true);
    let mut conn = SqliteConnection::connect_with(&opts).await?;
    let has: Option<i64> = sqlx::query_scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store'")
        .fetch_optional(&mut conn)
        .await?;
    let rows = match has {
        // Timestamps as text, as `str()` would see them; anything else reads as now.
        Some(_) => {
            sqlx::query_as(
                "SELECT prefix, key, value, CAST(created_at AS TEXT), CAST(updated_at AS TEXT) FROM store",
            )
            .fetch_all(&mut conn)
            .await?
        }
        None => vec![],
    };
    conn.close().await?;
    Ok(rows)
}

/// `_stamp`: a LangGraph store timestamp (`datetime.fromisoformat`, naive
/// taken as UTC) as SQLAlchemy binds it — the wall clock, any offset dropped —
/// or now when it doesn't parse.
fn stamp(raw: Option<&str>) -> String {
    let at = raw.and_then(fromisoformat).unwrap_or_else(|| Utc::now().fixed_offset());
    at.naive_local().format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

/// The `fromisoformat` shapes a timestamp column holds: a date, or a date and
/// time (`T` or a space; minutes, seconds, a fraction), maybe an offset or `Z`.
fn fromisoformat(raw: &str) -> Option<DateTime<FixedOffset>> {
    let raw = raw.trim_end_matches('Z');
    let utc = |naive: NaiveDateTime| Some(naive.and_utc().fixed_offset());
    if let Ok(date) = NaiveDate::parse_from_str(raw, "%Y-%m-%d") {
        return utc(date.and_hms_opt(0, 0, 0)?);
    }
    for sep in ['T', ' '] {
        for time in ["%H:%M:%S%.f", "%H:%M"] {
            let naive = format!("%Y-%m-%d{sep}{time}");
            if let Ok(at) = NaiveDateTime::parse_from_str(raw, &naive) {
                return utc(at);
            }
            if let Ok(at) = DateTime::parse_from_str(raw, &format!("{naive}%:z")) {
                return Some(at);
            }
        }
    }
    None
}

/// `datetime.isoformat()` for an aware value: microseconds only when set.
fn isoformat(at: &DateTime<FixedOffset>) -> String {
    let wall = if at.timestamp_subsec_micros() == 0 { "%Y-%m-%dT%H:%M:%S" } else { "%Y-%m-%dT%H:%M:%S%.6f" };
    format!("{}{}", at.format(wall), at.format("%:z"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_statement_belongs_to_a_table() {
        let tables = tables();
        assert_eq!(tables.len(), SCHEMA.matches("CREATE TABLE ").count());
        let statements: usize = tables.iter().map(|t| t.statements.len()).sum();
        assert_eq!(statements, SCHEMA.matches(";\n\n").count());
        assert!(tables.iter().all(|t| t.statements[0].starts_with("CREATE TABLE ") && t.statements[0].ends_with(')')));
    }

    #[test]
    fn store_stamps_read_as_python_reads_them() {
        assert_eq!(stamp(Some("2026-05-03 07:00:24")), "2026-05-03 07:00:24.000000");
        assert_eq!(stamp(Some("2026-05-03T07:00:24.5+02:00")), "2026-05-03 07:00:24.500000");
        assert_eq!(stamp(Some("2026-05-03")), "2026-05-03 00:00:00.000000");
        assert_eq!(stamp(Some("2026-05-03 07:00")), "2026-05-03 07:00:00.000000");
        assert_eq!(stamp(Some("2026-05-03T07:00:24Z")), "2026-05-03 07:00:24.000000");
        assert!(stamp(Some("not a time")).starts_with(&Utc::now().format("%Y-%m-%d").to_string()));
    }
}
