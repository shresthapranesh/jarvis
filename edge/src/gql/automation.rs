//! Automation and AutomationRun — `server/graphql/types/automation.py`,
//! `queries/automation.py`, and `mutations/automation.py`'s create, update
//! and delete (with `db/ops.py`'s automation CRUD). A change to any of those
//! is made here too.
//!
//! `nextRunAt` is the scheduler's answer, and the scheduler is the edge's
//! now (`schedule.rs`): the same cron engine that fires the job reports when
//! it will.

use std::collections::HashMap;

use async_graphql::{ComplexObject, Context, ID, InputObject, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::EdgeData;
use super::codec::{DateTime, decode_global_id, global_id, iso_from_db, new_id, now_stored};
use super::conversation::{delete_conversation, unknown_model};

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

#[derive(InputObject)]
pub struct AutomationInput {
    name: String,
    /// "prompt" | "code" | "webhook" | "monitor"
    input_type: String,
    description: Option<String>,
    prompt_text: Option<String>,
    model: Option<String>,
    code_text: Option<String>,
    webhook_url: Option<String>,
    webhook_method: Option<String>,
    /// JSON string
    webhook_headers: Option<String>,
    webhook_body: Option<String>,
    /// cron expression
    schedule: Option<String>,
    #[graphql(default = true)]
    enabled: bool,
    /// prompt type only: share one thread across runs
    #[graphql(default = false)]
    stateful: bool,
    /// JSON string
    notifications: Option<String>,
}

/// `_validate_input`: a known model, and a schedule the scheduler can build.
async fn validate(ctx: &Context<'_>, input: &AutomationInput) -> Result<()> {
    if let Some(m) = &input.model {
        if !crate::catalog::is_valid_model(ctx.data()?, m).await? {
            return Err(unknown_model(m).into());
        }
    }
    if let Some(expr) = input.schedule.as_deref().filter(|s| !s.is_empty()) {
        // Through `Trigger::parse` (= `_cron`), so validation accepts exactly
        // what the scheduler fires.
        if crate::cron::Trigger::parse(expr, ctx.data::<EdgeData>()?.tz).is_err() {
            return Err("invalid cron expression".into());
        }
    }
    Ok(())
}

/// The columns an input writes, in `AUTOMATION_COLUMNS` order after `id`.
const INPUT_COLUMNS: [&str; 14] = [
    "name", "description", "input_type", "prompt_text", "model", "code_text", "webhook_url", "webhook_method",
    "webhook_headers", "webhook_body", "schedule", "enabled", "stateful", "notifications",
];

fn bind_input<'q>(
    mut q: sqlx::query::Query<'q, sqlx::Sqlite, sqlx::sqlite::SqliteArguments<'q>>,
    input: &'q AutomationInput,
) -> sqlx::query::Query<'q, sqlx::Sqlite, sqlx::sqlite::SqliteArguments<'q>> {
    q = q.bind(&input.name).bind(&input.description).bind(&input.input_type).bind(&input.prompt_text);
    q = q.bind(&input.model).bind(&input.code_text).bind(&input.webhook_url).bind(&input.webhook_method);
    q = q.bind(&input.webhook_headers).bind(&input.webhook_body).bind(&input.schedule);
    q.bind(input.enabled).bind(input.stateful).bind(&input.notifications)
}

#[derive(Default)]
pub struct AutomationMutation;

#[Object]
impl AutomationMutation {
    async fn create_automation(&self, ctx: &Context<'_>, input: AutomationInput) -> Result<Automation> {
        validate(ctx, &input).await?;
        let pool: &SqlitePool = ctx.data()?;
        let (id, now) = (new_id(), now_stored());
        let sql = format!(
            "INSERT INTO automations (id, {}, created_at, updated_at) VALUES (?, {}?, ?)",
            INPUT_COLUMNS.join(", "),
            "?, ".repeat(INPUT_COLUMNS.len()),
        );
        bind_input(sqlx::query(&sql).bind(&id), &input).bind(&now).bind(&now).execute(pool).await?;
        if input.enabled && input.schedule.as_deref().is_some_and(|s| !s.is_empty()) {
            ctx.data::<EdgeData>()?.scheduler.schedules_changed();
        }
        Automation::by_id(pool, &id).await?.ok_or_else(|| "automation vanished".into())
    }

    // Every field is written, as Python's setattr loop writes them: one left
    // out of the input is cleared, not kept.
    async fn update_automation(&self, ctx: &Context<'_>, id: ID, input: AutomationInput) -> Result<Automation> {
        let (_, raw) = decode_global_id(&id)?;
        validate(ctx, &input).await?;
        let pool: &SqlitePool = ctx.data()?;
        let sets: String = INPUT_COLUMNS.iter().map(|c| format!("{c} = ?, ")).collect();
        let sql = format!("UPDATE automations SET {sets}updated_at = ? WHERE id = ?");
        let updated = bind_input(sqlx::query(&sql), &input).bind(now_stored()).bind(&raw).execute(pool).await?;
        if updated.rows_affected() == 0 {
            return Err("automation not found".into());
        }
        // Its old schedule goes either way.
        ctx.data::<EdgeData>()?.scheduler.schedules_changed();
        Automation::by_id(pool, &raw).await?.ok_or_else(|| "automation not found".into())
    }

    // An agent's delete may need a human's approval first (`gate_action`),
    // asked before anything is unscheduled. Its runs go with it, as the ORM
    // cascade takes them, and so does a stateful automation's conversation.
    async fn delete_automation(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        if ctx.data::<super::RequestFrom>()?.caller == super::router::Caller::Agent {
            let pool: &SqlitePool = ctx.data()?;
            let name: Option<String> =
                sqlx::query_scalar("SELECT name FROM automations WHERE id = ?").bind(&raw).fetch_optional(pool).await?;
            let name = name.ok_or("automation not found")?;
            let payload = serde_json::json!({"automation_id": raw, "name": name});
            super::approval::gate_action(ctx, "delete_automation", payload).await?;
        }
        if !delete_automation(ctx.data()?, &raw, ctx.data()?).await? {
            return Err("automation not found".into());
        }
        Ok(true)
    }
}

/// `db/ops.py:delete_automation` and the scheduler's `_remove_scheduler_job`:
/// false when there's no such automation.
pub async fn delete_automation(pool: &SqlitePool, raw_id: &str, data: &EdgeData) -> sqlx::Result<bool> {
    let mut tx = crate::db::write_tx(pool).await?;
    let exists: Option<String> =
        sqlx::query_scalar("SELECT id FROM automations WHERE id = ?").bind(raw_id).fetch_optional(&mut *tx).await?;
    if exists.is_none() {
        return Ok(false);
    }
    let teardown = delete_conversation(&mut tx, &format!("automation_{raw_id}"), &data.artifacts_dir).await?;
    sqlx::query("DELETE FROM automation_runs WHERE automation_id = ?").bind(raw_id).execute(&mut *tx).await?;
    sqlx::query("DELETE FROM automations WHERE id = ?").bind(raw_id).execute(&mut *tx).await?;
    tx.commit().await?;
    data.scheduler.schedules_changed();
    if let Some(t) = teardown {
        t.finish(&data.kernels).await;
    }
    Ok(true)
}
