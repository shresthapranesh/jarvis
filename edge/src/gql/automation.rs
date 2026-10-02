//! Automation and AutomationRun — `server/graphql/types/automation.py` and
//! `queries/automation.py`.
//!
//! `nextRunAt` is the scheduler's answer, and the scheduler is the edge's
//! now (`schedule.rs`): the same cron engine that fires the job reports when
//! it will.

use std::collections::HashMap;

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::EdgeData;
use super::codec::{DateTime, decode_global_id, global_id, iso_from_db};

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Automation {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub name: String,
    pub description: Option<String>,
    pub input_type: String,
    pub prompt_text: Option<String>,
    pub model: Option<String>,
    pub code_text: Option<String>,
    pub webhook_url: Option<String>,
    pub webhook_method: Option<String>,
    pub webhook_headers: Option<String>,
    pub webhook_body: Option<String>,
    pub schedule: Option<String>,
    pub enabled: bool,
    pub stateful: bool,
    pub notifications: Option<String>,
    pub created_at: DateTime,
    pub updated_at: DateTime,
    // Populated by the list query only, as in Python.
    #[sqlx(skip)]
    pub last_run_status: Option<String>,
    #[sqlx(skip)]
    pub last_run_at: Option<String>,
    #[sqlx(skip)]
    #[graphql(name = "successCount7d")]
    pub success_count_7d: Option<i64>,
    #[sqlx(skip)]
    #[graphql(name = "totalCount7d")]
    pub total_count_7d: Option<i64>,
}

const AUTOMATION_COLUMNS: &str = "id, name, description, input_type, prompt_text, model, code_text, webhook_url, \
     webhook_method, webhook_headers, webhook_body, schedule, enabled, stateful, notifications, created_at, updated_at";

impl Automation {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {AUTOMATION_COLUMNS} FROM automations WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Automation {
    pub async fn id(&self) -> ID {
        global_id("Automation", &self.raw_id)
    }

    /// The conversation backing a stateful automation's shared thread.
    async fn conversation_id(&self) -> Option<String> {
        (self.stateful || self.input_type == "monitor").then(|| format!("automation_{}", self.raw_id))
    }

    async fn next_run_at(&self, ctx: &Context<'_>) -> Result<Option<String>> {
        Ok(crate::schedule::next_run_at(self.schedule.as_deref(), self.enabled, ctx.data::<EdgeData>()?.tz))
    }
}

/// `list_automations_with_stats`: newest first, with the last 7 days'
/// totals and the latest run of any age.
async fn with_stats(pool: &SqlitePool) -> Result<Vec<Automation>> {
    let mut automations: Vec<Automation> =
        sqlx::query_as(&format!("SELECT {AUTOMATION_COLUMNS} FROM automations ORDER BY created_at DESC"))
            .fetch_all(pool)
            .await?;
    if automations.is_empty() {
        return Ok(automations);
    }
    let since = (chrono::Utc::now() - chrono::Duration::days(7)).format("%Y-%m-%d %H:%M:%S%.6f").to_string();
    let totals: Vec<(String, i64, i64)> = sqlx::query_as(
        "SELECT automation_id, COUNT(id), SUM(CASE WHEN status IN ('done', 'no_change') THEN 1 ELSE 0 END) \
         FROM automation_runs WHERE started_at >= ? GROUP BY automation_id",
    )
    .bind(since)
    .fetch_all(pool)
    .await?;
    let totals: HashMap<_, _> = totals.into_iter().map(|(id, total, ok)| (id, (total, ok))).collect();
    // A tie on the latest start time: the last row wins, as Python's dict does.
    let latest: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT r.automation_id, r.status, r.started_at FROM automation_runs r JOIN \
         (SELECT automation_id, max(started_at) AS latest_at FROM automation_runs GROUP BY automation_id) l \
         ON r.automation_id = l.automation_id AND r.started_at = l.latest_at",
    )
    .fetch_all(pool)
    .await?;
    let latest: HashMap<_, _> = latest.into_iter().map(|(id, status, at)| (id, (status, at))).collect();
    for a in &mut automations {
        let (total, ok) = totals.get(&a.raw_id).copied().unwrap_or((0, 0));
        a.total_count_7d = Some(total);
        a.success_count_7d = Some(ok);
        if let Some((status, at)) = latest.get(&a.raw_id) {
            a.last_run_status = Some(status.clone());
            a.last_run_at = Some(iso_from_db(at).0);
        }
    }
    Ok(automations)
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct AutomationRun {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub automation_id: String,
    pub status: String,
    pub triggered_by: String,
    pub output: Option<String>,
    pub error: Option<String>,
    pub started_at: DateTime,
    pub finished_at: Option<DateTime>,
}

const RUN_COLUMNS: &str = "id, automation_id, status, triggered_by, output, error, started_at, finished_at";

impl AutomationRun {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {RUN_COLUMNS} FROM automation_runs WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl AutomationRun {
    pub async fn id(&self) -> ID {
        global_id("AutomationRun", &self.raw_id)
    }
}

#[derive(Default)]
pub struct AutomationQuery;

#[Object]
impl AutomationQuery {
    async fn automations(&self, ctx: &Context<'_>) -> Result<Vec<Automation>> {
        with_stats(ctx.data()?).await
    }

    async fn automation(&self, ctx: &Context<'_>, id: ID) -> Result<Option<Automation>> {
        let (_, raw) = decode_global_id(&id)?;
        Automation::by_id(ctx.data()?, &raw).await
    }

    async fn automation_runs(&self, ctx: &Context<'_>, automation_id: ID) -> Result<Vec<AutomationRun>> {
        let (_, raw) = decode_global_id(&automation_id)?;
        Ok(sqlx::query_as(&format!(
            "SELECT {RUN_COLUMNS} FROM automation_runs WHERE automation_id = ? ORDER BY started_at DESC LIMIT 50"
        ))
        .bind(raw)
        .fetch_all(ctx.data::<SqlitePool>()?)
        .await?)
    }
}
