//! Workflow and WorkflowRun — `server/graphql/types/workflow.py` and
//! `queries/workflow.py`.

use async_graphql::{ComplexObject, Context, ID, InputObject, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id, new_id, now_stored};
use super::write::update_row;

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

#[derive(InputObject)]
pub struct WorkflowCreateInput {
    name: String,
    description: Option<String>,
    #[graphql(default_with = "\"{}\".to_string()")]
    definition: String,
    notifications: Option<String>,
}

#[derive(InputObject)]
pub struct WorkflowUpdateInput {
    name: Option<String>,
    description: Option<String>,
    definition: Option<String>,
    notifications: Option<String>,
}

#[derive(Default)]
pub struct WorkflowMutation;

#[Object]
impl WorkflowMutation {
    async fn create_workflow(&self, ctx: &Context<'_>, input: WorkflowCreateInput) -> Result<Workflow> {
        let pool: &SqlitePool = ctx.data()?;
        let (id, now) = (new_id(), now_stored());
        sqlx::query(
            "INSERT INTO workflows (id, name, description, definition, notifications, created_at, updated_at) \
             VALUES (?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(&id)
        .bind(&input.name)
        .bind(&input.description)
        .bind(&input.definition)
        .bind(&input.notifications)
        .bind(&now)
        .bind(&now)
        .execute(pool)
        .await?;
        Workflow::by_id(pool, &id).await?.ok_or_else(|| "workflow vanished".into())
    }

    // Null fields are left alone; an update naming none returns the row
    // unchanged (no `updated_at` bump).
    async fn update_workflow(&self, ctx: &Context<'_>, id: ID, input: WorkflowUpdateInput) -> Result<Workflow> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let sets: Vec<(&str, String)> = [
            ("name", input.name),
            ("description", input.description),
            ("definition", input.definition),
            ("notifications", input.notifications),
        ]
        .into_iter()
        .filter_map(|(col, v)| v.map(|v| (col, v)))
        .collect();
        if !sets.is_empty() && update_row(pool, "workflows", &raw, &sets).await? == 0 {
            return Err("workflow not found".into());
        }
        Workflow::by_id(pool, &raw).await?.ok_or_else(|| "workflow not found".into())
    }

    // The human path only — see `delete_skill`. Runs go with it, as the ORM
    // cascade takes them.
    async fn delete_workflow(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let mut tx = ctx.data::<SqlitePool>()?.begin().await?;
        sqlx::query("DELETE FROM workflow_runs WHERE workflow_id = ?").bind(&raw).execute(&mut *tx).await?;
        let deleted = sqlx::query("DELETE FROM workflows WHERE id = ?").bind(&raw).execute(&mut *tx).await?;
        if deleted.rows_affected() == 0 {
            return Err("workflow not found".into());
        }
        tx.commit().await?;
        Ok(true)
    }
}
