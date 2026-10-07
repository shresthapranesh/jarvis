//! Discrete memory items and their access log — `server/graphql/types/memory.py`,
//! the SQL-backed half of `queries/memory.py`, and `mutations/memory.py`'s
//! item writes (`addMemory`, `updateMemoryItem`, `deleteMemory`) — and the
//! agent's free-text blob (`agentMemory`, `updateMemory`, `deleteAgentMemory`),
//! the `AGENTS.md` document in `kv_store` that `KvStore` reads and writes.

use async_graphql::{ComplexObject, Context, Object, Result, SimpleObject};
use serde_json::{Value, json};
use sqlx::{Sqlite, SqlitePool, Transaction};

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

/// The agent's free-text memory blob.
#[derive(SimpleObject)]
pub struct Memory {
    content: String,
    exists: bool,
    modified_at: Option<String>,
}

impl Memory {
    fn absent() -> Self {
        Memory { content: String::new(), exists: false, modified_at: None }
    }
}

/// `memory_consolidation._MEMORY_NS` (joined, as `KvStore` keys it) and the keys.
const BLOB_NS: &str = "memory";
const BLOB_KEY: &str = "AGENTS.md";
const LEGACY_KEY: &str = "/AGENTS.md";

async fn kv_get(tx: &mut Transaction<'_, Sqlite>, key: &str) -> Result<Option<Value>> {
    let raw: Option<String> = sqlx::query_scalar("SELECT value FROM kv_store WHERE namespace = ? AND key = ?")
        .bind(BLOB_NS)
        .bind(key)
        .fetch_optional(&mut **tx)
        .await?;
    // A value only Python reads faithfully (not JSON) is Python's to answer.
    raw.map(|r| serde_json::from_str(&r).map_err(|_| defer("kv_store value is not JSON".into()))).transpose()
}

/// `KvStore.aput`: insert, or replace the value (and bump `updated_at`).
async fn kv_put(tx: &mut Transaction<'_, Sqlite>, key: &str, value: &Value) -> Result<()> {
    let now = now_stored();
    sqlx::query(
        "INSERT INTO kv_store (namespace, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?) \
         ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
    )
    .bind(BLOB_NS)
    .bind(key)
    .bind(crate::pyjson::dumps_unicode(value))
    .bind(&now)
    .bind(&now)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// `_migrate_legacy_key`: the pre-fix `/AGENTS.md` copied onto the canonical
/// key when that has nothing — the legacy row is kept as a backup.
pub(crate) async fn migrate_legacy_key(tx: &mut Transaction<'_, Sqlite>) -> Result<()> {
    if kv_get(tx, BLOB_KEY).await?.is_some() {
        return Ok(());
    }
    if let Some(legacy) = kv_get(tx, LEGACY_KEY).await? {
        kv_put(tx, BLOB_KEY, &legacy).await?;
    }
    Ok(())
}

/// `datetime.now(timezone.utc).isoformat()`: microseconds left out when zero.
pub(crate) fn now_iso() -> String {
    let now = chrono::Utc::now();
    let fmt = if now.timestamp_subsec_micros() == 0 { "%Y-%m-%dT%H:%M:%S+00:00" } else { "%Y-%m-%dT%H:%M:%S%.6f+00:00" };
    now.format(fmt).to_string()
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

    /// The legacy free-text blob (keyless fallback / pre-migration backup).
    async fn agent_memory(&self, ctx: &Context<'_>) -> Result<Memory> {
        let mut tx = crate::db::write_tx(ctx.data()?).await?;
        migrate_legacy_key(&mut tx).await?;
        let item = kv_get(&mut tx, BLOB_KEY).await?;
        tx.commit().await?;
        let Some(value) = item else { return Ok(Memory::absent()) };
        let content = match value.get("content") {
            None => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(Value::Array(lines)) if lines.iter().all(Value::is_string) => {
                lines.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("\n")
            }
            Some(_) => return Err(defer("agent memory content Python would coerce".into())),
        };
        let modified_at = match value.get("modified_at") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => return Err(defer("agent memory modified_at Python would coerce".into())),
        };
        Ok(Memory { content, exists: true, modified_at })
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
    // `remember` does. An embedder that fails fails the write, before
    // anything is written.
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
            .map_err(|e| format!("embedding failed: {e}"))?;
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
            .map_err(|e| format!("embedding failed: {e}"))?;
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

    // Replace the free-text blob; its first `created_at` is kept.
    async fn update_memory(&self, ctx: &Context<'_>, content: String) -> Result<Memory> {
        let mut tx = crate::db::write_tx(ctx.data()?).await?;
        migrate_legacy_key(&mut tx).await?;
        let now = now_iso();
        let created_at = kv_get(&mut tx, BLOB_KEY)
            .await?
            .and_then(|v| v.get("created_at").filter(|c| crate::pyjson::truthy(c)).cloned())
            .unwrap_or_else(|| Value::String(now.clone()));
        let value = json!({"content": content, "encoding": "utf-8", "created_at": created_at, "modified_at": now});
        kv_put(&mut tx, BLOB_KEY, &value).await?;
        tx.commit().await?;
        Ok(Memory { content, exists: true, modified_at: Some(now) })
    }

    // A consolidation pass now. With the agent loop off, Python's.
    async fn consolidate_memory(&self, ctx: &Context<'_>, model: Option<String>) -> Result<String> {
        if !crate::agent::route::enabled() {
            return Err(defer("JARVIS_AGENT_RUNTIME=python".into()));
        }
        let pool: &SqlitePool = ctx.data()?;
        Ok(crate::consolidate::memory::consolidate(pool, &ctx.data::<EdgeData>()?.http, model.as_deref()).await?)
    }

    // Delete the blob entirely — `main.py memory reset`. Not the same as
    // `updateMemory("")`, which leaves an empty entry that still `exists`.
    async fn delete_agent_memory(&self, ctx: &Context<'_>) -> Result<Memory> {
        let mut tx = crate::db::write_tx(ctx.data()?).await?;
        migrate_legacy_key(&mut tx).await?;
        sqlx::query("DELETE FROM kv_store WHERE namespace = ? AND key = ?")
            .bind(BLOB_NS)
            .bind(BLOB_KEY)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(Memory::absent())
    }

    // False when there was nothing to delete. The item's access log is left
    // behind, as Python leaves it: there's no ORM relationship and foreign
    // keys are off, so its `ON DELETE CASCADE` never fires there either.
    async fn delete_memory(&self, ctx: &Context<'_>, id: String) -> Result<bool> {
        let deleted = sqlx::query("DELETE FROM memories WHERE id = ?").bind(&id).execute(ctx.data::<SqlitePool>()?).await?;
        Ok(deleted.rows_affected() > 0)
    }
}
