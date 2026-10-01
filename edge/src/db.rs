//! The SQLite pool. Python still owns the schema (`init_db` + `_migrate`), so
//! nothing here creates tables; it only connects with the same per-connection
//! settings `db/engine.py:_set_sqlite_pragmas` applies.

use std::path::Path;
use std::str::FromStr;
use std::time::Duration;

use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePool, SqlitePoolOptions, SqliteSynchronous};

pub fn pool(path: &Path) -> Result<SqlitePool, sqlx::Error> {
    let opts = SqliteConnectOptions::from_str(&format!("sqlite://{}", path.display()))?
        // On a fresh install the edge may boot before Python has created the
        // file; creating it empty is what Python would do a moment later.
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
