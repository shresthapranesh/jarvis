//! Project — `server/graphql/types/project.py` and `queries/project.py`.

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id, iso_from_db};
use super::conversation::{CONVERSATION_COLUMNS, Conversation, ConversationRow};

#[derive(SimpleObject, Clone)]
#[graphql(complex)]
pub struct Project {
    pub id: ID,
    pub name: String,
    pub description: Option<String>,
    pub instructions: String,
    pub memory: String,
    pub created_at: DateTime,
    pub updated_at: DateTime,
    #[graphql(skip)]
    pub raw_id: String,
}

#[derive(sqlx::FromRow)]
struct ProjectRow {
    id: String,
    name: String,
    description: Option<String>,
    instructions: String,
    memory: String,
    created_at: String,
    updated_at: String,
}

const PROJECT_COLUMNS: &str = "id, name, description, instructions, memory, created_at, updated_at";

impl From<ProjectRow> for Project {
    fn from(r: ProjectRow) -> Self {
        Self {
            id: global_id("Project", &r.id),
            name: r.name,
            description: r.description,
            instructions: r.instructions,
            memory: r.memory,
            created_at: iso_from_db(&r.created_at),
            updated_at: iso_from_db(&r.updated_at),
            raw_id: r.id,
        }
    }
}

impl Project {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        let row: Option<ProjectRow> =
            sqlx::query_as(&format!("SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?"))
                .bind(raw_id)
                .fetch_optional(pool)
                .await?;
        Ok(row.map(Into::into))
    }
}

#[ComplexObject]
impl Project {
    async fn conversation_count(&self, ctx: &Context<'_>) -> Result<i64> {
        let pool: &SqlitePool = ctx.data()?;
        let (n,): (i64,) = sqlx::query_as("SELECT COUNT(id) FROM conversations WHERE project_id = ?")
            .bind(&self.raw_id)
            .fetch_one(pool)
            .await?;
        Ok(n)
    }

    async fn conversations(&self, ctx: &Context<'_>) -> Result<Vec<Conversation>> {
        let pool: &SqlitePool = ctx.data()?;
        let rows: Vec<ConversationRow> = sqlx::query_as(&format!(
            "SELECT {CONVERSATION_COLUMNS} FROM conversations WHERE project_id = ? \
             ORDER BY pinned DESC, created_at DESC"
        ))
        .bind(&self.raw_id)
        .fetch_all(pool)
        .await?;
        Ok(rows.into_iter().map(Into::into).collect())
    }
}

#[derive(Default)]
pub struct ProjectQuery;

#[Object]
impl ProjectQuery {
    async fn projects(&self, ctx: &Context<'_>) -> Result<Vec<Project>> {
        let pool: &SqlitePool = ctx.data()?;
        let rows: Vec<ProjectRow> =
            sqlx::query_as(&format!("SELECT {PROJECT_COLUMNS} FROM projects ORDER BY updated_at DESC"))
                .fetch_all(pool)
                .await?;
        Ok(rows.into_iter().map(Into::into).collect())
    }

    async fn project(&self, ctx: &Context<'_>, id: ID) -> Result<Option<Project>> {
        let (_, raw) = decode_global_id(&id)?;
        Project::by_id(ctx.data()?, &raw).await
    }
}
