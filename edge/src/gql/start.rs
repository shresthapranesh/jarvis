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
use crate::uploads::{self, Attachment, Staged};

/// A file staged by `POST /uploads`, sent with the turn.
#[derive(InputObject)]
pub struct UploadReferenceInput {
    upload_id: String,
}

#[derive(InputObject)]
pub struct StartTaskInput {
    query: String,
    model: Option<String>,
    /// The effort the conversation's turns ask for (`low` … `max`); it is
    /// sticky per conversation, as the model is. None keeps the one it has.
    effort: Option<String>,
    conversation_id: Option<String>,
    project_id: Option<String>,
    #[graphql(default)]
    ephemeral: bool,
    /// Files staged by `POST /uploads`: saved where the agent can open them,
    /// and images shown to the model. Not for a conversation with a run going.
    attachment_uploads: Option<Vec<UploadReferenceInput>>,
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

/// An effort that isn't one of the levels.
pub fn unknown_effort(effort: &str) -> String {
    format!("unknown effort {}; one of none, minimal, low, medium, high, xhigh, max", pyjson::repr_str(effort))
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

/// `conversations.surface` of a sub-agent's conversation: one an agent
/// spawned (`parent_id`), answered by its `agent_role`.
pub const SUBAGENT: &str = "subagent";

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
    /// Already checked (`catalog::effort_level`); None keeps the
    /// conversation's.
    pub effort: Option<&'static str>,
    /// Staged uploads, moved into the conversation's directory when the turn
    /// starts (`uploads.rs`).
    pub attachments: Vec<Staged>,
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
    let ChatTurn { query, model, conversation_id, title, surface, display, project_id, ephemeral, effort, attachments } =
        turn;
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
    if let Some(effort) = effort {
        sqlx::query("UPDATE conversations SET effort = ? WHERE id = ?").bind(effort).bind(&conversation_id).execute(&mut *tx).await?;
    }

    // A message for a busy conversation joins the run already going —
    // `route_to_live_run`. A second job would only wait: the conversation's
    // thread lease (`jobs.thread_id`) runs its turns one at a time.
    if let Some(run) = registry.in_flight_chat(&conversation_id) {
        tx.commit().await?;
        if !attachments.is_empty() {
            return Ok(Dispatched::Refused(
                "a run is already going on this conversation, and files can't be added to it — wait for it to \
                 finish, or stop it first"
                    .into(),
            ));
        }
        return Ok(match queue_onto(pool, &run, &query).await {
            Ok((message_id, _)) => Dispatched::Queued { task_id: run.id.clone(), conversation_id, message_id },
            Err(e) => Dispatched::Refused(e.message),
        });
    }

    let attachments = uploads::take(attachments, &conversation_id)?;
    let display = display.unwrap_or_else(|| {
        if attachments.is_empty() { query.clone() } else { uploads::display(&query, &attachments) }
    });
    insert_message(&mut *tx, &conversation_id, "user", &display, None, "done").await?;
    let task_id = enqueue_turn(registry, tx, &conversation_id, &query, &model, &attachments, None).await?;
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
    attachments: &[Attachment],
    from_agent: Option<FromAgent>,
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
    let mut payload = json!({"query": query, "model": model, "conv_id": conversation_id});
    if !attachments.is_empty() {
        payload["attachments"] = serde_json::to_value(attachments)?;
    }
    // A sub-agent's conversation is answered by its role, whoever wrote to
    // it — the agent that spawned it, or the user on its page.
    let role: Option<(String, Option<String>)> =
        sqlx::query_as("SELECT surface, agent_role FROM conversations WHERE id = ?")
            .bind(conversation_id)
            .fetch_optional(&mut *tx)
            .await?;
    let kind = match role {
        Some((surface, role)) if surface == SUBAGENT => {
            payload["role"] = json!(role);
            if let Some(from) = from_agent {
                // The agent that spawned it set the task: a stop of that
                // agent stops this turn, and `deliver` sends its answer back
                // as a message when nobody is waiting for it.
                payload["by"] = json!("agent");
                payload["deliver"] = json!(from.deliver);
            }
            "subagent"
        }
        _ => "chat",
    };
    // The conversation is the thread: its turns run one at a time.
    let enqueued_at = crate::jobs::insert(&mut *tx, &task_id, kind, &payload, Some(conversation_id)).await?;
    let label = match attachments.first() {
        Some(a) if query.trim().is_empty() => first_chars(&a.name, 60),
        _ => first_chars(query, 60),
    };
    commit_run(registry, tx, &task_id, kind, label, conversation_id, &enqueued_at).await?;
    Ok(task_id)
}

/// A sub-agent turn its parent agent starts, not the user.
#[derive(Clone, Copy)]
pub struct FromAgent {
    /// Send the answer to the parent's conversation when the turn ends with
    /// nobody waiting on it.
    pub deliver: bool,
}

/// `messages.source` of a task an agent set its sub-agent.
pub const FROM_AGENT: &str = "agent";

/// `messages.source` of a sub-agent's answer delivered to its parent.
pub fn from_subagent(conversation_id: &str) -> String {
    format!("subagent:{conversation_id}")
}

async fn insert_sourced(
    executor: impl sqlx::SqliteExecutor<'_>,
    conversation_id: &str,
    content: &str,
    status: &str,
    source: &str,
) -> sqlx::Result<String> {
    let id = new_id();
    sqlx::query(
        "INSERT INTO messages (id, conversation_id, role, content, model, created_at, status, source) \
         VALUES (?, ?, 'user', ?, NULL, ?, ?, ?)",
    )
    .bind(&id)
    .bind(conversation_id)
    .bind(content)
    .bind(now_stored())
    .bind(status)
    .bind(source)
    .execute(executor)
    .await?;
    Ok(id)
}

/// A sub-agent to spawn: its conversation, and the first task on it.
pub struct Spawn<'a> {
    /// The conversation of the agent spawning it, if it has one.
    pub parent_id: Option<&'a str>,
    /// The run spawning it (a chat's is its reply's id).
    pub spawned_by: &'a str,
    pub role: &'a str,
    /// Already resolved.
    pub model: &'a str,
    pub title: &'a str,
    pub query: &'a str,
    pub project_id: Option<&'a str>,
    pub ephemeral: bool,
    pub from: FromAgent,
}

/// Spawn a sub-agent: its conversation, linked to its parent, and its first
/// turn. The turn's id and the conversation's.
pub async fn spawn_subagent(pool: &SqlitePool, registry: &Registry, spawn: Spawn<'_>) -> Result<(String, String)> {
    let mut tx = crate::db::write_tx(pool).await?;
    let id = new_id();
    sqlx::query(
        "INSERT INTO conversations (id, title, model, created_at, surface, pinned, project_id, ephemeral, parent_id, \
         agent_role, spawned_by) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
    )
    .bind(&id)
    .bind(spawn.title)
    .bind(spawn.model)
    .bind(now_stored())
    .bind(SUBAGENT)
    .bind(spawn.project_id)
    .bind(spawn.ephemeral)
    .bind(spawn.parent_id)
    .bind(spawn.role)
    .bind(spawn.spawned_by)
    .execute(&mut *tx)
    .await?;
    insert_sourced(&mut *tx, &id, spawn.query, "done", FROM_AGENT).await?;
    let task_id = enqueue_turn(registry, tx, &id, spawn.query, spawn.model, &[], Some(spawn.from)).await?;
    Ok((task_id, id))
}

/// Another task for a sub-agent that isn't running: its next turn, on the
/// thread it already has.
pub async fn task_subagent(
    pool: &SqlitePool,
    registry: &Registry,
    conversation_id: &str,
    query: &str,
    model: &str,
    from: FromAgent,
) -> Result<String> {
    let mut tx = crate::db::write_tx(pool).await?;
    insert_sourced(&mut *tx, conversation_id, query, "done", FROM_AGENT).await?;
    enqueue_turn(registry, tx, conversation_id, query, model, &[], Some(from)).await
}

/// A sub-agent's answer for a parent that wasn't waiting for it: a message
/// on the parent's conversation, from the sub-agent. It joins the run going
/// there, or starts the parent's next turn.
///
/// `changed` is the `subagent` event a run going on the parent is told with.
pub async fn deliver_to_parent(
    pool: &SqlitePool,
    registry: &Registry,
    parent_id: &str,
    subagent_id: &str,
    text: &str,
    changed: &Value,
) {
    let source = from_subagent(subagent_id);
    let delivered = async {
        if let Some(run) = registry.in_flight_chat(parent_id) {
            run.emit_local("subagent", changed);
            let message_id = insert_sourced(pool, parent_id, text, "queued", &source).await?;
            let position: i64 = sqlx::query_scalar(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user' AND status = 'queued'",
            )
            .bind(parent_id)
            .fetch_one(pool)
            .await?;
            run.emit_local("queued_message", &json!({"message_id": message_id, "text": text, "position": position}));
            return Ok(());
        }
        let mut tx = crate::db::write_tx(pool).await?;
        let model: Option<String> =
            sqlx::query_scalar("SELECT model FROM conversations WHERE id = ?").bind(parent_id).fetch_optional(&mut *tx).await?;
        // A parent deleted meanwhile has nobody to tell.
        let Some(model) = model else { return Ok(()) };
        // Its stored model may have left the catalog since.
        let model = catalog::resolve_model(pool, Some(&model)).await?;
        insert_sourced(&mut *tx, parent_id, text, "done", &source).await?;
        enqueue_turn(registry, tx, parent_id, text, &model, &[], None).await.map(|_| ())
    };
    let delivered: Result<()> = delivered.await;
    if let Err(e) = delivered {
        tracing::warn!("delivering sub-agent {subagent_id}'s answer to {parent_id} failed: {}", e.message);
    }
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
        enqueue_turn(registry, tx, conversation_id, &text, model, &[], None).await
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
        let effort = match input.effort.as_deref().filter(|e| !e.is_empty()) {
            Some(e) => Some(catalog::effort_level(e).ok_or_else(|| unknown_effort(e))?),
            None => None,
        };
        // An incognito chat joins no project.
        let project_id = input.project_id.filter(|p| !p.is_empty() && !input.ephemeral);
        if let Some(project_id) = &project_id {
            let found: Option<String> =
                sqlx::query_scalar("SELECT id FROM projects WHERE id = ?").bind(project_id).fetch_optional(pool).await?;
            if found.is_none() {
                return Err(format!("project not found: {project_id}").into());
            }
        }
        let ids: Vec<String> =
            input.attachment_uploads.unwrap_or_default().into_iter().map(|u| u.upload_id).collect();
        let attachments = uploads::staged(&ids)?;
        if input.query.trim().is_empty() && attachments.is_empty() {
            return Err("empty message".into());
        }
        let new_conversation = input.conversation_id.as_deref().is_none_or(str::is_empty);
        // A message that is only files is titled by its first one.
        let title_from = match ids.first() {
            Some(_) if input.query.trim().is_empty() => attachments[0].filename().to_string(),
            _ => input.query.clone(),
        };
        let turn = ChatTurn {
            title: new_conversation.then(|| first_chars(&title_from, 60)),
            query: input.query,
            model,
            conversation_id: input.conversation_id,
            surface: "web",
            display: None,
            project_id,
            ephemeral: input.ephemeral,
            effort,
            attachments,
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
