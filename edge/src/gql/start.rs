//! Starting runs, and steering live ones — `startTask`, `runWorkflow` and
//! `triggerAutomation` (`server/*_runtime.py:register_*`), and the mutations
//! that act on a run's in-memory state: queueing a message onto it, answering
//! a workflow's interrupt.
//!
//! A trigger is only rows: the domain row the run reports into, a `jobs` row
//! for the agent loop to claim, and the run registered (`runs.rs`) so a
//! subscriber that gets the id back finds it. Then the loop is woken.

use std::sync::Arc;

use async_graphql::{Context, ID, InputObject, Json, Object, Result, SimpleObject};
use serde_json::{Value, json};
use sqlx::{Sqlite, SqlitePool, Transaction};

use super::codec::{decode_global_id, iso_from_db, new_id, now_stored};
use crate::catalog;
use crate::pyjson;
use crate::runs::{Meta, Registry, Run};

#[derive(InputObject)]
pub struct StartTaskInput {
    query: String,
    model: Option<String>,
    conversation_id: Option<String>,
    project_id: Option<String>,
    #[graphql(default)]
    ephemeral: bool,
}

#[derive(SimpleObject)]
pub struct StartTaskPayload {
    task_id: String,
    conversation_id: String,
    queued: bool,
    queued_message_id: Option<String>,
}

#[derive(SimpleObject)]
pub struct QueueMessagePayload {
    message_id: String,
    position: i64,
}

/// Python's `s[:60]`.
pub fn first_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `get_or_create_conversation`: an existing conversation takes the run's
/// model (it is sticky per conversation); a missing one is created, under the
/// requested id if there was one.
pub(crate) async fn conversation_for(
    tx: &mut Transaction<'_, Sqlite>,
    requested: Option<&str>,
    model: &str,
    title: Option<String>,
    surface: &str,
    project_id: Option<&str>,
    ephemeral: bool,
) -> sqlx::Result<String> {
    if let Some(id) = requested {
        let current: Option<String> =
            sqlx::query_scalar("SELECT model FROM conversations WHERE id = ?").bind(id).fetch_optional(&mut **tx).await?;
        if let Some(current) = current {
            if current != model {
                sqlx::query("UPDATE conversations SET model = ? WHERE id = ?").bind(model).bind(id).execute(&mut **tx).await?;
            }
            return Ok(id.to_string());
        }
    }
    let id = requested.map_or_else(new_id, str::to_string);
    sqlx::query(
        "INSERT INTO conversations (id, title, model, created_at, surface, pinned, project_id, ephemeral) \
         VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
    )
    .bind(&id)
    .bind(title)
    .bind(model)
    .bind(now_stored())
    .bind(surface)
    .bind(project_id)
    .bind(ephemeral)
    .execute(&mut **tx)
    .await?;
    Ok(id)
}

pub(crate) async fn insert_message(
    executor: impl sqlx::SqliteExecutor<'_>,
    conversation_id: &str,
    role: &str,
    content: &str,
    model: Option<&str>,
    status: &str,
) -> sqlx::Result<String> {
    let id = new_id();
    sqlx::query(
        "INSERT INTO messages (id, conversation_id, role, content, model, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(&id)
    .bind(conversation_id)
    .bind(role)
    .bind(content)
    .bind(model)
    .bind(now_stored())
    .bind(status)
    .execute(executor)
    .await?;
    Ok(id)
}

/// Register the run, then commit its rows and wake the agent loop.
/// Registered first: once the job is visible the loop may claim it, and must
/// find the run its subscribers watch.
async fn commit_run(
    registry: &Registry,
    tx: Transaction<'_, Sqlite>,
    id: &str,
    kind: &str,
    label: String,
    parent_id: &str,
    enqueued_at: &str,
) -> Result<()> {
    let meta = Meta {
        kind: kind.into(),
        label,
        parent_id: Some(parent_id.into()),
        // The run's clock starts at the job's `created_at`.
        started_at: iso_from_db(enqueued_at).utc().0,
    };
    registry.pre_register(id, meta);
    if let Err(e) = tx.commit().await {
        registry.discard_pending(id);
        return Err(e.into());
    }
    tracing::info!("{kind} run {id} enqueued for {parent_id}");
    registry.wake();
    Ok(())
}

/// Queue `query` onto a live chat run — `queue_chat_message`: a `queued`
/// row the turn takes in before its next model call.
async fn queue_onto(pool: &SqlitePool, run: &Arc<Run>, query: &str) -> Result<(String, i64)> {
    let text = query.trim();
    if text.is_empty() {
        return Err("empty message".into());
    }
    if run.fields().done {
        return Err("task not found or already finished".into());
    }
    let Some(conversation_id) = run.meta.parent_id.as_deref() else {
        return Err("task is not attached to a conversation".into());
    };
    let message_id = insert_message(pool, conversation_id, "user", text, None, "queued").await?;
    let position: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user' AND status = 'queued'",
    )
    .bind(conversation_id)
    .fetch_one(pool)
    .await?;
    run.emit_local("queued_message", &json!({"message_id": message_id, "text": text, "position": position}));
    Ok((message_id, position))
}

/// A chat turn to start: what `register_chat_task` takes from `startTask`,
/// and what the bots' `_dispatch` takes from a chat message.
pub struct ChatTurn {
    pub query: String,
    /// Already resolved (`catalog::resolve_model`).
    pub model: String,
    pub conversation_id: Option<String>,
    /// The title a conversation created here gets.
    pub title: Option<String>,
    pub surface: &'static str,
    /// The user message as stored. `None` stores the query, as `startTask`
    /// does.
    pub display: Option<String>,
    pub project_id: Option<String>,
    pub ephemeral: bool,
}

/// What a chat turn turned into.
pub enum Dispatched {
    Started { task_id: String, conversation_id: String },
    /// The conversation had a run going; the message joined it.
    Queued { task_id: String, conversation_id: String, message_id: String },
    /// The conversation had a run going and the message couldn't join it —
    /// `route_to_live_run`'s `ValueError`, which the bots send back as is.
    Refused(String),
}

/// Start a chat turn, or queue it onto the run already going on its
/// conversation — `register_chat_task`, and the bots' `_dispatch`.
pub async fn start_chat(
    pool: &SqlitePool,
    registry: &Registry,
    turn: ChatTurn,
) -> Result<Dispatched> {
    let ChatTurn { query, model, conversation_id, title, surface, display, project_id, ephemeral } = turn;
    let mut tx = crate::db::write_tx(pool).await?;
    let conversation_id = conversation_for(
        &mut tx,
        conversation_id.as_deref().filter(|c| !c.is_empty()),
        &model,
        title,
        surface,
        project_id.as_deref(),
        ephemeral,
    )
    .await?;

    // A message for a busy conversation joins the run already going —
    // `route_to_live_run`. A second job would only wait: the conversation's
    // thread lease (`jobs.thread_id`) runs its turns one at a time.
    if let Some(run) = registry.in_flight_chat(&conversation_id) {
        tx.commit().await?;
        return Ok(match queue_onto(pool, &run, &query).await {
            Ok((message_id, _)) => Dispatched::Queued { task_id: run.id.clone(), conversation_id, message_id },
            Err(e) => Dispatched::Refused(e.message),
        });
    }

    let display = display.unwrap_or_else(|| query.clone());
    insert_message(&mut *tx, &conversation_id, "user", &display, None, "done").await?;
    let task_id = enqueue_turn(registry, tx, &conversation_id, &query, &model).await?;
    Ok(Dispatched::Started { task_id, conversation_id })
}

/// `enqueue_chat_task`: the assistant row the run writes into (its id is the
/// task id) and the job, committed with `tx` and registered. The user's message
/// is the caller's.
async fn enqueue_turn(
    registry: &Registry,
    mut tx: Transaction<'_, Sqlite>,
    conversation_id: &str,
    query: &str,
    model: &str,
) -> Result<String> {
    let task_id = new_id();
    sqlx::query(
        "INSERT INTO messages (id, conversation_id, role, content, model, created_at, status) \
         VALUES (?, ?, 'assistant', '', ?, ?, 'running')",
    )
    .bind(&task_id)
    .bind(conversation_id)
    .bind(model)
    .bind(now_stored())
    .execute(&mut *tx)
    .await?;
    let payload = json!({"query": query, "model": model, "conv_id": conversation_id});
    // The conversation is the thread: its turns run one at a time.
    let enqueued_at = crate::jobs::insert(&mut *tx, &task_id, "chat", &payload, Some(conversation_id)).await?;
    commit_run(registry, tx, &task_id, "chat", first_chars(query, 60), conversation_id, &enqueued_at).await?;
    Ok(task_id)
}

/// `_redispatch_queued`: a message queued after a run's last model call has
/// no run left to join, so the first one becomes the next turn — its queued
/// row is that turn's user message. The rest stay queued; that turn adopts
/// them.
pub async fn redispatch_queued(pool: &SqlitePool, registry: &Registry, conversation_id: &str, model: &str) {
    let first: sqlx::Result<Option<(String, String)>> = sqlx::query_as(
        "SELECT id, content FROM messages WHERE conversation_id = ? AND role = 'user' AND status = 'queued' \
         ORDER BY created_at ASC LIMIT 1",
    )
    .bind(conversation_id)
    .fetch_optional(pool)
    .await;
    let Ok(Some((message_id, text))) = first else { return };
    let started = async {
        let mut tx = crate::db::write_tx(pool).await?;
        sqlx::query("UPDATE messages SET status = 'done' WHERE id = ?").bind(&message_id).execute(&mut *tx).await?;
        enqueue_turn(registry, tx, conversation_id, &text, model).await
    };
    if let Err(e) = started.await {
        // The rows are still queued: the next run on the conversation adopts them.
        tracing::warn!("re-dispatch of queued messages failed: {}", e.message);
    }
}

#[derive(Default)]
pub struct StartMutation;

#[Object]
impl StartMutation {
    async fn start_task(&self, ctx: &Context<'_>, input: StartTaskInput) -> Result<StartTaskPayload> {
        let pool: &SqlitePool = ctx.data()?;

        let registry: &Arc<Registry> = ctx.data()?;

        let model = catalog::resolve_model(pool, input.model.as_deref()).await?;
        // An incognito chat joins no project.
        let project_id = input.project_id.filter(|p| !p.is_empty() && !input.ephemeral);
        if let Some(project_id) = &project_id {
            let found: Option<String> =
                sqlx::query_scalar("SELECT id FROM projects WHERE id = ?").bind(project_id).fetch_optional(pool).await?;
            if found.is_none() {
                return Err(format!("project not found: {project_id}").into());
            }
        }
        let new_conversation = input.conversation_id.as_deref().is_none_or(str::is_empty);
        let turn = ChatTurn {
            title: new_conversation.then(|| first_chars(&input.query, 60)),
            query: input.query,
            model,
            conversation_id: input.conversation_id,
            surface: "web",
            display: None,
            project_id,
            ephemeral: input.ephemeral,
        };
        Ok(match start_chat(pool, registry, turn).await? {
            Dispatched::Started { task_id, conversation_id } => {
                StartTaskPayload { task_id, conversation_id, queued: false, queued_message_id: None }
            }
            Dispatched::Queued { task_id, conversation_id, message_id } => {
                StartTaskPayload { task_id, conversation_id, queued: true, queued_message_id: Some(message_id) }
            }
            Dispatched::Refused(reason) => return Err(reason.into()),
        })
    }

    /// Queue a message for a run that is already in flight; delivered just
    /// before its next LLM call.
    async fn queue_message(&self, ctx: &Context<'_>, task_id: String, query: String) -> Result<QueueMessagePayload> {
        if query.trim().is_empty() {
            return Err("empty message".into());
        }
        let registry: &Arc<Registry> = ctx.data()?;
        let run = registry.get(&task_id).ok_or("task not found or already finished")?;
        let (message_id, position) = queue_onto(ctx.data()?, &run, &query).await?;
        Ok(QueueMessagePayload { message_id, position })
    }

    /// Withdraw a queued message. False if the run already delivered it.
    async fn unqueue_message(&self, ctx: &Context<'_>, task_id: String, message_id: String) -> Result<bool> {
        let registry: &Arc<Registry> = ctx.data()?;
        let run = registry.get(&task_id).ok_or("task not found")?;
        let deleted = sqlx::query("DELETE FROM messages WHERE id = ? AND conversation_id = ? AND status = 'queued'")
            .bind(&message_id)
            .bind(run.meta.parent_id.as_deref())
            .execute(ctx.data::<SqlitePool>()?)
            .await?;
        if deleted.rows_affected() == 0 {
            return Ok(false);
        }
        run.emit_local("queued_withdrawn", &json!({"message_id": message_id}));
        Ok(true)
    }

    /// Trigger a workflow run. `inputs` is a JSON object of node defaults.
    /// Returns run_id; client subscribes to workflowRunEvents(runId).
    async fn run_workflow(&self, ctx: &Context<'_>, id: ID, inputs: Option<Json<Value>>) -> Result<String> {
        let (_, workflow_id) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let name: Option<String> = sqlx::query_scalar("SELECT name FROM workflows WHERE id = ?")
            .bind(&workflow_id)
            .fetch_optional(pool)
            .await?;
        let name = name.ok_or("workflow not found")?;
        let inputs = match inputs {
            Some(Json(Value::Object(map))) => Value::Object(map),
            _ => json!({}),
        };
        let run_id = new_id();
        let mut tx = crate::db::write_tx(pool).await?;
        sqlx::query(
            "INSERT INTO workflow_runs (id, workflow_id, status, inputs, outputs, node_results, error, started_at, \
             finished_at) VALUES (?, ?, 'running', ?, NULL, '[]', NULL, ?, NULL)",
        )
        .bind(&run_id)
        .bind(&workflow_id)
        .bind(pyjson::dumps(&inputs))
        .bind(now_stored())
        .execute(&mut *tx)
        .await?;
        let payload = json!({"workflow_id": workflow_id, "inputs": inputs});
        let enqueued_at = crate::jobs::insert(&mut *tx, &run_id, "workflow", &payload, None).await?;
        commit_run(ctx.data::<Arc<Registry>>()?, tx, &run_id, "workflow", name, &workflow_id, &enqueued_at).await?;
        Ok(run_id)
    }

    /// Resume a workflow paused at an approval/human_input node.
    async fn resume_workflow_run(&self, ctx: &Context<'_>, run_id: String, answer: String) -> Result<bool> {
        let registry: &Arc<Registry> = ctx.data()?;
        let run = registry.get(&run_id).ok_or("run not found or not running")?;
        // The paused node reads the answer off its request (`agent::workflow`).
        if run.fields().done || !crate::agent::workflow::answer(ctx.data()?, &run, "answered", &answer).await? {
            return Err("no pending human input for this run".into());
        }
        Ok(true)
    }

    /// Resolve a pending approval node with an explicit approved bool.
    async fn resolve_workflow_approval(
        &self,
        ctx: &Context<'_>,
        run_id: String,
        approved: bool,
        answer: Option<String>,
    ) -> Result<bool> {
        let registry: &Arc<Registry> = ctx.data()?;
        let run = registry.get(&run_id).ok_or("run not found or not running")?;
        let (status, default) = if approved { ("approved", "approved") } else { ("denied", "denied") };
        let answer = answer.filter(|a| !a.is_empty()).unwrap_or_else(|| default.into());
        if run.fields().done || !crate::agent::workflow::answer(ctx.data()?, &run, status, &answer).await? {
            return Err("no pending approval for this run".into());
        }
        Ok(true)
    }

    /// Returns the run_id; client subscribes to automationRunEvents(runId).
    async fn trigger_automation(&self, ctx: &Context<'_>, id: ID) -> Result<String> {
        let (_, automation_id) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let name: Option<String> = sqlx::query_scalar("SELECT name FROM automations WHERE id = ?")
            .bind(&automation_id)
            .fetch_optional(pool)
            .await?;
        let name = name.ok_or("automation not found")?;
        let run_id = new_id();
        let mut tx = crate::db::write_tx(pool).await?;
        sqlx::query(
            "INSERT INTO automation_runs (id, automation_id, status, triggered_by, output, error, started_at, \
             finished_at) VALUES (?, ?, 'running', 'manual', NULL, NULL, ?, NULL)",
        )
        .bind(&run_id)
        .bind(&automation_id)
        .bind(now_stored())
        .execute(&mut *tx)
        .await?;
        let payload = json!({"automation_id": automation_id, "triggered_by": "manual"});
        let enqueued_at = crate::jobs::insert(&mut *tx, &run_id, "automation", &payload, None).await?;
        commit_run(ctx.data::<Arc<Registry>>()?, tx, &run_id, "automation", name, &automation_id, &enqueued_at).await?;
        Ok(run_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_chars_counts_code_points() {
        assert_eq!(first_chars("héllo", 2), "hé");
        assert_eq!(first_chars("ab", 60), "ab");
    }
}
