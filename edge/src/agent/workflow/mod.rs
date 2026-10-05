//! The workflow engine in the edge — ports of `workflow/engine.py`
//! (`engine.rs`), `workflow/nodes.py` (`nodes.rs`, `agent.rs`),
//! `core/workflow_template.py` (`template.rs`), `server/workflow_runtime.py`
//! (`serve` here) and `tools/workflows.py:run_workflow` (`Call`) — a change
//! to either side is made in both.
//!
//! A run triggered while the edge's agent loop is on, and every model its
//! graph names is one the edge calls (`served`), is queued as an edge job
//! and run here start to finish. A paused node waits on its `approvals` row,
//! which the edge's `resumeWorkflowRun`, `resolveWorkflowApproval` and
//! `resolveApproval` answer (`answer`). As in Python, the run's state is in
//! memory only: an edge that restarts mid-run runs it again from the start.

mod agent;
mod engine;
mod nodes;
mod template;

use std::collections::HashSet;
use std::sync::Arc;

use serde_json::{Map, Value, json};
use sqlx::SqlitePool;

use super::queue::Job;
use super::{Agent, Outcome, route};
use crate::budget::{Budget, Limits};
use crate::gql::codec::now_stored;
use crate::pyjson;
use crate::runs::Run;
use engine::{Env, Halt, Meter, Pause};

/// `_MAX_WORKFLOW_DEPTH`: `run_workflow` calls inside one another.
const MAX_DEPTH: u32 = 3;

/// Whether the edge runs this workflow: the agent loop is on and every
/// model its graph can call — the ones its nodes name, the default for the
/// ones that name none, and those of the saved workflows its maps run — is
/// one the edge calls.
pub async fn served(pool: &SqlitePool, workflow_id: &str) -> bool {
    if !route::enabled() {
        return false;
    }
    let mut models: HashSet<Option<String>> = [None].into();
    let mut seen = HashSet::new();
    let mut todo = vec![workflow_id.to_string()];
    while let Some(id) = todo.pop() {
        if !seen.insert(id.clone()) {
            continue;
        }
        let definition: Option<String> =
            sqlx::query_scalar("SELECT definition FROM workflows WHERE id = ?").bind(&id).fetch_optional(pool).await.ok().flatten();
        // A missing map workflow fails its node either way.
        let Some(Ok(definition)) = definition.map(|d| serde_json::from_str::<Value>(&d)) else { continue };
        walk(&definition, &mut models, &mut todo);
    }
    for model in models {
        match crate::catalog::resolve_model(pool, model.as_deref()).await {
            Ok(m) if route::serves_model(pool, &m).await => {}
            _ => return false,
        }
    }
    true
}

/// Every `model` named anywhere in a definition, and every `workflow_id` a
/// map names.
fn walk(v: &Value, models: &mut HashSet<Option<String>>, workflows: &mut Vec<String>) {
    match v {
        Value::Object(m) => {
            for (k, v) in m {
                match (k.as_str(), v) {
                    ("model", Value::String(s)) if !s.is_empty() => {
                        models.insert(Some(s.clone()));
                    }
                    ("workflow_id", Value::String(s)) if !s.is_empty() => workflows.push(s.clone()),
                    _ => walk(v, models, workflows),
                }
            }
        }
        Value::Array(items) => items.iter().for_each(|i| walk(i, models, workflows)),
        _ => {}
    }
}

struct Workflow {
    name: String,
    definition: Option<String>,
    notifications: Option<String>,
}

async fn load(pool: &SqlitePool, id: &str) -> sqlx::Result<Option<Workflow>> {
    let row: Option<(String, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT name, definition, notifications FROM workflows WHERE id = ?").bind(id).fetch_optional(pool).await?;
    Ok(row.map(|(name, definition, notifications)| Workflow { name, definition, notifications }))
}

impl Agent {
    /// `workflow_job_handler` + `_run_workflow_inner`.
    pub(super) async fn serve_workflow(&self, job: &Job, run: &Arc<Run>) -> Outcome {
        let pool = &self.pool;
        let run_id = job.id.clone();
        let workflow_id = job.payload["workflow_id"].as_str().unwrap_or_default().to_string();
        let inputs = match &job.payload["inputs"] {
            Value::Object(m) => m.clone(),
            _ => Map::new(),
        };
        let wf = match load(pool, &workflow_id).await {
            Ok(Some(wf)) => wf,
            Ok(None) => {
                tracing::warn!("agent: workflow run {run_id} has no workflow; dropping it");
                self.end(run, "error");
                return Outcome::Finished;
            }
            Err(e) => {
                tracing::warn!("agent: loading workflow run {run_id}: {e}; handing it to Python");
                return Outcome::HandOver(None);
            }
        };
        if !served(pool, &workflow_id).await {
            return Outcome::HandOver(None);
        }
        if let Err(e) = begin(pool, &run_id, &workflow_id, &inputs).await {
            tracing::warn!("agent: starting workflow run {run_id}: {e}; handing it to Python");
            return Outcome::HandOver(None);
        }
        if job.cancel_requested {
            run.update(|st| st.fields.cancelled = true);
        }

        let meter = Arc::new(Meter::new(Budget::new(Limits::for_kind("workflow")), run.clone()));
        let env = Env {
            agent: self,
            run: Some(run.clone()),
            pause: Some(Pause { run_id: run_id.clone(), workflow_id: workflow_id.clone(), label: wf.name.clone() }),
            meter: Some(meter.clone()),
            depth: 0,
        };
        let ran = match serde_json::from_str::<Value>(wf.definition.as_deref().filter(|d| !d.is_empty()).unwrap_or("{}")) {
            Ok(definition) => engine::execute(&env, &run_id, &definition, &inputs).await,
            Err(e) => Err(Halt::Failed(e.to_string())),
        };
        let ran = match (ran, meter.exceeded()) {
            // A spent budget stops the run; Python's engine raises the stop.
            (_, Some(_)) if run.fields().cancelled => Err(Halt::Cancelled),
            (Ok(_), Some(reason)) => Err(Halt::Failed(format!("budget exceeded: {reason}"))),
            (ran, _) => ran,
        };
        let status = match ran {
            Ok((outputs, records)) => {
                let outputs = Value::Object(outputs);
                finish(pool, &run_id, "done", Some(&pyjson::dumps(&outputs)), Some(&pyjson::dumps(&Value::Array(records))), None).await;
                crate::notify::send(pool, wf.notifications.as_deref(), "done", &wf.name, &pyjson::dumps_indent(&outputs, 2)).await;
                "done"
            }
            Err(Halt::Cancelled) => {
                finish(pool, &run_id, "stopped", None, None, None).await;
                run.emit_local("workflow_stopped", &json!({"run_id": run_id}));
                "stopped"
            }
            Err(Halt::Failed(error)) => {
                if let Some(reason) = meter.exceeded() {
                    run.emit_local("budget_exceeded", &json!({"reason": reason, "run_id": run_id}));
                }
                tracing::warn!("agent: workflow run {run_id} failed: {error}");
                finish(pool, &run_id, "error", None, None, Some(&error)).await;
                crate::notify::send(pool, wf.notifications.as_deref(), "error", &wf.name, &error).await;
                run.emit_local("workflow_error", &json!({"error": error, "run_id": run_id}));
                "error"
            }
        };
        close_pauses(pool, &run_id, status).await;
        self.end(run, status);
        Outcome::Finished
    }
}

/// The run's row: created if the trigger didn't write one (Python's queue),
/// a pending one flipped to running. Requests a previous attempt left open
/// can't be answered any more — this attempt asks again.
async fn begin(pool: &SqlitePool, run_id: &str, workflow_id: &str, inputs: &Map<String, Value>) -> sqlx::Result<()> {
    let now = now_stored();
    let mut tx = crate::db::write_tx(pool).await?;
    sqlx::query(
        "INSERT INTO workflow_runs (id, workflow_id, status, inputs, outputs, node_results, error, started_at, finished_at) \
         VALUES (?, ?, 'running', ?, NULL, '[]', NULL, ?, NULL) ON CONFLICT(id) DO NOTHING",
    )
    .bind(run_id)
    .bind(workflow_id)
    .bind(pyjson::dumps(&Value::Object(inputs.clone())))
    .bind(&now)
    .execute(&mut *tx)
    .await?;
    sqlx::query("UPDATE workflow_runs SET status = 'running' WHERE id = ? AND status = 'pending'").bind(run_id).execute(&mut *tx).await?;
    sqlx::query(
        "UPDATE approvals SET status = 'expired', resolved_at = ?, updated_at = ?, \
         result = 'The run was lost when the server restarted.' WHERE status = 'pending' AND task_id = ?",
    )
    .bind(&now)
    .bind(&now)
    .bind(run_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

/// `finish_workflow_run`.
async fn finish(pool: &SqlitePool, run_id: &str, status: &str, outputs: Option<&str>, node_results: Option<&str>, error: Option<&str>) {
    let written = sqlx::query(
        "UPDATE workflow_runs SET status = ?, outputs = ?, node_results = ?, error = ?, finished_at = ? WHERE id = ?",
    )
    .bind(status)
    .bind(outputs)
    .bind(node_results)
    .bind(error)
    .bind(now_stored())
    .bind(run_id)
    .execute(pool)
    .await;
    if let Err(e) = written {
        tracing::error!("agent: finishing workflow run {run_id}: {e}");
    }
}

/// A request still open when the run ends answers nothing.
async fn close_pauses(pool: &SqlitePool, run_id: &str, status: &str) {
    let now = now_stored();
    let closed = sqlx::query(
        "UPDATE approvals SET status = 'expired', resolved_at = ?, result = ?, updated_at = ? \
         WHERE status = 'pending' AND task_id = ?",
    )
    .bind(&now)
    .bind(format!("The run finished ({status}) before this was answered."))
    .bind(&now)
    .bind(run_id)
    .execute(pool)
    .await;
    if let Err(e) = closed {
        tracing::warn!("agent: closing workflow run {run_id}'s requests: {e}");
    }
}

/// An answer to a run paused on a node, delivered: its open request closed
/// with the answer (the node reads it from the row), and the run told the
/// interrupt is resolved. False when the run asked nothing.
pub async fn answer(pool: &SqlitePool, run: &Run, status: &str, answer: &str) -> sqlx::Result<bool> {
    let open: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT id, interrupt_id FROM approvals WHERE task_id = ? AND status = 'pending' ORDER BY requested_at DESC LIMIT 1",
    )
    .bind(&run.id)
    .fetch_optional(pool)
    .await?;
    let Some((id, interrupt_id)) = open else { return Ok(false) };
    let now = now_stored();
    sqlx::query(
        "UPDATE approvals SET status = ?, answer = ?, result = 'Delivered to the run.', resolved_at = ?, updated_at = ? \
         WHERE id = ? AND status = 'pending'",
    )
    .bind(status)
    .bind(answer)
    .bind(&now)
    .bind(&now)
    .bind(&id)
    .execute(pool)
    .await?;
    run.emit_local("interrupt_resolved", &json!({"interrupt_id": interrupt_id}));
    Ok(true)
}

/// `run_workflow`, the main agent's tool: a saved workflow run to its
/// outputs, inside the call. Its events go nowhere (in Python its
/// `TaskState` is nobody's); the calling run sees a worker start and finish.
pub struct Call {
    name: String,
    definition: Value,
    inputs: Map<String, Value>,
    run_id: String,
    /// `abs(hash(run_id)) % 9000 + 1000`: a number, for the worker events.
    pub idx: u64,
}

impl Call {
    /// Everything up to the run: the depth guard, the inputs, the workflow.
    /// `Err` is the tool's answer.
    pub async fn prepare(pool: &SqlitePool, depth: u32, workflow_id: &str, inputs_json: Option<&str>) -> Result<Call, String> {
        if depth >= MAX_DEPTH {
            return Err(format!(
                "Error: workflow recursion depth {depth} exceeds limit {MAX_DEPTH} — possible self-invocation loop for \
                 workflow {}. Aborting.",
                pyjson::repr_str(workflow_id)
            ));
        }
        let inputs = match inputs_json.filter(|s| !s.is_empty()) {
            None => Map::new(),
            Some(raw) => match serde_json::from_str::<Value>(raw) {
                Ok(Value::Object(m)) => m,
                Ok(other) => return Err(format!("Error: inputs_json must be a JSON object, got {}", pyjson::py_type(&other))),
                Err(e) => return Err(format!("Error: inputs_json is not valid JSON: {e}")),
            },
        };
        let wf = load(pool, workflow_id).await.map_err(|e| e.to_string())?;
        let Some(wf) = wf else { return Err(format!("Workflow '{workflow_id}' not found.")) };
        let definition = serde_json::from_str(wf.definition.as_deref().unwrap_or("null"))
            .map_err(|e| format!("Failed to parse workflow definition: {e}"))?;
        let hex = uuid::Uuid::new_v4().simple().to_string();
        let idx = u64::from_str_radix(&hex[..12], 16).unwrap_or(0) % 9000 + 1000;
        Ok(Call { name: wf.name, definition, inputs, run_id: format!("tool_{workflow_id}_{}", &hex[..8]), idx })
    }

    pub fn start(&self) -> Value {
        json!({"idx": self.idx, "role": "workflow", "task": format!("Running workflow '{}'", self.name)})
    }

    /// The workflow, run as a child at `depth`: its outputs, or why not.
    pub async fn run(&self, agent: &Agent, depth: u32) -> Result<Map<String, Value>, String> {
        let env = Env { agent, run: None, pause: None, meter: None, depth };
        match engine::execute(&env, &self.run_id, &self.definition, &self.inputs).await {
            Ok((outputs, _)) => Ok(outputs),
            Err(Halt::Failed(e)) => Err(e),
            Err(Halt::Cancelled) => Err("cancelled".into()),
        }
    }

    pub fn done(&self, ran: &Result<Map<String, Value>, String>) -> Value {
        match ran {
            Ok(outputs) => json!({
                "idx": self.idx,
                "role": "workflow",
                "task": format!("Workflow '{}' done", self.name),
                "status": "done",
                "result": crate::pystr::prefix(&pyjson::dumps(&Value::Object(outputs.clone())), 2000),
            }),
            Err(e) => json!({
                "idx": self.idx,
                "role": "workflow",
                "task": format!("Workflow '{}' failed", self.name),
                "status": "error",
                "result": crate::pystr::prefix(e, 1000),
            }),
        }
    }

    /// The tool's answer.
    pub fn answer(&self, ran: Result<Map<String, Value>, String>) -> String {
        match ran {
            Ok(outputs) => pyjson::dumps_indent(&Value::Object(outputs), 2),
            Err(e) => format!("Workflow execution failed: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_model_a_graph_can_call_is_found() {
        let def = json!({"nodes": [
            {"id": "a", "type": "agent", "config": {"model": "ollama:x"}},
            {"id": "b", "type": "sequential", "config": {"steps": [{"model": "meta:y"}, {"model": ""}]}},
            {"id": "c", "type": "map", "config": {"workflow_id": "w2", "sub_graph": {"nodes": [{"config": {"model": "z:z"}}]}}},
        ]});
        let (mut models, mut workflows) = (HashSet::new(), vec![]);
        walk(&def, &mut models, &mut workflows);
        let want: HashSet<Option<String>> = ["ollama:x", "meta:y", "z:z"].iter().map(|m| Some(m.to_string())).collect();
        assert_eq!(models, want);
        assert_eq!(workflows, vec!["w2"]);
    }
}
