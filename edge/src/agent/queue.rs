//! The edge's side of the durable job queue (`core/queue/sqlite.py`): claiming
//! the jobs whose `runtime` is the edge's, keeping their locks, and handing
//! them to Python. Every statement is the one `SqliteJobQueue` runs, plus the
//! `runtime` condition that keeps the two sides off each other's jobs.

use serde_json::Value;
use sqlx::{Row, SqlitePool};

use crate::gql::codec::now_stored;
use crate::pyjson;

/// `Job.runtime` of a job the edge's agent loop claims (`db/models.py:EDGE_RUNTIME`).
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
}

/// `now + ttl`, stored as SQLAlchemy stores a datetime.
fn stamp_in(ttl: std::time::Duration) -> String {
    (chrono::Utc::now() + ttl).format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

/// `_thread_free()`: no other job is running on this job's thread.
const THREAD_FREE: &str = "(jobs.thread_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs AS holder \
     WHERE holder.thread_id = jobs.thread_id AND holder.status = 'running' AND holder.id != jobs.id))";

/// `SqliteJobQueue._claim` for the edge's jobs of `kinds`: the oldest due one
/// whose thread is free, flipped to running under `worker` — or None. A
/// claim that loses the race (another writer took the job or its thread's
/// lease in between) is None too.
pub async fn claim(pool: &SqlitePool, kinds: &[&str], worker: &str) -> sqlx::Result<Option<Job>> {
    let now = now_stored();
    let marks = vec!["?"; kinds.len()].join(", ");
    let sql = format!(
        "SELECT id, kind, payload, created_at FROM jobs \
         WHERE kind IN ({marks}) AND status = 'pending' AND run_at <= ? AND runtime = ? AND {THREAD_FREE} \
         ORDER BY run_at ASC LIMIT 1"
    );
    let mut select = sqlx::query(&sql);
    for kind in kinds {
        select = select.bind(*kind);
    }
    let Some(row) = select.bind(&now).bind(EDGE).fetch_optional(pool).await? else {
        return Ok(None);
    };
    let id: String = row.get("id");
    let updated = sqlx::query(&format!(
        "UPDATE jobs SET status = 'running', locked_by = ?, locked_until = ?, attempts = attempts + 1, \
         updated_at = ? WHERE id = ? AND status = 'pending' AND runtime = ? AND {THREAD_FREE}"
    ))
    .bind(worker)
    .bind(stamp_in(LOCK_TTL))
    .bind(&now)
    .bind(&id)
    .bind(EDGE)
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
    }))
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

/// Hand the job to Python: pending again, `runtime` cleared, so a Python
/// worker claims it next. `handoff` — what the turn carried, when the edge
/// had started it — goes into the payload for `chat_job_handler`; without it
/// the job is Python's to run from the start. False if the job wasn't ours.
pub async fn release(pool: &SqlitePool, job: &Job, worker: &str, handoff: Option<Value>) -> sqlx::Result<bool> {
    let mut payload = job.payload.clone();
    if let (Some(handoff), Value::Object(map)) = (handoff, &mut payload) {
        map.insert("handoff".into(), handoff);
    }
    let r = sqlx::query(
        "UPDATE jobs SET status = 'pending', runtime = NULL, locked_by = NULL, locked_until = NULL, payload = ?, \
         updated_at = ? WHERE id = ? AND status = 'running' AND locked_by = ?",
    )
    .bind(pyjson::dumps(&payload))
    .bind(now_stored())
    .bind(&job.id)
    .bind(worker)
    .execute(pool)
    .await?;
    Ok(r.rows_affected() == 1)
}

/// At the edge's start, its jobs left `running` belong to the edge that
/// just died: pending again, to be claimed anew — the zombie sweep Python
/// runs for its own. With the agent loop off, every live edge job goes to
/// Python instead, which would otherwise never see it.
pub async fn recover(pool: &SqlitePool, serving: bool) -> sqlx::Result<u64> {
    let sql = if serving {
        "UPDATE jobs SET status = 'pending', locked_by = NULL, locked_until = NULL, updated_at = ? \
         WHERE runtime = ? AND status = 'running'"
    } else {
        "UPDATE jobs SET status = 'pending', runtime = NULL, locked_by = NULL, locked_until = NULL, updated_at = ? \
         WHERE runtime = ? AND status IN ('pending', 'running')"
    };
    Ok(sqlx::query(sql).bind(now_stored()).bind(EDGE).execute(pool).await?.rows_affected())
}
