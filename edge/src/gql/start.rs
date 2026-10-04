//! Starting runs, and steering live ones — `startTask`, `runWorkflow` and
//! `triggerAutomation` (`server/*_runtime.py:register_*`), and the mutations
//! that act on a run's in-memory state: queueing a message onto it, answering
//! a workflow's interrupt.
//!
//! A trigger is only rows: the domain row the run reports into, a `jobs` row
//! for a worker to claim, and the run mirrored as *pending* (`runs.rs`) so a
//! subscriber that gets the id back finds it — what the Python triggers did by
//! registering a `TaskState` before their commit. The edge then wakes the
//! worker, which claims the job and creates the run's state from it.
//!
//! What acts on a claimed run's in-memory state (`TaskState.pending_input`, a
//! `resume_future`) goes to the worker as a `call`, which runs the function
//! the Python resolver would have and returns its result or its error
//! message. Before a worker claims the run, the edge answers for it.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_graphql::{Context, ID, InputObject, Json, Object, Result, SimpleObject};
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Value, json};
use sqlx::{Sqlite, SqlitePool, Transaction};

use super::EdgeData;
use super::codec::{decode_global_id, iso_from_db, new_id, now_stored};
use crate::catalog;
use crate::pyjson;
use crate::runs::{Meta, Registry, Run};

#[derive(InputObject)]
pub struct UploadReferenceInput {
    upload_id: String,
}

#[derive(InputObject)]
pub struct StartTaskInput {
    query: String,
    model: Option<String>,
    conversation_id: Option<String>,
    attachment_uploads: Option<Vec<UploadReferenceInput>>,
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

/// An attachment on a chat turn — `AttachmentIn`. From a staged upload
/// (`_resolve_staged_uploads`), or built in memory by a bot.
pub struct Attachment {
    kind: &'static str,
    name: String,
    mime_type: String,
    bytes: Vec<u8>,
    size: Value,
    /// The staged upload's bytes and meta files, removed once the run holds
    /// the bytes.
    staged: Option<(PathBuf, PathBuf)>,
    document_id: Option<String>,
    document_path: Option<String>,
    persist_error: Option<String>,
}

impl Attachment {
    /// An image a bot received — the bots' `AttachmentIn(type="image", ...)`.
    /// Never persisted as a document: it rides in the job payload.
    pub fn image(name: String, mime_type: String, bytes: Vec<u8>) -> Self {
        let size = Value::from(bytes.len());
        Self {
            kind: "image",
            name,
            mime_type,
            bytes,
            size,
            staged: None,
            document_id: None,
            document_path: None,
            persist_error: None,
        }
    }

    /// `AttachmentIn.model_dump()`, field order and all.
    fn dump(&self) -> Value {
        json!({
            "type": self.kind,
            "name": self.name,
            "mime_type": self.mime_type,
            "data": STANDARD.encode(&self.bytes),
            "size": self.size,
            "document_id": self.document_id,
            "document_path": self.document_path,
            "persist_error": self.persist_error,
        })
    }
}

fn attachment_kind(mime: &str) -> &'static str {
    ["image", "audio", "video"]
        .into_iter()
        .find(|kind| mime.starts_with(&format!("{kind}/")))
        .unwrap_or("document")
}

fn read_staged(staging_dir: &Path, uploads: &[UploadReferenceInput]) -> Result<Vec<Attachment>> {
    uploads
        .iter()
        .map(|u| {
            let unknown = || format!("unknown or expired upload id: {}", u.upload_id);
            // Ids are opaque uuids; anything that would step outside the
            // staging directory is no upload of ours.
            if u.upload_id.is_empty() || u.upload_id.contains(['/', '\\']) || u.upload_id.starts_with('.') {
                return Err(unknown().into());
            }
            let bytes_path = staging_dir.join(&u.upload_id);
            let meta_path = staging_dir.join(format!("{}.meta.json", u.upload_id));
            if !bytes_path.exists() || !meta_path.exists() {
                return Err(unknown().into());
            }
            let meta: Value = serde_json::from_slice(&std::fs::read(&meta_path)?)?;
            let field = |key: &str| meta.get(key).cloned().ok_or_else(|| format!("'{key}'"));
            let mime_type = field("mime_type")?.as_str().unwrap_or_default().to_string();
            Ok(Attachment {
                kind: attachment_kind(&mime_type),
                name: field("filename")?.as_str().unwrap_or_default().to_string(),
                mime_type,
                bytes: std::fs::read(&bytes_path)?,
                size: field("size")?,
                staged: Some((bytes_path, meta_path)),
                document_id: None,
                document_path: None,
                persist_error: None,
            })
        })
        .collect()
}

/// `os.path.splitext(name)[1]`: the last dot in the final component, unless
/// only dots precede it.
fn extension(name: &str) -> &str {
    let base = &name[name.rfind('/').map_or(0, |i| i + 1)..];
    match base.rfind('.') {
        Some(dot) if base[..dot].bytes().any(|b| b != b'.') => &base[dot..],
        _ => "",
    }
}

/// Python's `s[:60]`.
pub fn first_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `get_or_create_conversation`: an existing conversation takes the run's
/// model (it is sticky per conversation); a missing one is created, under the
/// requested id if there was one.
async fn conversation_for(
    tx: &mut Transaction<'_, Sqlite>,
    requested: Option<&str>,
    model: &str,
    title: Option<String>,
    surface: &str,
    project_id: Option<&str>,
    ephemeral: bool,
) -> Result<String> {
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

async fn insert_message(
    executor: impl sqlx::SqliteExecutor<'_>,
    conversation_id: &str,
    role: &str,
    content: &str,
    model: Option<&str>,
    status: &str,
) -> Result<String> {
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

/// Mirror the run, then commit its rows and wake the worker. Mirrored first,
/// as the Python triggers registered before committing: once the job is
/// visible a worker may claim it, and its report must find the run. `edge`:
/// the job is for the edge's agent loop, and so is the run.
#[allow(clippy::too_many_arguments)]
async fn commit_run(
    registry: &Registry,
    tx: Transaction<'_, Sqlite>,
    id: &str,
    kind: &str,
    label: String,
    parent_id: &str,
    enqueued_at: &str,
    edge: bool,
) -> Result<()> {
    let meta = Meta {
        kind: kind.into(),
        label,
        parent_id: Some(parent_id.into()),
        // The worker starts the run's clock from the job's `created_at` too.
        started_at: iso_from_db(enqueued_at).utc().0,
    };
    registry.pre_register(id, meta, edge);
    if let Err(e) = tx.commit().await {
        registry.discard_pending(id);
        return Err(e.into());
    }
    tracing::info!("{kind} run {id} enqueued for {parent_id}");
    registry.wake();
    Ok(())
}

fn worker_error(e: String) -> async_graphql::Error {
    e.into()
}

/// Queue `query` onto a live chat run — `queue_chat_message`. A claimed run
/// is the worker's; a pending one the edge queues onto itself, and the
/// worker adopts the row when it claims the job.
async fn queue_onto(registry: &Registry, pool: &SqlitePool, run: &Arc<Run>, query: &str) -> Result<(String, i64)> {
    if run.claimed() {
        let value = registry
            .call("queue_message", json!({"task_id": run.id, "query": query}))
            .await
            .map_err(worker_error)?;
        let message_id = value["message_id"].as_str().unwrap_or_default().to_string();
        return Ok((message_id, value["position"].as_i64().unwrap_or_default()));
    }
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
    // What the run will hold once the worker adopts the conversation's queue.
    let position: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user' AND status = 'queued'",
    )
    .bind(conversation_id)
    .fetch_one(pool)
    .await?;
    if !run.queue_local(&message_id, text, position) {
        // Claimed since the check above: the worker announces it instead.
        registry.adopt_queued(&run.id, &[&message_id]);
    }
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
    /// The user message as stored. `None` stores what `startTask` does: the
    /// query, or its parts with the attachments' when there are any.
    pub display: Option<String>,
    pub attachments: Vec<Attachment>,
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
    documents_dir: &Path,
    registry: &Registry,
    turn: ChatTurn,
) -> Result<Dispatched> {
    let ChatTurn { query, model, conversation_id, title, surface, display, mut attachments, project_id, ephemeral } =
        turn;
    let mut tx = pool.begin().await?;
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
        if !attachments.is_empty() {
            return Ok(Dispatched::Refused(
                "a run is already in flight on this conversation and attachments cannot be queued onto it \
                 — wait for it to finish, or stop it first"
                    .into(),
            ));
        }
        return Ok(match queue_onto(registry, pool, &run, &query).await {
            Ok((message_id, _)) => Dispatched::Queued { task_id: run.id.clone(), conversation_id, message_id },
            Err(e) => Dispatched::Refused(e.message),
        });
    }

    let display = display.unwrap_or_else(|| {
        if attachments.is_empty() {
            return query.clone();
        }
        let mut parts = vec![json!({"type": "text", "text": query})];
        parts.extend(
            attachments.iter().map(|a| json!({"type": a.kind, "name": a.name, "size": a.size, "mimeType": a.mime_type})),
        );
        pyjson::dumps(&Value::Array(parts))
    });
    let user_message = insert_message(&mut *tx, &conversation_id, "user", &display, None, "done").await?;

    if attachments.iter().any(|a| a.kind == "document") {
        std::fs::create_dir_all(documents_dir)?;
    }
    for att in attachments.iter_mut().filter(|a| a.kind == "document") {
        let doc_id = new_id();
        let ext = match extension(&att.name) {
            "" => ".bin",
            ext => ext,
        };
        let path = documents_dir.join(format!("{doc_id}{ext}"));
        if let Err(e) = std::fs::write(&path, &att.bytes) {
            // Carried into the message, so the agent says the file failed
            // rather than concluding it doesn't exist.
            tracing::error!("failed to persist document {} to {}: {e}", att.name, documents_dir.display());
            att.persist_error = Some(format!("OSError: {e}"));
            continue;
        }
        let path = path.to_string_lossy().into_owned();
        sqlx::query(
            "INSERT INTO documents (id, conversation_id, message_id, filename, mime_type, size, path, created_at, \
             index_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        )
        .bind(&doc_id)
        .bind(&conversation_id)
        .bind(&user_message)
        .bind(&att.name)
        .bind(&att.mime_type)
        .bind(att.size.as_i64().unwrap_or_default())
        .bind(&path)
        .bind(now_stored())
        .execute(&mut *tx)
        .await?;
        att.document_id = Some(doc_id);
        att.document_path = Some(path);
    }

    let task_id = enqueue_turn(pool, registry, tx, &conversation_id, &query, &model, &attachments).await?;

    // The bytes now live in documents_dir or the job payload.
    for (bytes, meta) in attachments.iter().filter_map(|a| a.staged.as_ref()) {
        let _ = std::fs::remove_file(bytes);
        let _ = std::fs::remove_file(meta);
    }
    Ok(Dispatched::Started { task_id, conversation_id })
}

/// `enqueue_chat_task`: the assistant row the run writes into (its id is the
/// task id) and the job, committed with `tx` and mirrored. The user's message
/// is the caller's.
async fn enqueue_turn(
    pool: &SqlitePool,
    registry: &Registry,
    mut tx: Transaction<'_, Sqlite>,
    conversation_id: &str,
    query: &str,
    model: &str,
    attachments: &[Attachment],
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
        payload["attachments"] = attachments.iter().map(Attachment::dump).collect();
    }
    // The conversation is the thread: its turns run one at a time. The edge
    // runs the turn itself when it can (`agent/route.rs`).
    let edge = crate::agent::route::serves_chat(pool, model, !attachments.is_empty()).await;
    let enqueued_at = crate::jobs::insert(&mut *tx, &task_id, "chat", &payload, Some(conversation_id), edge).await?;
    commit_run(registry, tx, &task_id, "chat", first_chars(query, 60), conversation_id, &enqueued_at, edge).await?;
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
        let mut tx = pool.begin().await?;
        sqlx::query("UPDATE messages SET status = 'done' WHERE id = ?").bind(&message_id).execute(&mut *tx).await?;
        enqueue_turn(pool, registry, tx, conversation_id, &text, model, &[]).await
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
        let data: &EdgeData = ctx.data()?;
        let registry: &Arc<Registry> = ctx.data()?;

        let model = catalog::resolve_model(pool, input.model.as_deref()).await?;
        let attachments = match input.attachment_uploads.as_deref() {
            Some(uploads) if !uploads.is_empty() => read_staged(&data.staging_dir, uploads)?,
            _ => vec![],
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
        let new_conversation = input.conversation_id.as_deref().is_none_or(str::is_empty);
        let turn = ChatTurn {
            title: new_conversation.then(|| first_chars(&input.query, 60)),
            query: input.query,
            model,
            conversation_id: input.conversation_id,
            surface: "web",
            display: None,
            attachments,
            project_id,
            ephemeral: input.ephemeral,
        };
        Ok(match start_chat(pool, &data.documents_dir, registry, turn).await? {
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
        let (message_id, position) = queue_onto(registry, ctx.data()?, &run, &query).await?;
        Ok(QueueMessagePayload { message_id, position })
    }

    /// Withdraw a queued message. False if the run already delivered it.
    async fn unqueue_message(&self, ctx: &Context<'_>, task_id: String, message_id: String) -> Result<bool> {
        let registry: &Arc<Registry> = ctx.data()?;
        let run = registry.get(&task_id).ok_or("task not found")?;
        let params = json!({"task_id": task_id, "message_id": message_id});
        if run.claimed() {
            let value = registry.call("unqueue_message", params).await.map_err(worker_error)?;
            return Ok(value.as_bool().unwrap_or(false));
        }
        let deleted = sqlx::query("DELETE FROM messages WHERE id = ? AND conversation_id = ? AND status = 'queued'")
            .bind(&message_id)
            .bind(run.meta.parent_id.as_deref())
            .execute(ctx.data::<SqlitePool>()?)
            .await?;
        if deleted.rows_affected() == 0 {
            return Ok(false);
        }
        if !run.emit_local("queued_withdrawn", &json!({"message_id": message_id})) {
            // Claimed meanwhile, and the worker may have adopted it already.
            let registry = registry.clone();
            tokio::spawn(async move {
                let _ = registry.call("unqueue_message", params).await;
            });
        }
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
        let mut tx = pool.begin().await?;
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
        let enqueued_at = crate::jobs::insert(&mut *tx, &run_id, "workflow", &payload, None, false).await?;
        commit_run(ctx.data::<Arc<Registry>>()?, tx, &run_id, "workflow", name, &workflow_id, &enqueued_at, false).await?;
        Ok(run_id)
    }

    /// Resume a workflow paused at an approval/human_input node.
    async fn resume_workflow_run(&self, ctx: &Context<'_>, run_id: String, answer: String) -> Result<bool> {
        let registry: &Arc<Registry> = ctx.data()?;
        let run = registry.get(&run_id).ok_or("run not found or not running")?;
        if !run.claimed() {
            return Err("no pending human input for this run".into());
        }
        registry.call("resume_workflow_run", json!({"run_id": run_id, "answer": answer})).await.map_err(worker_error)?;
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
        if !run.claimed() {
            return Err("no pending approval for this run".into());
        }
        let params = json!({"run_id": run_id, "approved": approved, "answer": answer});
        registry.call("resolve_workflow_approval", params).await.map_err(worker_error)?;
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
        let mut tx = pool.begin().await?;
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
        let enqueued_at = crate::jobs::insert(&mut *tx, &run_id, "automation", &payload, None, false).await?;
        commit_run(ctx.data::<Arc<Registry>>()?, tx, &run_id, "automation", name, &automation_id, &enqueued_at, false).await?;
        Ok(run_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extension_matches_splitext() {
        // os.path.splitext(name)[1] for each.
        for (name, ext) in [
            ("report.pdf", ".pdf"),
            ("a.tar.gz", ".gz"),
            (".bashrc", ""),
            ("..x", ""),
            ("a.", "."),
            ("noext", ""),
            ("dir.d/file", ""),
            ("x/.hidden.txt", ".txt"),
        ] {
            assert_eq!(extension(name), ext, "{name}");
        }
    }

    #[test]
    fn first_chars_counts_code_points() {
        assert_eq!(first_chars("héllo", 2), "hé");
        assert_eq!(first_chars("ab", 60), "ab");
    }
}
