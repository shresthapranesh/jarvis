//! Prompt and monitor automations — `server/automation_runtime.py`, for the
//! runs the edge serves. The agent turn itself is `turn.rs`'s; this is what
//! is around it: the `AutomationRun` row, the stateful conversation, the
//! monitor's prompt and its "nothing changed" gate, and the notifications.
//! A change to the Python module is made here too.

use serde_json::Value;
use sqlx::{Row, SqlitePool};

use super::queue::Job;
use crate::gql::codec::now_stored;

/// `_MONITOR_WRAPPER`.
const MONITOR_WRAPPER: &str = "You are running as a scheduled monitor. Check the target described below and compare \
what you observe against your previous observations earlier in this conversation.

- First check (no previous observations): reply with a concise baseline of the current state.
- Nothing meaningful has changed since the last check: reply with exactly NO_CHANGE on the first line. You may note \
minor details below it; nothing will be delivered.
- Something meaningful changed: reply with a concise report of what changed and the new state. Do NOT start the reply \
with NO_CHANGE.

Target to monitor:
";

/// One automation run, as its job and row describe it.
pub struct Spec {
    pub run_id: String,
    pub automation_id: String,
    pub name: String,
    pub input_type: String,
    pub prompt_text: String,
    /// `_is_stateful_prompt`: a monitor always, a prompt when asked.
    pub stateful: bool,
    pub notifications: Option<String>,
    /// Resolved (`_resolve_model`).
    pub model: String,
}

impl Spec {
    /// The thread the agent runs on: the automation's own conversation when
    /// it is stateful, else one for this run alone.
    pub fn thread_id(&self) -> String {
        if self.stateful { conversation_id(&self.automation_id) } else { format!("automation_{}", self.run_id) }
    }

    /// The agent's prompt: a monitor's target goes inside its instructions.
    pub fn prompt(&self) -> String {
        if self.input_type == "monitor" { format!("{MONITOR_WRAPPER}{}", self.prompt_text) } else { self.prompt_text.clone() }
    }
}

/// `automation_conversation_id`.
pub fn conversation_id(automation_id: &str) -> String {
    format!("automation_{automation_id}")
}

/// What the claim found.
pub enum Prepared {
    /// The automation was deleted: nothing to run.
    Gone,
    /// Python's to run (a code or webhook automation).
    Python,
    Ready(Spec),
}

/// The start of `automation_job_handler`: the automation, and its run's row
/// — created for a scheduled run, flipped to running for a manual one.
pub async fn prepare(pool: &SqlitePool, job: &Job) -> sqlx::Result<Prepared> {
    let Some(automation_id) = job.payload["automation_id"].as_str() else { return Ok(Prepared::Gone) };
    let Some(row) = sqlx::query(
        "SELECT name, input_type, prompt_text, stateful, notifications, model FROM automations WHERE id = ?",
    )
    .bind(automation_id)
    .fetch_optional(pool)
    .await?
    else {
        return Ok(Prepared::Gone);
    };
    let input_type: String = row.get("input_type");
    if input_type != "prompt" && input_type != "monitor" {
        return Ok(Prepared::Python);
    }
    let triggered_by = job.payload["triggered_by"].as_str().unwrap_or("manual");
    let existing: Option<String> =
        sqlx::query_scalar("SELECT status FROM automation_runs WHERE id = ?").bind(&job.id).fetch_optional(pool).await?;
    match existing.as_deref() {
        None => {
            tracing::info!("task received: kind=automation parent={automation_id} via={triggered_by}");
            sqlx::query(
                "INSERT INTO automation_runs (id, automation_id, status, triggered_by, started_at) VALUES (?, ?, 'running', ?, ?)",
            )
            .bind(&job.id)
            .bind(automation_id)
            .bind(triggered_by)
            .bind(now_stored())
            .execute(pool)
            .await?;
        }
        Some("pending") => {
            sqlx::query("UPDATE automation_runs SET status = 'running' WHERE id = ?").bind(&job.id).execute(pool).await?;
        }
        Some(_) => {}
    }
    let model: Option<String> = row.get("model");
    let model = crate::catalog::resolve_model(pool, model.as_deref()).await?;
    let stateful: Option<bool> = row.get("stateful");
    Ok(Prepared::Ready(Spec {
        run_id: job.id.clone(),
        automation_id: automation_id.into(),
        name: row.get("name"),
        stateful: input_type == "monitor" || stateful.unwrap_or(false),
        input_type,
        prompt_text: row.get::<Option<String>, _>("prompt_text").unwrap_or_default(),
        notifications: row.get("notifications"),
        model,
    }))
}

/// `_has_inflight_sibling`: another run of the same automation is executing.
/// Stateful runs share a thread, so overlapping ones are skipped, not queued.
pub async fn sibling_running(pool: &SqlitePool, spec: &Spec) -> sqlx::Result<bool> {
    let payloads: Vec<String> =
        sqlx::query_scalar("SELECT payload FROM jobs WHERE kind = 'automation' AND status = 'running' AND id != ?")
            .bind(&spec.run_id)
            .fetch_all(pool)
            .await?;
    Ok(payloads.iter().any(|p| {
        serde_json::from_str::<Value>(p).is_ok_and(|v| v["automation_id"].as_str() == Some(spec.automation_id.as_str()))
    }))
}

/// A stateful run's start: its conversation (`surface = 'automation'`) and
/// the prompt as the user's message — the target alone, not the wrapper.
pub async fn begin_conversation(pool: &SqlitePool, spec: &Spec) -> sqlx::Result<()> {
    let mut tx = crate::db::write_tx(pool).await?;
    let id = conversation_id(&spec.automation_id);
    crate::gql::start::conversation_for(&mut tx, Some(&id), &spec.model, Some(spec.name.clone()), "automation", None, false)
        .await?;
    crate::gql::start::insert_message(&mut *tx, &id, "user", &spec.prompt_text, None, "done").await?;
    tx.commit().await
}

/// `_persist_stateful_message`: the run's reply in its conversation.
pub async fn reply(pool: &SqlitePool, spec: &Spec, content: &str, status: &str) {
    if !spec.stateful {
        return;
    }
    let conversation = conversation_id(&spec.automation_id);
    if let Err(e) = crate::gql::start::insert_message(pool, &conversation, "assistant", content, None, status).await {
        tracing::warn!("automation {}: writing its reply: {e}", spec.automation_id);
    }
}

/// `finish_automation_run`.
pub async fn finish_run(pool: &SqlitePool, run_id: &str, status: &str, output: Option<&str>, error: Option<&str>) {
    let done = sqlx::query("UPDATE automation_runs SET status = ?, output = ?, error = ?, finished_at = ? WHERE id = ?")
        .bind(status)
        .bind(output)
        .bind(error)
        .bind(now_stored())
        .bind(run_id)
        .execute(pool)
        .await;
    if let Err(e) = done {
        tracing::error!("automation run {run_id}: recording its end: {e}");
    }
}

/// `_monitor_reported_no_change`: the reply's first line is the sentinel,
/// give or take markdown and punctuation.
pub fn reported_no_change(output: &str) -> bool {
    let Some(first) = output.trim().lines().next() else { return false };
    first.trim().trim_matches(|c| "*_`\"'. ".contains(c)).to_uppercase() == "NO_CHANGE"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_change_matches_python() {
        for (output, want) in [
            ("NO_CHANGE", true),
            ("  **NO_CHANGE**.\nminor drift", true),
            ("`no_change`", true),
            ("NO_CHANGE here", false),
            ("Price went up\nNO_CHANGE", false),
            ("", false),
            ("   ", false),
        ] {
            assert_eq!(reported_no_change(output), want, "{output:?}");
        }
    }
}
