//! Board tasks — `server/task_board_runtime.py` (`board_task_job_handler`)
//! and the board tools in `tools/board.py`, for the runs the edge serves.
//! The agent turn is `turn.rs`'s with the board tools bound; this is what is
//! around it: re-asserting the claim, the task's prompt (or the answer to
//! its question), its conversation, the tools' writes, and the end, which
//! never overwrites what the task no longer owns. A change to either Python
//! module is made here too.

use serde_json::{Value, json};
use sqlx::{Row, SqlitePool};

use super::automation::End;
use super::queue::Job;
use crate::gql::codec::{new_id, now_stored};
use crate::runs::Run;

/// `_TASK_PROMPT`, around the title, body, skill and handoffs.
const TASK_PROMPT_HEAD: &str = "You are executing a task from the shared task board.\n\n# Task: ";
const TASK_PROMPT_TAIL: &str = "\nWhen you have finished the task, call complete_task(summary=...) with a concise
handoff summary for downstream tasks (optionally metadata as a JSON object
string). If you cannot finish, call block_task(reason=...) instead — pass
needs_input=True when a human answer would unblock you; the answer is delivered
when the task resumes. If you call neither, your final reply is recorded as the
summary.";

pub struct Spec {
    pub run_id: String,
    pub task_id: String,
    pub title: String,
    /// Resolved (`resolve_model(task.model)`).
    pub model: String,
    /// The answer consumed at claim time, if the task was waiting on one.
    pub pending_answer: Option<String>,
    /// The run's prompt: the task, or the answer to its question.
    pub prompt: String,
}

/// `board_task_conversation_id`.
pub fn conversation_id(task_id: &str) -> String {
    format!("boardtask_{task_id}")
}

/// The task as a run of it would start, read without changing anything —
/// so a run handed to Python before it began finds the task as it was.
pub async fn load(pool: &SqlitePool, job: &Job) -> sqlx::Result<Option<Spec>> {
    let Some(task_id) = job.payload["task_id"].as_str() else { return Ok(None) };
    let Some(row) = sqlx::query("SELECT title, body, status, model, skill, pending_answer FROM board_tasks WHERE id = ?")
        .bind(task_id)
        .fetch_optional(pool)
        .await?
    else {
        return Ok(None);
    };
    let status: String = row.get("status");
    if status == "done" || status == "archived" {
        return Ok(None);
    }
    let title: String = row.get("title");
    let model: Option<String> = row.get("model");
    let model = crate::catalog::resolve_model(pool, model.as_deref()).await?;
    let pending_answer: Option<String> = row.get::<Option<String>, _>("pending_answer").filter(|a| !a.is_empty());
    let prompt = match &pending_answer {
        Some(answer) => resume_prompt(&title, answer),
        None => {
            let body: Option<String> = row.get("body");
            let skill: Option<String> = row.get::<Option<String>, _>("skill").filter(|s| !s.is_empty());
            let parents = sqlx::query(
                "SELECT b.title, b.summary, b.result_metadata, b.status FROM board_tasks b \
                 JOIN board_task_links l ON l.parent_id = b.id WHERE l.child_id = ? ORDER BY l.created_at ASC",
            )
            .bind(task_id)
            .fetch_all(pool)
            .await?;
            let done: Vec<(String, Option<String>, Option<String>)> = parents
                .iter()
                .filter(|p| p.get::<String, _>("status") == "done")
                .map(|p| (p.get("title"), p.get("summary"), p.get("result_metadata")))
                .collect();
            task_prompt(&title, body.as_deref().unwrap_or_default(), skill.as_deref(), &done)
        }
    };
    Ok(Some(Spec { run_id: job.id.clone(), task_id: task_id.into(), title, model, pending_answer, prompt }))
}

/// `_compose_task_prompt`.
fn task_prompt(title: &str, body: &str, skill: Option<&str>, done_parents: &[(String, Option<String>, Option<String>)]) -> String {
    let skill_part =
        skill.map(|s| format!("\nFirst call use_skill('{s}') and follow its instructions.\n")).unwrap_or_default();
    let handoff_part = if done_parents.is_empty() {
        String::new()
    } else {
        let sections: Vec<String> = done_parents
            .iter()
            .map(|(title, summary, metadata)| {
                let summary = summary.as_deref().filter(|s| !s.is_empty()).unwrap_or("(no summary)");
                let mut block = format!("### {title}\n{summary}");
                if let Some(m) = metadata.as_deref().filter(|m| !m.is_empty()) {
                    block.push_str(&format!("\nMetadata: {m}"));
                }
                block
            })
            .collect();
        format!("\n## Handoffs from completed upstream tasks\n\n{}\n", sections.join("\n\n"))
    };
    format!("{TASK_PROMPT_HEAD}{title}\n{body}\n{skill_part}{handoff_part}{TASK_PROMPT_TAIL}")
}

/// `_RESUME_PROMPT`.
fn resume_prompt(title: &str, answer: &str) -> String {
    format!(
        "You previously blocked the board task \"{title}\" with a question. The user has answered:\n\n{answer}\n\n\
         Continue the task using this answer. When you have finished, call complete_task(summary=...); if you are \
         still blocked, call block_task(reason=...)."
    )
}

/// The claim, re-asserted as `board_task_job_handler` does: running under
/// this run, and the answer consumed — it rides in the prompt now.
pub async fn claim(pool: &SqlitePool, spec: &Spec) -> sqlx::Result<()> {
    let now = now_stored();
    sqlx::query(
        "UPDATE board_tasks SET status = 'running', job_id = ?, started_at = ?, updated_at = ?, pending_answer = NULL \
         WHERE id = ?",
    )
    .bind(&spec.run_id)
    .bind(&now)
    .bind(&now)
    .bind(&spec.task_id)
    .execute(pool)
    .await?;
    Ok(())
}

/// The task's conversation (`surface = 'task'`) and the prompt as its user
/// message.
pub async fn begin_conversation(pool: &SqlitePool, spec: &Spec) -> sqlx::Result<()> {
    let mut tx = crate::db::write_tx(pool).await?;
    let id = conversation_id(&spec.task_id);
    crate::gql::start::conversation_for(&mut tx, Some(&id), &spec.model, Some(spec.title.clone()), "task", None, false)
        .await?;
    crate::gql::start::insert_message(&mut *tx, &id, "user", &spec.prompt, None, "done").await?;
    tx.commit().await
}

async fn message(pool: &SqlitePool, spec: &Spec, content: &str, status: &str) {
    let id = conversation_id(&spec.task_id);
    if let Err(e) = crate::gql::start::insert_message(pool, &id, "assistant", content, None, status).await {
        tracing::warn!("board task {}: writing its reply: {e}", spec.task_id);
    }
}

// ── the board tools ─────────────────────────────────────────────────────────

/// `update_board_task`'s side effect: a task that is no longer waiting on an
/// answer leaves no question in the inbox.
async fn close_questions(tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, task_id: &str, status: &str) -> sqlx::Result<()> {
    let now = now_stored();
    sqlx::query(
        "UPDATE approvals SET status = 'cancelled', resolved_at = ?, result = ?, updated_at = ? \
         WHERE status = 'pending' AND board_task_id = ?",
    )
    .bind(&now)
    .bind(format!("The task moved to {status}."))
    .bind(&now)
    .bind(task_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// `complete_task(summary, metadata)`; the metadata is already known to be
/// JSON (`tools.rs` leaves anything else to Python, which words the error).
pub async fn complete(pool: &SqlitePool, spec: &Spec, summary: &str, metadata: Option<&str>) -> Result<String, String> {
    if metadata.is_some_and(|m| !serde_json::from_str::<Value>(m).is_ok_and(|v| v.is_object())) {
        return Ok("Error: metadata must be a JSON object.".into());
    }
    let mut tx = crate::db::write_tx(pool).await.map_err(|e| e.to_string())?;
    let done = sqlx::query(
        "UPDATE board_tasks SET status = 'done', summary = ?, result_metadata = ?, blocked_reason = NULL, \
         blocked_kind = NULL, updated_at = ? WHERE id = ?",
    )
    .bind(summary)
    .bind(metadata)
    .bind(now_stored())
    .bind(&spec.task_id)
    .execute(&mut *tx)
    .await
    .map_err(|e| e.to_string())?;
    if done.rows_affected() == 0 {
        return Ok("Error: current board task not found.".into());
    }
    close_questions(&mut tx, &spec.task_id, "done").await.map_err(|e| e.to_string())?;
    tx.commit().await.map_err(|e| e.to_string())?;
    Ok("Task marked done. Wrap up with a short final reply.".into())
}

/// `block_task(reason, needs_input)`: a question for a human goes to the
/// inbox as well (`record_blocking_request`).
pub async fn block(pool: &SqlitePool, spec: &Spec, reason: &str, needs_input: bool) -> Result<String, String> {
    let kind = if needs_input { "needs_input" } else { "agent" };
    let mut tx = crate::db::write_tx(pool).await.map_err(|e| e.to_string())?;
    let blocked = sqlx::query(
        "UPDATE board_tasks SET status = 'blocked', blocked_reason = ?, blocked_kind = ?, updated_at = ? WHERE id = ?",
    )
    .bind(reason)
    .bind(kind)
    .bind(now_stored())
    .bind(&spec.task_id)
    .execute(&mut *tx)
    .await
    .map_err(|e| e.to_string())?;
    if blocked.rows_affected() == 0 {
        return Ok("Error: current board task not found.".into());
    }
    if !needs_input {
        close_questions(&mut tx, &spec.task_id, "blocked").await.map_err(|e| e.to_string())?;
    }
    tx.commit().await.map_err(|e| e.to_string())?;
    if needs_input {
        let now = now_stored();
        let asked = sqlx::query(
            "INSERT INTO approvals (id, source, kind, status, question, label, parent_id, board_task_id, requested_at, \
             updated_at) VALUES (?, 'board_task', 'input', 'pending', ?, ?, ?, ?, ?, ?)",
        )
        .bind(new_id())
        .bind(reason)
        .bind(&spec.title)
        .bind(conversation_id(&spec.task_id))
        .bind(&spec.task_id)
        .bind(&now)
        .bind(&now)
        .execute(pool)
        .await;
        // Best-effort, as Python's: the card still shows the question.
        if let Err(e) = asked {
            tracing::warn!("could not persist approval request: {e}");
        }
    }
    Ok("Task marked blocked. Wrap up with a short final reply explaining the blocker.".into())
}

// ── the end ─────────────────────────────────────────────────────────────────

/// `_finish_task`: the run's outcome, unless the task isn't this run's any
/// more (a newer run, or re-queued by a human). A status the agent already
/// set through its tools stands; only the finish time is stamped.
async fn finish_task(
    pool: &SqlitePool,
    spec: &Spec,
    status: &str,
    summary: Option<&str>,
    blocked: Option<(&str, &str)>,
    bump_failures: bool,
) {
    let done = async {
        let mut tx = crate::db::write_tx(pool).await?;
        let row: Option<(String, Option<String>)> =
            sqlx::query_as("SELECT status, job_id FROM board_tasks WHERE id = ?").bind(&spec.task_id).fetch_optional(&mut *tx).await?;
        let Some((current, job_id)) = row else { return Ok(()) };
        if job_id.as_deref() != Some(spec.run_id.as_str()) || current == "ready" || current == "todo" {
            return Ok(());
        }
        let now = now_stored();
        if current == "running" {
            sqlx::query(
                "UPDATE board_tasks SET status = ?, summary = COALESCE(?, summary), blocked_reason = ?, blocked_kind = ?, \
                 failure_count = failure_count + ? WHERE id = ?",
            )
            .bind(status)
            .bind(summary)
            .bind(blocked.map(|b| b.0))
            .bind(blocked.map(|b| b.1))
            .bind(i64::from(bump_failures))
            .bind(&spec.task_id)
            .execute(&mut *tx)
            .await?;
        }
        sqlx::query("UPDATE board_tasks SET finished_at = ?, updated_at = ? WHERE id = ?")
            .bind(&now)
            .bind(&now)
            .bind(&spec.task_id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await
    };
    let done: sqlx::Result<()> = done.await;
    if let Err(e) = done {
        tracing::error!("board task {}: recording its end: {e}", spec.task_id);
    }
}

/// The end of `_run_board_task_inner`. Returns the run's final status, and
/// whether the task is done — a finished parent may unblock its children,
/// so the caller runs a dispatch pass.
pub async fn finish(pool: &SqlitePool, run: &Run, spec: &Spec, end: End) -> (String, bool) {
    let emit = |event: &str, data: Value| {
        run.emit_local(event, &data);
    };
    match end {
        End::Budget { output, reason } => {
            let why = format!("budget exceeded: {reason}");
            finish_task(pool, spec, "blocked", Some(&output).filter(|o| !o.is_empty()).map(String::as_str), Some((&why, "budget")), false)
                .await;
            let said = if output.is_empty() { format!("[budget exceeded: {reason}]") } else { output.clone() };
            message(pool, spec, &said, "blocked").await;
            emit("budget_exceeded", json!({"reason": reason, "run_id": spec.run_id}));
            emit("stopped", json!({"output": output, "run_id": spec.run_id}));
            ("blocked".into(), false)
        }
        End::Stopped(output) => {
            let output = output.unwrap_or_default();
            let summary = Some(output.as_str()).filter(|o| !o.is_empty());
            finish_task(pool, spec, "blocked", summary, Some(("stopped by user", "stopped")), false).await;
            message(pool, spec, &output, "stopped").await;
            emit("stopped", json!({"output": output, "run_id": spec.run_id}));
            ("stopped".into(), false)
        }
        End::Done(output) => {
            // complete_task or block_task may have set the status already;
            // only a task still `running` takes this run's outcome.
            finish_task(pool, spec, "done", Some(&output), None, false).await;
            message(pool, spec, &output, "done").await;
            let status: Option<String> =
                sqlx::query_scalar("SELECT status FROM board_tasks WHERE id = ?").bind(&spec.task_id).fetch_optional(pool).await.ok().flatten();
            emit("done", json!({"output": output, "run_id": spec.run_id}));
            let status = status.unwrap_or_else(|| "done".into());
            let done = status == "done";
            (status, done)
        }
        End::Failed(error) => {
            let why = format!("error: {error}");
            finish_task(pool, spec, "blocked", None, Some((&why, "error")), true).await;
            message(pool, spec, &error, "error").await;
            // The answer was consumed at claim; put it back so a retry after
            // a transient failure still resumes with it.
            if let Some(answer) = &spec.pending_answer {
                let restored = sqlx::query(
                    "UPDATE board_tasks SET pending_answer = ? WHERE id = ? AND job_id = ? \
                     AND (pending_answer IS NULL OR pending_answer = '')",
                )
                .bind(answer)
                .bind(&spec.task_id)
                .bind(&spec.run_id)
                .execute(pool)
                .await;
                if let Err(e) = restored {
                    tracing::warn!("board task {}: restoring its answer: {e}", spec.task_id);
                }
            }
            emit("error", json!({"error": error}));
            ("error".into(), false)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prompts_match_python() {
        // _compose_task_prompt(BoardTask(title="T", body="B", skill="s"), parents=[done P1 w/ metadata, done P2 no summary])
        let p = task_prompt("T", "B", Some("s"), &[
            ("P1".into(), Some("did it".into()), Some(r#"{"k": 1}"#.into())),
            ("P2".into(), None, None),
        ]);
        assert_eq!(p, PYTHON_TASK_PROMPT);
        assert_eq!(resume_prompt("T", "yes"), PYTHON_RESUME_PROMPT);
    }

    const PYTHON_TASK_PROMPT: &str = "You are executing a task from the shared task board.\n\n# Task: T\nB\n\nFirst call use_skill('s') and follow its instructions.\n\n## Handoffs from completed upstream tasks\n\n### P1\ndid it\nMetadata: {\"k\": 1}\n\n### P2\n(no summary)\n\nWhen you have finished the task, call complete_task(summary=...) with a concise\nhandoff summary for downstream tasks (optionally metadata as a JSON object\nstring). If you cannot finish, call block_task(reason=...) instead — pass\nneeds_input=True when a human answer would unblock you; the answer is delivered\nwhen the task resumes. If you call neither, your final reply is recorded as the\nsummary.";
    const PYTHON_RESUME_PROMPT: &str = "You previously blocked the board task \"T\" with a question. The user has answered:\n\nyes\n\nContinue the task using this answer. When you have finished, call complete_task(summary=...); if you are still blocked, call block_task(reason=...).";
}
