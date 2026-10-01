//! The small list pages: NotificationChannel (`types/notification.py`), Skill
//! (`types/skill.py`) and PendingApproval (`types/approval.py`).

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, global_id};

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
