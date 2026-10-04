//! An agent thread in the transcript tables — the edge's half of
//! `core/transcript_store.py`. A thread is its live `thread_messages` rows in
//! `seq` order (v1 records, `llm::transcript`) and its `thread_state` todos.
//! Python reads what this writes and the other way round, so a conversation
//! can move between the runtimes turn by turn, or mid-turn.

use std::collections::HashSet;

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;
use sqlx::{Row, SqlitePool};

use crate::gql::codec::{new_id, now_stored};
use crate::llm::Blobs;
use crate::llm::transcript::{Content, Message, Part, Typed};
use crate::pyjson;

pub struct Thread {
    pub id: String,
    pub messages: Vec<Message>,
    /// The todo list as stored (`[{text, status}]`), empty when none.
    pub todos: Vec<Value>,
    /// Media bytes the messages refer to, base64, for the model call.
    pub blobs: Blobs,
    /// Whether the thread has any rows at all.
    pub exists: bool,
}

impl Thread {
    /// `load_thread`: the live messages in order, and the todos.
    pub async fn load(pool: &SqlitePool, id: &str) -> Result<Self, String> {
        let rows: Vec<String> = sqlx::query_scalar(
            "SELECT data FROM thread_messages WHERE thread_id = ? AND evicted_at IS NULL ORDER BY seq",
        )
        .bind(id)
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
        let mut messages = Vec::with_capacity(rows.len());
        for data in &rows {
            messages.push(serde_json::from_str::<Message>(data).map_err(|e| format!("thread {id}: {e}"))?);
        }
        let state: Option<Option<String>> = sqlx::query_scalar("SELECT todos FROM thread_state WHERE thread_id = ?")
            .bind(id)
            .fetch_optional(pool)
            .await
            .map_err(|e| e.to_string())?;
        let todos = match state.as_ref().and_then(|t| t.as_deref()).map(serde_json::from_str::<Value>) {
            Some(Ok(Value::Array(items))) => items,
            _ => vec![],
        };
        let blobs = load_blobs(pool, &messages).await?;
        Ok(Thread { id: id.into(), exists: !rows.is_empty() || state.is_some(), messages, todos, blobs })
    }

    /// `apply_messages` for what the agent loop writes — replies, results,
    /// prompts: a message whose id is already live replaces it in place,
    /// anything else is appended. Each gets an id if it has none.
    pub async fn apply(&mut self, pool: &SqlitePool, mut incoming: Vec<Message>) -> Result<(), String> {
        for m in &mut incoming {
            if m.id.is_none() {
                m.id = Some(new_id());
            }
        }
        let mut tx = pool.begin().await.map_err(|e| e.to_string())?;
        let top: Option<i64> = sqlx::query_scalar("SELECT MAX(seq) FROM thread_messages WHERE thread_id = ?")
            .bind(&self.id)
            .fetch_one(&mut *tx)
            .await
            .map_err(|e| e.to_string())?;
        let mut next = top.map_or(0, |t| t + 1);
        let now = now_stored();
        for m in &incoming {
            let id = m.id.as_deref().expect("given above");
            let data = pyjson::dumps_unicode(&serde_json::to_value(m).map_err(|e| e.to_string())?);
            let role = serde_json::to_value(m.role).map_err(|e| e.to_string())?;
            let role = role.as_str().unwrap_or_default();
            let replaced = sqlx::query(
                "UPDATE thread_messages SET data = ?, role = ? \
                 WHERE thread_id = ? AND message_id = ? AND evicted_at IS NULL",
            )
            .bind(&data)
            .bind(role)
            .bind(&self.id)
            .bind(id)
            .execute(&mut *tx)
            .await
            .map_err(|e| e.to_string())?;
            if replaced.rows_affected() == 0 {
                sqlx::query(
                    "INSERT INTO thread_messages (id, thread_id, seq, message_id, role, data, evicted_at, created_at) \
                     VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                )
                .bind(new_id())
                .bind(&self.id)
                .bind(next)
                .bind(id)
                .bind(role)
                .bind(&data)
                .bind(&now)
                .execute(&mut *tx)
                .await
                .map_err(|e| e.to_string())?;
                next += 1;
            }
        }
        tx.commit().await.map_err(|e| e.to_string())?;
        for m in incoming {
            match self.messages.iter_mut().find(|have| have.id == m.id) {
                Some(have) => *have = m,
                None => self.messages.push(m),
            }
        }
        self.exists = true;
        Ok(())
    }

    /// `set_todos`: the list as given, stored as Python stores it.
    pub async fn set_todos(&mut self, pool: &SqlitePool, todos: Vec<Value>) -> Result<(), String> {
        let value = pyjson::dumps_unicode(&Value::Array(todos.clone()));
        let now = now_stored();
        sqlx::query(
            "INSERT INTO thread_state (thread_id, todos, source, updated_at) VALUES (?, ?, NULL, ?) \
             ON CONFLICT(thread_id) DO UPDATE SET todos = excluded.todos, updated_at = excluded.updated_at",
        )
        .bind(&self.id)
        .bind(value)
        .bind(now)
        .execute(pool)
        .await
        .map_err(|e| e.to_string())?;
        self.todos = todos;
        self.exists = true;
        Ok(())
    }
}

/// The `transcript_blobs` the messages' media parts name, as base64.
async fn load_blobs(pool: &SqlitePool, messages: &[Message]) -> Result<Blobs, String> {
    let mut refs = HashSet::new();
    for m in messages {
        if let Content::Parts(parts) = &m.content {
            for part in parts {
                if let Part::Typed(Typed::Image(media) | Typed::File(media)) = part {
                    refs.extend(media.blob.clone());
                }
            }
        }
    }
    let mut blobs = Blobs::new();
    for hash in refs {
        let row = sqlx::query("SELECT data FROM transcript_blobs WHERE hash = ?")
            .bind(&hash)
            .fetch_optional(pool)
            .await
            .map_err(|e| e.to_string())?;
        if let Some(row) = row {
            blobs.insert(hash, STANDARD.encode(row.get::<Vec<u8>, _>("data")));
        }
    }
    Ok(blobs)
}
