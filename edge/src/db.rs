//! The SQLite pool, connected with the same per-connection settings
//! `db/engine.py:_set_sqlite_pragmas` applies. The schema is `schema.rs`'s.

use std::path::Path;
use std::str::FromStr;
use std::time::Duration;

use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePool, SqlitePoolOptions, SqliteSynchronous};

pub fn pool(path: &Path) -> Result<SqlitePool, sqlx::Error> {
    let opts = SqliteConnectOptions::from_str(&format!("sqlite://{}", path.display()))?
        // A fresh install: `schema::init` makes the tables.
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .busy_timeout(Duration::from_secs(5))
        // sqlx turns foreign keys ON by default; the Python side never does,
        // and its deletes rely on that (see "Adding a new conversation-scoped
        // resource" in CLAUDE.md). Two writers must not disagree on it.
        .foreign_keys(false);

    Ok(SqlitePoolOptions::new()
        // Idle footprint is the point of this process: hold no connection
        // while nobody is asking for anything.
        .min_connections(0)
        .max_connections(4)
        .idle_timeout(Duration::from_secs(60))
        .connect_lazy_with(opts))
}

/// A write transaction. `BEGIN IMMEDIATE` takes the write lock up front, so
/// a busy database waits out `busy_timeout`; a deferred one that reads before
/// it writes fails at once (SQLITE_BUSY) if another connection committed in
/// between.
pub async fn write_tx(pool: &SqlitePool) -> sqlx::Result<sqlx::Transaction<'static, sqlx::Sqlite>> {
    pool.begin_with("BEGIN IMMEDIATE").await
}
