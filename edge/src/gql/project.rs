//! Project — `server/graphql/types/project.py` and `queries/project.py`.

use async_graphql::{ComplexObject, Context, ID, InputObject, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id, new_id, now_stored};
use super::write::update_row;
use super::conversation::{CONVERSATION_COLUMNS, Conversation};

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Project {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub name: String,
    pub description: Option<String>,
    pub instructions: String,
    pub memory: String,
    pub created_at: DateTime,
    pub updated_at: DateTime,
}

const PROJECT_COLUMNS: &str = "id, name, description, instructions, memory, created_at, updated_at";

impl Project {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Project {
    pub async fn id(&self) -> ID {
        global_id("Project", &self.raw_id)
    }

    async fn conversation_count(&self, ctx: &Context<'_>) -> Result<i64> {
        let pool: &SqlitePool = ctx.data()?;
        let (n,): (i64,) = sqlx::query_as("SELECT COUNT(id) FROM conversations WHERE project_id = ?")
            .bind(&self.raw_id)
            .fetch_one(pool)
            .await?;
        Ok(n)
    }

    async fn conversations(&self, ctx: &Context<'_>) -> Result<Vec<Conversation>> {
        Ok(sqlx::query_as(&format!(
            "SELECT {CONVERSATION_COLUMNS} FROM conversations WHERE project_id = ? \
             ORDER BY pinned DESC, created_at DESC"
        ))
        .bind(&self.raw_id)
        .fetch_all(ctx.data::<SqlitePool>()?)
        .await?)
    }
}

#[derive(Default)]
pub struct ProjectQuery;

#[Object]
impl ProjectQuery {
    async fn projects(&self, ctx: &Context<'_>) -> Result<Vec<Project>> {
        Ok(sqlx::query_as(&format!("SELECT {PROJECT_COLUMNS} FROM projects ORDER BY updated_at DESC"))
            .fetch_all(ctx.data::<SqlitePool>()?)
            .await?)
    }

    async fn project(&self, ctx: &Context<'_>, id: ID) -> Result<Option<Project>> {
        let (_, raw) = decode_global_id(&id)?;
        Project::by_id(ctx.data()?, &raw).await
    }
}

#[derive(InputObject)]
pub struct ProjectCreateInput {
    name: String,
    description: Option<String>,
    #[graphql(default)]
    instructions: String,
}

#[derive(InputObject)]
pub struct ProjectUpdateInput {
    name: Option<String>,
    description: Option<String>,
    instructions: Option<String>,
    memory: Option<String>,
}

#[derive(Default)]
pub struct ProjectMutation;

#[Object]
impl ProjectMutation {
    // The pass for one project now, without the quiet-period wait the
    // timer observes. A model the edge doesn't call is Python's.
    async fn consolidate_project_memory(&self, ctx: &Context<'_>, id: ID, model: Option<String>) -> Result<String> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        if Project::by_id(pool, &raw).await?.is_none() {
            return Err("project not found".into());
        }
        let model = crate::consolidate::served(pool, model.as_deref())
            .await
            .ok_or_else(|| super::defer("the consolidation model is called from Python".into()))?;
        let http = &ctx.data::<super::EdgeData>()?.http;
        Ok(crate::consolidate::project::consolidate(pool, http, &raw, Some(&model), true).await?)
    }

    async fn create_project(&self, ctx: &Context<'_>, input: ProjectCreateInput) -> Result<Project> {
        let name = input.name.trim();
        if name.is_empty() {
            return Err("name required".into());
        }
        let pool: &SqlitePool = ctx.data()?;
        let (id, now) = (new_id(), now_stored());
        sqlx::query(
            "INSERT INTO projects (id, name, description, instructions, memory, created_at, updated_at) \
             VALUES (?, ?, ?, ?, '', ?, ?)",
        )
        .bind(&id)
        .bind(name)
        .bind(&input.description)
        .bind(&input.instructions)
        .bind(&now)
        .bind(&now)
        .execute(pool)
        .await?;
        Project::by_id(pool, &id).await?.ok_or_else(|| "project vanished".into())
    }

    async fn update_project(&self, ctx: &Context<'_>, id: ID, input: ProjectUpdateInput) -> Result<Project> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        if Project::by_id(pool, &raw).await?.is_none() {
            return Err("project not found".into());
        }
        let mut sets: Vec<(&str, String)> = Vec::new();
        if let Some(name) = &input.name {
            let name = name.trim();
            if name.is_empty() {
                return Err("name required".into());
            }
            sets.push(("name", name.to_string()));
        }
        for (col, value) in [
            ("description", &input.description),
            ("instructions", &input.instructions),
            ("memory", &input.memory),
        ] {
            if let Some(v) = value {
                sets.push((col, v.clone()));
            }
        }
        if sets.is_empty() {
            return Err("nothing to update".into());
        }
        update_row(pool, "projects", &raw, &sets).await?;
        Project::by_id(pool, &raw).await?.ok_or_else(|| "project not found".into())
    }

    // Delete a project. Its conversations are kept, with membership cleared:
    // there's no ORM cascade to lean on and foreign keys are off, so the
    // UPDATE is what keeps them from pointing at nothing.
    async fn delete_project(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let mut tx = crate::db::write_tx(ctx.data::<SqlitePool>()?).await?;
        sqlx::query("UPDATE conversations SET project_id = NULL WHERE project_id = ?")
            .bind(&raw)
            .execute(&mut *tx)
            .await?;
        let deleted = sqlx::query("DELETE FROM projects WHERE id = ?").bind(&raw).execute(&mut *tx).await?;
        if deleted.rows_affected() == 0 {
            return Err("project not found".into());
        }
        tx.commit().await?;
        Ok(true)
    }

    // Assign a conversation to a project, or remove it (projectId: null).
    // Only web conversations may join one.
    async fn set_conversation_project(
        &self,
        ctx: &Context<'_>,
        conversation_id: ID,
        project_id: Option<ID>,
    ) -> Result<Conversation> {
        let (_, conv_raw) = decode_global_id(&conversation_id)?;
        let project_raw = project_id.map(|id| decode_global_id(&id)).transpose()?.map(|(_, raw)| raw);
        let pool: &SqlitePool = ctx.data()?;
        let conv = Conversation::by_id(pool, &conv_raw).await?.ok_or("conversation not found")?;
        if conv.surface != "web" {
            return Err("only web conversations can belong to a project".into());
        }
        if let Some(pid) = &project_raw {
            if Project::by_id(pool, pid).await?.is_none() {
                return Err(format!("project not found: {pid}").into());
            }
        }
        sqlx::query("UPDATE conversations SET project_id = ? WHERE id = ?")
            .bind(&project_raw)
            .bind(&conv_raw)
            .execute(pool)
            .await?;
        Ok(Conversation { project_id: project_raw, ..conv })
    }
}
