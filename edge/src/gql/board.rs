//! BoardTask — `server/graphql/types/board_task.py`, `queries/board_task.py`
//! and `mutations/board_task.py` (with `db/ops.py`'s board CRUD and
//! `server/task_board_runtime.py`'s `answer_board_task` / `stop_board_task` /
//! `decompose_board_task`). A change to any of those is made here too.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use async_graphql::{ComplexObject, Context, ID, InputObject, Object, Result, SimpleObject};
use serde_json::{Value, json};
use sqlx::{Sqlite, SqlitePool, Transaction};

use super::{EdgeData, defer};
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

async fn insert_link(tx: &mut Transaction<'_, Sqlite>, parent: &str, child: &str) -> sqlx::Result<()> {
    sqlx::query("INSERT INTO board_task_links (id, parent_id, child_id, created_at) VALUES (?, ?, ?, ?)")
        .bind(new_id())
        .bind(parent)
        .bind(child)
        .bind(now_stored())
        .execute(&mut **tx)
        .await?;
    Ok(())
}

// ── auto-decompose ───────────────────────────────────────────────────────────

const MAX_SUBTASKS: usize = 8;

const DECOMPOSE_SYSTEM: &str =
    "You are a planner for a multi-agent task board. Respond ONLY with a JSON object — no prose, no code fences.";

/// `_DECOMPOSE_PROMPT`, filled in.
fn decompose_prompt(title: &str, body: &str) -> String {
    format!(
        "Break the following task into 2-{MAX_SUBTASKS} smaller subtasks that together accomplish it.

# Task: {title}
{body}

Rules:
- Each subtask needs a short imperative \"title\" and a self-contained \"body\" an \
agent can execute without seeing the other subtasks (dependency results are \
handed to it automatically).
- \"depends_on\" lists the 0-based indexes of other subtasks whose output this \
one needs; it may only reference EARLIER subtasks (smaller index). Prefer no \
dependencies so subtasks run in parallel.
- Do NOT add a final \"combine the results\" subtask — the original task runs \
last automatically with every subtask's summary as context.

JSON shape: {{\"subtasks\": [{{\"title\": \"...\", \"body\": \"...\", \"depends_on\": []}}]}}"
    )
}

/// The planner call: no tools, nothing streamed, the reply's text blocks —
/// joined by a space, as Python joins a reasoning model's list.
async fn plan(pool: &SqlitePool, http: &reqwest::Client, model: &str, task: &BoardTask) -> Result<String> {
    use crate::llm::shape::SystemBlock;
    use crate::llm::transcript::{Content, Part, Role, Typed};

    let user = crate::llm::Message::new(
        Role::User,
        Content::Text(decompose_prompt(&task.title, task.body.as_deref().unwrap_or(""))),
    );
    let prompt = crate::llm::Prompt {
        system: vec![SystemBlock { text: DECOMPOSE_SYSTEM.into(), breakpoint: false }],
        messages: vec![user],
        history_breakpoint: None,
        cached: false,
    };
    let ends = crate::llm::Endpoints {
        compatible: crate::catalog::endpoints(pool).await.unwrap_or_default(),
        ..crate::llm::Endpoints::from_env()
    };
    let req = crate::llm::Request { model, prompt: &prompt, tools: &[], blobs: &Default::default() };
    let reply = crate::llm::complete(http, &ends, &req, &mut |_| {}).await.map_err(|e| e.message)?;
    Ok(match reply.message.content {
        Content::Text(s) => s,
        Content::Parts(parts) => parts
            .iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join(" "),
    })
}

#[derive(Debug, PartialEq)]
struct SubtaskSpec {
    title: String,
    body: String,
    /// Earlier subtasks' indexes, sorted, no repeats.
    depends_on: Vec<usize>,
}

/// Python's type name, for the error its `.get` on a non-dict raises.
fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `_parse_decomposition`: the planner's text → validated subtask specs, or
/// Python's refusal. One departure: an unparseable object's error detail is
/// serde's, not Python's `json` module's.
fn parse_decomposition(text: &str) -> Result<Vec<SubtaskSpec>, String> {
    let text = text.trim();
    let (start, end) = (text.find('{'), text.rfind('}'));
    let (Some(start), Some(end)) = (start, end) else { return Err("decomposer returned no JSON object".into()) };
    if end <= start {
        return Err("decomposer returned no JSON object".into());
    }
    let data: Value =
        serde_json::from_str(&text[start..=end]).map_err(|e| format!("decomposer returned invalid JSON: {e}"))?;
    let subtasks = match data.get("subtasks") {
        Some(Value::Array(items)) if (2..=MAX_SUBTASKS).contains(&items.len()) => items,
        other => {
            let got = match other {
                Some(Value::Array(items)) => items.len().to_string(),
                _ => "none".into(),
            };
            return Err(format!("decomposer must return 2-{MAX_SUBTASKS} subtasks (got {got})"));
        }
    };
    let mut specs = vec![];
    for (i, s) in subtasks.iter().enumerate() {
        let Value::Object(fields) = s else {
            return Err(format!("'{}' object has no attribute 'get'", py_type(s)));
        };
        let field = |key: &str| match fields.get(key) {
            Some(v) if crate::pyjson::truthy(v) => crate::pyjson::py_str(v).trim().to_string(),
            _ => String::new(),
        };
        let (title, body) = (field("title"), field("body"));
        if title.is_empty() || body.is_empty() {
            return Err(format!("subtask {i} is missing a title or body"));
        }
        let invalid = || format!("subtask {i} has invalid depends_on (must be earlier indexes)");
        let mut deps = vec![];
        match fields.get("depends_on") {
            Some(v) if crate::pyjson::truthy(v) => {
                let Value::Array(items) = v else { return Err(invalid()) };
                for d in items {
                    // `isinstance(d, int)`: a bool is one too.
                    let d = match d {
                        Value::Bool(b) => *b as i64,
                        Value::Number(n) if !n.is_f64() => n.as_i64().ok_or_else(invalid)?,
                        _ => return Err(invalid()),
                    };
                    if d < 0 || d as usize >= i {
                        return Err(invalid());
                    }
                    deps.push(d as usize);
                }
            }
            _ => {}
        }
        deps.sort_unstable();
        deps.dedup();
        specs.push(SubtaskSpec { title, body, depends_on: deps });
    }
    Ok(specs)
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

    // Split a standalone waiting task into planner-made subtasks, which
    // become its parents: the original runs last as the synthesis step.
    // Returns the subtasks as created. A model the edge doesn't call goes to
    // Python before anything is written.
    async fn decompose_board_task(&self, ctx: &Context<'_>, id: ID) -> Result<Vec<BoardTask>> {
        let (_, raw) = decode_global_id(&id)?;
        let (pool, data) = (ctx.data::<SqlitePool>()?, ctx.data::<EdgeData>()?);
        let task = BoardTask::by_id(pool, &raw).await?.ok_or("task not found")?;
        if !["todo", "ready", "blocked"].contains(&task.status.as_str()) {
            return Err("only waiting (todo/ready/blocked) tasks can be decomposed".into());
        }
        let has_parents: Option<String> = sqlx::query_scalar("SELECT id FROM board_task_links WHERE child_id = ? LIMIT 1")
            .bind(&raw)
            .fetch_optional(pool)
            .await?;
        if has_parents.is_some() {
            return Err("task already has dependencies — decompose only standalone tasks".into());
        }
        if !crate::agent::route::serves_board(pool, task.model.as_deref()).await {
            return Err(defer("the planner's model is called from Python".into()));
        }
        let model = crate::catalog::resolve_model(pool, task.model.as_deref()).await?;
        let specs = parse_decomposition(&plan(pool, &data.http, &model, &task).await?)?;

        // Park the original first, so no dispatch pass starts it while the
        // subtasks that gate it are being written.
        sqlx::query(
            "UPDATE board_tasks SET status = 'todo', blocked_reason = NULL, blocked_kind = NULL, finished_at = NULL, \
             updated_at = ? WHERE id = ?",
        )
        .bind(now_stored())
        .bind(&raw)
        .execute(pool)
        .await?;
        let mut tx = crate::db::write_tx(pool).await?;
        let mut created: Vec<String> = vec![];
        for spec in &specs {
            let (sub, now) = (new_id(), now_stored());
            // `create_board_task(status="ready")`: a subtask with parents waits.
            let status = if spec.depends_on.is_empty() { "ready" } else { "todo" };
            sqlx::query(
                "INSERT INTO board_tasks (id, title, body, status, priority, created_by, model, failure_count, \
                 created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'agent', ?, 0, ?, ?)",
            )
            .bind(&sub)
            .bind(&spec.title)
            .bind(&spec.body)
            .bind(status)
            .bind(task.priority)
            .bind(&task.model)
            .bind(&now)
            .bind(&now)
            .execute(&mut *tx)
            .await?;
            for &d in &spec.depends_on {
                insert_link(&mut tx, &created[d], &sub).await?;
            }
            created.push(sub);
        }
        for sub in &created {
            insert_link(&mut tx, sub, &raw).await?;
        }
        tx.commit().await?;
        tracing::info!("board decompose: task {raw} split into {} subtasks", created.len());
        let mut out = vec![];
        for sub in &created {
            out.push(BoardTask::by_id(pool, sub).await?.ok_or("task vanished")?);
        }
        kick_dispatch(data).await;
        Ok(out)
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
        answer_task(ctx.data()?, ctx.data()?, &raw, &answer).await
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

/// `answer_board_task`: shared by the board card and the inbox
/// (`resolveApproval`), so the two can't disagree on what an answer may be.
pub async fn answer_task(pool: &SqlitePool, data: &EdgeData, raw: &str, answer: &str) -> Result<BoardTask> {
    let answer = answer.trim();
    if answer.is_empty() {
        return Err("answer must not be empty".into());
    }
    let task = BoardTask::by_id(pool, raw).await?.ok_or("task not found")?;
    if task.status != "blocked" {
        return Err("only blocked tasks can be answered".into());
    }
    let mut tx = crate::db::write_tx(pool).await?;
    // The question is closed as answered first: the update would close it
    // as cancelled.
    close_open_approvals(&mut tx, raw, "answered", "Task resumed.", Some(answer)).await?;
    let sets = [
        ("status", Some("ready".to_string())),
        ("pending_answer", Some(answer.to_string())),
        ("blocked_reason", None),
        ("blocked_kind", None),
        ("finished_at", None),
    ];
    update_task(&mut tx, raw, &sets).await?;
    tx.commit().await?;
    let task = BoardTask::by_id(pool, raw).await?.ok_or("task not found")?;
    kick_dispatch(data).await;
    Ok(task)
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

#[cfg(test)]
mod decompose_tests {
    use super::*;

    fn spec(title: &str, body: &str, deps: &[usize]) -> SubtaskSpec {
        SubtaskSpec { title: title.into(), body: body.into(), depends_on: deps.to_vec() }
    }

    #[test]
    fn a_plan_is_read_from_around_prose() {
        let text = r#"Sure! {"subtasks": [{"title": 7, "body": " b ", "depends_on": null},
            {"title": "t", "body": "u", "depends_on": [0, false, 0]}]} done"#;
        assert_eq!(parse_decomposition(text).unwrap(), vec![spec("7", "b", &[]), spec("t", "u", &[0])]);
    }

    #[test]
    fn refusals_are_worded_as_pythons() {
        let cases = [
            ("}{", "decomposer returned no JSON object"),
            (r#"{"subtasks": 3}"#, "decomposer must return 2-8 subtasks (got none)"),
            (r#"{"subtasks": [1, 2]}"#, "'int' object has no attribute 'get'"),
            (r#"{"subtasks": [{"title": "a", "body": "b", "depends_on": "0"}, {"title": "a", "body": "b"}]}"#,
             "subtask 0 has invalid depends_on (must be earlier indexes)"),
            (r#"{"subtasks": [{"title": "a", "body": "b"}, {"title": "a", "body": "b", "depends_on": [0.0]}]}"#,
             "subtask 1 has invalid depends_on (must be earlier indexes)"),
        ];
        for (text, error) in cases {
            assert_eq!(parse_decomposition(text).unwrap_err(), error, "{text}");
        }
        // The one departure: the detail of a JSON error is serde's.
        assert!(parse_decomposition("{nope}").unwrap_err().starts_with("decomposer returned invalid JSON: "));
    }
}
