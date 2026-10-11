//! Archiving `fact` memories nobody uses — the edge's own (Python had none).
//!
//! An unused fact costs little in a chat's prompt (retrieval only injects
//! what clears its cutoff), but every live item is listed to the
//! consolidation sweep on every call, so the store can't grow forever. A
//! fact neither retrieved nor confirmed over the user's last
//! `ACTIVE_DAYS` active days is archived: out of retrieval, the prompt and
//! the sweep, kept in the table, restored by `restoreMemory` or by being
//! restated (`retrieve::upsert_memory`).
//!
//! Time is counted in active days — days the user wrote to the assistant
//! themselves (not an automation's or a board task's turn) — so months away
//! from the app age nothing. Never archived: `core` items (always in the
//! prompt, so "retrieved" says nothing) and `manual` ones (the user's own).
//!
//! Guards: nothing below `MIN_LIVE_FACTS` (a small store costs nothing);
//! nothing when retrieval logged too few of the recent active days (a broken
//! embedder would make every fact look unused); at most `cap` a pass.

use sqlx::SqlitePool;

/// How long a fact may go unused, in the user's active days.
pub const ACTIVE_DAYS: i64 = 90;
/// Fewer live facts than this and nothing is archived.
const MIN_LIVE_FACTS: i64 = 30;
/// Retrieval must have logged at least this share of the active days the
/// access log still covers.
const MIN_RETRIEVAL_SHARE: f64 = 1.0 / 3.0;
/// The access log's reach (`schedule.rs` prunes it past this).
pub const LOG_DAYS: i64 = 90;

/// A user message in a conversation the user had themselves.
const USER_TURNS: &str = "FROM messages m JOIN conversations c ON c.id = m.conversation_id \
     WHERE m.role = 'user' AND c.surface NOT IN ('automation', 'task')";

/// One pass: what was archived, or why nothing was.
pub async fn archive_stale(pool: &SqlitePool) -> Result<String, String> {
    let db = |e: sqlx::Error| e.to_string();

    let (live,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM memories WHERE kind = 'fact' AND archived_at IS NULL")
            .fetch_one(pool)
            .await
            .map_err(db)?;
    if live < MIN_LIVE_FACTS {
        return Ok(format!("skipped: {live} live facts, under {MIN_LIVE_FACTS}"));
    }

    // The day `ACTIVE_DAYS` active days back: anything last used before it
    // has gone that long unused.
    let cutoff: Option<String> = sqlx::query_scalar(&format!(
        "SELECT d FROM (SELECT DISTINCT date(m.created_at) AS d {USER_TURNS}) ORDER BY d DESC LIMIT 1 OFFSET ?"
    ))
    .bind(ACTIVE_DAYS - 1)
    .fetch_optional(pool)
    .await
    .map_err(db)?;
    let Some(cutoff) = cutoff else {
        return Ok(format!("skipped: fewer than {ACTIVE_DAYS} active days"));
    };

    let log_since = (chrono::Utc::now() - chrono::Duration::days(LOG_DAYS)).format("%Y-%m-%d").to_string();
    let (active,): (i64,) =
        sqlx::query_as(&format!("SELECT COUNT(DISTINCT date(m.created_at)) {USER_TURNS} AND m.created_at >= ?"))
            .bind(&log_since)
            .fetch_one(pool)
            .await
            .map_err(db)?;
    let (retrieving,): (i64,) = sqlx::query_as(
        "SELECT COUNT(DISTINCT date(accessed_at)) FROM memory_activities WHERE source = 'retrieval' AND accessed_at >= ?",
    )
    .bind(&log_since)
    .fetch_one(pool)
    .await
    .map_err(db)?;
    if !retrieval_healthy(active, retrieving) {
        return Ok(format!("skipped: retrieval logged {retrieving} of the last {active} active days"));
    }

    let cap = cap(live);
    let stale: Vec<(String, String)> = sqlx::query_as(
        "SELECT id, text FROM memories WHERE kind = 'fact' AND archived_at IS NULL AND COALESCE(source, '') != 'manual' \
         AND max(created_at, COALESCE(last_confirmed_at, ''), COALESCE(last_retrieved_at, '')) < ? \
         ORDER BY max(created_at, COALESCE(last_confirmed_at, ''), COALESCE(last_retrieved_at, '')) ASC LIMIT ?",
    )
    .bind(&cutoff)
    .bind(cap)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    if stale.is_empty() {
        return Ok(format!("nothing unused since {cutoff}"));
    }
    let now = crate::gql::codec::now_stored();
    for (id, text) in &stale {
        sqlx::query("UPDATE memories SET archived_at = ? WHERE id = ?").bind(&now).bind(id).execute(pool).await.map_err(db)?;
        tracing::info!("memory: archived {id} (unused since {cutoff}): {text}");
    }
    Ok(format!("archived {} of {live} facts, unused since {cutoff}", stale.len()))
}

/// Retrieval logged enough of the active days the log covers. No active
/// days there at all says nothing either way: the window decides alone.
fn retrieval_healthy(active: i64, retrieving: i64) -> bool {
    active == 0 || retrieving as f64 >= MIN_RETRIEVAL_SHARE * active as f64
}

/// At most this many a pass: a tenth of the live facts, at least 5.
fn cap(live: i64) -> i64 {
    (live / 10).max(5)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn guards() {
        assert!(retrieval_healthy(0, 0));
        assert!(retrieval_healthy(30, 10));
        assert!(!retrieval_healthy(30, 9));
        assert_eq!(cap(30), 5);
        assert_eq!(cap(400), 40);
    }
}
