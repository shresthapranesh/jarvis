//! Workflow and WorkflowRun — `server/graphql/types/workflow.py` and
//! `queries/workflow.py`.

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id};

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Workflow {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub name: String,
    pub description: Option<String>,
    pub definition: String,
    pub notifications: Option<String>,
    pub created_at: DateTime,
    pub updated_at: DateTime,
}

const WORKFLOW_COLUMNS: &str = "id, name, description, definition, notifications, created_at, updated_at";

impl Workflow {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {WORKFLOW_COLUMNS} FROM workflows WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Workflow {
    pub async fn id(&self) -> ID {
        global_id("Workflow", &self.raw_id)
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct WorkflowRun {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub workflow_id: String,
    pub status: String,
    pub inputs: Option<String>,
    pub outputs: Option<String>,
    pub node_results: Option<String>,
    pub error: Option<String>,
    pub started_at: DateTime,
    pub finished_at: Option<DateTime>,
}

const RUN_COLUMNS: &str =
    "id, workflow_id, status, inputs, outputs, node_results, error, started_at, finished_at";

impl WorkflowRun {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {RUN_COLUMNS} FROM workflow_runs WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl WorkflowRun {
    pub async fn id(&self) -> ID {
        global_id("WorkflowRun", &self.raw_id)
    }
}

#[derive(Default)]
pub struct WorkflowQuery;

#[Object]
impl WorkflowQuery {
    async fn workflows(&self, ctx: &Context<'_>) -> Result<Vec<Workflow>> {
        Ok(sqlx::query_as(&format!("SELECT {WORKFLOW_COLUMNS} FROM workflows ORDER BY created_at DESC"))
            .fetch_all(ctx.data::<SqlitePool>()?)
            .await?)
    }

    async fn workflow(&self, ctx: &Context<'_>, id: ID) -> Result<Option<Workflow>> {
        let (_, raw) = decode_global_id(&id)?;
        Workflow::by_id(ctx.data()?, &raw).await
    }

    async fn workflow_runs(&self, ctx: &Context<'_>, workflow_id: ID) -> Result<Vec<WorkflowRun>> {
        let (_, raw) = decode_global_id(&workflow_id)?;
        Ok(sqlx::query_as(&format!(
            "SELECT {RUN_COLUMNS} FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT 50"
        ))
        .bind(raw)
        .fetch_all(ctx.data::<SqlitePool>()?)
        .await?)
    }

    async fn workflow_run(&self, ctx: &Context<'_>, id: ID) -> Result<Option<WorkflowRun>> {
        let (_, raw) = decode_global_id(&id)?;
        WorkflowRun::by_id(ctx.data()?, &raw).await
    }
}
