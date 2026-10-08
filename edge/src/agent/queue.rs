//! The agent loop's side of the durable job queue (a port of
//! `core/queue/sqlite.py`): claiming jobs, keeping their locks, ending them.

use serde_json::Value;
use sqlx::{Row, SqlitePool};

use crate::gql::codec::now_stored;

/// `Job.runtime` as every job is written now. Jobs Python queued before it
/// went have none, and are claimed all the same.
pub const EDGE: &str = "edge";

/// How long a claim holds before it must be renewed — the Python worker's TTL.
pub const LOCK_TTL: std::time::Duration = std::time::Duration::from_secs(300);

/// A claimed job, as `Job` in `core/queue/protocol.py`.
#[derive(Clone, Debug)]
pub struct Job {
    pub id: String,
    pub kind: String,
    pub payload: Value,
    /// As stored: the run's clock starts here.
    pub created_at: String,
    /// A stop reached it before the claim.
    pub cancel_requested: bool,
}

/// `now + ttl`, stored as SQLAlchemy stores a datetime.
fn stamp_in(ttl: std::time::Duration) -> String {
    (chrono::Utc::now() + ttl).format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

/// `_thread_free()`: no other job is running on this job's thread.
const THREAD_FREE: &str = "(jobs.thread_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs AS holder \
     WHERE holder.thread_id = jobs.thread_id AND holder.status = 'running' AND holder.id != jobs.id))";

/// `SqliteJobQueue._claim` for jobs of `kinds`: the oldest due one
/// whose thread is free, flipped to running under `worker` — or None. A
/// claim that loses the race (another writer took the job or its thread's
/// lease in between) is None too.
pub async fn claim(pool: &SqlitePool, kinds: &[&str], worker: &str) -> sqlx::Result<Option<Job>> {
    let now = now_stored();
    let marks = vec!["?"; kinds.len()].join(", ");
    let sql = format!(
        "SELECT id, kind, payload, created_at, cancel_requested FROM jobs \
         WHERE kind IN ({marks}) AND status = 'pending' AND run_at <= ? AND {THREAD_FREE} \
         ORDER BY run_at ASC LIMIT 1"
    );
    let mut select = sqlx::query(&sql);
    for kind in kinds {
        select = select.bind(*kind);
    }
    let Some(row) = select.bind(&now).fetch_optional(pool).await? else {
        return Ok(None);
    };
    let id: String = row.get("id");
    let updated = sqlx::query(&format!(
        "UPDATE jobs SET status = 'running', locked_by = ?, locked_until = ?, attempts = attempts + 1, \
         updated_at = ? WHERE id = ? AND status = 'pending' AND {THREAD_FREE}"
    ))
    .bind(worker)
    .bind(stamp_in(LOCK_TTL))
    .bind(&now)
    .bind(&id)
    .execute(pool)
    .await;
    match updated {
        Ok(r) if r.rows_affected() == 1 => {}
        Ok(_) => return Ok(None),
        // The thread's lease went to another writer: `ux_jobs_thread_lease`.
        Err(sqlx::Error::Database(e)) if e.is_unique_violation() => return Ok(None),
        Err(e) => return Err(e),
    }
    let payload: String = row.get("payload");
    Ok(Some(Job {
        id,
        kind: row.get("kind"),
        payload: serde_json::from_str(&payload).unwrap_or(Value::Null),
        created_at: row.get("created_at"),
        cancel_requested: row.get::<Option<bool>, _>("cancel_requested").unwrap_or(false),
    }))
}

/// `is_cancel_requested`.
pub async fn cancel_requested(pool: &SqlitePool, id: &str) -> sqlx::Result<bool> {
    let flag: Option<Option<bool>> =
        sqlx::query_scalar("SELECT cancel_requested FROM jobs WHERE id = ?").bind(id).fetch_optional(pool).await?;
    Ok(flag.flatten().unwrap_or(false))
}

/// `extend_lock`: false when the job is no longer ours to run.
pub async fn extend_lock(pool: &SqlitePool, id: &str, worker: &str) -> sqlx::Result<bool> {
    let r = sqlx::query(
        "UPDATE jobs SET locked_until = ?, updated_at = ? WHERE id = ? AND status = 'running' AND locked_by = ?",
    )
    .bind(stamp_in(LOCK_TTL))
    .bind(now_stored())
    .bind(id)
    .bind(worker)
    .execute(pool)
    .await?;
    Ok(r.rows_affected() == 1)
}

/// `complete`: the job is done, and its thread's lease free.
pub async fn complete(pool: &SqlitePool, id: &str, worker: &str) -> sqlx::Result<bool> {
    let now = now_stored();
    let r = sqlx::query(
        "UPDATE jobs SET status = 'done', completed_at = ?, locked_by = NULL, locked_until = NULL, updated_at = ? \
         WHERE id = ? AND status = 'running' AND locked_by = ?",
    )
    .bind(&now)
    .bind(&now)
    .bind(id)
    .bind(worker)
    .execute(pool)
    .await?;
    Ok(r.rows_affected() == 1)
}

/// `fail` with no retry: the job ends `error`, and its thread's lease is free.
pub async fn fail(pool: &SqlitePool, id: &str, worker: &str, error: &str) -> sqlx::Result<bool> {
    let now = now_stored();
    let r = sqlx::query(
        "UPDATE jobs SET status = 'error', last_error = ?, completed_at = ?, locked_by = NULL, locked_until = NULL, \
         updated_at = ? WHERE id = ? AND status = 'running' AND locked_by = ?",
    )
    .bind(error)
    .bind(&now)
    .bind(&now)
    .bind(id)
    .bind(worker)
    .execute(pool)
    .await?;
    Ok(r.rows_affected() == 1)
}

/// At start, jobs left `running` belong to a process that died: pending
/// again, to be claimed anew.
pub async fn recover(pool: &SqlitePool) -> sqlx::Result<u64> {
    let sql = "UPDATE jobs SET status = 'pending', locked_by = NULL, locked_until = NULL, updated_at = ? \
               WHERE status = 'running'";
    Ok(sqlx::query(sql).bind(now_stored()).execute(pool).await?.rows_affected())
}
