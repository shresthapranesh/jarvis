//! Project — `server/graphql/types/project.py` and `queries/project.py`.

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id};
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
