//! Discrete memory items and their access log — `server/graphql/types/memory.py`
//! and the SQL-backed half of `queries/memory.py`. (`agentMemory`, the legacy
//! blob, lives in the LangGraph store and stays in Python.)

use async_graphql::{ComplexObject, Context, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::DateTime;

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
