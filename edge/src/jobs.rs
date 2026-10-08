//! Writing to the durable job queue; the agent loop (`agent/queue.rs`)
//! claims and runs what's written here.

use serde_json::Value;

use crate::gql::codec::now_stored;
use crate::pyjson;

/// `SqliteJobQueue.enqueue(kind, payload, job_id=id, thread_id=thread)`.
/// Returns the row's `created_at`, which the run's clock starts from.
/// `thread` is the transcript thread the job shares with others, whose lease
/// it holds while running (`Job.thread_id`).
pub async fn insert(
    executor: impl sqlx::SqliteExecutor<'_>,
    id: &str,
    kind: &str,
    payload: &Value,
    thread: Option<&str>,
) -> sqlx::Result<String> {
    let now = now_stored();
    sqlx::query(
        "INSERT INTO jobs (id, kind, payload, status, run_at, attempts, max_attempts, last_error, locked_by, \
         locked_until, cancel_requested, created_at, updated_at, completed_at, thread_id) \
         VALUES (?, ?, ?, 'pending', ?, 0, 3, NULL, NULL, NULL, 0, ?, ?, NULL, ?)",
    )
    .bind(id)
    .bind(kind)
    .bind(pyjson::dumps(payload))
    .bind(&now)
    .bind(&now)
    .bind(&now)
    .bind(thread)
    .execute(executor)
    .await?;
    Ok(now)
}
