//! BoardTask — `server/graphql/types/board_task.py`, `queries/board_task.py`
//! and `mutations/board_task.py` (with `db/ops.py`'s board CRUD and
//! `server/task_board_runtime.py`'s `answer_board_task` / `stop_board_task`).
//! A change to any of those is made here too.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use async_graphql::{ComplexObject, Context, ID, InputObject, Object, Result, SimpleObject};
use serde_json::json;
use sqlx::{Sqlite, SqlitePool, Transaction};

use super::EdgeData;
use super::codec::{DateTime, decode_global_id, global_id, new_id, now_stored};
use super::conversation::{delete_conversation, unknown_model};
use crate::runs::Registry;

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

    async fn in_tx(tx: &mut Transaction<'_, Sqlite>, raw_id: &str) -> sqlx::Result<Option<Self>> {
        sqlx::query_as(&format!("SELECT {TASK_COLUMNS} FROM board_tasks WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(&mut **tx)
            .await
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

// ── mutations ───────────────────────────────────────────────────────────────

/// Statuses a human can move a card to. "running" is the dispatcher's.
const MOVABLE_STATUSES: [&str; 4] = ["todo", "ready", "done", "archived"];

#[derive(InputObject)]
pub struct BoardTaskInput {
    title: String,
    body: Option<String>,
    #[graphql(default)]
    priority: i32,
    model: Option<String>,
    skill: Option<String>,
    parent_ids: Option<Vec<ID>>,
    /// False parks the new task in "todo" instead of dispatching it.
    #[graphql(default = true)]
    start: bool,
}

#[derive(InputObject)]
pub struct BoardTaskUpdateInput {
    title: Option<String>,
    body: Option<String>,
    priority: Option<i32>,
    model: Option<String>,
    skill: Option<String>,
    /// When provided, REPLACES the task's parent links (empty list clears them).
    parent_ids: Option<Vec<ID>>,
}

fn raw_ids(ids: &[ID]) -> Result<Vec<String>> {
    ids.iter().map(|id| decode_global_id(id).map(|(_, raw)| raw).map_err(Into::into)).collect()
}

async fn check_model(pool: &SqlitePool, model: Option<&str>) -> Result<()> {
    match model {
        Some(m) if !crate::catalog::is_valid_model(pool, m).await? => Err(unknown_model(m).into()),
        _ => Ok(()),
    }
}

/// The ids among `ids` that name no task, sorted, as Python's message lists them.
async fn missing_tasks(tx: &mut Transaction<'_, Sqlite>, ids: &[String]) -> sqlx::Result<Vec<String>> {
    let mut missing = vec![];
    for id in ids.iter().collect::<HashSet<_>>() {
        let found: Option<String> =
            sqlx::query_scalar("SELECT id FROM board_tasks WHERE id = ?").bind(id).fetch_optional(&mut **tx).await?;
        if found.is_none() {
            missing.push(id.clone());
        }
    }
    missing.sort();
    Ok(missing)
}

/// `close_open_approvals(board_task_id=…)`: the task's questions still open
/// in the inbox, closed as `status`.
pub async fn close_open_approvals(
    tx: &mut Transaction<'_, Sqlite>,
    task_id: &str,
    status: &str,
    result: &str,
    answer: Option<&str>,
) -> sqlx::Result<()> {
    let now = now_stored();
    sqlx::query(
        "UPDATE approvals SET status = ?, resolved_at = ?, result = ?, answer = COALESCE(?, answer), updated_at = ? \
         WHERE status = 'pending' AND board_task_id = ?",
    )
    .bind(status)
    .bind(&now)
    .bind(result)
    .bind(answer)
    .bind(&now)
    .bind(task_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// `db.ops.update_board_task`: set the columns, bump `updated_at`, and — as
/// every status change does — close the task's question unless it is still
/// waiting on one. Column names come from code, never from input.
async fn update_task(
    tx: &mut Transaction<'_, Sqlite>,
    task_id: &str,
    sets: &[(&str, Option<String>)],
) -> sqlx::Result<()> {
    let mut sql = String::from("UPDATE board_tasks SET ");
    for (col, _) in sets {
        sql.push_str(col);
        sql.push_str(" = ?, ");
    }
    sql.push_str("updated_at = ? WHERE id = ?");
    let mut q = sqlx::query(&sql);
    for (_, value) in sets {
        q = q.bind(value);
    }
    q.bind(now_stored()).bind(task_id).execute(&mut **tx).await?;
    let (status, kind): (String, Option<String>) =
        sqlx::query_as("SELECT status, blocked_kind FROM board_tasks WHERE id = ?").bind(task_id).fetch_one(&mut **tx).await?;
    if !(status == "blocked" && kind.as_deref() == Some("needs_input")) {
        close_open_approvals(tx, task_id, "cancelled", &format!("The task moved to {status}."), None).await?;
    }
    Ok(())
}

/// `_kick_dispatch`: a dispatch pass now rather than at the next tick.
async fn kick_dispatch(data: &EdgeData) {
    if let Err(e) = data.scheduler.dispatch().await {
        tracing::error!("board dispatch failed: {e}");
    }
}

#[derive(Default)]
pub struct BoardTaskMutation;

#[Object]
impl BoardTaskMutation {
    // A card, optionally under existing parents — then it waits in "todo"
    // until they're all done, whatever `start` says.
    async fn create_board_task(&self, ctx: &Context<'_>, input: BoardTaskInput) -> Result<BoardTask> {
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<EdgeData>()?);
        check_model(pool, input.model.as_deref()).await?;
        let parent_ids = raw_ids(input.parent_ids.as_deref().unwrap_or_default())?;
        let parents: Vec<String> = parent_ids.iter().filter(|p| !p.is_empty()).cloned().collect();
        let mut tx = crate::db::write_tx(pool).await?;
        let mut status = if input.start { "ready" } else { "todo" };
        if !parents.is_empty() {
            let missing = missing_tasks(&mut tx, &parents).await?;
            if !missing.is_empty() {
                return Err(format!("parent task(s) not found: {}", missing.join(", ")).into());
            }
            status = "todo";
        }
        let (id, now) = (new_id(), now_stored());
        sqlx::query(
            "INSERT INTO board_tasks (id, title, body, status, priority, created_by, model, skill, failure_count, \
             created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'user', ?, ?, 0, ?, ?)",
        )
        .bind(&id)
        .bind(&input.title)
        .bind(&input.body)
        .bind(status)
        .bind(input.priority)
        .bind(&input.model)
        .bind(&input.skill)
        .bind(&now)
        .bind(&now)
        .execute(&mut *tx)
        .await?;
        for parent in &parents {
            sqlx::query("INSERT INTO board_task_links (id, parent_id, child_id, created_at) VALUES (?, ?, ?, ?)")
                .bind(new_id())
                .bind(parent)
                .bind(&id)
                .bind(&now)
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
        // Read before the dispatch, as Python returns the row it wrote.
        let mut task = BoardTask::by_id(pool, &id).await?.ok_or("task vanished")?;
        task.parent_ids = parent_ids;
        if status == "ready" {
            kick_dispatch(data).await;
        }
        Ok(task)
    }

    async fn update_board_task(&self, ctx: &Context<'_>, id: ID, input: BoardTaskUpdateInput) -> Result<BoardTask> {
        let pool: &SqlitePool = ctx.data()?;
        check_model(pool, input.model.as_deref()).await?;
        let (_, raw) = decode_global_id(&id)?;
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        if task.status == "running" {
            return Err("task is running — stop it before editing".into());
        }
        let mut sets: Vec<(&str, Option<String>)> = vec![];
        for (col, value) in [("title", &input.title), ("body", &input.body)] {
            if let Some(v) = value {
                sets.push((col, Some(v.clone())));
            }
        }
        if let Some(p) = input.priority {
            sets.push(("priority", Some(p.to_string())));
        }
        for (col, value) in [("model", &input.model), ("skill", &input.skill)] {
            if let Some(v) = value {
                sets.push((col, Some(v.clone())));
            }
        }
        // Committed on its own, as Python's two steps are: a bad parent list
        // below is refused after the fields are saved.
        let mut tx = crate::db::write_tx(pool).await?;
        update_task(&mut tx, &raw, &sets).await?;
        tx.commit().await?;
        let mut parent_ids = vec![];
        if let Some(ids) = &input.parent_ids {
            parent_ids = raw_ids(ids)?;
            replace_parents(pool, &raw, &parent_ids).await?;
        }
        let mut task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        task.parent_ids = parent_ids;
        Ok(task)
    }

    // Move a card: park (todo), queue (ready — also unblocks or re-runs),
    // mark done by hand, or archive. A running task must be stopped first.
    async fn set_board_task_status(&self, ctx: &Context<'_>, id: ID, status: String) -> Result<BoardTask> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<EdgeData>()?);
        if !MOVABLE_STATUSES.contains(&status.as_str()) {
            return Err(format!("status must be one of {}", MOVABLE_STATUSES.join(", ")).into());
        }
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        if task.status == "running" {
            return Err("task is running — stop it before moving".into());
        }
        let mut sets = vec![("status", Some(status.clone()))];
        if status == "todo" || status == "ready" {
            sets.extend([("blocked_reason", None), ("blocked_kind", None), ("finished_at", None)]);
        }
        let mut tx = crate::db::write_tx(pool).await?;
        update_task(&mut tx, &raw, &sets).await?;
        tx.commit().await?;
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        if status == "ready" {
            kick_dispatch(data).await;
        }
        Ok(task)
    }

    // Answer a blocked task's question and queue it again; the next run gets
    // the answer on the same conversation, so it knows what it asked.
    async fn answer_board_task(&self, ctx: &Context<'_>, id: ID, answer: String) -> Result<BoardTask> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<EdgeData>()?);
        let answer = answer.trim();
        if answer.is_empty() {
            return Err("answer must not be empty".into());
        }
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        if task.status != "blocked" {
            return Err("only blocked tasks can be answered".into());
        }
        let mut tx = crate::db::write_tx(pool).await?;
        // The question is closed as answered first: the update would close it
        // as cancelled.
        close_open_approvals(&mut tx, &raw, "answered", "Task resumed.", Some(answer)).await?;
        let sets = [
            ("status", Some("ready".to_string())),
            ("pending_answer", Some(answer.to_string())),
            ("blocked_reason", None),
            ("blocked_kind", None),
            ("finished_at", None),
        ];
        update_task(&mut tx, &raw, &sets).await?;
        tx.commit().await?;
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        kick_dispatch(data).await;
        Ok(task)
    }

    // A task, its links both ways, and its run's conversation.
    async fn delete_board_task(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<EdgeData>()?);
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        if task.status == "running" {
            return Err("task is running — stop it before deleting".into());
        }
        let mut tx = crate::db::write_tx(pool).await?;
        sqlx::query("DELETE FROM board_task_links WHERE parent_id = ? OR child_id = ?")
            .bind(&raw)
            .bind(&raw)
            .execute(&mut *tx)
            .await?;
        let teardown = delete_conversation(&mut tx, &format!("boardtask_{raw}"), &data.artifacts_dir).await?;
        sqlx::query("DELETE FROM board_tasks WHERE id = ?").bind(&raw).execute(&mut *tx).await?;
        tx.commit().await?;
        if let Some(t) = teardown {
            t.finish(data).await;
        }
        Ok(true)
    }

    // Cancel the task's current run: the worker that has it, the edge's own
    // loop, or — still queued — the job itself, which then ends the run here.
    async fn stop_board_task(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, registry) = (ctx.data::<SqlitePool>()?, ctx.data::<Arc<Registry>>()?);
        let run_id = BoardTask::by_id(pool, &raw)
            .await?
            .filter(|t| t.status == "running")
            .and_then(|t| t.run_id)
            .ok_or("task is not running")?;
        let run = registry.get(&run_id);
        if let Some(run) = &run {
            if run.claimed() {
                registry.control(&json!({"type": "cancel", "task_id": run_id, "resume": false}));
            }
            run.update(|st| st.fields.cancelled = true);
        }
        super::runs::cancel_job(pool, &run_id).await?;

        // Still pending, the job is now cancelled with no handler to write
        // the end: write it here, or the card stays "running" forever.
        let status: Option<String> =
            sqlx::query_scalar("SELECT status FROM jobs WHERE id = ?").bind(&run_id).fetch_optional(pool).await?;
        if status.as_deref() == Some("cancelled") {
            let now = now_stored();
            sqlx::query(
                "UPDATE board_tasks SET status = 'blocked', blocked_reason = 'stopped by user', blocked_kind = 'stopped', \
                 finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            )
            .bind(&now)
            .bind(&now)
            .bind(&raw)
            .execute(pool)
            .await?;
            if let Some(run) = run.filter(|r| !r.fields().done) {
                run.emit_local("stopped", &json!({"run_id": run_id}));
                run.update(|st| st.fields.done = true);
                registry.retire(&run_id);
            }
        }
        Ok(true)
    }
}

/// `replace_board_task_parents`: refuses a missing parent, the task itself
/// and a cycle; a waiting task with an unfinished parent goes back to "todo".
async fn replace_parents(pool: &SqlitePool, task_id: &str, parent_ids: &[String]) -> Result<()> {
    let mut seen = HashSet::new();
    let parents: Vec<&String> = parent_ids.iter().filter(|p| !p.is_empty() && seen.insert(p.as_str())).collect();
    if parents.iter().any(|p| *p == task_id) {
        return Err("a task cannot depend on itself".into());
    }
    let mut tx = crate::db::write_tx(pool).await?;
    let task = BoardTask::in_tx(&mut tx, task_id).await?.ok_or("task not found")?;
    if !parents.is_empty() {
        let owned: Vec<String> = parents.iter().map(|p| p.to_string()).collect();
        let missing = missing_tasks(&mut tx, &owned).await?;
        if !missing.is_empty() {
            return Err(format!("parent task(s) not found: {}", missing.join(", ")).into());
        }
        let descendants = descendants(&mut tx, task_id).await?;
        if parents.iter().any(|p| descendants.contains(p.as_str())) {
            return Err("dependency cycle: a task's descendant cannot be its parent".into());
        }
    }
    sqlx::query("DELETE FROM board_task_links WHERE child_id = ?").bind(task_id).execute(&mut *tx).await?;
    let now = now_stored();
    for parent in &parents {
        sqlx::query("INSERT INTO board_task_links (id, parent_id, child_id, created_at) VALUES (?, ?, ?, ?)")
            .bind(new_id())
            .bind(parent)
            .bind(task_id)
            .bind(&now)
            .execute(&mut *tx)
            .await?;
    }
    let mut status = task.status;
    if (status == "todo" || status == "ready") && !parents.is_empty() {
        let mut unfinished = false;
        for parent in &parents {
            let s: Option<String> = sqlx::query_scalar("SELECT status FROM board_tasks WHERE id = ?")
                .bind(parent)
                .fetch_optional(&mut *tx)
                .await?;
            unfinished |= s.as_deref() != Some("done");
        }
        if unfinished {
            status = "todo".into();
        }
    }
    sqlx::query("UPDATE board_tasks SET status = ?, updated_at = ? WHERE id = ?")
        .bind(&status)
        .bind(now_stored())
        .bind(task_id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(())
}

/// `list_board_task_descendants`: every task reachable along parent→child links.
async fn descendants(tx: &mut Transaction<'_, Sqlite>, task_id: &str) -> sqlx::Result<HashSet<String>> {
    let links: Vec<(String, String)> =
        sqlx::query_as("SELECT parent_id, child_id FROM board_task_links").fetch_all(&mut **tx).await?;
    let mut children: HashMap<&str, Vec<&str>> = HashMap::new();
    for (parent, child) in &links {
        children.entry(parent).or_default().push(child);
    }
    let (mut seen, mut frontier) = (HashSet::new(), vec![task_id]);
    while let Some(node) = frontier.pop() {
        for child in children.get(node).into_iter().flatten() {
            if seen.insert(child.to_string()) {
                frontier.push(child);
            }
        }
    }
    Ok(seen)
}
