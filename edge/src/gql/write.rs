//! Shared write helpers for the edge's mutations.
//!
//! Python is still a writer on the same file, so every write here has to
//! leave a row exactly as SQLAlchemy would: ids from `uuid4()`, timestamps in
//! its stored text form, and `updated_at` bumped by hand wherever an
//! `onupdate=_now` column would have bumped it.

use sqlx::SqlitePool;

use super::codec::now_stored;

/// `UPDATE {table} SET col = ?, …, updated_at = now WHERE id = ?`. Column
/// names come from code, never from input.
pub async fn update_row(pool: &SqlitePool, table: &str, id: &str, sets: &[(&str, String)]) -> sqlx::Result<u64> {
    let mut sql = format!("UPDATE {table} SET ");
    for (col, _) in sets {
        sql.push_str(col);
        sql.push_str(" = ?, ");
    }
    sql.push_str("updated_at = ? WHERE id = ?");
    let mut q = sqlx::query(&sql);
    for (_, value) in sets {
        q = q.bind(value);
    }
    Ok(q.bind(now_stored()).bind(id).execute(pool).await?.rows_affected())
}
