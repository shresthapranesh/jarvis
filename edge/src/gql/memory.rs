//! Discrete memory items and their access log — `server/graphql/types/memory.py`,
//! the SQL-backed half of `queries/memory.py`, and `mutations/memory.py`'s
//! item writes (`addMemory`, `updateMemoryItem`, `deleteMemory`). (`agentMemory`,
//! the legacy blob, lives in the store and stays in Python.)

use async_graphql::{ComplexObject, Context, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, now_stored};
use super::{EdgeData, defer};

/// One discrete memory (kind = core | fact). Raw DB id; `updatedAt` is a
/// plain string here, not the `DateTime` scalar, as in the Python type.
#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct MemoryItem {
    pub id: String,
    pub kind: String,
    pub text: String,
    #[graphql(skip)]
    #[sqlx(rename = "updated_at")]
    pub updated: DateTime,
}

#[ComplexObject]
impl MemoryItem {
    async fn updated_at(&self) -> String {
        self.updated.0.clone()
    }

    /// When this memory was last surfaced.
    async fn last_used_at(&self, ctx: &Context<'_>) -> Result<Option<String>> {
        let row: Option<(Option<DateTime>,)> =
            sqlx::query_as("SELECT MAX(accessed_at) FROM memory_activities WHERE memory_id = ? GROUP BY memory_id")
                .bind(&self.id)
                .fetch_optional(ctx.data::<SqlitePool>()?)
                .await?;
        Ok(row.and_then(|(at,)| at).map(|at| at.0))
    }

    /// How many times this memory was surfaced.
    async fn use_count(&self, ctx: &Context<'_>) -> Result<i64> {
        let (n,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM memory_activities WHERE memory_id = ?")
            .bind(&self.id)
            .fetch_one(ctx.data::<SqlitePool>()?)
            .await?;
        Ok(n)
    }

    /// Recent audit log entries for this memory.
    async fn activities(&self, ctx: &Context<'_>, #[graphql(default = 20)] limit: i32) -> Result<Vec<MemoryActivity>> {
        MemoryActivity::for_memory(ctx.data()?, &self.id, limit).await
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct MemoryActivity {
    pub id: String,
    pub memory_id: String,
    pub conversation_id: Option<String>,
    pub kind: String,
    pub score: Option<f64>,
    pub query: Option<String>,
    pub source: String,
    #[graphql(skip)]
    #[sqlx(rename = "accessed_at")]
    pub accessed: DateTime,
}

#[ComplexObject]
impl MemoryActivity {
    async fn accessed_at(&self) -> String {
        self.accessed.0.clone()
    }
}

impl MemoryActivity {
    async fn for_memory(pool: &SqlitePool, memory_id: &str, limit: i32) -> Result<Vec<Self>> {
        Ok(sqlx::query_as(
            "SELECT id, memory_id, conversation_id, kind, score, query, source, accessed_at \
             FROM memory_activities WHERE memory_id = ? ORDER BY accessed_at DESC LIMIT ?",
        )
        .bind(memory_id)
        .bind(limit)
        .fetch_all(pool)
        .await?)
    }
}

impl MemoryItem {
    async fn by_id(pool: &SqlitePool, id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as("SELECT id, kind, text, updated_at FROM memories WHERE id = ?").bind(id).fetch_optional(pool).await?)
    }
}

async fn list_memories(pool: &SqlitePool, kind: Option<&str>) -> Result<Vec<MemoryItem>> {
    let mut sql = String::from("SELECT id, kind, text, updated_at FROM memories");
    if kind.is_some() {
        sql.push_str(" WHERE kind = ?");
    }
    sql.push_str(" ORDER BY updated_at DESC");
    let mut q = sqlx::query_as(&sql);
    if let Some(k) = kind {
        q = q.bind(k);
    }
    Ok(q.fetch_all(pool).await?)
}

#[derive(Default)]
pub struct MemoryQuery;

#[Object]
impl MemoryQuery {
    /// Discrete memory items (empty on keyless setups, which use the blob).
    async fn memories(&self, ctx: &Context<'_>, kind: Option<String>) -> Result<Vec<MemoryItem>> {
        list_memories(ctx.data()?, kind.as_deref()).await
    }

    /// Audit log for when a specific memory was surfaced.
    async fn memory_activities(
        &self,
        ctx: &Context<'_>,
        memory_id: String,
        #[graphql(default = 50)] limit: i32,
    ) -> Result<Vec<MemoryActivity>> {
        MemoryActivity::for_memory(ctx.data()?, &memory_id, limit).await
    }

    /// Every memory, for the usage overview — same rows as `memories`.
    async fn memory_usage(&self, ctx: &Context<'_>) -> Result<Vec<MemoryItem>> {
        list_memories(ctx.data()?, None).await
    }
}

#[derive(Default)]
pub struct MemoryMutation;

#[Object]
impl MemoryMutation {
    // Embedded and merged into a near-duplicate of its kind, as the agent's
    // `remember` does. An embedder that fails is Python's to report: the
    // operation goes there before anything is written.
    async fn add_memory(
        &self,
        ctx: &Context<'_>,
        text: String,
        #[graphql(default_with = "\"fact\".to_string()")] kind: String,
    ) -> Result<MemoryItem> {
        let text = text.trim();
        if text.is_empty() {
            return Err("memory text is empty".into());
        }
        let kind = if kind == "core" || kind == "fact" { kind.as_str() } else { "fact" };
        let pool: &SqlitePool = ctx.data()?;
        let id = crate::agent::retrieve::upsert_memory(pool, &ctx.data::<EdgeData>()?.http, text, kind)
            .await
            .map_err(|e| defer(format!("embedding failed: {e}")))?;
        MemoryItem::by_id(pool, &id).await?.ok_or_else(|| "memory vanished".into())
    }

    // Re-embedded before the row is looked up, as Python does; a kind other
    // than core/fact leaves the kind alone.
    async fn update_memory_item(&self, ctx: &Context<'_>, id: String, text: String, kind: Option<String>) -> Result<MemoryItem> {
        let text = text.trim();
        if text.is_empty() {
            return Err("memory text is empty".into());
        }
        let pool: &SqlitePool = ctx.data()?;
        let blob = crate::agent::embed::for_storage(pool, &ctx.data::<EdgeData>()?.http, text)
            .await
            .map_err(|e| defer(format!("embedding failed: {e}")))?;
        let kind = kind.filter(|k| k == "core" || k == "fact");
        let updated = sqlx::query(
            "UPDATE memories SET text = ?, kind = COALESCE(?, kind), embedding = ?, updated_at = ? WHERE id = ?",
        )
        .bind(text)
        .bind(&kind)
        .bind(&blob)
        .bind(now_stored())
        .bind(&id)
        .execute(pool)
        .await?;
        if updated.rows_affected() == 0 {
            return Err("memory not found".into());
        }
        MemoryItem::by_id(pool, &id).await?.ok_or_else(|| "memory not found".into())
    }

    // False when there was nothing to delete. The item's access log is left
    // behind, as Python leaves it: there's no ORM relationship and foreign
    // keys are off, so its `ON DELETE CASCADE` never fires there either.
    async fn delete_memory(&self, ctx: &Context<'_>, id: String) -> Result<bool> {
        let deleted = sqlx::query("DELETE FROM memories WHERE id = ?").bind(&id).execute(ctx.data::<SqlitePool>()?).await?;
        Ok(deleted.rows_affected() > 0)
    }
}
