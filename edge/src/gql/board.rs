//! BoardTask — `server/graphql/types/board_task.py` and `queries/board_task.py`.

use std::collections::HashMap;

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::codec::{DateTime, decode_global_id, global_id};

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct BoardTask {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub title: String,
    pub body: Option<String>,
    pub status: String,
    pub priority: i64,
    pub created_by: String,
    pub model: Option<String>,
    pub skill: Option<String>,
    pub blocked_reason: Option<String>,
    pub blocked_kind: Option<String>,
    pub failure_count: i64,
    pub summary: Option<String>,
    pub result_metadata: Option<String>,
    /// Job id of the current or latest dispatch — the key for boardTaskEvents.
    #[sqlx(rename = "job_id")]
    pub run_id: Option<String>,
    /// Raw ids of linked tasks. Filled by `boardTasks` / `boardTask`; empty
    /// when resolved through `node`, as Python's `resolve_node` leaves them.
    #[sqlx(skip)]
    pub parent_ids: Vec<String>,
    #[sqlx(skip)]
    pub child_ids: Vec<String>,
    pub created_at: DateTime,
    pub updated_at: DateTime,
    pub started_at: Option<DateTime>,
    pub finished_at: Option<DateTime>,
}

const TASK_COLUMNS: &str = "id, title, body, status, priority, created_by, model, skill, blocked_reason, \
     blocked_kind, failure_count, summary, result_metadata, job_id, created_at, updated_at, started_at, finished_at";

impl BoardTask {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {TASK_COLUMNS} FROM board_tasks WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl BoardTask {
    pub async fn id(&self) -> ID {
        global_id("BoardTask", &self.raw_id)
    }

    /// The conversation holding the task's run transcript.
    async fn conversation_id(&self) -> String {
        format!("boardtask_{}", self.raw_id)
    }
}

/// Every link as (parent, child), in table order — the order Python appends
/// them to each task's id lists.
async fn links(pool: &SqlitePool) -> Result<Vec<(String, String)>> {
    Ok(sqlx::query_as("SELECT parent_id, child_id FROM board_task_links").fetch_all(pool).await?)
}

#[derive(Default)]
pub struct BoardTaskQuery;

#[Object]
impl BoardTaskQuery {
    async fn board_tasks(
        &self,
        ctx: &Context<'_>,
        #[graphql(default = false)] include_archived: bool,
    ) -> Result<Vec<BoardTask>> {
        let pool: &SqlitePool = ctx.data()?;
        let mut sql = format!("SELECT {TASK_COLUMNS} FROM board_tasks");
        if !include_archived {
            sql.push_str(" WHERE status != 'archived'");
        }
        sql.push_str(" ORDER BY priority DESC, created_at ASC");
        let mut tasks: Vec<BoardTask> = sqlx::query_as(&sql).fetch_all(pool).await?;

        let mut parents: HashMap<String, Vec<String>> = HashMap::new();
        let mut children: HashMap<String, Vec<String>> = HashMap::new();
        for (parent, child) in links(pool).await? {
            parents.entry(child.clone()).or_default().push(parent.clone());
            children.entry(parent).or_default().push(child);
        }
        for t in &mut tasks {
            t.parent_ids = parents.remove(&t.raw_id).unwrap_or_default();
            t.child_ids = children.remove(&t.raw_id).unwrap_or_default();
        }
        Ok(tasks)
    }

    async fn board_task(&self, ctx: &Context<'_>, id: ID) -> Result<Option<BoardTask>> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let Some(mut task) = BoardTask::by_id(pool, &raw).await? else {
            return Ok(None);
        };
        for (parent, child) in links(pool).await? {
            if child == task.raw_id {
                task.parent_ids.push(parent.clone());
            }
            if parent == task.raw_id {
                task.child_ids.push(child);
            }
        }
        Ok(Some(task))
    }
}
