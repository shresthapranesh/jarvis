//! Live runs: the four subscriptions, `runningTasks`, and the stop mutations —
//! `server/graphql/subscriptions/*`, `queries/task_run.py`,
//! `mutations/task_run.py` and the per-kind stops.
//!
//! All of it reads the run mirror (`runs.rs`), which is only current while a
//! worker is linked, so the router hands these to the edge only then
//! (`router.rs`); with no worker linked they go to Python as before.

use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use async_graphql::{Context, Json, Object, Result, SimpleObject, Subscription};
use futures_util::Stream;
use serde_json::json;
use sqlx::SqlitePool;

use super::codec::{DateTime, now_stored};
use super::events::{
    self, AutomationDoneEvent, AutomationEvent, ChatEvent, DoneEvent, ErrorEvent, WorkflowDoneEvent,
    WorkflowErrorEvent, WorkflowEvent,
};
use crate::runs::{Registry, Run};

/// How long a subscription for an unknown run waits for the worker to report
/// it. The mutation that handed out the id answers over HTTP while the worker
/// reports the run over the link; the two can arrive in either order.
const REGISTRATION_GRACE: Duration = Duration::from_secs(2);

type Events<T> = Pin<Box<dyn Stream<Item = Result<T>> + Send>>;

/// A terminal event from the database for a run that isn't live, and whether
/// the row still claimed the run was in progress — the one case worth
/// waiting out `REGISTRATION_GRACE` for.
type Fallback<T> = (T, bool);

/// Stream a run: wait briefly for it if needed, replay its events from the
/// first, follow it until `done`, and fall back to the DB if it never shows
/// up or disappears unfinished.
fn follow<T, F, Fut>(
    registry: Arc<Registry>,
    pool: SqlitePool,
    id: String,
    coerce: fn(&serde_json::Value) -> Result<Option<T>>,
    fallback: F,
) -> Events<T>
where
    T: Send + 'static,
    F: Fn(SqlitePool, String) -> Fut + Send + 'static,
    Fut: std::future::Future<Output = Result<Fallback<T>>> + Send,
{
    Box::pin(async_stream::stream! {
        let run = match registry.get(&id) {
            Some(run) => run,
            None => {
                let (event, in_progress) = match fallback(pool.clone(), id.clone()).await {
                    Ok(f) => f,
                    Err(e) => { yield Err(e); return; }
                };
                match if in_progress { wait_for(&registry, &id).await } else { None } {
                    Some(run) => run,
                    None => { yield Ok(event); return; }
                }
            }
        };

        let mut version = run.subscribe();
        let mut cursor = 0;
        loop {
            version.borrow_and_update();
            let (batch, done, gone) = {
                let st = run.state.lock().expect("run state lock");
                (st.events[cursor.min(st.events.len())..].to_vec(), st.fields.done, st.gone)
            };
            cursor += batch.len();
            for raw in &batch {
                match coerce(raw) {
                    Ok(Some(event)) => yield Ok(event),
                    Ok(None) => {}
                    Err(e) => yield Err(e),
                }
            }
            if done {
                return;
            }
            if gone || version.changed().await.is_err() {
                match fallback(pool.clone(), id.clone()).await {
                    Ok((event, _)) => yield Ok(event),
                    Err(e) => yield Err(e),
                }
                return;
            }
        }
    })
}

async fn wait_for(registry: &Registry, id: &str) -> Option<Arc<Run>> {
    let mut registered = registry.on_registration();
    tokio::time::timeout(REGISTRATION_GRACE, async {
        loop {
            registered.borrow_and_update();
            if let Some(run) = registry.get(id) {
                return Some(run);
            }
            if registered.changed().await.is_err() {
                return None;
            }
        }
    })
    .await
    .ok()
    .flatten()
}

// ── DB fallbacks — the Python resolvers' `if run_id not in _tasks` branches ──

async fn chat_fallback(pool: SqlitePool, id: String) -> Result<Fallback<ChatEvent>> {
    let row: Option<(String, String, String)> =
        sqlx::query_as("SELECT status, content, conversation_id FROM messages WHERE id = ?")
            .bind(&id)
            .fetch_optional(&pool)
            .await?;
    Ok(match row {
        None => (ChatEvent::ErrorEvent(ErrorEvent { error: "task not found".into() }), false),
        Some((status, message, conversation_id)) if status == "done" => {
            (ChatEvent::DoneEvent(DoneEvent { message, conversation_id }), false)
        }
        Some(_) => (ChatEvent::ErrorEvent(ErrorEvent { error: "task interrupted (server restarted)".into() }), true),
    })
}

async fn automation_fallback(pool: SqlitePool, id: String) -> Result<Fallback<AutomationEvent>> {
    let row: Option<(String, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT status, output, error FROM automation_runs WHERE id = ?")
            .bind(&id)
            .fetch_optional(&pool)
            .await?;
    Ok(match row {
        None => (AutomationEvent::ErrorEvent(ErrorEvent { error: "run not found".into() }), false),
        Some((status, output, _)) if status == "done" => {
            (AutomationEvent::AutomationDoneEvent(AutomationDoneEvent { output, run_id: id }), false)
        }
        Some((status, _, error)) if status == "error" => (
            AutomationEvent::ErrorEvent(ErrorEvent { error: error.filter(|e| !e.is_empty()).unwrap_or("unknown error".into()) }),
            false,
        ),
        Some(_) => (AutomationEvent::ErrorEvent(ErrorEvent { error: "run interrupted (server restarted)".into() }), true),
    })
}

async fn board_fallback(pool: SqlitePool, id: String) -> Result<Fallback<AutomationEvent>> {
    let row: Option<(String, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT status, summary, blocked_reason FROM board_tasks WHERE job_id = ? LIMIT 1")
            .bind(&id)
            .fetch_optional(&pool)
            .await?;
    Ok(match row {
        None => (AutomationEvent::ErrorEvent(ErrorEvent { error: "run not found".into() }), false),
        Some((status, summary, _)) if status == "done" => {
            (AutomationEvent::AutomationDoneEvent(AutomationDoneEvent { output: summary, run_id: id }), false)
        }
        Some((status, _, reason)) if status == "blocked" => (
            AutomationEvent::ErrorEvent(ErrorEvent { error: reason.filter(|r| !r.is_empty()).unwrap_or("blocked".into()) }),
            false,
        ),
        Some(_) => (AutomationEvent::ErrorEvent(ErrorEvent { error: "run interrupted (server restarted)".into() }), true),
    })
}

async fn workflow_fallback(pool: SqlitePool, id: String) -> Result<Fallback<WorkflowEvent>> {
    let row: Option<(String, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT status, outputs, error FROM workflow_runs WHERE id = ?")
            .bind(&id)
            .fetch_optional(&pool)
            .await?;
    let error = |error: &str, run_id: String| WorkflowEvent::WorkflowErrorEvent(WorkflowErrorEvent { error: error.into(), run_id });
    Ok(match row {
        None => (error("run not found", id), false),
        Some((status, outputs, _)) if status == "done" => {
            // json.loads(run.outputs or "{}")
            let text = outputs.filter(|o| !o.is_empty()).unwrap_or("{}".into());
            let outputs = serde_json::from_str(&text)?;
            (WorkflowEvent::WorkflowDoneEvent(WorkflowDoneEvent { outputs: Json(outputs), run_id: id }), false)
        }
        Some((status, _, err)) if status == "error" => {
            (error(err.as_deref().filter(|e| !e.is_empty()).unwrap_or("unknown error"), id), false)
        }
        Some(_) => (error("run interrupted (server restarted)", id), true),
    })
}

#[derive(Default)]
pub struct RunSubscription;

#[Subscription(name = "Subscription")]
impl RunSubscription {
    /// Stream events for a chat task.
    async fn task_events(&self, ctx: &Context<'_>, task_id: String) -> Result<Events<ChatEvent>> {
        Ok(follow(ctx.data::<Arc<Registry>>()?.clone(), ctx.data::<SqlitePool>()?.clone(), task_id, events::chat, chat_fallback))
    }

    async fn automation_run_events(&self, ctx: &Context<'_>, run_id: String) -> Result<Events<AutomationEvent>> {
        Ok(follow(
            ctx.data::<Arc<Registry>>()?.clone(),
            ctx.data::<SqlitePool>()?.clone(),
            run_id,
            events::automation,
            automation_fallback,
        ))
    }

    /// Live events for one board-task run (run_id == BoardTask.runId).
    async fn board_task_events(&self, ctx: &Context<'_>, run_id: String) -> Result<Events<AutomationEvent>> {
        Ok(follow(ctx.data::<Arc<Registry>>()?.clone(), ctx.data::<SqlitePool>()?.clone(), run_id, events::automation, board_fallback))
    }

    async fn workflow_run_events(&self, ctx: &Context<'_>, run_id: String) -> Result<Events<WorkflowEvent>> {
        Ok(follow(
            ctx.data::<Arc<Registry>>()?.clone(),
            ctx.data::<SqlitePool>()?.clone(),
            run_id,
            events::workflow,
            workflow_fallback,
        ))
    }
}

// ── the registry, read and controlled ────────────────────────────────────────

#[derive(SimpleObject)]
pub struct RunningTask {
    pub id: String,
    pub kind: String,
    pub label: String,
    pub parent_id: Option<String>,
    pub started_at: DateTime,
    pub has_interrupt: bool,
    pub cancelled: bool,
    pub done: bool,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub total_tokens: i64,
    pub llm_calls: i64,
    pub tool_calls: i64,
    pub budget_exceeded: bool,
    pub budget_reason: Option<String>,
}

#[derive(SimpleObject)]
pub struct StopRunningTaskPayload {
    pub ok: bool,
    pub task_id: String,
    pub kind: String,
}

#[derive(Default)]
pub struct RunQuery;

#[Object]
impl RunQuery {
    /// All currently-tracked in-flight tasks, newest first.
    async fn running_tasks(&self, ctx: &Context<'_>) -> Result<Vec<RunningTask>> {
        let mut rows: Vec<(Option<chrono::DateTime<chrono::FixedOffset>>, RunningTask)> = ctx
            .data::<Arc<Registry>>()?
            .all()
            .into_iter()
            .map(|run| {
                let f = run.fields();
                let started = chrono::DateTime::parse_from_rfc3339(&run.meta.started_at).ok();
                (
                    started,
                    RunningTask {
                        id: run.id.clone(),
                        kind: run.meta.kind.clone(),
                        label: run.meta.label.clone(),
                        parent_id: run.meta.parent_id.clone(),
                        started_at: DateTime(run.meta.started_at.clone()),
                        has_interrupt: f.has_interrupt,
                        cancelled: f.cancelled,
                        done: f.done,
                        input_tokens: f.input_tokens,
                        output_tokens: f.output_tokens,
                        total_tokens: f.total_tokens,
                        llm_calls: f.llm_calls,
                        tool_calls: f.tool_calls,
                        budget_exceeded: f.budget_exceeded,
                        budget_reason: f.budget_reason,
                    },
                )
            })
            .collect();
        // Stable, so equal start times keep registration order — as Python's
        // `sort(reverse=True)` does.
        rows.sort_by(|a, b| b.0.cmp(&a.0));
        Ok(rows.into_iter().map(|(_, r)| r).collect())
    }
}

/// The checks every stop shares, then the in-process half: tell the worker,
/// and mirror the flag at once so `runningTasks` doesn't show it un-cancelled
/// until the worker's state report comes back.
fn stop(registry: &Registry, id: &str, missing: &'static str, finished: &'static str, resume: bool) -> Result<Arc<Run>> {
    let run = registry.get(id).ok_or(missing)?;
    if run.fields().done {
        return Err(finished.into());
    }
    registry.control(&json!({"type": "cancel", "task_id": id, "resume": resume}));
    run.update(|st| st.fields.cancelled = true);
    Ok(run)
}

/// `SqliteJobQueue.cancel`: a pending job won't be claimed; a running one is
/// asked to stop, which its handler polls for. Durable, so it reaches the
/// worker even if the link message didn't.
async fn cancel_job(pool: &SqlitePool, job_id: &str) -> Result<()> {
    let now = now_stored();
    sqlx::query("UPDATE jobs SET status = 'cancelled', completed_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'")
        .bind(&now)
        .bind(&now)
        .bind(job_id)
        .execute(pool)
        .await?;
    sqlx::query("UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status = 'running'")
        .bind(&now)
        .bind(job_id)
        .execute(pool)
        .await?;
    Ok(())
}

#[derive(Default)]
pub struct RunMutation;

#[Object]
impl RunMutation {
    async fn stop_running_task(&self, ctx: &Context<'_>, task_id: String) -> Result<StopRunningTaskPayload> {
        let run = stop(ctx.data::<Arc<Registry>>()?, &task_id, "task not found or already finished", "task already finished", true)?;
        cancel_job(ctx.data()?, &task_id).await?;
        Ok(StopRunningTaskPayload { ok: true, kind: run.meta.kind.clone(), task_id })
    }

    // The chat stop is in-process only in Python — no queue cancel — and so
    // is this one.
    async fn stop_task(&self, ctx: &Context<'_>, task_id: String) -> Result<bool> {
        stop(ctx.data::<Arc<Registry>>()?, &task_id, "task not found or already finished", "task already finished", true)?;
        Ok(true)
    }

    async fn stop_automation_run(&self, ctx: &Context<'_>, run_id: String) -> Result<bool> {
        stop(ctx.data::<Arc<Registry>>()?, &run_id, "run not found or already finished", "run already finished", false)?;
        cancel_job(ctx.data()?, &run_id).await?;
        Ok(true)
    }

    async fn stop_workflow_run(&self, ctx: &Context<'_>, run_id: String) -> Result<bool> {
        stop(ctx.data::<Arc<Registry>>()?, &run_id, "run not found or already finished", "run already finished", false)?;
        cancel_job(ctx.data()?, &run_id).await?;
        Ok(true)
    }
}
