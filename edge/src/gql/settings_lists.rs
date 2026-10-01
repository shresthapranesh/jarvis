//! The small list pages: NotificationChannel (`types/notification.py`), Skill
//! (`types/skill.py`) and PendingApproval (`types/approval.py`).

use async_graphql::{ComplexObject, Context, ID, InputObject, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id, new_id, now_stored};
use super::write::update_row;

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct NotificationChannel {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub name: String,
    #[sqlx(rename = "type")]
    pub r#type: String,
    pub target: String,
    pub created_at: DateTime,
    pub updated_at: DateTime,
}

const CHANNEL_COLUMNS: &str = "id, name, type, target, created_at, updated_at";

impl NotificationChannel {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {CHANNEL_COLUMNS} FROM notification_channels WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl NotificationChannel {
    pub async fn id(&self) -> ID {
        global_id("NotificationChannel", &self.raw_id)
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Skill {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub name: String,
    pub description: String,
    pub body: String,
    pub enabled: bool,
    pub created_at: DateTime,
    pub updated_at: DateTime,
}

const SKILL_COLUMNS: &str = "id, name, description, body, enabled, created_at, updated_at";

impl Skill {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {SKILL_COLUMNS} FROM skills WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Skill {
    pub async fn id(&self) -> ID {
        global_id("Skill", &self.raw_id)
    }
}

/// One outstanding human-in-the-loop request. Not a Node: nothing refetches
/// a single approval.
#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct PendingApproval {
    pub id: String,
    pub source: String,
    pub kind: String,
    pub question: String,
    pub label: String,
    pub tool: Option<String>,
    pub args_json: Option<String>,
    #[graphql(skip)]
    #[sqlx(rename = "requested_at")]
    pub requested_at_naive: DateTime,
    #[graphql(skip)]
    pub action: Option<String>,
    pub parent_id: Option<String>,
    pub board_task_id: Option<String>,
}

#[ComplexObject]
impl PendingApproval {
    /// UTC-aware, so `+00:00` — SQLite hands back naive datetimes and the
    /// Python type forces them aware.
    async fn requested_at(&self) -> DateTime {
        self.requested_at_naive.utc()
    }

    /// True when approving is what performs the operation, because nothing
    /// is blocked waiting on it.
    async fn deferred(&self) -> bool {
        self.action.is_some()
    }
}

#[derive(Default)]
pub struct ListsQuery;

#[Object]
impl ListsQuery {
    async fn notification_channels(&self, ctx: &Context<'_>) -> Result<Vec<NotificationChannel>> {
        Ok(sqlx::query_as(&format!(
            "SELECT {CHANNEL_COLUMNS} FROM notification_channels ORDER BY created_at ASC"
        ))
        .fetch_all(ctx.data::<SqlitePool>()?)
        .await?)
    }

    async fn skills(&self, ctx: &Context<'_>) -> Result<Vec<Skill>> {
        Ok(sqlx::query_as(&format!("SELECT {SKILL_COLUMNS} FROM skills ORDER BY name ASC"))
            .fetch_all(ctx.data::<SqlitePool>()?)
            .await?)
    }

    /// Everything currently awaiting a human, newest request first.
    async fn pending_approvals(&self, ctx: &Context<'_>) -> Result<Vec<PendingApproval>> {
        Ok(sqlx::query_as(
            "SELECT id, source, kind, question, label, tool, args_json, requested_at, action, parent_id, \
             board_task_id FROM approvals WHERE status = 'pending' ORDER BY requested_at DESC LIMIT 200",
        )
        .fetch_all(ctx.data::<SqlitePool>()?)
        .await?)
    }
}

#[derive(InputObject)]
pub struct NotificationChannelCreateInput {
    name: String,
    r#type: String,
    target: String,
}

#[derive(InputObject)]
pub struct NotificationChannelUpdateInput {
    name: Option<String>,
    r#type: Option<String>,
    target: Option<String>,
}

/// Whether a `notifications` JSON array names this channel —
/// `db/ops.py:_notifications_ref`. Anything that isn't a list of objects
/// references nothing.
fn notifications_ref(raw: Option<&str>, channel_id: &str) -> bool {
    let Some(Ok(serde_json::Value::Array(entries))) = raw.map(serde_json::from_str::<serde_json::Value>) else {
        return false;
    };
    entries.iter().any(|e| e.get("id").and_then(|v| v.as_str()) == Some(channel_id))
}

#[derive(Default)]
pub struct ListsMutation;

#[Object]
impl ListsMutation {
    async fn create_notification_channel(
        &self,
        ctx: &Context<'_>,
        input: NotificationChannelCreateInput,
    ) -> Result<NotificationChannel> {
        if input.target.trim().is_empty() {
            return Err("target required".into());
        }
        if input.name.trim().is_empty() {
            return Err("name required".into());
        }
        let pool: &SqlitePool = ctx.data()?;
        let (id, now) = (new_id(), now_stored());
        sqlx::query(
            "INSERT INTO notification_channels (id, name, type, target, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        )
        .bind(&id)
        .bind(input.name.trim())
        .bind(&input.r#type)
        .bind(input.target.trim())
        .bind(&now)
        .bind(&now)
        .execute(pool)
        .await?;
        NotificationChannel::by_id(pool, &id).await?.ok_or_else(|| "channel vanished".into())
    }

    async fn update_notification_channel(
        &self,
        ctx: &Context<'_>,
        id: ID,
        input: NotificationChannelUpdateInput,
    ) -> Result<NotificationChannel> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        if NotificationChannel::by_id(pool, &raw).await?.is_none() {
            return Err("channel not found".into());
        }
        if input.target.as_deref().is_some_and(|t| t.trim().is_empty()) {
            return Err("target required".into());
        }
        if input.name.as_deref().is_some_and(|n| n.trim().is_empty()) {
            return Err("name required".into());
        }
        let mut sets: Vec<(&str, String)> = Vec::new();
        if let Some(name) = &input.name {
            sets.push(("name", name.trim().to_string()));
        }
        if let Some(ty) = &input.r#type {
            sets.push(("type", ty.clone()));
        }
        if let Some(target) = &input.target {
            sets.push(("target", target.trim().to_string()));
        }
        // An update with nothing in it still bumps updated_at, as in Python.
        update_row(pool, "notification_channels", &raw, &sets).await?;
        NotificationChannel::by_id(pool, &raw).await?.ok_or_else(|| "channel not found".into())
    }

    // Refused while any automation or workflow still delivers to it.
    async fn delete_notification_channel(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        if NotificationChannel::by_id(pool, &raw).await?.is_none() {
            return Err("channel not found".into());
        }
        let mut refs = Vec::new();
        for (kind, table) in [("automation", "automations"), ("workflow", "workflows")] {
            let rows: Vec<(String, Option<String>)> =
                sqlx::query_as(&format!("SELECT name, notifications FROM {table}")).fetch_all(pool).await?;
            for (name, raw_json) in rows {
                if notifications_ref(raw_json.as_deref(), &raw) {
                    refs.push(format!("{kind}:{name}"));
                }
            }
        }
        if !refs.is_empty() {
            return Err(format!("channel in use by {} reference(s): {}", refs.len(), refs.join(", ")).into());
        }
        sqlx::query("DELETE FROM notification_channels WHERE id = ?").bind(&raw).execute(pool).await?;
        Ok(true)
    }

    // The human path only: an agent's delete is approval-gated, and the
    // router sends `X-Jarvis-Caller: agent` requests for it to Python.
    async fn delete_skill(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let deleted = sqlx::query("DELETE FROM skills WHERE id = ?").bind(&raw).execute(ctx.data::<SqlitePool>()?).await?;
        if deleted.rows_affected() == 0 {
            return Err("skill not found".into());
        }
        Ok(true)
    }
}
