//! Conversation, Message and Step — `server/graphql/types/conversation.py`
//! and `server/graphql/queries/conversation.py`.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use serde_json::Value;
use sqlx::{Sqlite, SqlitePool, Transaction};

use super::codec::{DateTime, decode_cursor, decode_global_id, encode_cursor, global_id};
use super::events::TodoItem;
use super::project::Project;

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Conversation {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub title: Option<String>,
    pub model: String,
    pub surface: String,
    pub pinned: bool,
    pub ephemeral: bool,
    pub project_id: Option<String>,
    pub created_at: DateTime,
}

pub const CONVERSATION_COLUMNS: &str =
    "id, title, model, surface, pinned, ephemeral, project_id, created_at";

impl Conversation {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {CONVERSATION_COLUMNS} FROM conversations WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Conversation {
    pub async fn id(&self) -> ID {
        global_id("Conversation", &self.raw_id)
    }

    async fn project(&self, ctx: &Context<'_>) -> Result<Option<Project>> {
        match &self.project_id {
            Some(pid) => Project::by_id(ctx.data()?, pid).await,
            None => Ok(None),
        }
    }

    async fn message_count(&self, ctx: &Context<'_>) -> Result<i64> {
        let pool: &SqlitePool = ctx.data()?;
        let (n,): (i64,) = sqlx::query_as("SELECT COUNT(id) FROM messages WHERE conversation_id = ?")
            .bind(&self.raw_id)
            .fetch_one(pool)
            .await?;
        Ok(n)
    }

    /// Backward-paginated message connection. Newest-N older than the cursor,
    /// returned oldest-first; `(created_at, id)` keeps the order stable when
    /// two messages share a timestamp.
    async fn messages(
        &self,
        ctx: &Context<'_>,
        #[graphql(default = 10)] last: i32,
        before: Option<String>,
    ) -> Result<MessageConnection> {
        let pool: &SqlitePool = ctx.data()?;
        let last = last.clamp(1, 100) as i64;
        let mut sql = format!("SELECT {MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ?");
        let cursor = before.as_deref().map(decode_cursor).transpose()?;
        if cursor.is_some() {
            sql.push_str(" AND (created_at < ? OR (created_at = ? AND id < ?))");
        }
        sql.push_str(" ORDER BY created_at DESC, id DESC LIMIT ?");

        let mut q = sqlx::query_as::<_, Message>(&sql).bind(&self.raw_id);
        if let Some((ts, id)) = &cursor {
            q = q.bind(ts).bind(ts).bind(id);
        }
        let mut rows = q.bind(last + 1).fetch_all(pool).await?;

        let has_previous_page = rows.len() as i64 > last;
        rows.truncate(last as usize);
        rows.reverse();

        let messages = Message::with_steps(pool, rows).await?;
        let edges: Vec<MessageEdge> = messages
            .into_iter()
            .map(|m| MessageEdge { cursor: encode_cursor(&m.created_at.0, &m.raw_id), node: m })
            .collect();
        Ok(MessageConnection {
            page_info: PageInfo {
                has_next_page: false,
                has_previous_page,
                start_cursor: edges.first().map(|e| e.cursor.clone()),
                end_cursor: edges.last().map(|e| e.cursor.clone()),
            },
            edges,
        })
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Message {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub role: String,
    pub content: String,
    pub model: Option<String>,
    pub status: String,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub ttft_ms: Option<f64>,
    pub llm_ms: Option<f64>,
    pub prefill_tps: Option<f64>,
    pub eval_tps: Option<f64>,
    pub duration_ms: Option<f64>,
    pub created_at: DateTime,
    /// Loaded alongside the page (`with_steps`), ordered by `seq`.
    #[sqlx(skip)]
    pub steps: Vec<Step>,
}

const MESSAGE_COLUMNS: &str = "id, role, content, model, status, input_tokens, output_tokens, \
     ttft_ms, llm_ms, prefill_tps, eval_tps, duration_ms, created_at";

impl Message {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        let row: Option<Message> = sqlx::query_as(&format!("SELECT {MESSAGE_COLUMNS} FROM messages WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?;
        Ok(match row {
            Some(m) => Self::with_steps(pool, vec![m]).await?.pop(),
            None => None,
        })
    }

    /// Attach steps to a page of messages in one query (the Python side's
    /// `selectinload`).
    async fn with_steps(pool: &SqlitePool, mut messages: Vec<Self>) -> Result<Vec<Self>> {
        if messages.is_empty() {
            return Ok(messages);
        }
        let placeholders = vec!["?"; messages.len()].join(", ");
        let sql = format!(
            "SELECT id, message_id, node, source, subagent, data, seq, created_at FROM steps \
             WHERE message_id IN ({placeholders}) ORDER BY seq, rowid"
        );
        let mut q = sqlx::query_as::<_, Step>(&sql);
        for m in &messages {
            q = q.bind(&m.raw_id);
        }
        let mut by_message: HashMap<String, Vec<Step>> = HashMap::new();
        for s in q.fetch_all(pool).await? {
            by_message.entry(s.message_id.clone()).or_default().push(s);
        }
        for m in &mut messages {
            m.steps = by_message.remove(&m.raw_id).unwrap_or_default();
        }
        Ok(messages)
    }
}

#[ComplexObject]
impl Message {
    pub async fn id(&self) -> ID {
        global_id("Message", &self.raw_id)
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
pub struct Step {
    pub id: String,
    #[graphql(skip)]
    pub message_id: String,
    pub node: String,
    pub source: String,
    pub subagent: Option<String>,
    pub data: Option<String>,
    pub seq: i64,
    pub created_at: DateTime,
}

#[derive(SimpleObject)]
pub struct MessageEdge {
    pub node: Message,
    pub cursor: String,
}

#[derive(SimpleObject)]
pub struct MessageConnection {
    pub edges: Vec<MessageEdge>,
    pub page_info: PageInfo,
}

#[derive(SimpleObject)]
pub struct PageInfo {
    /// When paginating forwards, are there more items?
    pub has_next_page: bool,
    /// When paginating backwards, are there more items?
    pub has_previous_page: bool,
    /// When paginating backwards, the cursor to continue.
    pub start_cursor: Option<String>,
    /// When paginating forwards, the cursor to continue.
    pub end_cursor: Option<String>,
}

#[derive(Default)]
pub struct ConversationQuery;

#[Object]
impl ConversationQuery {
    /// The thread's todo list (`thread_state`).
    async fn todos(&self, ctx: &Context<'_>, conversation_id: String) -> Result<Vec<TodoItem>> {
        let pool: &SqlitePool = ctx.data()?;
        let row: Option<Option<String>> =
            match sqlx::query_scalar("SELECT todos FROM thread_state WHERE thread_id = ?")
                .bind(&conversation_id)
                .fetch_optional(pool)
                .await
            {
                Err(sqlx::Error::Database(e)) if e.message().contains("no such table") => None,
                other => other?,
            };
        let raw = match row.flatten().filter(|raw| !raw.is_empty()) {
            Some(raw) => serde_json::from_str(&raw).map_err(|e| super::defer(format!("todos: {e}")))?,
            None => Value::Array(vec![]),
        };
        let todos = crate::agent::tools::normalise_todos(raw.as_array().map_or(&[], Vec::as_slice));
        Ok(todos
            .into_iter()
            .map(|t| TodoItem { text: t["text"].as_str().unwrap_or_default().into(), status: t["status"].as_str().unwrap_or_default().into() })
            .collect())
    }

    /// List conversations for one surface (default "web", so bot/automation
    /// threads stay out of the sidebar). Pass surface: null to list all.
    async fn conversations(
        &self,
        ctx: &Context<'_>,
        #[graphql(default_with = "Some(\"web\".to_string())")] surface: Option<String>,
    ) -> Result<Vec<Conversation>> {
        let pool: &SqlitePool = ctx.data()?;
        // Incognito conversations never appear in history listings.
        let mut sql = format!("SELECT {CONVERSATION_COLUMNS} FROM conversations WHERE ephemeral = 0");
        if surface.is_some() {
            sql.push_str(" AND surface = ?");
        }
        sql.push_str(" ORDER BY pinned DESC, created_at DESC");
        let mut q = sqlx::query_as(&sql);
        if let Some(s) = &surface {
            q = q.bind(s);
        }
        Ok(q.fetch_all(pool).await?)
    }

    async fn conversation(&self, ctx: &Context<'_>, id: ID) -> Result<Option<Conversation>> {
        let (_, raw) = decode_global_id(&id)?;
        Conversation::by_id(ctx.data()?, &raw).await
    }
}

#[derive(Default)]
pub struct ConversationMutation;

#[Object]
impl ConversationMutation {
    // Rename, pin, or pick the model; a model must be in the catalog.
    async fn update_conversation(
        &self,
        ctx: &Context<'_>,
        id: ID,
        title: Option<String>,
        model: Option<String>,
        pinned: Option<bool>,
    ) -> Result<Conversation> {
        let pool: &SqlitePool = ctx.data()?;
        if let Some(m) = &model {
            if !crate::catalog::is_valid_model(pool, m).await? {
                return Err(unknown_model(m).into());
            }
        }
        if title.is_none() && model.is_none() && pinned.is_none() {
            return Err("no fields to update".into());
        }
        let (_, raw) = decode_global_id(&id)?;
        let mut conv = Conversation::by_id(pool, &raw).await?.ok_or("conversation not found")?;
        if let Some(t) = title {
            conv.title = Some(t);
        }
        if let Some(m) = model {
            conv.model = m;
        }
        if let Some(p) = pinned {
            conv.pinned = p;
        }
        // Conversation has no updated_at column.
        sqlx::query("UPDATE conversations SET title = ?, model = ?, pinned = ? WHERE id = ?")
            .bind(&conv.title)
            .bind(&conv.model)
            .bind(conv.pinned)
            .bind(&raw)
            .execute(pool)
            .await?;
        Ok(conv)
    }

    async fn delete_conversation(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<super::EdgeData>()?);
        let mut tx = crate::db::write_tx(pool).await?;
        let teardown = delete_conversation(&mut tx, &raw, &data.artifacts_dir).await?;
        tx.commit().await?;
        if let Some(t) = teardown {
            t.finish(data).await;
        }
        Ok(true)
    }

    // Tear down an incognito conversation (fired on tab close / ending
    // incognito). Only an ephemeral row, so a stray call can never delete a
    // real conversation: false for anything else.
    async fn discard_conversation(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<super::EdgeData>()?);
        if !Conversation::by_id(pool, &raw).await?.is_some_and(|c| c.ephemeral) {
            return Ok(false);
        }
        let mut tx = crate::db::write_tx(pool).await?;
        let teardown = delete_conversation(&mut tx, &raw, &data.artifacts_dir).await?;
        tx.commit().await?;
        if let Some(t) = teardown {
            t.finish(data).await;
        }
        Ok(true)
    }
}

/// `is_valid_model`'s refusal, worded as Python's mutations word it.
pub fn unknown_model(model: &str) -> String {
    format!("unknown model {}; query `models` for the catalog", crate::pyjson::repr_str(model))
}

/// What's left of a deleted conversation once its rows are committed: its
/// files and its notebook.
pub struct Teardown {
    conversation_id: String,
    files: Vec<PathBuf>,
    /// Artifact ids whose `{id}_v*` files are swept from the artifact
    /// directory, in case a version escaped its row.
    version_globs: Vec<String>,
    artifacts_dir: PathBuf,
}

impl Teardown {
    pub async fn finish(self, data: &super::EdgeData) {
        for path in &self.files {
            if let Err(e) = std::fs::remove_file(path) {
                if e.kind() != std::io::ErrorKind::NotFound {
                    tracing::warn!("failed to unlink {}: {e}", path.display());
                }
            }
        }
        if !self.version_globs.is_empty() {
            if let Ok(entries) = std::fs::read_dir(&self.artifacts_dir) {
                for entry in entries.flatten() {
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    if self.version_globs.iter().any(|id| name.starts_with(&format!("{id}_v"))) {
                        let _ = std::fs::remove_file(entry.path());
                    }
                }
            }
        }
        data.kernels.shutdown(&self.conversation_id).await;
    }
}

/// `db/ops.py:delete_conversation`, rows only: the conversation and what its
/// ORM relationships cascade to — messages and their steps, artifacts and
/// their versions, documents and their chunks, episodes — then its
/// transcript thread (`transcript_store.delete_thread`). None when there's
/// no such conversation, which deletes nothing at all, thread included. The
/// caller commits, then runs the returned `Teardown`.
pub async fn delete_conversation(
    tx: &mut Transaction<'_, Sqlite>,
    conv_id: &str,
    artifacts_dir: &Path,
) -> sqlx::Result<Option<Teardown>> {
    let exists: Option<String> =
        sqlx::query_scalar("SELECT id FROM conversations WHERE id = ?").bind(conv_id).fetch_optional(&mut **tx).await?;
    if exists.is_none() {
        return Ok(None);
    }
    let artifacts: Vec<(String, Option<String>)> =
        sqlx::query_as("SELECT id, filename FROM artifacts WHERE conversation_id = ?")
            .bind(conv_id)
            .fetch_all(&mut **tx)
            .await?;
    let versions: Vec<String> = sqlx::query_scalar(
        "SELECT filename FROM artifact_versions WHERE artifact_id IN (SELECT id FROM artifacts WHERE conversation_id = ?)",
    )
    .bind(conv_id)
    .fetch_all(&mut **tx)
    .await?;
    let documents: Vec<String> = sqlx::query_scalar("SELECT path FROM documents WHERE conversation_id = ?")
        .bind(conv_id)
        .fetch_all(&mut **tx)
        .await?;

    for sql in [
        "DELETE FROM steps WHERE message_id IN (SELECT id FROM messages WHERE conversation_id = ?)",
        "DELETE FROM messages WHERE conversation_id = ?",
        "DELETE FROM artifact_versions WHERE artifact_id IN (SELECT id FROM artifacts WHERE conversation_id = ?)",
        "DELETE FROM artifacts WHERE conversation_id = ?",
        "DELETE FROM document_chunks WHERE document_id IN (SELECT id FROM documents WHERE conversation_id = ?)",
        "DELETE FROM documents WHERE conversation_id = ?",
        "DELETE FROM conversation_episodes WHERE conversation_id = ?",
        "DELETE FROM conversations WHERE id = ?",
    ] {
        sqlx::query(sql).bind(conv_id).execute(&mut **tx).await?;
    }
    delete_thread(tx, conv_id).await?;

    let mut files: Vec<PathBuf> = artifacts
        .iter()
        .map(|(id, filename)| match filename.as_deref().filter(|f| !f.is_empty()) {
            Some(f) => PathBuf::from(f),
            None => artifacts_dir.join(format!("{id}.md")),
        })
        .collect();
    files.extend(versions.into_iter().map(PathBuf::from));
    files.extend(documents.into_iter().map(PathBuf::from));
    Ok(Some(Teardown {
        conversation_id: conv_id.to_string(),
        files,
        version_globs: artifacts.into_iter().map(|(id, _)| id).collect(),
        artifacts_dir: artifacts_dir.to_path_buf(),
    }))
}

/// `transcript_store.delete_thread`: every row of the thread, and each of its
/// blobs no other thread still names.
async fn delete_thread(tx: &mut Transaction<'_, Sqlite>, thread_id: &str) -> sqlx::Result<()> {
    let datas: Vec<String> = sqlx::query_scalar("SELECT data FROM thread_messages WHERE thread_id = ?")
        .bind(thread_id)
        .fetch_all(&mut **tx)
        .await?;
    let mut refs = HashSet::new();
    for data in &datas {
        if let Ok(record) = serde_json::from_str::<Value>(data) {
            if let Some(content) = record.get("content") {
                blob_refs(content, &mut refs);
            }
        }
    }
    sqlx::query("DELETE FROM thread_messages WHERE thread_id = ?").bind(thread_id).execute(&mut **tx).await?;
    sqlx::query("DELETE FROM thread_state WHERE thread_id = ?").bind(thread_id).execute(&mut **tx).await?;
    for blob in refs {
        // `ThreadMessage.data.contains(ref)`, LIKE's case folding included.
        let still_used: Option<String> =
            sqlx::query_scalar("SELECT id FROM thread_messages WHERE data LIKE '%' || ? || '%' LIMIT 1")
                .bind(&blob)
                .fetch_optional(&mut **tx)
                .await?;
        if still_used.is_none() {
            sqlx::query("DELETE FROM transcript_blobs WHERE hash = ?").bind(&blob).execute(&mut **tx).await?;
        }
    }
    Ok(())
}

/// `_blob_refs`: every string under a `"blob"` key, at any depth.
fn blob_refs(value: &Value, out: &mut HashSet<String>) {
    match value {
        Value::Object(map) => {
            if let Some(Value::String(r)) = map.get("blob") {
                out.insert(r.clone());
            }
            for v in map.values() {
                blob_refs(v, out);
            }
        }
        Value::Array(items) => items.iter().for_each(|v| blob_refs(v, out)),
        _ => {}
    }
}
