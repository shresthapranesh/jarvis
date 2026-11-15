//! The board: every top-level conversation by what it is doing. A board task
//! is the record behind its conversation (`boardtask_<id>`), and takes its
//! column from its status; a chat takes it from its run, the questions open
//! on it and how its last reply ended.

use std::collections::HashMap;
use std::sync::Arc;

use async_graphql::{Context, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::board::{BoardTask, TASK_COLUMNS, links};
use super::codec::DateTime;
use crate::approvals::{ASK_SOURCE, GATE_SOURCE};
use crate::runs::Registry;

#[derive(SimpleObject)]
pub struct BoardItem {
    /// The conversation's id — for a task, the one its runs answer in,
    /// whether or not it has run yet.
    pub id: String,
    /// `todo`, `working`, `needs_me`, `blocked` or `done`.
    pub state: String,
    pub title: Option<String>,
    pub last_active_at: DateTime,
    /// None for a task that has not run: it has no conversation to open.
    pub conversation_id: Option<String>,
    /// The run working in it now.
    pub run_id: Option<String>,
    /// What it is waiting on or why it stopped: the question, the error.
    pub reason: Option<String>,
    /// The task behind the conversation; none for a chat.
    pub task: Option<BoardTask>,
}

#[derive(SimpleObject)]
pub struct Board {
    /// In column order: todo by priority, then age; every other column
    /// newest first.
    pub items: Vec<BoardItem>,
    /// How many are done, before `doneLimit` cut the list.
    pub done_total: i64,
}

#[derive(sqlx::FromRow)]
struct ConversationRow {
    id: String,
    title: Option<String>,
    surface: String,
    last_active_at: DateTime,
    reply_status: Option<String>,
    reply: Option<String>,
}

/// Where a task stands on the board.
fn task_state(task: &BoardTask) -> &'static str {
    match task.status.as_str() {
        "running" => "working",
        "blocked" if task.blocked_kind.as_deref() == Some("needs_input") => "needs_me",
        "blocked" => "blocked",
        "done" => "done",
        _ => "todo",
    }
}

#[derive(Default)]
pub struct BoardQuery;

#[Object]
impl BoardQuery {
    /// Chats and board tasks, each in the column of what it is doing. Done
    /// keeps its `doneLimit` most recent.
    async fn board(&self, ctx: &Context<'_>, #[graphql(default = 20)] done_limit: i32) -> Result<Board> {
        let (pool, registry) = (ctx.data::<SqlitePool>()?, ctx.data::<Arc<Registry>>()?);

        let mut tasks: HashMap<String, BoardTask> =
            sqlx::query_as::<_, BoardTask>(&format!("SELECT {TASK_COLUMNS} FROM board_tasks WHERE status != 'archived'"))
                .fetch_all(pool)
                .await?
                .into_iter()
                .map(|t| (t.raw_id.clone(), t))
                .collect();
        for (parent, child) in links(pool).await? {
            if let Some(t) = tasks.get_mut(&child) {
                t.parent_ids.push(parent.clone());
            }
            if let Some(t) = tasks.get_mut(&parent) {
                t.child_ids.push(child);
            }
        }

        // The chat turns going now, by conversation: (run, stopped on a question).
        let live: HashMap<String, (String, bool)> = registry
            .all()
            .into_iter()
            .filter(|run| run.meta.kind == "chat")
            .filter_map(|run| {
                let f = run.fields();
                let parent = run.meta.parent_id.clone()?;
                (!f.done).then(|| (parent, (run.id.clone(), f.has_interrupt)))
            })
            .collect();
        // The questions open on a conversation: a tool to approve, or an ask.
        let asked: HashMap<String, String> = sqlx::query_as::<_, (String, String)>(
            "SELECT parent_id, question FROM approvals WHERE status = 'pending' AND source IN (?, ?) \
             AND action IS NULL AND parent_id IS NOT NULL ORDER BY requested_at DESC",
        )
        .bind(GATE_SOURCE)
        .bind(ASK_SOURCE)
        .fetch_all(pool)
        .await?
        .into_iter()
        .collect();

        let rows: Vec<ConversationRow> = sqlx::query_as(
            "SELECT c.id, c.title, c.surface, \
             COALESCE((SELECT MAX(created_at) FROM messages WHERE conversation_id = c.id), c.created_at) AS last_active_at, \
             m.status AS reply_status, CASE WHEN m.status = 'error' THEN substr(m.content, 1, 500) END AS reply \
             FROM conversations c LEFT JOIN messages m ON m.id = (SELECT id FROM messages WHERE conversation_id = c.id \
             AND role = 'assistant' ORDER BY created_at DESC, rowid DESC LIMIT 1) \
             WHERE c.ephemeral = 0 AND c.parent_id IS NULL AND c.surface IN ('web', 'task')",
        )
        .fetch_all(pool)
        .await?;

        let mut items = Vec::with_capacity(rows.len() + tasks.len());
        for row in rows {
            let run = live.get(&row.id);
            // A chat turn in it now, or a question open on it.
            let chat_state = match run {
                Some((_, true)) => Some("needs_me"),
                Some(_) if asked.contains_key(&row.id) => Some("needs_me"),
                Some(_) => Some("working"),
                None if asked.contains_key(&row.id) => Some("needs_me"),
                None => None,
            };
            if row.surface == "task" {
                // Its task is archived or gone: off the board.
                let Some(task) = row.id.strip_prefix("boardtask_").and_then(|id| tasks.remove(id)) else { continue };
                // A finished task gone on with as a chat is at work again.
                let state = match (task_state(&task), chat_state) {
                    ("done", Some(chat)) => chat,
                    (state, _) => state,
                };
                let last_active_at = if task.updated_at.0 > row.last_active_at.0 { task.updated_at.clone() } else { row.last_active_at };
                items.push(BoardItem {
                    id: row.id.clone(),
                    state: state.into(),
                    title: Some(task.title.clone()),
                    last_active_at,
                    conversation_id: Some(row.id),
                    run_id: if task.status == "running" { task.run_id.clone() } else { run.map(|r| r.0.clone()) },
                    reason: task.blocked_reason.clone(),
                    task: Some(task),
                });
                continue;
            }
            let failed = row.reply_status.as_deref() == Some("error");
            let state = chat_state.unwrap_or(if failed { "blocked" } else { "done" });
            let reason = match state {
                "needs_me" => asked.get(&row.id).cloned(),
                "blocked" => row.reply,
                _ => None,
            };
            items.push(BoardItem {
                id: row.id.clone(),
                state: state.into(),
                title: row.title,
                last_active_at: row.last_active_at,
                conversation_id: Some(row.id),
                run_id: run.map(|r| r.0.clone()),
                reason,
                task: None,
            });
        }
        // Tasks that have not run: no conversation yet.
        for (id, task) in tasks {
            items.push(BoardItem {
                id: format!("boardtask_{id}"),
                state: task_state(&task).into(),
                title: Some(task.title.clone()),
                last_active_at: task.updated_at.clone(),
                conversation_id: None,
                run_id: None,
                reason: task.blocked_reason.clone(),
                task: Some(task),
            });
        }

        // By column, so the order within one is a total order.
        let column = |state: &str| ["todo", "working", "needs_me", "blocked", "done"].iter().position(|s| *s == state);
        items.sort_by(|a, b| {
            column(&a.state).cmp(&column(&b.state)).then_with(|| match (&a.task, &b.task) {
                (Some(x), Some(y)) if a.state == "todo" => {
                    y.priority.cmp(&x.priority).then_with(|| x.created_at.0.cmp(&y.created_at.0))
                }
                _ => b.last_active_at.0.cmp(&a.last_active_at.0).then_with(|| a.id.cmp(&b.id)),
            })
        });
        let done_total = items.iter().filter(|i| i.state == "done").count() as i64;
        let mut room = done_limit.max(0) as usize;
        items.retain(|i| {
            if i.state != "done" {
                return true;
            }
            let keep = room > 0;
            room = room.saturating_sub(1);
            keep
        });
        Ok(Board { items, done_total })
    }
}
