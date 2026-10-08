//! What a start finds a crash left behind, cleaned up once after
//! `queue::recover` — ports of what Python ran at its start:
//! `db/ops.py:cleanup_zombie_running_rows`, `core/approvals.py:reconcile_startup`
//! and `db/ops.py:sweep_ephemeral_conversations`.
//!
//! A run row is a zombie when no live job — pending or running — stands
//! behind it: after `recover` the interrupted jobs are pending again and
//! will be re-claimed. A gate's or a paused node's request is always gone:
//! its waiter was in the process that died, and a re-claimed run asks again.

use sqlx::SqlitePool;

use crate::gql::codec::now_stored;
use crate::kernels::Kernels;

/// Jobs a run row may still be finished by.
const LIVE: &str = "SELECT id FROM jobs WHERE status IN ('pending', 'running')";

/// Everything, in Python's order; each pass logs its own failure.
pub async fn run(pool: &SqlitePool, kernels: &Kernels, artifacts_dir: &std::path::Path) {
    match zombies(pool).await {
        Ok(n) if n.iter().any(|(_, c)| *c > 0) => tracing::info!("startup zombie sweep: {n:?}"),
        Ok(_) => {}
        Err(e) => tracing::warn!("startup zombie sweep failed: {e}"),
    }
    match approvals(pool).await {
        Ok(0) => {}
        Ok(n) => tracing::info!("approvals reconciled: {n} expired"),
        Err(e) => tracing::warn!("reconciling approvals failed: {e}"),
    }
    match ephemeral(pool, kernels, artifacts_dir).await {
        Ok(0) => {}
        Ok(n) => tracing::info!("startup incognito sweep: reaped {n} conversation(s)"),
        Err(e) => tracing::warn!("incognito sweep failed: {e}"),
    }
}

/// `cleanup_zombie_running_rows`, for the run rows: a message, automation
/// run or workflow run still `running` with no live job is an error; a board
/// task `running` with none goes back to `ready` for a fresh dispatch.
async fn zombies(pool: &SqlitePool) -> sqlx::Result<Vec<(&'static str, u64)>> {
    let now = now_stored();
    let mut tx = crate::db::write_tx(pool).await?;
    let messages = sqlx::query(&format!("UPDATE messages SET status = 'error' WHERE status = 'running' AND id NOT IN ({LIVE})"))
        .execute(&mut *tx)
        .await?
        .rows_affected();
    let mut runs = [0; 2];
    for (n, table) in runs.iter_mut().zip(["automation_runs", "workflow_runs"]) {
        *n = sqlx::query(&format!(
            "UPDATE {table} SET status = 'error', error = 'interrupted by server restart', finished_at = ? \
             WHERE status = 'running' AND id NOT IN ({LIVE})"
        ))
        .bind(&now)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    }
    let [automation_runs, workflow_runs] = runs;
    let board_tasks = sqlx::query(&format!(
        "UPDATE board_tasks SET status = 'ready', job_id = NULL, updated_at = ? \
         WHERE status = 'running' AND (job_id IS NULL OR job_id NOT IN ({LIVE}))"
    ))
    .bind(&now)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    tx.commit().await?;
    Ok(vec![
        ("messages", messages),
        ("automation_runs", automation_runs),
        ("workflow_runs", workflow_runs),
        ("board_tasks", board_tasks),
    ])
}

/// `reconcile_startup`: a pending request whose waiter died with the last
/// process — a tool gate, a paused workflow node — can't be answered any
/// more. A deferred action waits for nobody and a board task's block is a
/// column on the task, so both stay.
async fn approvals(pool: &SqlitePool) -> sqlx::Result<u64> {
    let now = now_stored();
    let expired = sqlx::query(
        "UPDATE approvals SET status = 'expired', result = 'The run was lost when the server restarted.', \
         resolved_at = ?, updated_at = ? WHERE status = 'pending' AND action IS NULL AND board_task_id IS NULL",
    )
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await?;
    Ok(expired.rows_affected())
}

/// `sweep_ephemeral_conversations`: incognito conversations a crash, or a
/// client that never fired `discardConversation`, left behind — except one
/// a pending or running chat job still belongs to.
async fn ephemeral(pool: &SqlitePool, kernels: &Kernels, artifacts_dir: &std::path::Path) -> sqlx::Result<u64> {
    let ids: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM conversations WHERE ephemeral = 1 AND id NOT IN \
         (SELECT conversation_id FROM messages WHERE conversation_id IS NOT NULL AND id IN \
         (SELECT id FROM jobs WHERE kind = 'chat' AND status IN ('pending', 'running')))",
    )
    .fetch_all(pool)
    .await?;
    let mut swept = 0;
    for id in ids {
        let mut tx = crate::db::write_tx(pool).await?;
        let teardown = crate::gql::conversation::delete_conversation(&mut tx, &id, artifacts_dir).await?;
        tx.commit().await?;
        if let Some(t) = teardown {
            t.finish(kernels).await;
        }
        swept += 1;
    }
    Ok(swept)
}
