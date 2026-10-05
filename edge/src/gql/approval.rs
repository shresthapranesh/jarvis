//! `resolveApproval` (`server/graphql/mutations/approval.py`, `core/approvals.py:resolve`)
//! and `requestToolApproval` (`server/graphql/mutations/tool.py`) — a change
//! to either is made in both.
//!
//! The edge answers what lives in rows: a tool gate (the waiter polls the
//! row), a board task's question, and a deferred action — a denial, or an
//! approved delete of a workflow, automation or skill (`ACTIONS`' executors).
//! A request whose answer needs Python's memory — an approved MCP call, a
//! workflow paused on a future, a gate whose run a worker has — is deferred to
//! Python, before anything is written.

use std::sync::Arc;

use async_graphql::{Context, Object, Result, SimpleObject};
use serde_json::{Value, json};
use sqlx::SqlitePool;

use super::codec::now_stored;
use super::router::Caller;
use super::{EdgeData, RequestFrom, defer};
use crate::approvals;
use crate::runs::{Registry, Run};

#[derive(SimpleObject)]
pub struct ResolveApprovalPayload {
    id: String,
    status: String,
    /// What resolving it produced.
    result: Option<String>,
}

/// The durable request a gated SDK call blocks on.
#[derive(SimpleObject)]
pub struct ToolApprovalRequest {
    id: String,
    status: String,
}

#[derive(sqlx::FromRow)]
struct Row {
    status: String,
    kind: String,
    source: String,
    action: Option<String>,
    action_payload: Option<String>,
    board_task_id: Option<String>,
    task_id: Option<String>,
    parent_id: Option<String>,
    tool: Option<String>,
    result: Option<String>,
}

async fn row(pool: &SqlitePool, id: &str) -> sqlx::Result<Option<Row>> {
    sqlx::query_as(
        "SELECT status, kind, source, action, action_payload, board_task_id, task_id, parent_id, tool, result FROM approvals WHERE id = ?",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
}

/// `live_task_id`: the first unfinished run whose parent is the conversation.
pub(super) fn live_run(registry: &Registry, conversation: Option<&str>) -> Option<Arc<Run>> {
    let conversation = conversation?;
    registry.all().into_iter().find(|run| !run.fields().done && run.meta.parent_id.as_deref() == Some(conversation))
}

/// `_emit_to_run`'s target: the row's run if it names one, else the
/// conversation's live run.
fn run_of(registry: &Registry, task_id: Option<&str>, parent_id: Option<&str>) -> Option<Arc<Run>> {
    match task_id.filter(|t| !t.is_empty()) {
        Some(task) => registry.get(task),
        None => live_run(registry, parent_id),
    }
}

/// `_emit_to_run`: onto a live run's stream, and only one the edge appends
/// to (the callers defer a worker's run to Python first).
fn emit(run: Option<&Arc<Run>>, event: &str, data: &Value) {
    if let Some(run) = run.filter(|r| !r.fields().done) {
        run.emit_local(event, data);
    }
}

/// `resolve_approval_row`: out of `pending`, unless it was answered a moment
/// ago from elsewhere.
async fn close(pool: &SqlitePool, id: &str, status: &str, answer: &str, result: &str) -> Result<()> {
    let now = now_stored();
    let done = sqlx::query(
        "UPDATE approvals SET status = ?, answer = ?, result = ?, resolved_at = ?, updated_at = ? \
         WHERE id = ? AND status = 'pending'",
    )
    .bind(status)
    .bind(answer)
    .bind(result)
    .bind(&now)
    .bind(&now)
    .bind(id)
    .execute(pool)
    .await?;
    if done.rows_affected() == 0 {
        let now_status = row(pool, id).await?.map_or_else(|| "gone".into(), |r| r.status);
        return Err(format!("approval already {now_status}").into());
    }
    Ok(())
}

/// `core/approvals.py:ACTIONS[action].execute` for an approved deferred
/// action — run before the row is closed, so a failure leaves it pending
/// and answerable. An MCP call, or a payload only Python would read (or fail
/// on) faithfully, goes to Python.
async fn execute(ctx: &Context<'_>, action: &str, payload: Option<&str>) -> Result<String> {
    let payload: Value = serde_json::from_str(payload.unwrap_or("{}")).map_err(|_| defer("unreadable payload".into()))?;
    let id = |key: &str| -> Result<String> {
        payload.get(key).and_then(Value::as_str).map(str::to_string).ok_or_else(|| defer(format!("payload without {key}")))
    };
    let pool: &SqlitePool = ctx.data()?;
    let (deleted, gone) = match action {
        "delete_workflow" => (super::workflow::delete_workflow(pool, &id("workflow_id")?).await?, "Workflow"),
        "delete_automation" => {
            (super::automation::delete_automation(pool, &id("automation_id")?, ctx.data()?).await?, "Automation")
        }
        "delete_skill" => (super::settings_lists::delete_skill(pool, &id("skill_id")?).await?, "Skill"),
        "call_mcp_tool" => return Err(defer("an MCP call runs in Python".into())),
        other => {
            return Err(format!("approval references unknown action {}", crate::pyjson::repr_str(other)).into());
        }
    };
    Ok(if deleted { "Deleted.".into() } else { format!("{gone} no longer exists.") })
}

#[derive(Default)]
pub struct ApprovalMutation;

#[Object]
impl ApprovalMutation {
    // Answer a pending approval. For an approve/deny gate, anything that
    // isn't clearly a yes is a no: a reply matching no keyword is usually a
    // question.
    async fn resolve_approval(&self, ctx: &Context<'_>, id: String, answer: String) -> Result<ResolveApprovalPayload> {
        let answer = answer.trim();
        if answer.is_empty() {
            return Err("answer must not be empty".into());
        }
        let (pool, registry) = (ctx.data::<SqlitePool>()?, ctx.data::<Arc<Registry>>()?);
        let row = row(pool, &id).await?.ok_or("approval not found")?;
        if row.status != "pending" {
            return Err(format!("approval already {}", row.status).into());
        }
        let approved = row.kind != "approval" || approvals::is_affirmative(answer) == Some(true);

        if let Some(action) = &row.action {
            let result = if approved {
                execute(ctx, action, row.action_payload.as_deref()).await?
            } else {
                "Not executed.".into()
            };
            let status = if approved { "approved" } else { "denied" };
            close(pool, &id, status, answer, &result).await?;
            return Ok(ResolveApprovalPayload { id, status: status.into(), result: Some(result) });
        }
        if row.source == approvals::GATE_SOURCE {
            let run = run_of(registry, row.task_id.as_deref(), row.parent_id.as_deref());
            if run.as_ref().is_some_and(|r| r.claimed()) {
                return Err(defer("the gated run is a worker's".into()));
            }
            let (status, result) =
                if approved { ("approved", "Released the waiting call.") } else { ("denied", "The call was not run.") };
            close(pool, &id, status, answer, result).await?;
            // The answer usually comes from the inbox; the chat showing the
            // prompt is told to stop showing it.
            emit(run.as_ref(), "approval_resolved", &approvals::resolved_event(row.tool.as_deref().unwrap_or(""), approved, answer));
            return Ok(ResolveApprovalPayload { id, status: status.into(), result: Some(result.into()) });
        }
        if let Some(task) = &row.board_task_id {
            super::board::answer_task(pool, ctx.data::<EdgeData>()?, task, answer).await?;
            let after = self::row(pool, &id).await?.ok_or("approval not found")?;
            return Ok(ResolveApprovalPayload { id, status: after.status, result: after.result });
        }
        Err(defer("a paused run waits in Python".into()))
    }

    // The agent's side of a gate: the `jarvis` SDK, in a kernel, records the
    // request here and then polls the row. A human's click is an approval,
    // not a request for one.
    async fn request_tool_approval(
        &self,
        ctx: &Context<'_>,
        tool_key: String,
        tool: String,
        #[graphql(default_with = "\"{}\".to_string()")] args_json: String,
        conversation_id: Option<String>,
    ) -> Result<ToolApprovalRequest> {
        let from = ctx.data::<RequestFrom>()?;
        if from.caller != Caller::Agent {
            return Err("requestToolApproval is only for agent-initiated calls".into());
        }
        match tool_key.split_once(':') {
            Some(("bound" | "sdk" | "mcp", rest)) if !rest.is_empty() => {}
            _ => return Err(format!("unknown tool key {}", crate::pyjson::repr_str(&tool_key)).into()),
        }
        let args = if args_json.is_empty() {
            json!({})
        } else {
            // Python words the parse error from its own parser.
            serde_json::from_str::<Value>(&args_json).map_err(|_| defer("args_json is not JSON".into()))?
        };
        if !args.is_object() {
            return Err("args_json must be a JSON object".into());
        }
        let conversation = conversation_id.filter(|c| !c.is_empty()).or_else(|| from.conversation.clone());
        let registry = ctx.data::<Arc<Registry>>()?;
        let run = live_run(registry, conversation.as_deref());
        if run.as_ref().is_some_and(|r| r.claimed()) {
            return Err(defer("the asking run is a worker's".into()));
        }
        let request = approvals::create(
            ctx.data()?,
            &tool_key,
            &tool,
            &args,
            conversation.as_deref(),
            run.as_ref().map(|r| r.id.as_str()),
        )
        .await?;
        emit(run.as_ref(), "approval_request", &request.event);
        Ok(ToolApprovalRequest { id: request.id, status: "pending".into() })
    }
}
