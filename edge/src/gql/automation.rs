//! AutomationRun — `server/graphql/types/automation.py` and
//! `queries/automation.py`.
//!
//! `Automation` itself stays in Python until the scheduler moves: its
//! `nextRunAt` is APScheduler's answer, DST quirks included, and only the
//! process that fires the job can say when it will.

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id};

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
