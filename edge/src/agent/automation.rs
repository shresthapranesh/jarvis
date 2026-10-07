//! Automations — `server/automation_runtime.py`, for the runs the edge
//! serves. A prompt or monitor run's agent turn is `turn.rs`'s; this is what
//! is around it — the `AutomationRun` row, the stateful conversation, the
//! monitor's prompt and its "nothing changed" gate, the end and its
//! notifications — and the code and webhook runs, which need no model.
//! A change to the Python module is made here too.

use std::io::BufRead;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{Value, json};
use sqlx::{Row, SqlitePool};

use super::queue::Job;
use crate::gql::codec::{new_id, now_stored};
use crate::runs::Run;

/// `_CODE_TIMEOUT_SECONDS`.
const CODE_TIMEOUT: Duration = Duration::from_secs(60);

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
    /// Resolved (`_resolve_model`); only a prompt or monitor run reads it.
    pub model: String,
    pub code_text: String,
    pub webhook_url: String,
    pub webhook_method: Option<String>,
    pub webhook_headers: Option<String>,
    pub webhook_body: String,
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
    Ready(Spec),
}

/// The start of `automation_job_handler`: the automation, and its run's row
/// — created for a scheduled run, flipped to running for a manual one.
pub async fn prepare(pool: &SqlitePool, job: &Job) -> sqlx::Result<Prepared> {
    let Some(automation_id) = job.payload["automation_id"].as_str() else { return Ok(Prepared::Gone) };
    let Some(row) = sqlx::query(
        "SELECT name, input_type, prompt_text, stateful, notifications, model, code_text, webhook_url, webhook_method, \
         webhook_headers, webhook_body FROM automations WHERE id = ?",
    )
    .bind(automation_id)
    .fetch_optional(pool)
    .await?
    else {
        return Ok(Prepared::Gone);
    };
    let input_type: String = row.get("input_type");
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
    let llm = input_type == "prompt" || input_type == "monitor";
    let model: Option<String> = row.get("model");
    let model = if llm { crate::catalog::resolve_model(pool, model.as_deref()).await? } else { String::new() };
    let stateful: Option<bool> = row.get("stateful");
    let text = |col: &str| row.get::<Option<String>, _>(col).unwrap_or_default();
    Ok(Prepared::Ready(Spec {
        run_id: job.id.clone(),
        automation_id: automation_id.into(),
        name: row.get("name"),
        stateful: input_type == "monitor" || (input_type == "prompt" && stateful.unwrap_or(false)),
        prompt_text: text("prompt_text"),
        notifications: row.get("notifications"),
        model,
        code_text: text("code_text"),
        webhook_url: text("webhook_url"),
        webhook_method: row.get("webhook_method"),
        webhook_headers: row.get("webhook_headers"),
        webhook_body: text("webhook_body"),
        input_type,
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

/// What a run came to — the branches of `_run_automation_inner`.
pub enum End {
    /// Finished: its output.
    Done(String),
    /// Stopped, with what it had said — none when the stop interrupted it
    /// (Python's `CancelledError`).
    Stopped(Option<String>),
    /// The run's budget ran out.
    Budget { output: String, reason: String },
    /// It raised.
    Failed(String),
}

/// The end of `_run_automation_inner`: the run's row, its reply in a
/// stateful conversation, notifications, and the closing event. Returns the
/// final status. A monitor that saw nothing new finishes `no_change`, silently.
pub async fn finish(pool: &SqlitePool, run: &Run, spec: &Spec, end: End) -> &'static str {
    let emit = |event: &str, data: Value| {
        run.emit_local(event, &data);
    };
    match end {
        End::Failed(error) => {
            finish_run(pool, &spec.run_id, "error", None, Some(&error)).await;
            crate::notify::send(pool, spec.notifications.as_deref(), "error", &spec.name, &error).await;
            reply(pool, spec, &error, "error").await;
            emit("error", json!({"error": error}));
            "error"
        }
        End::Budget { output, reason } => {
            let error = format!("budget exceeded: {reason}");
            finish_run(pool, &spec.run_id, "error", Some(&output), Some(&error)).await;
            let said = if output.is_empty() { format!("[budget exceeded: {reason}]") } else { output };
            reply(pool, spec, &said, "error").await;
            emit("budget_exceeded", json!({"reason": reason, "run_id": spec.run_id}));
            emit("error", json!({"error": error}));
            "error"
        }
        End::Stopped(output) => {
            finish_run(pool, &spec.run_id, "stopped", output.as_deref(), None).await;
            reply(pool, spec, output.as_deref().unwrap_or_default(), "stopped").await;
            match output {
                Some(output) => emit("stopped", json!({"output": output, "run_id": spec.run_id})),
                None => emit("stopped", json!({"run_id": spec.run_id})),
            }
            "stopped"
        }
        End::Done(output) => {
            let status = if spec.input_type == "monitor" && reported_no_change(&output) { "no_change" } else { "done" };
            finish_run(pool, &spec.run_id, status, Some(&output), None).await;
            // The delta gate: an unchanged monitor stays silent.
            if status != "no_change" {
                crate::notify::send(pool, spec.notifications.as_deref(), "done", &spec.name, &output).await;
            }
            // "no_change" is a run status, not a message status.
            reply(pool, spec, &output, "done").await;
            emit("done", json!({"output": output, "run_id": spec.run_id}));
            status
        }
    }
}

// ── code and webhook runs ───────────────────────────────────────────────────

/// `_execute_code_type`: the script on jarvis's interpreter, stdout and
/// stderr as one stream, each line a token as it comes. 60 s, then killed;
/// a stop terminates it (killed 2 s later). The exit code isn't looked at.
pub async fn run_code(spec: &Spec, run: &Arc<Run>) -> End {
    let file = std::env::temp_dir().join(format!("jarvis-automation-{}.py", new_id()));
    let ended = code(spec, run, &file).await;
    let _ = std::fs::remove_file(&file);
    ended
}

async fn code(spec: &Spec, run: &Arc<Run>, file: &std::path::Path) -> End {
    if let Err(e) = std::fs::write(file, &spec.code_text) {
        return End::Failed(e.to_string());
    }
    let (reader, writer) = match std::io::pipe() {
        Ok(p) => p,
        Err(e) => return End::Failed(e.to_string()),
    };
    let mut cmd = tokio::process::Command::new(crate::config::python());
    cmd.arg(file)
        .current_dir(crate::config::app_dir())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .kill_on_drop(true);
    match writer.try_clone() {
        Ok(w) => cmd.stdout(w).stderr(writer),
        Err(e) => return End::Failed(e.to_string()),
    };
    let spawned = cmd.spawn();
    // The parent's copies of the write end go, so the read end sees EOF
    // once the script (and anything it left holding the pipe) is done.
    drop(cmd);
    let mut child = match spawned {
        Ok(c) => c,
        Err(e) => return End::Failed(e.to_string()),
    };
    let (tx, mut lines) = tokio::sync::mpsc::unbounded_channel::<String>();
    tokio::task::spawn_blocking(move || {
        let mut reader = std::io::BufReader::new(reader);
        let mut buf = vec![];
        while reader.read_until(b'\n', &mut buf).is_ok_and(|n| n > 0) {
            if tx.send(String::from_utf8_lossy(&buf).into_owned()).is_err() {
                return;
            }
            buf.clear();
        }
    });

    let mut output = String::new();
    let deadline = tokio::time::sleep(CODE_TIMEOUT);
    tokio::pin!(deadline);
    let drained = async {
        while let Some(line) = lines.recv().await {
            run.emit_local("token", &json!({"text": line, "source": "main"}));
            output.push_str(&line);
        }
        child.wait().await
    };
    tokio::select! {
        _ = drained => {}
        () = &mut deadline => {
            let _ = child.start_kill();
            let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
            return End::Failed(format!("code timed out after {}s and was killed", CODE_TIMEOUT.as_secs()));
        }
        () = super::turn::until_stopped(run.clone()) => {
            if let Some(pid) = child.id() {
                // SAFETY: signalling our own child, which hasn't been reaped.
                unsafe { libc::kill(pid as i32, libc::SIGTERM) };
            }
            if tokio::time::timeout(Duration::from_secs(2), child.wait()).await.is_err() {
                let _ = child.start_kill();
                let _ = child.wait().await;
            }
            return End::Stopped(None);
        }
    }
    if run.fields().cancelled {
        return End::Stopped(None);
    }
    End::Done(output.trim().to_string())
}

/// `_execute_webhook_type`: one request — the method (POST by default), the
/// headers if they are a JSON object, the body — answered as
/// `HTTP <status>\n<body>`, which is also the run's one token. Redirects
/// aren't followed and it gives up after 30 s, as Python's client does.
pub async fn run_webhook(spec: &Spec, run: &Arc<Run>) -> End {
    let method = spec.webhook_method.as_deref().filter(|m| !m.is_empty()).unwrap_or("POST").to_uppercase();
    let method = match reqwest::Method::from_bytes(method.as_bytes()) {
        Ok(m) => m,
        Err(e) => return End::Failed(e.to_string()),
    };
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(c) => c,
        Err(e) => return End::Failed(e.to_string()),
    };
    let mut req = client.request(method, &spec.webhook_url).body(spec.webhook_body.clone());
    if let Some(Ok(Value::Object(headers))) = spec.webhook_headers.as_deref().map(serde_json::from_str::<Value>) {
        for (k, v) in headers {
            req = req.header(k, v.as_str().map_or_else(|| v.to_string(), str::to_string));
        }
    }
    let resp = match req.send().await {
        Ok(r) => r,
        Err(e) => return End::Failed(e.to_string()),
    };
    let status = resp.status().as_u16();
    let body = resp.text().await.unwrap_or_default();
    let result = format!("HTTP {status}\n{body}");
    run.emit_local("token", &json!({"text": result, "source": "main"}));
    if run.fields().cancelled { End::Stopped(Some(result)) } else { End::Done(result) }
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
