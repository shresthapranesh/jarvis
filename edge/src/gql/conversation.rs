//! Conversation, Message and Step — `server/graphql/types/conversation.py`
//! and `server/graphql/queries/conversation.py`.

use std::collections::HashMap;

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_cursor, decode_global_id, encode_cursor, global_id};
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
