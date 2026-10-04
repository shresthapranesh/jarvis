//! Writing to the durable job queue (`core/queue/sqlite.py`). The edge
//! enqueues; a Python worker claims and runs. Every row is one Python's
//! `SqliteJobQueue.enqueue` would have written.

use serde_json::Value;

use crate::gql::codec::now_stored;
use crate::pyjson;

/// `SqliteJobQueue.enqueue(kind, payload, job_id=id, thread_id=thread)`.
/// Returns the row's `created_at`, which the worker starts the run's clock
/// from. `thread` is the transcript thread the job shares with others, whose
/// lease it holds while running (`Job.thread_id`). `edge` makes it a job
/// for the edge's own agent loop (`Job.runtime`, `agent/`), which Python
/// never claims.
pub async fn insert(
    executor: impl sqlx::SqliteExecutor<'_>,
    id: &str,
    kind: &str,
    payload: &Value,
    thread: Option<&str>,
    edge: bool,
) -> sqlx::Result<String> {
    let now = now_stored();
    sqlx::query(
        "INSERT INTO jobs (id, kind, payload, status, run_at, attempts, max_attempts, last_error, locked_by, \
         locked_until, cancel_requested, created_at, updated_at, completed_at, thread_id, runtime) \
         VALUES (?, ?, ?, 'pending', ?, 0, 3, NULL, NULL, NULL, 0, ?, ?, NULL, ?, ?)",
    )
    .bind(id)
    .bind(kind)
    .bind(pyjson::dumps(payload))
    .bind(&now)
    .bind(&now)
    .bind(&now)
    .bind(thread)
    .bind(edge.then_some(crate::agent::EDGE_RUNTIME))
    .execute(executor)
    .await?;
    Ok(now)
}
